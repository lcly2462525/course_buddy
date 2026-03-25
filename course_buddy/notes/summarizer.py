"""
笔记生成模块：将转录 JSON 通过 LLM 生成结构化课堂笔记。

笔记结构：
1. 总体概要（知识点 + 重要公式）
2. 详细内容按时间分块（推导过程 + 公式 + 重点标注）
3. 课堂事务（签到/小测、课程通知、课后任务）
"""

import json
import os
import re
from datetime import datetime
from typing import Dict, Any, List, Optional

import requests as _requests
from rich import print as rprint


# --------------- LLM 调用 ---------------

from ..llm_providers import resolve_provider as _resolve_provider


def _get_llm_config(llm_cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    解析 LLM 配置，自动处理 model 字段中的 provider 前缀。
    """
    cfg = llm_cfg or {}

    key_env = cfg.get("api_key_env", "LLM_API_KEY")
    default_api_key = os.environ.get(key_env) or os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    default_base_url = cfg.get("base_url") or os.environ.get("OPENAI_BASE_URL") or "https://aihubmix.com/v1"
    model = cfg.get("model", "qwen3-max")
    temperature = cfg.get("temperature", 0.3)

    api_key = default_api_key
    base_url = default_base_url

    # 检测 model 字段是否含 provider 前缀
    if model:
        resolved = _resolve_provider(model, cfg)
        model = resolved["model"]
        if resolved["base_url"]:
            base_url = resolved["base_url"]
        if resolved["api_key"]:
            api_key = resolved["api_key"]

    return {
        "api_key": api_key,
        "base_url": base_url.rstrip("/"),
        "model": model,
        "temperature": temperature,
    }


def _call_llm(prompt: str, llm: Dict[str, Any], max_tokens: int = 8000, retries: int = 3) -> Optional[str]:
    """通过 requests 调用 OpenAI 兼容 API，带重试"""
    api_key = llm["api_key"]
    if not api_key:
        rprint("[red]未配置 LLM API key（LLM_API_KEY 或 OPENAI_API_KEY）[/red]")
        return None

    url = llm["base_url"] + "/chat/completions"
    headers = {
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json",
    }
    payload = {
        "model": llm["model"],
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": llm["temperature"],
    }

    for attempt in range(1, retries + 1):
        try:
            resp = _requests.post(url, json=payload, headers=headers, timeout=600)
            if resp.status_code != 200:
                rprint(f"[red]LLM API 错误 ({resp.status_code})[/red]: {resp.text[:500]}")
                return None
            result = resp.json()
            content = result["choices"][0]["message"]["content"]
            usage = result.get("usage", {})
            rprint(f"[dim]LLM tokens: ↑{usage.get('prompt_tokens', '?')} ↓{usage.get('completion_tokens', '?')}[/dim]")
            return content
        except Exception as e:
            if attempt < retries:
                import time
                rprint(f"[yellow]LLM 调用失败 (尝试 {attempt}/{retries}): {e}，{5 * attempt}s 后重试...[/yellow]")
                time.sleep(5 * attempt)
            else:
                rprint(f"[red]LLM 调用失败（已重试 {retries} 次）[/red]: {e}")
                return None


# --------------- 转录文本处理 ---------------

def _load_transcript(json_path: str) -> Dict[str, Any]:
    """加载转录 JSON"""
    with open(json_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _fmt_time(seconds: float) -> str:
    """秒数 -> HH:MM:SS 或 MM:SS"""
    s = int(seconds)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if h > 0:
        return f"{h:02}:{m:02}:{s:02}"
    return f"{m:02}:{s:02}"


def _clean_transcript(text: str) -> str:
    """清理转录文本中的垃圾内容"""
    # 移除重复的广告/口播（"请不吝点赞 订阅..."）
    import re
    # 检测连续重复片段（同一句话出现 3 次以上）
    lines = text.split("\n")
    cleaned = []
    prev = ""
    repeat_count = 0
    for line in lines:
        stripped = line.strip()
        if stripped == prev:
            repeat_count += 1
            if repeat_count >= 2:
                continue  # 跳过连续重复
        else:
            repeat_count = 0
        prev = stripped
        cleaned.append(line)

    text = "\n".join(cleaned)

    # 移除末尾的垃圾重复（单字重复 "我 我 我 我..."）
    text = re.sub(r"(\S)\s*(\1\s*){5,}", r"\1...", text)

    # 移除广告类内容
    ad_patterns = [
        r"请不吝点赞\s*订阅\s*转发\s*打赏.*?栏目",
    ]
    for pat in ad_patterns:
        text = re.sub(pat, "", text)

    return text.strip()


def _build_transcript_text(segments: List[Dict], duration: float = 0) -> str:
    """
    将 segments 合并成文本。

    如果有多个 segment（带时间信息），加上时间标记。
    如果只有 1 个大 segment（summarize 无时间戳输出），直接返回清理后的文本。
    """
    if not segments:
        return ""

    # 合并所有文本
    all_text = "\n".join(seg.get("text", "").strip() for seg in segments if seg.get("text", "").strip())

    # 清理垃圾
    all_text = _clean_transcript(all_text)

    # 如果只有 1 个 segment 或 segment 没有有效时间信息，直接返回
    if len(segments) <= 1:
        return all_text

    # 多 segment：加时间标记
    chunk_sec = 300  # 5 分钟
    lines = []
    current_chunk_start = 0.0

    for seg in segments:
        start = seg.get("start", 0)
        text = seg.get("text", "").strip()
        if not text:
            continue
        if start >= current_chunk_start + chunk_sec:
            current_chunk_start = (start // chunk_sec) * chunk_sec
            lines.append(f"\n[{_fmt_time(current_chunk_start)}]")
        lines.append(text)

    return _clean_transcript("\n".join(lines))


def _chunk_text(text: str, max_chars: int = 50000) -> List[str]:
    """如果文本太长，按段落边界分块"""
    if len(text) <= max_chars:
        return [text]

    chunks = []
    current = []
    current_len = 0

    for line in text.split("\n"):
        if current_len + len(line) > max_chars and current:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += len(line) + 1

    if current:
        chunks.append("\n".join(current))

    return chunks


# --------------- Prompt 构建 ---------------

def _build_prompt(
    transcript_text: str,
    course_name: str,
    date_str: str,
    title: str,
    key_terms: List[str],
    duration_str: str,
) -> str:
    """构建 LLM prompt"""

    terms_hint = ""
    if key_terms:
        terms_hint = f"\n本课程的关键术语包括：{', '.join(key_terms)}。转录中可能出现这些术语的错误拼写。"

    return rf"""你是一个课堂笔记整理员。你的唯一任务是：将以下语音转录文本**忠实地**整理成结构化笔记。

来源：「{course_name}」，{date_str}，时长约 {duration_str}。
转录由 Whisper 自动生成，术语错误很多。

## 你必须做的

1. **纠正转录错误**：根据上下文修正数学术语、公式描述、人名等明显的语音识别错误{terms_hint}
2. **结构化整理**：把口语化的、零散的讲解整理成条理清晰的笔记
3. **补全公式**：老师口述的数学表达式，用 LaTeX 准确写出
4. **标注重点**：老师明确说了"重要"/"会考"/"注意"/"容易错"的地方，用 `> ⚠️ **重点**：` 标出

## 数学与 Markdown 输出硬性规范（必须遵守）

- 仅使用数学定界符 `$...$`（行内）与 `$$...$$`（块级）。**严禁**用反引号 `...` 或代码块 ``` 包裹公式。
- 范数写作 `\lVert x \rVert`，绝对值写作 `\lvert x \rvert`；不要写成 `||x||` 或 `|x|`。
- 上下标用 `^` 和 `_`，多字符必须加花括号，如 `x^{{n+1}}`、`\|\cdot\|_{{\infty}}`。
- `\sup`/`\inf`/`\lim`/`\sum`/`\prod` 等需要把指标置于下方时：使用显示公式或显式 `\limits`，如 `$$\sup\limits_{{x\in[0,1]}} f(x)$$`。
- Markdown 表格内避免直接使用竖线 `|` 作为数学符号，改用 `\lvert\,\rvert` 或 `\Vert`。
- 不要过度转义：数学环境内用单反斜杠，如 `\frac`、`\alpha`；不要写成 `\\frac`。

## 你绝对不能做的

1. **不要添加转录中没有的内容**——不要自己延伸、补充知识点、加"延伸思考"
2. **不要解释老师没解释的东西**——如果老师跳过了某步，你也跳过，不要代替老师补全
3. **不要臆测**——不确定的地方标 `[?]`，而不是猜
4. **不要加自己的评论或注释**（如"这是正态分布独有的性质"这种总结，除非老师原话说了）
5. **不要重复**——同一个内容只写一次

## 输出格式（严格遵守）

# {course_name} · {date_str} · {title}

## 一、总体概要

本节课主要内容：
- （分条列出，每条一句话，只列老师实际讲了的主题）

### 重要知识点
（列出核心定理/公式/概念，用 `$...$` 或 `$$...$$`，只列老师实际给出的）

## 二、详细内容

（按老师的讲课逻辑分块，每块一个小标题。不强求时间标记——如果转录有时间信息就加，没有就不加。）

### 小标题1

（忠实展开老师的讲解：推导步骤、公式、举的例子。保留老师的表述风格和口头提示。）

> ⚠️ **重点**：（仅当老师明确强调时才加）

### 小标题2

...

## 三、课堂事务

### 签到 / 课堂互动
- （老师是否在课上进行了扫码签到、课堂小测、问卷、点名等？记录发生的大致时间点。如果没有，写：本节课无签到或课堂测试。）

### 课程安排通知
- （老师是否提到了调课、换教室、教材使用、习题课安排、考试时间/范围、期中/期末相关事项等重要通知？逐条列出。如果没有，写：无。）

### 课后任务
- [ ] （老师布置的作业、项目进展、小组演讲/展示、分组任务、需要课后完成的练习等，注明截止日期（如果提到了的话）。）
- [ ] （如果老师未布置任何课后任务，写：本节课未布置具体作业。）

---

转录文本：

{transcript_text}
"""


def _build_merge_prompt(partial_notes: List[str], course_name: str, date_str: str, title: str) -> str:
    """当转录太长需要分段处理时，合并多段笔记"""
    combined = "\n\n---\n\n".join(partial_notes)
    return f"""以下是「{course_name}」（{date_str}）课程笔记的多个分段，请将它们合并成一份完整的笔记。

要求：
1. 保持三段式结构（总体概要 → 详细内容 → 课堂事务）
2. 去除重复内容，但不要丢失任何段落独有的信息
3. 确保讲课逻辑连贯
4. **不要添加任何分段笔记中没有的内容**
5. 课堂事务中的签到/小测、课程通知、课后任务信息要完整保留
6. 标题用：# {course_name} · {date_str} · {title}

分段笔记：

{combined}
"""


# --------------- 主函数 ---------------

def summarize_transcript(
    json_path: str,
    rules: Dict[str, Any],
    course_name: str = "课程",
    llm: Dict[str, Any] | None = None,
) -> str:
    """
    从转录 JSON 生成结构化课堂笔记。

    Args:
        json_path: 转录 JSON 文件路径
        rules: 课程特定规则（key_terms 等）
        course_name: 课程名称
        llm: LLM 配置 dict

    Returns:
        Markdown 格式的笔记字符串
    """
    js = _load_transcript(json_path)
    segments = js.get("segments", [])
    duration = js.get("duration", 0)

    # 元信息
    title = os.path.splitext(os.path.basename(json_path))[0]
    # 尝试从文件名提取日期（格式：2026-03-20_xxx）
    date_match = re.match(r"(\d{4}-\d{2}-\d{2})[_\s]*(.*)", title)
    if date_match:
        date_str = date_match.group(1)
        title_clean = date_match.group(2) or title
    else:
        date_str = datetime.fromtimestamp(os.path.getmtime(json_path)).strftime("%Y-%m-%d")
        title_clean = title

    duration_str = _fmt_time(duration) if duration else "未知"
    key_terms = rules.get("key_terms", [])

    # 构建转录文本
    transcript_text = _build_transcript_text(segments, duration)

    if not transcript_text.strip():
        return f"# {course_name} · {date_str}\n\n> 转录文本为空，无法生成笔记。\n"

    # LLM 配置
    llm_config = _get_llm_config(llm)

    if not llm_config["api_key"]:
        rprint("[yellow]未配置 LLM API key，生成基础笔记（无 LLM）[/yellow]")
        return _fallback_notes(segments, course_name, date_str, title_clean, key_terms)

    rprint(f"[cyan]生成笔记[/cyan] model={llm_config['model']}")

    # 分块处理
    chunks = _chunk_text(transcript_text, max_chars=50000)

    if len(chunks) == 1:
        # 单次调用
        prompt = _build_prompt(
            transcript_text, course_name, date_str, title_clean, key_terms, duration_str,
        )
        result = _call_llm(prompt, llm_config, max_tokens=8000)
        if result:
            return result
        rprint("[red]⚠ LLM 调用失败，生成 fallback 基础笔记[/red]")
        return _fallback_notes(segments, course_name, date_str, title_clean, key_terms)
    else:
        # 多次调用 + 合并
        rprint(f"[yellow]转录文本较长，分 {len(chunks)} 段处理[/yellow]")
        partial_notes = []
        for i, chunk in enumerate(chunks):
            rprint(f"[cyan]处理第 {i + 1}/{len(chunks)} 段[/cyan]")
            prompt = _build_prompt(
                chunk, course_name, date_str, title_clean, key_terms, duration_str,
            )
            result = _call_llm(prompt, llm_config, max_tokens=6000)
            if result:
                partial_notes.append(result)

        if not partial_notes:
            return _fallback_notes(segments, course_name, date_str, title_clean, key_terms)

        if len(partial_notes) == 1:
            return partial_notes[0]

        # 合并
        rprint("[cyan]合并分段笔记[/cyan]")
        merge_prompt = _build_merge_prompt(partial_notes, course_name, date_str, title_clean)
        merged = _call_llm(merge_prompt, llm_config, max_tokens=10000)
        return merged or "\n\n---\n\n".join(partial_notes)


def _fallback_notes(
    segments: List[Dict],
    course_name: str,
    date_str: str,
    title: str,
    key_terms: List[str],
) -> str:
    """无 LLM 时的基础笔记（关键词匹配 + 采样）"""
    md = [
        f"# {course_name} · {date_str} · {title}",
        "",
        "> ⚠️ 未使用 LLM 生成，以下为基础提取结果",
        "",
        "## 一、总体概要",
        "",
        "（需要 LLM API key 才能生成结构化摘要）",
        "",
        "## 二、转录片段（采样）",
        "",
    ]

    # 关键词匹配
    key_set = set(key_terms)
    highlighted = []
    for seg in segments:
        text = seg.get("text", "").strip()
        if any(k in text for k in key_set):
            ts = _fmt_time(seg.get("start", 0))
            highlighted.append(f"- **[{ts}]** {text}")

    if highlighted:
        md.append("### 关键词匹配片段")
        md.extend(highlighted[:30])
        md.append("")

    # 采样
    md.append("### 时间线采样")
    step = max(1, len(segments) // 40)
    for i, seg in enumerate(segments):
        if i % step == 0:
            ts = _fmt_time(seg.get("start", 0))
            md.append(f"- [{ts}] {seg.get('text', '').strip()}")

    md.extend([
        "",
        "## 三、课堂事务",
        "",
        "### 签到 / 课堂互动",
        "- （需要 LLM 生成）",
        "",
        "### 课程安排通知",
        "- （需要 LLM 生成）",
        "",
        "### 课后任务",
        "- [ ] 配置 LLM API key 以生成完整笔记",
    ])

    return "\n".join(md) + "\n"
