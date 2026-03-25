import argparse
import glob
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, List, Optional

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .config import load_config
from .fetch.canvas_api import (
    courses_to_config,
    filter_real_courses,
    get_active_courses,
    load_canvas_token,
)
from .fetch.downloader import download_videos
from .intent import ACTION_LIST, parse_user_intent
from .notes.summarizer import summarize_transcript
from .transcribe.asr import transcribe_paths

console = Console()


# ============================================================
# 工具函数
# ============================================================

def _ensure_course(course: str | None, cfg):
    if not course:
        console.print("[red]需要课程 ID 或可匹配的课程名[/red]")
        sys.exit(2)
    if course not in cfg.get("courses", {}):
        console.print(f"[red]未配置课程[/red]：{course}")
        sys.exit(2)


def _iter_video_files(in_dir: str, pattern: str | None) -> Iterable[str]:
    if pattern:
        yield from sorted(glob.glob(os.path.join(in_dir, pattern)))
        return
    for ext in ("*.mp4", "*.mkv", "*.webm", "*.mov"):
        yield from sorted(glob.glob(os.path.join(in_dir, ext)))


def _course_dirs(cfg, course: str):
    """返回课程相关的所有目录路径"""
    root = cfg["root_dir"]
    return {
        "downloads": os.path.join(root, "downloads", course),
        "audio": os.path.join(root, "audio", course),
        "transcripts": os.path.join(root, "transcripts", course),
        "notes": os.path.join(root, "notes", course),
    }


def _file_size_mb(path: str) -> float:
    try:
        return os.path.getsize(path) / 1024 / 1024
    except OSError:
        return 0


def _count_files(directory: str, pattern: str = "*") -> int:
    if not os.path.isdir(directory):
        return 0
    return len(glob.glob(os.path.join(directory, pattern)))


def _clean_files(directory: str, patterns: List[str]) -> List[str]:
    """删除匹配的文件，返回已删除的文件列表"""
    removed = []
    if not os.path.isdir(directory):
        return removed
    for pat in patterns:
        for f in glob.glob(os.path.join(directory, pat)):
            try:
                os.remove(f)
                removed.append(f)
            except OSError as e:
                console.print(f"  [red]删除失败: {f}: {e}[/red]")
    return removed


def _parse_since_date(since: str | None) -> str | None:
    """将 '7d'/'2w'/'1m' 转为 YYYY-MM-DD。"""
    if not since:
        return None
    try:
        n = int(since[:-1])
        u = since[-1].lower()
        if u == "d":
            start = datetime.now() - timedelta(days=n)
        elif u == "w":
            start = datetime.now() - timedelta(weeks=n)
        elif u == "m":
            start = datetime.now() - timedelta(days=30 * n)
        else:
            return None
        return start.strftime("%Y-%m-%d")
    except Exception:
        return None


# ============================================================
# 进度跟踪
# ============================================================

class PipelineProgress:
    """跟踪工作流各阶段的进度和错误"""

    def __init__(self, course: str, course_name: str):
        self.course = course
        self.course_name = course_name
        self.start_time = time.time()
        self.stages: dict = {}

    def start_stage(self, stage: str):
        self.stages[stage] = {
            "status": "running",
            "start": time.time(),
            "count": 0,
            "errors": [],
            "files": [],
        }
        console.print(f"\n{'='*60}")
        console.print(f"[bold cyan]▶ {stage.upper()}[/bold cyan]  {self.course_name} ({self.course})")
        console.print(f"{'='*60}")

    def finish_stage(self, stage: str, count: int = 0, files: list = None):
        if stage not in self.stages:
            return
        s = self.stages[stage]
        s["status"] = "done"
        s["end"] = time.time()
        s["count"] = count
        s["files"] = files or []
        elapsed = s["end"] - s["start"]
        console.print(
            f"\n[green]✅ {stage} 完成[/green]  "
            f"处理 {count} 个文件  耗时 {elapsed:.0f}s"
        )
        if s["errors"]:
            console.print(f"  [yellow]⚠ {len(s['errors'])} 个错误[/yellow]")

    def fail_stage(self, stage: str, error: str):
        if stage not in self.stages:
            self.stages[stage] = {"status": "failed", "errors": [], "files": [], "start": time.time()}
        s = self.stages[stage]
        s["status"] = "failed"
        s["end"] = time.time()
        s["errors"].append(error)
        console.print(f"\n[red]❌ {stage} 失败: {error}[/red]")

    def add_error(self, stage: str, error: str):
        if stage in self.stages:
            self.stages[stage]["errors"].append(error)
            console.print(f"  [red]✗ {error}[/red]")

    def summary(self):
        elapsed = time.time() - self.start_time
        console.print(f"\n{'='*60}")
        console.print(f"[bold]📊 处理报告 — {self.course_name}[/bold]")
        console.print(f"{'='*60}")
        console.print(f"总耗时: {elapsed:.0f}s ({elapsed/60:.1f}min)\n")

        table = Table(box=box.SIMPLE)
        table.add_column("阶段", style="bold")
        table.add_column("状态")
        table.add_column("文件数", justify="right")
        table.add_column("耗时", justify="right")
        table.add_column("错误", justify="right")

        for stage_name, s in self.stages.items():
            status_icon = {
                "done": "[green]✅[/green]",
                "failed": "[red]❌[/red]",
                "running": "[yellow]⏳[/yellow]",
                "skipped": "[dim]⏭[/dim]",
            }.get(s["status"], "?")
            elapsed_s = s.get("end", time.time()) - s.get("start", self.start_time)
            table.add_row(
                stage_name,
                status_icon,
                str(s.get("count", 0)),
                f"{elapsed_s:.0f}s",
                str(len(s.get("errors", []))) if s.get("errors") else "-",
            )

        console.print(table)

        # 输出文件
        all_files = []
        for s in self.stages.values():
            all_files.extend(s.get("files", []))
        if all_files:
            console.print(f"[bold]输出文件:[/bold]")
            for f in all_files:
                console.print(f"  📄 {f}")

        # 错误汇总
        all_errors = []
        for s in self.stages.values():
            all_errors.extend(s.get("errors", []))
        if all_errors:
            console.print(f"\n[bold red]错误汇总:[/bold red]")
            for e in all_errors:
                console.print(f"  ✗ {e}")
        console.print()


# ============================================================
# 子命令实现
# ============================================================

def cmd_list(args):
    cfg = load_config(args.config)
    table = Table(title="课程配置", box=box.MINIMAL_HEAVY_HEAD)
    table.add_column("ID")
    table.add_column("课程名")
    table.add_column("别名")
    for cid, meta in cfg.get("courses", {}).items():
        aliases = ", ".join(meta.get("aliases", []))
        table.add_row(cid, meta.get("name", ""), aliases)
    console.print(table)


def cmd_list_videos(args):
    from .fetch.downloader import ensure_cookies, get_video_platform_token, get_video_list
    cfg = load_config(args.config)
    course = args.course
    _ensure_course(course, cfg)
    course_name = cfg["courses"][course].get("name", course)

    console.print(f"\n📹 [bold]{course_name}[/bold] (ID: {course}) 的课程回放:\n")

    oc_cookies = ensure_cookies()
    access_token, canvas_cid, v_session = get_video_platform_token(course, oc_cookies)
    videos, _ = get_video_list(access_token, canvas_cid, v_session)

    if not videos:
        console.print("  [yellow](无视频)[/yellow]")
        return

    # 可选：按日期过滤最近 n 天视频（如 7d/2w/1m）
    since = getattr(args, "since", None)
    if since:
        since_date = _parse_since_date(since)
        if not since_date:
            console.print(f"[yellow]忽略非法 since 参数: {since}（应为 7d/2w/1m）[/yellow]")
        else:
            videos = [
                v for v in videos
                if isinstance(
                    (
                        v.get("courseBeginTime")
                        or v.get("videBeginTime")
                        or v.get("createTime")
                        or v.get("recordDate")
                        or ""
                    ),
                    str,
                )
                and (
                    (
                        v.get("courseBeginTime")
                        or v.get("videBeginTime")
                        or v.get("createTime")
                        or v.get("recordDate")
                        or ""
                    )[:10]
                    >= since_date
                )
            ]

    table = Table(box=box.MINIMAL_HEAVY_HEAD)
    table.add_column("#", justify="right")
    table.add_column("日期")
    table.add_column("标题")
    table.add_column("时长")

    for i, v in enumerate(videos):
        title = v.get("videoName") or v.get("title") or v.get("name") or "未知"
        date = (
            v.get("courseBeginTime") or v.get("videBeginTime")
            or v.get("createTime") or v.get("recordDate") or "?"
        )[:10]
        duration = str(v.get("duration") or v.get("videoLength") or "?")
        table.add_row(str(i), date, title, duration)

    console.print(table)
    if since:
        console.print(f"\n近 {since} 共 {len(videos)} 个视频")
    else:
        console.print(f"\n共 {len(videos)} 个视频")
    console.print(f"下载: [cyan]cb fetch --course {course} --index 0,1[/cyan]")


def cmd_fetch(args):
    cfg = load_config(args.config)
    course = args.course
    _ensure_course(course, cfg)
    out_dir = os.path.join(cfg["root_dir"], "downloads", course)
    os.makedirs(out_dir, exist_ok=True)
    urls = list(dict.fromkeys((args.url or []) + cfg["courses"][course].get("urls", [])))
    cookies = cfg.get("cookies_path", "")
    referer = cfg["courses"][course].get("referer")
    cookies_from_browser = cfg.get("cookies_from_browser") or cfg["courses"][course].get("cookies_from_browser")
    index = getattr(args, "index", None)
    download_videos(
        urls, out_dir, cookies,
        since=args.since, referer=referer,
        cookies_from_browser=cookies_from_browser,
        course_id=course, index=index,
    )


def cmd_transcribe(args):
    cfg = load_config(args.config)
    course = args.course
    _ensure_course(course, cfg)
    dirs = _course_dirs(cfg, course)
    for d in dirs.values():
        os.makedirs(d, exist_ok=True)

    paths = list(_iter_video_files(dirs["downloads"], args.glob))
    transcribe_cfg = cfg.get("transcribe", {})
    backend = transcribe_cfg.get("backend", "whisper-api")
    clean_video = transcribe_cfg.get("clean_video", False)
    clean_audio = transcribe_cfg.get("clean_audio", False)
    target_langs = transcribe_cfg.get("target_langs", ["zh", "en"])

    results = transcribe_paths(
        paths, audio_dir=dirs["audio"], out_dir=dirs["transcripts"],
        lang=args.lang, backend=backend, target_langs=target_langs,
    )

    # 自动清理已成功转录的视频
    if results and clean_video:
        console.print("\n[dim]🧹 清理已转录的视频...[/dim]")
        for vp in paths:
            base = os.path.splitext(os.path.basename(vp))[0]
            if os.path.exists(os.path.join(dirs["transcripts"], base + ".json")):
                size = _file_size_mb(vp)
                try:
                    os.remove(vp)
                    console.print(f"  [dim]删除: {os.path.basename(vp)} ({size:.0f}MB)[/dim]")
                except OSError:
                    pass

    if results and clean_audio:
        removed = _clean_files(dirs["audio"], ["*.wav", "*.mp3", "*.flac"])
        if removed:
            console.print(f"  [dim]删除 {len(removed)} 个临时音频[/dim]")


def cmd_notes(args):
    cfg = load_config(args.config)
    course = args.course
    _ensure_course(course, cfg)
    dirs = _course_dirs(cfg, course)
    os.makedirs(dirs["notes"], exist_ok=True)

    files = sorted(glob.glob(os.path.join(dirs["transcripts"], args.glob or "*.json")))
    if not files:
        console.print(f"[yellow]没有转录 JSON[/yellow]：{dirs['transcripts']}")
        return

    rules = cfg["courses"][course].get("note_rules", {})
    llm_cfg = dict(cfg.get("llm", {}))
    # --model 覆盖配置文件
    if getattr(args, "model", None):
        llm_cfg["model"] = args.model
        console.print(f"[cyan]使用模型: {args.model}[/cyan]")
    generated = []

    for fp in files:
        base = os.path.splitext(os.path.basename(fp))[0]
        out_path = os.path.join(dirs["notes"], f"{base}.md")

        if os.path.exists(out_path) and not getattr(args, "force", False):
            console.print(f"[dim]跳过已有笔记: {base}[/dim]")
            continue

        md = summarize_transcript(
            fp, rules=rules,
            course_name=cfg["courses"][course].get("name", course),
            llm=llm_cfg,
        )
        # Postprocess: fix math rendering pitfalls
        try:
            from .notes.postprocess import postprocess_math
            md = postprocess_math(md)
        except Exception as e:
            console.print(f"  [yellow]后处理失败，跳过 math 修复：{e}[/yellow]")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(md)
        console.print(f"[green]笔记已写入[/green] {out_path}")
        generated.append(out_path)

    return generated


def cmd_all(args):
    """完整工作流：下载 → 转录 → 笔记，带进度跟踪和错误汇报"""
    cfg = load_config(args.config)
    course = args.course
    _ensure_course(course, cfg)
    course_name = cfg["courses"][course].get("name", course)
    dirs = _course_dirs(cfg, course)
    for d in dirs.values():
        os.makedirs(d, exist_ok=True)

    progress = PipelineProgress(course, course_name)

    # ===== 阶段 1: 下载 =====
    progress.start_stage("下载")
    try:
        urls = list(dict.fromkeys((args.url or []) + cfg["courses"][course].get("urls", [])))
        downloaded = download_videos(
            urls, dirs["downloads"], cfg.get("cookies_path", ""),
            since=args.since,
            referer=cfg["courses"][course].get("referer"),
            cookies_from_browser=cfg.get("cookies_from_browser"),
            course_id=course,
            index=getattr(args, "index", None),
        )
        progress.finish_stage("下载", count=len(downloaded), files=downloaded)
    except Exception as e:
        progress.fail_stage("下载", str(e))
        progress.summary()
        return

    # ===== 阶段 2: 转录 =====
    progress.start_stage("转录")
    video_glob = getattr(args, "glob", None)
    video_paths = list(_iter_video_files(dirs["downloads"], video_glob))
    if not video_paths:
        progress.fail_stage("转录", "没有可转录的视频")
        progress.summary()
        return

    transcribe_cfg = cfg.get("transcribe", {})
    backend = transcribe_cfg.get("backend", "whisper-api")
    target_langs = transcribe_cfg.get("target_langs", ["zh", "en"])
    try:
        transcribed = transcribe_paths(
            video_paths, audio_dir=dirs["audio"], out_dir=dirs["transcripts"],
            lang=args.lang, backend=backend, target_langs=target_langs,
        )
        progress.finish_stage("转录", count=len(transcribed), files=transcribed)
    except Exception as e:
        progress.fail_stage("转录", str(e))
        progress.summary()
        return

    # 自动清理
    clean_video = cfg.get("transcribe", {}).get("clean_video", False)
    clean_audio = cfg.get("transcribe", {}).get("clean_audio", False)

    if transcribed and clean_video:
        console.print("\n[dim]🧹 清理视频...[/dim]")
        for vp in video_paths:
            base = os.path.splitext(os.path.basename(vp))[0]
            if os.path.exists(os.path.join(dirs["transcripts"], base + ".json")):
                size = _file_size_mb(vp)
                try:
                    os.remove(vp)
                    console.print(f"  [dim]删除: {os.path.basename(vp)} ({size:.0f}MB)[/dim]")
                except OSError:
                    pass

    if transcribed and clean_audio:
        removed = _clean_files(dirs["audio"], ["*.wav", "*.mp3", "*.flac"])
        if removed:
            console.print(f"  [dim]删除 {len(removed)} 个临时音频[/dim]")

    # ===== 阶段 3: 笔记生成 =====
    progress.start_stage("笔记生成")
    transcript_glob = "*.json"
    if video_glob:
        # 从视频 glob 推导转录 glob（把视频扩展名换成 .json）
        transcript_glob = video_glob.rsplit(".", 1)[0] + "*.json" if "." in video_glob else video_glob + "*.json"
    json_files = sorted(glob.glob(os.path.join(dirs["transcripts"], transcript_glob)))
    if not json_files:
        # fallback 到全部
        json_files = sorted(glob.glob(os.path.join(dirs["transcripts"], "*.json")))
    if not json_files:
        progress.fail_stage("笔记生成", "没有转录 JSON")
        progress.summary()
        return

    rules = cfg["courses"][course].get("note_rules", {})
    llm_cfg = dict(cfg.get("llm", {}))
    if getattr(args, "model", None):
        llm_cfg["model"] = args.model
        console.print(f"  [cyan]使用模型: {args.model}[/cyan]")
    note_files = []

    for fp in json_files:
        base = os.path.splitext(os.path.basename(fp))[0]
        out_path = os.path.join(dirs["notes"], f"{base}.md")

        if os.path.exists(out_path) and not getattr(args, "force", False):
            console.print(f"  [dim]跳过已有: {base}[/dim]")
            note_files.append(out_path)
            continue

        try:
            md = summarize_transcript(
                fp, rules=rules, course_name=course_name, llm=llm_cfg,
            )
            # Postprocess: fix math rendering pitfalls
            try:
                from .notes.postprocess import postprocess_math
                md = postprocess_math(md)
            except Exception as e:
                console.print(f"  [yellow]后处理失败，跳过 math 修复：{e}[/yellow]")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(md)
            console.print(f"  [green]✅ {base}[/green]")
            note_files.append(out_path)
        except Exception as e:
            progress.add_error("笔记生成", f"{base}: {e}")

    progress.finish_stage("笔记生成", count=len(note_files), files=note_files)
    progress.summary()


def cmd_status(args):
    """查看各课程当前处理状态"""
    cfg = load_config(args.config)
    courses = cfg.get("courses", {})

    if args.course:
        _ensure_course(args.course, cfg)
        courses = {args.course: courses[args.course]}

    for cid, meta in courses.items():
        dirs = _course_dirs(cfg, cid)
        name = meta.get("name", cid)

        n_videos = _count_files(dirs["downloads"], "*.mp4") + _count_files(dirs["downloads"], "*.mkv")
        n_transcripts = _count_files(dirs["transcripts"], "*.json")
        n_notes = _count_files(dirs["notes"], "*.md")

        video_size = 0
        if os.path.isdir(dirs["downloads"]):
            video_size = sum(
                _file_size_mb(os.path.join(dirs["downloads"], f))
                for f in os.listdir(dirs["downloads"])
                if os.path.isfile(os.path.join(dirs["downloads"], f))
            )

        console.print(f"\n[bold]{name}[/bold] ({cid})")

        table = Table(box=box.SIMPLE, show_header=True)
        table.add_column("阶段")
        table.add_column("文件数", justify="right")
        table.add_column("占用", justify="right")

        table.add_row("📥 视频", str(n_videos), f"{video_size:.0f}MB" if video_size else "-")
        table.add_row("📝 转录", str(n_transcripts), "-")
        table.add_row("📄 笔记", str(n_notes), "-")
        console.print(table)

        # 待处理提示
        if n_videos > n_transcripts:
            console.print(f"  [yellow]→ {n_videos - n_transcripts} 个视频待转录[/yellow]")
        if n_transcripts > n_notes:
            console.print(f"  [yellow]→ {n_transcripts - n_notes} 个转录待生成笔记[/yellow]")
        if n_videos == 0 and n_transcripts == 0 and n_notes == 0:
            console.print(f"  [dim](无数据)[/dim]")


def cmd_clean(args):
    """清理中间文件"""
    cfg = load_config(args.config)
    course = args.course
    _ensure_course(course, cfg)
    dirs = _course_dirs(cfg, course)
    course_name = cfg["courses"][course].get("name", course)

    console.print(f"\n[bold]🧹 清理 {course_name} ({course})[/bold]")

    targets = []
    what = args.what
    if what in ("video", "all"):
        targets.append(("视频", dirs["downloads"], ["*.mp4", "*.mkv", "*.webm", "*.mov"]))
    if what in ("audio", "all"):
        targets.append(("音频", dirs["audio"], ["*.wav", "*.mp3", "*.flac"]))

    total_removed = 0
    total_size = 0.0
    for label, directory, patterns in targets:
        if not os.path.isdir(directory):
            continue
        for pat in patterns:
            for f in glob.glob(os.path.join(directory, pat)):
                size = _file_size_mb(f)
                if args.dry_run:
                    console.print(f"  [dim]将删除 {label}: {os.path.basename(f)} ({size:.1f}MB)[/dim]")
                else:
                    try:
                        os.remove(f)
                        console.print(f"  删除 {label}: {os.path.basename(f)} ({size:.1f}MB)")
                        total_size += size
                        total_removed += 1
                    except OSError as e:
                        console.print(f"  [red]失败: {f}: {e}[/red]")

    if args.dry_run:
        console.print(f"\n[dim](dry run, 未实际删除)[/dim]")
    else:
        console.print(f"\n[green]已清理 {total_removed} 个文件, 释放 {total_size:.0f}MB[/green]")


def cmd_init(args):
    """从 Canvas API 自动获取课程列表并写入 config.yaml"""
    import yaml

    config_path = Path(args.config).expanduser().resolve()
    config_dir = config_path.parent

    # 加载 Canvas Token
    token = load_canvas_token()
    if not token:
        console.print("[red]❌ 未找到 Canvas API Token[/red]")
        console.print()
        console.print("请先配置 Token：")
        console.print("  1. 登录 [cyan]https://oc.sjtu.edu.cn[/cyan]")
        console.print("  2. 点击左下角「设置」→「+ 新建访问许可证」")
        console.print("  3. 复制生成的 token，然后运行：")
        console.print("     [cyan]mkdir -p ~/.config/canvas && echo 'YOUR_TOKEN' > ~/.config/canvas/token[/cyan]")
        sys.exit(1)

    # 获取课程列表
    console.print("[cyan]🔍 正在从 Canvas 获取课程列表...[/cyan]")
    try:
        all_courses = get_active_courses(token)
    except RuntimeError as e:
        console.print(f"[red]❌ {e}[/red]")
        sys.exit(1)

    if not all_courses:
        console.print("[yellow]未找到任何活跃课程。可能 Token 权限不足或当前没有选课。[/yellow]")
        sys.exit(0)

    # 过滤真实课程
    courses = filter_real_courses(all_courses)
    if not courses:
        console.print(f"[yellow]获取到 {len(all_courses)} 个课程，但过滤后无有效课程。[/yellow]")
        console.print("[dim]尝试使用 --all 显示全部课程[/dim]")
        if getattr(args, "show_all", False):
            courses = all_courses
        else:
            sys.exit(0)

    # 显示课程列表
    console.print(f"\n[bold]找到 {len(courses)} 门课程：[/bold]\n")
    table = Table(box=box.MINIMAL_HEAVY_HEAD)
    table.add_column("#", justify="right", style="dim")
    table.add_column("课程 ID")
    table.add_column("课程名")
    table.add_column("课程代码")
    term_col = any(c.get("term", {}).get("name") for c in courses)
    if term_col:
        table.add_column("学期")

    for i, c in enumerate(courses):
        row = [
            str(i),
            str(c["id"]),
            c.get("name") or "未知",
            c.get("course_code") or "-",
        ]
        if term_col:
            row.append(c.get("term", {}).get("name") or "-")
        table.add_row(*row)

    console.print(table)

    # 交互式选择
    if not getattr(args, "yes", False):
        console.print()
        console.print("[bold]选择要添加的课程[/bold]（输入序号，逗号分隔；直接回车 = 全部添加）：")
        try:
            raw = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]已取消[/dim]")
            return

        if raw:
            try:
                indices = [int(x.strip()) for x in raw.split(",")]
                courses = [courses[i] for i in indices if 0 <= i < len(courses)]
            except (ValueError, IndexError):
                console.print("[red]无效输入[/red]")
                sys.exit(1)

    if not courses:
        console.print("[yellow]未选择任何课程[/yellow]")
        return

    # 转为 config 格式
    new_courses = courses_to_config(courses)

    # 加载现有 config.yaml（如果存在）
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    else:
        # 从 example 创建
        example_path = config_dir / "config.yaml.example"
        if example_path.exists():
            with example_path.open("r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            # 清空 example 中的示例课程
            cfg["courses"] = {}
        else:
            # 从 .env 读取用户配置的 base_url
            base_url = os.environ.get("OPENAI_BASE_URL", "https://aihubmix.com/v1")
            cfg = {
                "cookies_path": "~/.config/canvas/cookies.json",
                "root_dir": "data",
                "courses": {},
                "transcribe": {
                    "backend": "whisper-cpp",
                    "language": "zh",
                    "target_langs": ["zh", "en"],
                    "clean_video": True,
                    "clean_audio": True,
                },
                "llm": {
                    "enabled": True,
                    "api_key_env": "LLM_API_KEY",
                    "base_url": base_url,
                    "model": "qwen3-max",
                    "temperature": 0.3,
                },
                "ask": {
                    "llm": {
                        "enabled": True,
                        "model": "qwen3-max",
                        "temperature": 0,
                        "api_key_env": "LLM_API_KEY",
                    }
                },
            }

    existing_courses = cfg.get("courses") or {}

    # 合并课程：新课程不覆盖已有配置（保留用户自定义的 aliases/note_rules）
    added = []
    skipped = []
    for cid, meta in new_courses.items():
        if cid in existing_courses:
            skipped.append((cid, meta["name"]))
        else:
            existing_courses[cid] = meta
            added.append((cid, meta["name"]))

    cfg["courses"] = existing_courses

    # 写入 config.yaml
    with config_path.open("w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    # 结果
    console.print()
    if added:
        console.print(f"[green]✅ 已添加 {len(added)} 门课程：[/green]")
        for cid, name in added:
            console.print(f"  [green]+[/green] {cid}: {name}")
    if skipped:
        console.print(f"[yellow]⏭  跳过 {len(skipped)} 门已存在的课程：[/yellow]")
        for cid, name in skipped:
            console.print(f"  [dim]-[/dim] {cid}: {name}")

    console.print(f"\n[bold]配置已写入 {config_path}[/bold]")
    console.print()
    console.print("[dim]提示：你可以编辑 config.yaml 添加课程别名（aliases）和关键词（key_terms）来提升体验[/dim]")


def cmd_ask(args):
    cfg = load_config(args.config)
    text = " ".join(args.text).strip()
    intent = parse_user_intent(
        text, cfg.get("courses", {}),
        ask_cfg=cfg.get("ask", {}),
        prefer_llm=not args.no_llm,
    )
    if not intent:
        console.print("[red]无法理解指令[/red]。试试：'整理现代操作系统最近两周的笔记'")
        sys.exit(2)

    console.print(
        f"[cyan]ask[/cyan] action={intent.action} "
        f"course={intent.course or '-'} since={intent.since} via={intent.source}"
    )

    if intent.action == ACTION_LIST:
        cmd_list(args)
        return

    _ensure_course(intent.course, cfg)
    dispatch_args = argparse.Namespace(
        config=args.config, course=intent.course,
        since=intent.since, glob=None, lang=args.lang,
        url=intent.urls, force=False,
    )
    handlers = {"fetch": cmd_fetch, "transcribe": cmd_transcribe, "notes": cmd_notes}
    handler = handlers.get(intent.action, cmd_all)
    handler(dispatch_args)


# ============================================================
# CLI Parser
# ============================================================

def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS, help="配置文件路径")

    parser = argparse.ArgumentParser(
        prog="cb",
        description="course-buddy — SJTU 课程回放 → 转录 → 笔记",
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    p = sub.add_parser("list", help="列出已配置课程", parents=[common])
    p.set_defaults(handler=cmd_list)

    p = sub.add_parser("list-videos", help="列出课程回放视频", parents=[common])
    p.add_argument("--course", required=True)
    p.add_argument("--since", default=None, help="仅列出最近时间范围内视频（如 7d/2w/1m）")
    p.set_defaults(handler=cmd_list_videos)

    p = sub.add_parser("fetch", help="下载课程回放", parents=[common])
    p.add_argument("--course", required=True)
    p.add_argument("--since", default="7d")
    p.add_argument("--index", default=None, help="视频序号，逗号分隔")
    p.add_argument("--url", action="append", default=[])
    p.set_defaults(handler=cmd_fetch)

    p = sub.add_parser("transcribe", help="转录已下载视频", parents=[common])
    p.add_argument("--course", required=True)
    p.add_argument("--glob", default=None)
    p.add_argument("--lang", default="zh")
    p.set_defaults(handler=cmd_transcribe)

    p = sub.add_parser("notes", help="从转录生成笔记", parents=[common])
    p.add_argument("--course", required=True)
    p.add_argument("--glob", default=None, help="文件匹配模式")
    p.add_argument("--force", action="store_true", help="覆盖已有笔记")
    p.add_argument("--model", default=None, help="指定 LLM 模型（覆盖 config.yaml）")
    p.set_defaults(handler=cmd_notes)

    p = sub.add_parser("all", help="一条龙：下载→转录→笔记", parents=[common])
    p.add_argument("--course", required=True)
    p.add_argument("--since", default="7d")
    p.add_argument("--lang", default="zh")
    p.add_argument("--index", default=None, help="视频序号，逗号分隔")
    p.add_argument("--glob", default=None, help="文件匹配模式（过滤转录和笔记阶段）")
    p.add_argument("--url", action="append", default=[])
    p.add_argument("--force", action="store_true", help="覆盖已有笔记")
    p.add_argument("--model", default=None, help="指定 LLM 模型（覆盖 config.yaml）")
    p.set_defaults(handler=cmd_all)

    p = sub.add_parser("status", help="查看处理进度", parents=[common])
    p.add_argument("--course", default=None)
    p.set_defaults(handler=cmd_status)

    p = sub.add_parser("clean", help="清理中间文件", parents=[common])
    p.add_argument("--course", required=True)
    p.add_argument("--what", choices=["video", "audio", "all"], default="all", help="清理什么")
    p.add_argument("--dry-run", action="store_true", help="只显示不删除")
    p.set_defaults(handler=cmd_clean)

    p = sub.add_parser("init", help="从 Canvas 自动获取课程并写入配置", parents=[common])
    p.add_argument("--yes", "-y", action="store_true", help="跳过交互确认，直接添加所有课程")
    p.add_argument("--all", dest="show_all", action="store_true", help="显示所有课程（含非正式课程）")
    p.set_defaults(handler=cmd_init)

    p = sub.add_parser("ask", help="自然语言控制", parents=[common])
    p.add_argument("--lang", default="zh")
    p.add_argument("--no-llm", action="store_true")
    p.add_argument("text", nargs="+")
    p.set_defaults(handler=cmd_ask)

    return parser


def _default_config_path() -> str:
    """Return the default config.yaml path relative to the project root."""
    project_root = Path(__file__).resolve().parent.parent
    return str(project_root / "config.yaml")


def ask_main():
    parser = build_parser()
    args = parser.parse_args()
    if not hasattr(args, "config"):
        args.config = _default_config_path()
    if not hasattr(args, "handler"):
        parser.print_help()
        sys.exit(2)
    args.handler(args)


if __name__ == "__main__":
    ask_main()
