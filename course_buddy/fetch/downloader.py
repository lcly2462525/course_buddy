"""
SJTU Canvas 视频回放下载器

通过 LTI 1.3 认证流程获取 v.sjtu.edu.cn 的 access token，
然后下载课程回放视频。

认证链路:
  Canvas API Token → OC Session Cookies → LTI Launch → Video Platform Token → 视频列表/下载

Cookie 获取策略（按优先级）:
  1. 缓存的 cookies.json（未过期）
  2. 自动通过 Canvas API Token 建立 session（如果平台支持 session_token）
  3. 启动本地 HTTP server 引导用户在浏览器中登录（自动拦截）
"""

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import shutil
from hashlib import md5
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, quote, urlparse

import requests
from bs4 import BeautifulSoup
from rich import print as rprint

# ============================================================
# 常量
# ============================================================
CANVAS_BASE = "https://oc.sjtu.edu.cn"
VIDEO_BASE = "https://v.sjtu.edu.cn/jy-application-canvas-sjtu"
TOKEN_FILE = os.path.expanduser("~/.config/canvas/token")
COOKIE_FILE = os.path.expanduser("~/.config/canvas/cookies.json")


# ============================================================
# Canvas API Token
# ============================================================
def load_canvas_token() -> Optional[str]:
    if os.path.exists(TOKEN_FILE):
        return open(TOKEN_FILE).read().strip()
    return os.environ.get("CANVAS_TOKEN")


# ============================================================
# Cookie 管理
# ============================================================
def save_cookies(cookies_dict: dict):
    os.makedirs(os.path.dirname(COOKIE_FILE), exist_ok=True)
    # 保存为标准格式：带 _format 标记
    data = {"_format": "session_cookies", "cookies": cookies_dict}
    with open(COOKIE_FILE, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(COOKIE_FILE, 0o600)


def load_cookies() -> Optional[dict]:
    if not os.path.exists(COOKIE_FILE):
        return None
    with open(COOKIE_FILE) as f:
        data = json.load(f)
    # 兼容新旧格式
    if isinstance(data, dict):
        if data.get("_format") == "session_cookies":
            return data.get("cookies", {})
        # 旧格式：直接是 {name: value} 的 dict
        if "_format" not in data and not isinstance(list(data.values())[0] if data else None, (dict, list)):
            return data
    return None


def validate_cookies(cookies: dict) -> bool:
    """检查 cookies 是否还能访问 Canvas web 页面"""
    try:
        r = requests.get(
            f"{CANVAS_BASE}/courses",
            cookies=cookies,
            allow_redirects=False,
            timeout=10,
        )
        # 200 = 正常，302 到 courses 页面也算正常
        # 302 到 /login = 失效
        if r.status_code == 200:
            return True
        if r.status_code == 302:
            loc = r.headers.get("Location", "")
            return "login" not in loc
        return False
    except Exception:
        return False


def get_cookies_via_session_token(token: str) -> Optional[dict]:
    """尝试通过 Canvas session_token API 获取 cookies（部分平台支持）"""
    session = requests.Session()
    try:
        r = session.post(
            f"{CANVAS_BASE}/login/session_token",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        session_url = r.json().get("session_url")
        if not session_url:
            return None
        session.get(session_url, allow_redirects=True, timeout=10)
        cookies = dict(session.cookies)
        if cookies:
            save_cookies(cookies)
            return cookies
    except Exception:
        pass
    return None


def get_cookies_from_browser(browser: str = "auto") -> Optional[dict]:
    """从浏览器 cookie 数据库直接读取 oc.sjtu.edu.cn 的 cookies（含 HttpOnly）

    注意：macOS 上 Chrome/Edge 需要 Keychain 访问权限，可能弹出系统授权弹窗。
    使用超时保护避免无限阻塞。
    """
    try:
        import browser_cookie3
    except ImportError:
        return None

    import concurrent.futures

    browsers = []
    if browser == "auto":
        browsers = ["chrome", "safari", "firefox", "edge"]
    else:
        browsers = [browser]

    def _try_browser(br_name: str) -> Optional[dict]:
        loader = getattr(browser_cookie3, br_name, None)
        if not loader:
            return None
        cj = loader(domain_name="oc.sjtu.edu.cn")
        cookies = {c.name: c.value for c in cj}
        has_session = any(
            k in cookies
            for k in ("_normandy_session", "_legacy_normandy_session", "log_session_id")
        )
        return cookies if has_session else None

    for br_name in browsers:
        try:
            rprint(f"  [dim]尝试 {br_name}...[/dim]")
            # 超时保护：Keychain 弹窗可能阻塞，15 秒后放弃
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(_try_browser, br_name)
                try:
                    cookies = future.result(timeout=15)
                    if cookies:
                        rprint(f"[green]✅ 从 {br_name} 读取到 {len(cookies)} 个 cookies[/green]")
                        return cookies
                except concurrent.futures.TimeoutError:
                    rprint(f"  [yellow]⏳ {br_name} 读取超时（可能需要在系统弹窗中点击「允许」）[/yellow]")
                    continue
        except Exception:
            continue

    return None


def _parse_cookie_string(raw: str) -> dict:
    """解析 Cookie 字符串为 dict，自动清理前缀和引号"""
    raw = raw.strip().strip("'\"")

    # 自动去掉常见的前缀错误
    for prefix in ("Cookie:", "cookie:", "Cookie :", "Set-Cookie:", "set-cookie:"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):].strip()
            rprint(f"[dim]已自动去掉 '{prefix}' 前缀[/dim]")
            break

    cookies = {}
    for pair in raw.split(";"):
        pair = pair.strip()
        if "=" in pair:
            k, _, v = pair.partition("=")
            cookies[k.strip()] = v.strip()
    return cookies


def _try_clipboard_paste() -> Optional[str]:
    """尝试从系统剪贴板读取内容（macOS pbpaste / Linux xclip）"""
    import platform
    cmds = {
        "Darwin": ["pbpaste"],
        "Linux": ["xclip", "-selection", "clipboard", "-o"],
    }
    cmd = cmds.get(platform.system())
    if not cmd:
        return None
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return None


def get_cookies_via_browser_paste() -> Optional[dict]:
    """引导用户从浏览器获取 cookies，支持剪贴板自动读取和手动粘贴"""
    rprint("\n[bold yellow]需要浏览器 cookies 来访问视频回放平台[/bold yellow]")
    rprint()
    rprint("[bold]操作步骤：[/bold]")
    rprint("  1. 在浏览器中打开并登录: [cyan]https://oc.sjtu.edu.cn[/cyan]")
    rprint("  2. 按 F12（或 Cmd+Option+I）打开开发者工具")
    rprint("  3. 切换到 [bold]Network（网络）[/bold] 标签")
    rprint("  4. 刷新页面（Cmd+R / F5）")
    rprint("  5. 点击列表中第一个请求（通常是 [cyan]oc.sjtu.edu.cn[/cyan]）")
    rprint("  6. 在右侧找到 [bold]Request Headers（请求标头）[/bold]")
    rprint("  7. 找到 [green]Cookie:[/green] 那一行，复制 [bold]冒号后面的全部内容[/bold]（Cmd+C）")
    rprint()

    # ── 尝试从剪贴板自动读取 ──
    clipboard = _try_clipboard_paste()
    if clipboard and "=" in clipboard and ";" in clipboard:
        rprint("[bold green]📋 检测到剪贴板中有 Cookie 内容[/bold green]")
        preview = clipboard[:80] + ("..." if len(clipboard) > 80 else "")
        rprint(f"  [dim]{preview}[/dim]")
        rprint()
        try:
            answer = input("使用剪贴板内容？[Y/n] > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            rprint("\n[dim]已取消[/dim]")
            return None

        if answer in ("", "y", "yes"):
            cookies = _parse_cookie_string(clipboard)
            if cookies:
                rprint(f"[dim]解析到 {len(cookies)} 个 cookie，验证中...[/dim]")
                if validate_cookies(cookies):
                    save_cookies(cookies)
                    rprint(f"[green]✅ Cookies 有效！已保存到 {COOKIE_FILE}[/green]")
                    return cookies
                else:
                    rprint("[red]❌ 剪贴板中的 Cookies 无效（可能未登录或已过期）[/red]")
            else:
                rprint("[yellow]剪贴板内容无法解析为 Cookie[/yellow]")
            rprint("[dim]将回退到手动输入...[/dim]")

    # ── 手动输入（回退方案）──
    rprint("[bold]复制的内容应该长这样（不要带 Cookie: 前缀）：[/bold]")
    rprint("  [dim]_normandy_session=abc123; log_session_id=xyz789; _csrf_token=...[/dim]")
    rprint()
    rprint("[bold yellow]⚠ 注意：[/bold yellow]")
    rprint("  • [bold]不要[/bold]带 [red]Cookie:[/red] 前缀，只复制后面的值")
    rprint("  • [bold]不要[/bold]用 Console 的 document.cookie（读不到 HttpOnly cookies）")
    rprint("  • 输入 [cyan]q[/cyan] 退出，[cyan]s[/cyan] 跳过此步骤")
    rprint("  • 如果粘贴后回车无反应，先 [bold]Cmd+C 复制 Cookie[/bold]，然后输入 [cyan]p[/cyan] 自动从剪贴板读取")

    max_retries = 3
    for attempt in range(max_retries):
        rprint()
        try:
            raw = input(f"Cookie 字符串 ({attempt + 1}/{max_retries}, 输入 p 读剪贴板) > ").strip()
        except (EOFError, KeyboardInterrupt):
            rprint("\n[dim]已取消[/dim]")
            return None

        if not raw:
            rprint("[yellow]输入为空，请重试[/yellow]")
            continue

        if raw.lower() in ("q", "quit", "exit"):
            return None

        if raw.lower() in ("s", "skip"):
            rprint("[dim]已跳过[/dim]")
            return None

        # 'p' / 'paste' → 从剪贴板读取
        if raw.lower() in ("p", "paste"):
            clip = _try_clipboard_paste()
            if clip:
                rprint(f"[dim]从剪贴板读取了 {len(clip)} 个字符[/dim]")
                raw = clip
            else:
                rprint("[red]无法读取剪贴板（确保已复制 Cookie 到剪贴板）[/red]")
                continue

        cookies = _parse_cookie_string(raw)

        if not cookies:
            rprint("[red]解析失败：未找到有效的 key=value 对[/red]")
            rprint("[dim]格式应为: key1=value1; key2=value2; ...[/dim]")
            continue

        rprint(f"[dim]解析到 {len(cookies)} 个 cookie，验证中...[/dim]")

        # 验证
        if validate_cookies(cookies):
            save_cookies(cookies)
            rprint(f"[green]✅ Cookies 有效！已保存到 {COOKIE_FILE}[/green]")
            return cookies
        else:
            remaining = max_retries - attempt - 1
            if remaining > 0:
                rprint(f"[red]❌ Cookies 无效（可能未登录或已过期）[/red]，还可重试 {remaining} 次")
            else:
                rprint("[red]❌ Cookies 无效，已用完重试次数[/red]")

    return None


def ensure_cookies() -> dict:
    """确保有可用的 OC session cookies，按优先级尝试多种方式"""
    # 1. 尝试缓存
    cookies = load_cookies()
    if cookies and validate_cookies(cookies):
        rprint("[green]✅ 缓存的 cookies 有效[/green]")
        return cookies

    # 2. 尝试从浏览器 cookie 数据库直接读取（含 HttpOnly）
    rprint("[cyan]尝试从浏览器自动读取 cookies...[/cyan]")
    rprint("[dim]  （如果弹出系统权限弹窗，请点击「允许」）[/dim]")
    cookies = get_cookies_from_browser()
    if cookies and validate_cookies(cookies):
        save_cookies(cookies)
        rprint(f"[green]✅ 浏览器 cookies 有效！已保存到 {COOKIE_FILE}[/green]")
        return cookies

    # 3. 尝试 session_token API
    token = load_canvas_token()
    if token:
        rprint("[cyan]尝试通过 Canvas API Token 获取 cookies...[/cyan]")
        cookies = get_cookies_via_session_token(token)
        if cookies:
            rprint("[green]✅ 通过 API Token 获取成功[/green]")
            return cookies

    # 4. 引导用户手动粘贴（使用 Network tab 而非 Console）
    cookies = get_cookies_via_browser_paste()
    if cookies:
        return cookies

    rprint("[red]无法获取有效的 cookies，退出[/red]")
    sys.exit(1)


# ============================================================
# JWT / URL 解析工具
# ============================================================
def decode_jwt_payload(token: str) -> dict:
    if not token or token.count(".") < 2:
        return {}
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def parse_redirect_params(url: str) -> dict:
    if not url:
        return {}
    parsed = urlparse(url)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if "?" in parsed.fragment:
        _, _, fq = parsed.fragment.partition("?")
        params.update(parse_qsl(fq, keep_blank_values=True))
    return params


# ============================================================
# LTI 认证 → 获取视频平台 Token
# ============================================================
def get_video_platform_token(
    course_id: str, oc_cookies: dict
) -> Tuple[str, str, requests.Session]:
    """通过 LTI 1.3 流程获取 v.sjtu.edu.cn 的 access token"""
    session = requests.Session()
    for k, v in oc_cookies.items():
        session.cookies.set(k, v, domain="oc.sjtu.edu.cn")

    # Step 1: 获取 LTI launch form
    rprint(f"  [LTI] Step 1: 获取课程 {course_id} 的视频工具页面...")
    r = session.get(f"{CANVAS_BASE}/courses/{course_id}/external_tools/8329")
    soup = BeautifulSoup(r.content, "html.parser")

    launch_form = soup.find("form", attrs={
        "action": f"{VIDEO_BASE}/oidc/login_initiations"
    })
    if not launch_form:
        raise RuntimeError(
            "未找到视频平台登录表单。Cookie 可能已失效，请删除 "
            f"{COOKIE_FILE} 后重试。"
        )

    data = {
        i["name"]: i["value"]
        for i in launch_form.find_all("input")
        if i.get("name")
    }

    # Step 2: OIDC login initiation
    rprint("  [LTI] Step 2: OIDC 登录...")
    r2 = session.post(
        f"{VIDEO_BASE}/oidc/login_initiations",
        data=data,
        allow_redirects=True,
    )
    soup2 = BeautifulSoup(r2.content, "html.parser")

    auth_form = soup2.find("form", attrs={
        "action": f"{VIDEO_BASE}/lti3/lti3Auth/ivs"
    })
    if not auth_form:
        raise RuntimeError("未找到 LTI 鉴权表单。登录状态可能失效。")

    data2 = {
        i["name"]: i["value"]
        for i in auth_form.find_all("input")
        if i.get("name")
    }

    # Step 3: LTI auth → tokenId
    rprint("  [LTI] Step 3: LTI 鉴权...")
    r3 = session.post(
        f"{VIDEO_BASE}/lti3/lti3Auth/ivs",
        data=data2,
        allow_redirects=False,
    )
    loc = r3.headers.get("location", "")
    params = parse_redirect_params(loc)

    token_id = params.get("tokenId")
    if not token_id:
        raise RuntimeError(
            f"无法获取 tokenId。返回字段: {sorted(params.keys())}"
        )

    # Step 4: tokenId → access token
    rprint("  [LTI] Step 4: 获取 access token...")
    r4 = session.get(
        f"{VIDEO_BASE}/lti3/getAccessTokenByTokenId",
        params={"tokenId": token_id},
    )
    token_data = r4.json()["data"]
    access_token = token_data["token"]
    access_params = token_data.get("params") or {}

    canvas_cid = (
        access_params.get("courId")
        or access_params.get("canvasCourseId")
        or access_params.get("courseId")
        or params.get("canvasCourseId")
        or course_id
    )

    rprint(f"  [green]✅ 获取到视频平台 token, courseId={canvas_cid}[/green]")
    return access_token, str(canvas_cid), session


# ============================================================
# 视频列表
# ============================================================
def _extract_records(payload) -> Optional[list]:
    if isinstance(payload, list):
        return payload
    candidates = [
        ("data", "records"),
        ("data", "list"),
        ("data", "rows"),
        ("data", "items"),
        ("data", "page", "records"),
        ("data",),
    ]
    for path in candidates:
        cur = payload
        for key in path:
            cur = cur.get(key) if isinstance(cur, dict) else None
        if isinstance(cur, list):
            return cur
    return None


def get_video_list(
    access_token: str, canvas_course_id: str, session: requests.Session
) -> Tuple[list, dict]:
    """获取课程的视频列表"""
    headers = {"token": access_token}
    # courseId 可能包含 / + 等特殊字符（base64 编码），需要 URL encode
    encoded_cid = quote(canvas_course_id, safe="")

    bodies = [
        {"canvasCourseId": encoded_cid, "pageIndex": 1, "pageSize": 1000},
        {"canvasCourseId": encoded_cid},
        {"canvasCourseId": canvas_course_id, "pageIndex": 1, "pageSize": 1000},
        {"courId": encoded_cid, "pageIndex": 1, "pageSize": 1000},
        {"courId": encoded_cid},
        {"courId": canvas_course_id},
    ]

    for body in bodies:
        r = session.post(
            f"{VIDEO_BASE}/directOnDemandPlay/findVodVideoList",
            json=body,
            headers=headers,
        )
        records = _extract_records(r.json())
        if records is not None:
            return records, headers

    raise RuntimeError("视频列表接口未返回可识别的数据")


def get_video_detail(
    video_id: str, access_token: str, session: requests.Session
) -> Optional[dict]:
    """获取单个视频的详细信息（含下载链接）"""
    headers = {"token": access_token}
    r = session.post(
        f"{VIDEO_BASE}/directOnDemandPlay/getVodVideoInfos",
        data={"playTypeHls": "true", "id": video_id, "isAudit": "true"},
        headers=headers,
    )
    payload = r.json()
    for path in [("data",), ("body",)]:
        cur = payload
        for key in path:
            cur = cur.get(key) if isinstance(cur, dict) else None
        if isinstance(cur, dict):
            return cur
    return payload


def extract_video_url(detail: dict) -> Optional[str]:
    """从视频详情中提取最佳下载链接"""
    url_keys = [
        "videoPlayUrl", "rtmpUrlHdv", "videoUrl", "playUrl",
        "hlsUrl", "url", "rtmpUrl", "flvUrl", "flvUrlHdv", "mp4Url",
    ]
    # 直接查找
    for key in url_keys:
        url = detail.get(key)
        if url and url.startswith("http"):
            return url
    # 嵌套查找
    for sub_key in ["videoInfo", "vodVideoInfo"]:
        sub = detail.get(sub_key, {})
        if isinstance(sub, dict):
            for key in url_keys:
                url = sub.get(key)
                if url and url.startswith("http"):
                    return url
    return None


# ============================================================
# 下载
# ============================================================
def download_file(url: str, output_path: str, referer: str = "https://v.sjtu.edu.cn"):
    """下载文件，优先用 aria2c"""
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    if os.path.exists(output_path):
        size = os.path.getsize(output_path)
        if size > 1024:  # > 1KB 认为有效
            rprint(f"  [yellow]跳过（已存在 {size / 1024 / 1024:.1f}MB）: {os.path.basename(output_path)}[/yellow]")
            return output_path

    aria2 = shutil.which("aria2c")
    if aria2:
        rprint(f"  [cyan]aria2c 下载中: {os.path.basename(output_path)}[/cyan]")
        subprocess.run(
            [
                aria2, "-x", "16", "-s", "16",
                "-d", os.path.dirname(output_path) or ".",
                "-o", os.path.basename(output_path),
                f"--header=Referer: {referer}",
                url,
            ],
            check=True,
        )
    else:
        rprint(f"  [cyan]HTTP 下载中: {os.path.basename(output_path)}[/cyan]")
        r = requests.get(
            url,
            headers={"Referer": referer},
            stream=True,
            timeout=600,
        )
        r.raise_for_status()
        tmp = output_path + ".part"
        total = int(r.headers.get("content-length", 0))
        downloaded = 0
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = downloaded / total * 100
                        rprint(f"\r  [{pct:.0f}%] {downloaded / 1024 / 1024:.1f}MB / {total / 1024 / 1024:.1f}MB", end="")
        if total:
            rprint()
        os.replace(tmp, output_path)

    rprint(f"  [green]✅ 已下载: {output_path}[/green]")
    return output_path


def _safe_filename(s: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", s).strip()


def _parse_since(since: str) -> Optional[str]:
    """将 '7d', '2w', '1m' 转为 YYYYMMDD 格式的起始日期"""
    import datetime as dt
    try:
        n = int(since[:-1])
        u = since[-1].lower()
        if u == "d":
            start = dt.datetime.now() - dt.timedelta(days=n)
        elif u == "w":
            start = dt.datetime.now() - dt.timedelta(weeks=n)
        elif u == "m":
            start = dt.datetime.now() - dt.timedelta(days=30 * n)
        else:
            return None
        return start.strftime("%Y-%m-%d")
    except Exception:
        return None


# ============================================================
# 主下载入口
# ============================================================
def download_videos(
    urls: List[str],      # 这里 urls 不再使用（兼容 CLI 接口），改为按 course_id 获取
    out_dir: str,
    cookies: str,         # cookies_path（兼容接口，实际不再使用）
    since: str = "7d",
    referer: Optional[str] = None,
    cookies_from_browser: Optional[str] = None,
    course_id: Optional[str] = None,
    index: Optional[str] = None,
) -> List[str]:
    """
    下载课程回放视频。

    参数:
      course_id: Canvas 课程 ID（必需）
      out_dir: 输出目录
      since: 时间范围 (7d / 2w / 1m)
      index: 指定视频序号 (逗号分隔)，None = 全部
    """
    if not course_id:
        # 从 urls 里尝试提取 course_id
        for u in (urls or []):
            m = re.search(r'/course[s]?/(\d+)', u)
            if m:
                course_id = m.group(1)
                break
    if not course_id:
        rprint("[red]需要课程 ID[/red]")
        return []

    os.makedirs(out_dir, exist_ok=True)

    # Step 1: 获取 cookies
    oc_cookies = ensure_cookies()

    # Step 2: LTI 认证
    try:
        access_token, canvas_cid, v_session = get_video_platform_token(
            course_id, oc_cookies
        )
    except RuntimeError as e:
        rprint(f"[red]LTI 认证失败: {e}[/red]")
        # Cookie 可能过期，清除缓存
        if os.path.exists(COOKIE_FILE):
            os.remove(COOKIE_FILE)
            rprint("[yellow]已清除缓存的 cookies，请重新运行[/yellow]")
        return []

    # Step 3: 获取视频列表
    rprint("[cyan]获取视频列表...[/cyan]")
    videos, headers = get_video_list(access_token, canvas_cid, v_session)

    if not videos:
        rprint("[yellow]该课程没有视频回放[/yellow]")
        return []

    rprint(f"  共 {len(videos)} 个视频")

    # 过滤逻辑：--index 基于完整列表，--since 基于时间范围
    # 两者同时指定时，--index 优先（直接从完整列表选）
    if index is not None:
        indices = [int(x) for x in index.split(",")]
        selected = [videos[i] for i in indices if i < len(videos)]
        skipped = [i for i in indices if i >= len(videos)]
        if skipped:
            rprint(f"  [yellow]序号越界（共 {len(videos)} 个视频）: {skipped}[/yellow]")
        videos = selected
        rprint(f"  按序号选择: {len(videos)} 个视频")
    else:
        # 只按时间过滤
        since_date = _parse_since(since)
        if since_date:
            filtered = []
            for v in videos:
                date_str = (
                    v.get("courseBeginTime")
                    or v.get("videBeginTime")
                    or v.get("createTime")
                    or v.get("recordDate")
                    or ""
                )
                if isinstance(date_str, str) and date_str[:10] >= since_date:
                    filtered.append(v)
            rprint(f"  过滤 since={since} ({since_date}): {len(filtered)} 个视频")
            videos = filtered

    # 打印视频列表
    rprint("\n[bold]视频列表:[/bold]")
    for i, v in enumerate(videos):
        title = v.get("videoName") or v.get("title") or v.get("name") or "未知"
        date = (
            v.get("courseBeginTime") or v.get("videBeginTime")
            or v.get("createTime") or v.get("recordDate") or "?"
        )[:10]
        rprint(f"  [{i}] {date} - {title}")
    rprint()

    # Step 4: 逐个下载
    downloaded = []
    for i, v in enumerate(videos):
        video_id = v.get("videoId") or v.get("id")
        title = v.get("videoName") or v.get("title") or v.get("name") or f"video_{i}"
        date = (
            v.get("courseBeginTime") or v.get("videBeginTime")
            or v.get("createTime") or v.get("recordDate") or "unknown"
        )[:10]
        safe_title = _safe_filename(title)

        rprint(f"\n[bold][{i + 1}/{len(videos)}] {title}[/bold]")

        detail = get_video_detail(str(video_id), access_token, v_session)
        if not detail:
            rprint("  [red]无法获取视频详情[/red]")
            continue

        video_url = extract_video_url(detail)
        if not video_url:
            rprint(f"  [red]未找到下载链接[/red]")
            rprint(f"  [dim]返回字段: {list(detail.keys())[:15]}[/dim]")
            continue

        # 确定扩展名
        ext = ".mp4"
        if ".m3u8" in video_url:
            ext = ".m3u8"
        elif ".flv" in video_url:
            ext = ".flv"

        output_path = os.path.join(out_dir, f"{date}_{safe_title}{ext}")
        try:
            path = download_file(video_url, output_path)
            if path:
                downloaded.append(path)
        except Exception as e:
            rprint(f"  [red]下载失败: {e}[/red]")

    rprint(f"\n[green]✅ 完成！成功下载 {len(downloaded)}/{len(videos)} 个视频[/green]")
    return downloaded


# ============================================================
# 独立运行（列出视频）
# ============================================================
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法: python3 downloader.py <course_id> [since]")
        sys.exit(1)
    cid = sys.argv[1]
    since = sys.argv[2] if len(sys.argv) > 2 else "7d"
    download_videos([], f"data/downloads/{cid}", "", since=since, course_id=cid)
