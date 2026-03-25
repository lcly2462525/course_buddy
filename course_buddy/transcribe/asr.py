"""
转录模块：支持 whisper-cpp（默认推荐）和 whisper-api 两种后端。

whisper-cpp 后端（推荐）：
  - 本地运行 whisper.cpp，完全免费
  - Apple Silicon 优化，M4 上 ~11x 实时速度
  - 显式 language 参数，支持中英混合
  - 自动输出 JSON + SRT + TXT

whisper-api 后端：
  - 调用 OpenAI-compatible Whisper API（如 aihubmix、Groq）
  - 显式 language 参数（防止中英混合 hallucination）
  - 自动分段（大文件拆成 <25MB 的 chunks）
"""

import os
import json
import re
import subprocess
import tempfile
import time
import unicodedata
from collections import Counter
from typing import List, Dict, Any, Optional, Tuple

import requests
from rich import print as rprint


# ==================== 常量 ====================

# Whisper API 单次上传限制 25MB，留点余量用 24MB
MAX_CHUNK_BYTES = 24 * 1024 * 1024
# 默认分段时长（秒），ffmpeg 会按此切分音频
DEFAULT_SEGMENT_SECONDS = 600  # 10 分钟

# 质量检测：允许的目标语言 Unicode 范围
_LANG_CHARSETS = {
    "zh": {
        # CJK Unified Ideographs + 常用中文标点
        "ranges": [
            (0x4E00, 0x9FFF),    # CJK Unified
            (0x3400, 0x4DBF),    # CJK Extension A
            (0x3000, 0x303F),    # CJK Symbols
            (0xFF00, 0xFFEF),    # Fullwidth Forms
        ],
        "name": "中文",
    },
    "en": {
        "ranges": [
            (0x0041, 0x007A),    # Basic Latin letters
            (0x0030, 0x0039),    # Digits
        ],
        "name": "英文",
    },
}

# hallucination 检测阈值
HALLUCINATION_REPEAT_THRESHOLD = 10   # 同一短语连续重复超过此数视为 hallucination
HALLUCINATION_FOREIGN_RATIO = 0.25    # 非目标语言字符占比超过此值视为 hallucination
MIN_CHARS_FOR_QUALITY_CHECK = 100     # 文本太短不做质量检测

# Whisper 常见 hallucination 短语（静音段产生的垃圾）
_HALLUCINATION_PHRASES = {
    "字幕志愿者", "请不吝点赞", "订阅 转发", "打赏支持", "明镜与点点",
    "谢谢观看", "谢谢大家", "字幕由", "字幕提供",
    "感谢收看", "感谢观看", "下期再见",
    "Thank you for watching", "Thanks for watching",
    "Please subscribe", "Like and subscribe",
    "Subtitles by", "Amara.org",
    "李宗盛",  # whisper-cpp 常见静音段 hallucination
}

# 静音检测参数
SILENCE_DETECT_NOISE_DB = -25   # 静音阈值（dB），-25 比 -30 更准确区分课堂噪声和语音
SILENCE_DETECT_MIN_DUR = 3     # 最小静音时长（秒）
SPEECH_START_MARGIN = 2.0       # 语音开始前保留的缓冲时间（秒）


# ==================== 时间戳解析 ====================

def _parse_ts(ts_str: str) -> float:
    """解析时间戳字符串 -> 秒数"""
    ts_str = ts_str.strip("[]() ")
    parts = ts_str.replace(",", ".").split(":")
    try:
        if len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            return float(parts[0]) * 60 + float(parts[1])
        else:
            return float(parts[0])
    except ValueError:
        return 0.0


# ==================== 静音检测 / 语音起始点 ====================

def _detect_speech_start(video_path: str, noise_db: int = SILENCE_DETECT_NOISE_DB,
                         min_dur: float = SILENCE_DETECT_MIN_DUR) -> float:
    """
    检测视频中第一个有效语音的起始时间（秒）。

    策略：用 ffmpeg silencedetect 找所有静音段，然后用滑动窗口分析
    哪个时间点之后静音占比显著下降（说明正式讲课开始了）。

    对于课程回放视频，通常前几分钟是等待期（静音/背景噪声），
    这个函数能帮我们跳过这段，避免 whisper 产生 hallucination。
    """
    try:
        result = subprocess.run(
            ["ffmpeg", "-i", video_path,
             "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}",
             "-t", "900",  # 只扫描前 15 分钟
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=120,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return 0.0

    stderr = result.stderr

    # 解析静音段为 (start, end) 列表
    silence_starts = []
    silence_ends = []
    for line in stderr.split("\n"):
        m_start = re.search(r'silence_start:\s*([\d.]+)', line)
        m_end = re.search(r'silence_end:\s*([\d.]+)', line)
        if m_start:
            silence_starts.append(float(m_start.group(1)))
        if m_end:
            silence_ends.append(float(m_end.group(1)))

    # 构建静音区间
    silence_intervals = []
    for i in range(min(len(silence_starts), len(silence_ends))):
        silence_intervals.append((silence_starts[i], silence_ends[i]))
    # 处理最后一个未闭合的静音段（仍在静音中）
    if len(silence_starts) > len(silence_ends):
        silence_intervals.append((silence_starts[-1], 900.0))

    if not silence_intervals:
        return 0.0

    # 如果第一个静音段不从开头开始（>10s），说明开头就有语音
    if silence_intervals[0][0] > 10:
        return 0.0

    # 滑动窗口分析：在每个时间点，计算后续 60 秒窗口内的静音占比
    # 当静音占比 < 20% 时，认为正式语音开始了
    WINDOW = 60.0          # 分析窗口长度（秒）
    SILENCE_RATIO_THRESHOLD = 0.20  # 静音占比低于此值认为是语音段
    STEP = 10.0            # 步进（秒）

    scan_end = min(silence_intervals[-1][1] + 120, 900.0)

    best_start = 0.0
    for t in [i * STEP for i in range(int(scan_end / STEP) + 1)]:
        window_start = t
        window_end = t + WINDOW

        # 计算窗口内的静音时长
        silence_in_window = 0.0
        for s_start, s_end in silence_intervals:
            overlap_start = max(s_start, window_start)
            overlap_end = min(s_end, window_end)
            if overlap_start < overlap_end:
                silence_in_window += overlap_end - overlap_start

        silence_ratio = silence_in_window / WINDOW

        if silence_ratio < SILENCE_RATIO_THRESHOLD:
            # 找到了！从这个窗口开始有持续语音
            best_start = max(0, t - SPEECH_START_MARGIN)
            break
    else:
        # 整个扫描范围都很安静，返回最后一个静音段结束
        if silence_ends:
            best_start = max(0, silence_ends[-1] - SPEECH_START_MARGIN)

    return best_start


# ==================== 质量检测 ====================

def _is_target_lang_char(ch: str, langs: List[str]) -> bool:
    """检查字符是否属于目标语言集合（含空格/标点/数字等通用字符）"""
    cp = ord(ch)

    # 通用字符：空白、ASCII 标点、数字
    if ch.isspace() or ch in '.,;:!?-—–\'\"()[]{}…·、。，；：！？""''【】《》（）':
        return True
    if 0x0030 <= cp <= 0x0039:  # digits
        return True

    for lang in langs:
        charset = _LANG_CHARSETS.get(lang)
        if not charset:
            continue
        for lo, hi in charset["ranges"]:
            if lo <= cp <= hi:
                return True

    return False


def _detect_repetition(text: str) -> Tuple[bool, Optional[str]]:
    """检测文本中是否有异常重复模式"""
    # 按句号/逗号/空格分割为短语
    phrases = re.split(r'[。，,.\s]+', text)
    phrases = [p.strip() for p in phrases if len(p.strip()) >= 2]

    if len(phrases) < 5:
        return False, None

    # 统计连续重复
    max_repeat = 1
    current_repeat = 1
    repeat_phrase = None

    for i in range(1, len(phrases)):
        if phrases[i] == phrases[i - 1]:
            current_repeat += 1
            if current_repeat > max_repeat:
                max_repeat = current_repeat
                repeat_phrase = phrases[i]
        else:
            current_repeat = 1

    if max_repeat >= HALLUCINATION_REPEAT_THRESHOLD:
        return True, f"短语 '{repeat_phrase}' 连续重复 {max_repeat} 次"

    # 统计整体重复率
    counter = Counter(phrases)
    if counter and len(phrases) >= 10:
        most_common_phrase, most_common_count = counter.most_common(1)[0]
        ratio = most_common_count / len(phrases)
        if ratio > 0.5 and most_common_count >= 10:
            return True, f"短语 '{most_common_phrase}' 占总短语的 {ratio:.0%}（{most_common_count}/{len(phrases)}）"

    return False, None


def _check_transcript_quality(text: str, target_langs: List[str]) -> Tuple[bool, List[str]]:
    """
    检查转录质量。

    返回 (is_good, issues)
    """
    issues = []

    if len(text.strip()) < MIN_CHARS_FOR_QUALITY_CHECK:
        issues.append(f"文本过短（{len(text.strip())} 字符）")
        return False, issues

    # 1. 检测非目标语言字符比例
    total_chars = 0
    foreign_chars = 0
    for ch in text:
        if ch.isspace():
            continue
        total_chars += 1
        if not _is_target_lang_char(ch, target_langs):
            foreign_chars += 1

    if total_chars > 0:
        foreign_ratio = foreign_chars / total_chars
        if foreign_ratio > HALLUCINATION_FOREIGN_RATIO:
            # 识别外语种类
            foreign_scripts = Counter()
            for ch in text:
                if ch.isspace():
                    continue
                if not _is_target_lang_char(ch, target_langs):
                    try:
                        script = unicodedata.name(ch, "UNKNOWN").split()[0]
                        foreign_scripts[script] += 1
                    except ValueError:
                        foreign_scripts["UNKNOWN"] += 1

            top_scripts = foreign_scripts.most_common(3)
            script_info = ", ".join(f"{s}({c})" for s, c in top_scripts)
            issues.append(
                f"非目标语言字符占 {foreign_ratio:.0%}（{foreign_chars}/{total_chars}），"
                f"主要外文: {script_info}"
            )

    # 2. 检测异常重复
    is_repetitive, repeat_info = _detect_repetition(text)
    if is_repetitive:
        issues.append(f"异常重复: {repeat_info}")

    # 3. 检测文本信息密度（每分钟字数太少也不对）
    # 这个在 segment 级别更适合检测，这里只做整体检查

    is_good = len(issues) == 0
    return is_good, issues


def _filter_hallucination_segments(
    segments: List[Dict[str, Any]],
    target_langs: List[str],
) -> List[Dict[str, Any]]:
    """
    过滤掉 hallucination segment：
    1. 匹配已知 hallucination 短语
    2. 非目标语言字符占比过高的短 segment
    3. 极短且无信息的 segment（< 3 字符）
    4. 连续重复的 segment（不限长度）
    5. 内部高度重复的 segment（同一句话在 segment 内部反复出现）
    6. 全局重复检测：如果某段文本在所有 segments 中出现次数过多，标记为 hallucination
    """
    filtered = []
    removed = 0

    # === 第一遍：全局统计，找出高频重复文本 ===
    text_counts = Counter(seg["text"].strip() for seg in segments if seg["text"].strip())
    total_segs = len(segments)
    # 如果某段文本出现次数 > 总段数的 10%（且至少 5 次），很可能是 hallucination
    global_hallucination_texts = set()
    for text, count in text_counts.items():
        if count >= 5 and (count / max(total_segs, 1)) > 0.10:
            # 额外检查：如果文本很短（<= 20 字符），更可疑
            if len(text) <= 20 or count >= 10:
                global_hallucination_texts.add(text)

    if global_hallucination_texts:
        rprint(f"  [dim]全局重复检测: {len(global_hallucination_texts)} 个疑似 hallucination 文本[/dim]")
        for t in list(global_hallucination_texts)[:3]:
            rprint(f"    [dim]'{t[:40]}' × {text_counts[t]}[/dim]")

    # === 第二遍：逐 segment 过滤 ===

    for seg in segments:
        text = seg["text"].strip()

        # 跳过空 segment
        if not text:
            removed += 1
            continue

        # 跳过极短无意义 segment
        if len(text) < 3:
            removed += 1
            continue

        # --- 全局重复检测：在所有 segments 中出现过多次的文本 ---
        if text in global_hallucination_texts:
            removed += 1
            continue

        # --- 连续重复检测（任意长度文本） ---
        if len(filtered) >= 2:
            recent_same = sum(
                1 for prev in filtered[-12:]
                if prev["text"].strip() == text
            )
            # 短文本 (<20 字符) 容忍度低：连续 3 次就过滤
            # 长文本 (>=20 字符) 容忍度更低：连续 2 次完全相同就很可疑
            threshold = 3 if len(text) < 20 else 2
            if recent_same >= threshold:
                removed += 1
                continue

        # --- 内部重复检测（一个 segment 内同一短语/句子反复出现） ---
        if _is_internally_repetitive(text):
            removed += 1
            continue

        # 检查已知 hallucination 短语
        is_hallucination = False
        for phrase in _HALLUCINATION_PHRASES:
            if phrase in text:
                # 短 segment 基本就是 hallucination
                if len(text) < 80:
                    is_hallucination = True
                    break
                # 长 segment 中如果 hallucination 短语占比高，也过滤
                elif text.count(phrase) * len(phrase) > len(text) * 0.5:
                    is_hallucination = True
                    break

        if is_hallucination:
            removed += 1
            continue

        # 对短 segment 做外语检查（长 segment 可能有混合内容，不过滤）
        if len(text) < 50:
            total = sum(1 for ch in text if not ch.isspace())
            if total > 0:
                foreign = sum(
                    1 for ch in text
                    if not ch.isspace() and not _is_target_lang_char(ch, target_langs)
                )
                if foreign / total > 0.6:
                    removed += 1
                    continue

        filtered.append(seg)

    if removed > 0:
        rprint(f"  [dim]过滤 {removed} 个 hallucination segments[/dim]")

    return filtered


def _is_internally_repetitive(text: str) -> bool:
    """
    检测一段文本内部是否高度重复。
    例如 "So we need X. So we need X. So we need X." → True

    策略：
    - 按句号/感叹号/问号/换行分句
    - 如果最高频句子出现次数占总句数 > 60%（且至少 3 次），判定为重复
    - 也检测 N-gram 级别的重复（滑动窗口）
    """
    if len(text) < 12:
        return False

    # 按标点分句
    sentences = re.split(r'[.!?。！？\n]+', text)
    sentences = [s.strip().lower() for s in sentences if len(s.strip()) >= 2]

    if len(sentences) >= 3:
        counts = Counter(sentences)
        most_common_count = counts.most_common(1)[0][1]
        if most_common_count >= 3 and most_common_count / len(sentences) > 0.6:
            return True

    # 也检测连续 N 个 word/token 的重复（针对不以标点分隔的重复）
    words = text.lower().split()
    if len(words) >= 12:
        ngram_size = min(4, len(words) // 3)
        ngrams = [" ".join(words[i:i+ngram_size]) for i in range(len(words) - ngram_size + 1)]
        ngram_counts = Counter(ngrams)
        if ngram_counts:
            top_count = ngram_counts.most_common(1)[0][1]
            if top_count >= 5 and top_count / len(ngrams) > 0.4:
                return True

    return False


# ==================== 音频工具 ====================

def _get_video_duration(video_path: str) -> Optional[float]:
    """用 ffprobe 获取视频/音频时长"""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", video_path],
            capture_output=True, text=True, timeout=30,
        )
        return float(result.stdout.strip())
    except Exception:
        return None


def _extract_audio_mp3(video_path: str, audio_dir: str, trim_start: float = 0) -> str:
    """提取音频为 MP3（比 WAV 小很多，whisper API 接受 mp3）。
    trim_start: 跳过视频开头的秒数（用于跳过静音段）。"""
    base = os.path.splitext(os.path.basename(video_path))[0]
    suffix = f"_trimmed_{int(trim_start)}s" if trim_start > 0 else ""
    out = os.path.join(audio_dir, base + suffix + ".mp3")
    if os.path.exists(out):
        return out
    os.makedirs(audio_dir, exist_ok=True)
    cmd = ["ffmpeg", "-y"]
    if trim_start > 0:
        cmd += ["-ss", str(trim_start)]
    cmd += ["-i", video_path, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k", out]
    subprocess.run(cmd, check=True, capture_output=True)
    return out


def _extract_audio_wav(video_path: str, audio_dir: str, trim_start: float = 0) -> str:
    """提取音频为 16kHz mono WAV（whisper-cpp 需要）。
    trim_start: 跳过视频开头的秒数（用于跳过静音段）。"""
    base = os.path.splitext(os.path.basename(video_path))[0]
    suffix = f"_trimmed_{int(trim_start)}s" if trim_start > 0 else ""
    out = os.path.join(audio_dir, base + suffix + ".wav")
    if os.path.exists(out):
        return out
    os.makedirs(audio_dir, exist_ok=True)
    cmd = ["ffmpeg", "-y"]
    if trim_start > 0:
        cmd += ["-ss", str(trim_start)]
    cmd += ["-i", video_path, "-vn", "-ac", "1", "-ar", "16000", out]
    subprocess.run(cmd, check=True, capture_output=True)
    return out


def _split_audio_chunks(audio_path: str, segment_seconds: int = DEFAULT_SEGMENT_SECONDS) -> List[str]:
    """
    将音频文件拆分为多个 chunks。
    如果文件 < MAX_CHUNK_BYTES，直接返回原文件。
    """
    file_size = os.path.getsize(audio_path)
    if file_size <= MAX_CHUNK_BYTES:
        return [audio_path]

    rprint(f"  [yellow]音频文件 {file_size / 1024 / 1024:.1f}MB > 24MB，自动分段...[/yellow]")

    tmpdir = tempfile.mkdtemp(prefix="cb-whisper-chunks-")
    ext = os.path.splitext(audio_path)[1]
    pattern = os.path.join(tmpdir, f"chunk-%03d{ext}")

    cmd = [
        "ffmpeg", "-y", "-i", audio_path,
        "-f", "segment",
        "-segment_time", str(segment_seconds),
        "-c", "copy",
        pattern,
    ]
    subprocess.run(cmd, check=True, capture_output=True)

    chunks = sorted([
        os.path.join(tmpdir, f)
        for f in os.listdir(tmpdir)
        if f.startswith("chunk-")
    ])

    rprint(f"  分为 {len(chunks)} 段（每段 ~{segment_seconds}s）")
    return chunks


# ==================== whisper-api 后端 ====================

def _whisper_api_call(
    audio_path: str,
    api_key: str,
    base_url: str,
    language: Optional[str] = None,
    response_format: str = "verbose_json",
    timeout: int = 300,
) -> Optional[Dict[str, Any]]:
    """
    调用 OpenAI-compatible Whisper API。

    显式传 language 参数，返回 verbose_json（含 segments）。
    """
    url = f"{base_url.rstrip('/')}/audio/transcriptions"

    with open(audio_path, "rb") as f:
        files = {"file": (os.path.basename(audio_path), f, "audio/mpeg")}
        data = {
            "model": "whisper-large-v3-turbo",
            "response_format": response_format,
        }
        if language:
            data["language"] = language

        headers = {"Authorization": f"Bearer {api_key}"}

        try:
            resp = requests.post(
                url, headers=headers, files=files, data=data,
                timeout=timeout,
            )
            resp.raise_for_status()
        except requests.exceptions.Timeout:
            rprint(f"  [red]Whisper API 超时（{timeout}s）[/red]")
            return None
        except requests.exceptions.HTTPError as e:
            rprint(f"  [red]Whisper API 错误: {e.response.status_code} {e.response.text[:300]}[/red]")
            return None
        except Exception as e:
            rprint(f"  [red]Whisper API 请求失败: {e}[/red]")
            return None

    if response_format == "verbose_json":
        return resp.json()
    else:
        return {"text": resp.text.strip()}


def _transcribe_whisper_api(
    video_path: str,
    audio_dir: str,
    out_dir: str,
    lang: str = "zh",
    target_langs: Optional[List[str]] = None,
) -> Optional[str]:
    """
    用 Whisper API 直接转录（推荐后端）。

    特性：
    - 显式传 language 参数
    - 大文件自动分段
    - 转录质量检测 + hallucination 时自动重试
    """
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY")
    if not api_key:
        rprint("[red]未设置 OPENAI_API_KEY 或 LLM_API_KEY 环境变量[/red]")
        return None

    base_url = os.environ.get("OPENAI_BASE_URL", "https://aihubmix.com/v1")
    base = os.path.splitext(os.path.basename(video_path))[0]

    if target_langs is None:
        target_langs = ["zh", "en"]  # 默认中英混合

    rprint(f"[cyan]whisper-api transcribe[/cyan] {base} (lang={lang})")

    # 0. 检测语音起始点（跳过课前静音段，避免 hallucination）
    rprint("  检测语音起始点...")
    speech_start = _detect_speech_start(video_path)
    if speech_start > 5:
        rprint(f"  [yellow]跳过开头 {speech_start:.0f}s 静音段[/yellow]")
    else:
        speech_start = 0

    # 1. 提取音频（mp3，体积小）
    rprint("  提取音频...")
    mp3_path = _extract_audio_mp3(video_path, audio_dir, trim_start=speech_start)
    mp3_size = os.path.getsize(mp3_path)
    rprint(f"  音频: {mp3_size / 1024 / 1024:.1f}MB")

    # 2. 分段（如果需要）
    chunks = _split_audio_chunks(mp3_path)

    # 3. 逐段转录
    all_segments = []
    time_offset = speech_start  # 从裁剪起始点开始偏移，使时间戳对齐原始视频
    full_text_parts = []

    for i, chunk_path in enumerate(chunks):
        chunk_label = f"[{i + 1}/{len(chunks)}]" if len(chunks) > 1 else ""
        rprint(f"  转录 {chunk_label}...")

        # 获取 chunk 时长用于 offset 计算
        chunk_duration = _get_video_duration(chunk_path) or 0

        # 第一次尝试：使用指定语言
        result = _whisper_api_call(
            chunk_path, api_key, base_url,
            language=lang,
            response_format="verbose_json",
        )

        if not result or not result.get("text"):
            rprint(f"  [red]转录失败 {chunk_label}[/red]")
            continue

        chunk_text = result.get("text", "")

        # 质量检测
        is_good, issues = _check_transcript_quality(chunk_text, target_langs)

        if not is_good:
            rprint(f"  [yellow]质量问题 {chunk_label}: {'; '.join(issues)}[/yellow]")

            # 重试策略：不指定语言让模型自动检测
            rprint(f"  [yellow]重试（不指定语言）...[/yellow]")
            result2 = _whisper_api_call(
                chunk_path, api_key, base_url,
                language=None,  # 让 whisper 自动检测
                response_format="verbose_json",
            )

            if result2 and result2.get("text"):
                chunk_text2 = result2.get("text", "")
                is_good2, issues2 = _check_transcript_quality(chunk_text2, target_langs)

                if is_good2 or (not is_good2 and len(issues2) < len(issues)):
                    rprint(f"  [green]重试结果更好[/green]")
                    result = result2
                    chunk_text = chunk_text2
                    is_good = is_good2
                    issues = issues2
                else:
                    rprint(f"  [yellow]重试结果未改善，保留原结果[/yellow]")

            if not is_good:
                rprint(f"  [red]⚠ 转录质量仍有问题: {'; '.join(issues)}[/red]")

        # 解析 segments
        api_segments = result.get("segments", [])
        if api_segments:
            for seg in api_segments:
                all_segments.append({
                    "start": (seg.get("start", 0) or 0) + time_offset,
                    "end": (seg.get("end", 0) or 0) + time_offset,
                    "text": seg.get("text", "").strip(),
                })
        else:
            # 没有 segments，用整个 chunk 作为一个 segment
            all_segments.append({
                "start": time_offset,
                "end": time_offset + chunk_duration,
                "text": chunk_text.strip(),
            })

        full_text_parts.append(chunk_text.strip())
        time_offset += chunk_duration

    # 4. 清理临时 chunks
    for chunk_path in chunks:
        if chunk_path != mp3_path and os.path.exists(chunk_path):
            os.remove(chunk_path)
    # 清理临时目录
    for chunk_path in chunks:
        chunk_dir = os.path.dirname(chunk_path)
        if chunk_dir != audio_dir and os.path.isdir(chunk_dir):
            try:
                os.rmdir(chunk_dir)
            except OSError:
                pass
            break

    if not all_segments:
        rprint("[red]所有段转录失败[/red]")
        return None

    # 5. 过滤 hallucination segments
    all_segments = _filter_hallucination_segments(all_segments, target_langs)

    if not all_segments:
        rprint("[red]过滤后没有有效 segment[/red]")
        return None

    # 6. 整体质量检测
    full_text = " ".join(seg["text"] for seg in all_segments)
    overall_good, overall_issues = _check_transcript_quality(full_text, target_langs)
    if not overall_good:
        rprint(f"[red]⚠ 整体转录质量警告: {'; '.join(overall_issues)}[/red]")

    # 7. 保存结果
    duration = _get_video_duration(video_path) or time_offset

    js_data = {
        "path": video_path,
        "language": lang,
        "target_langs": target_langs,
        "duration": duration,
        "speech_start": round(speech_start, 1),
        "backend": "whisper-api",
        "quality": {
            "passed": overall_good,
            "issues": overall_issues,
        },
        "segments": all_segments,
    }
    js_path = os.path.join(out_dir, base + ".json")
    with open(js_path, "w", encoding="utf-8") as f:
        json.dump(js_data, f, ensure_ascii=False, indent=2)

    srt_path = os.path.join(out_dir, base + ".srt")
    _write_srt(all_segments, srt_path)

    txt_path = os.path.join(out_dir, base + ".txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        for seg in all_segments:
            f.write(seg["text"].strip() + "\n")

    rprint(f"[green]转录完成[/green] {js_path}")
    rprint(f"  segments: {len(all_segments)}, duration: {duration:.0f}s, quality: {'✅' if overall_good else '⚠️'}")
    return js_path


# ==================== whisper-cpp 后端 ====================

# 默认模型路径
_WHISPER_CPP_MODEL_PATHS = [
    os.path.expanduser("~/.local/share/whisper-cpp/ggml-large-v3-turbo.bin"),
    os.path.expanduser("~/.local/share/whisper-cpp/ggml-large-v3.bin"),
    os.path.expanduser("~/.local/share/whisper-cpp/ggml-medium.bin"),
]


def _find_whisper_cpp() -> Optional[Tuple[str, str]]:
    """查找 whisper-cli 可执行文件和模型路径，返回 (binary, model) 或 None"""
    # 查找 binary
    binary = os.environ.get("WHISPER_CPP_BINARY")
    if not binary:
        result = subprocess.run(["which", "whisper-cli"], capture_output=True, text=True)
        if result.returncode == 0:
            binary = result.stdout.strip()
    if not binary:
        return None

    # 查找 model
    model = os.environ.get("WHISPER_CPP_MODEL")
    if model and os.path.exists(model):
        return binary, model

    for path in _WHISPER_CPP_MODEL_PATHS:
        if os.path.exists(path):
            return binary, path

    return None


def _parse_whisper_cpp_json(json_path: str) -> List[Dict[str, Any]]:
    """解析 whisper-cli 输出的 JSON 文件"""
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    segments = []
    for item in data.get("transcription", []):
        ts = item.get("timestamps", {})
        text = item.get("text", "").strip()
        if not text:
            continue
        # whisper-cpp timestamps 格式: "HH:MM:SS.mmm"
        start_str = ts.get("from", "00:00:00.000")
        end_str = ts.get("to", "00:00:00.000")
        segments.append({
            "start": _parse_ts(start_str),
            "end": _parse_ts(end_str),
            "text": text,
        })

    return segments


def _transcribe_whisper_cpp(
    video_path: str,
    audio_dir: str,
    out_dir: str,
    lang: str = "zh",
    target_langs: Optional[List[str]] = None,
) -> Optional[str]:
    """
    用本地 whisper.cpp 转录（推荐后端）。

    特性：
    - 完全免费，本地运行
    - Apple Silicon 优化，M4 上 ~7x 实时速度（55min ≈ 8min）
    - 显式 language 参数
    - 直接输出 JSON（含时间戳）
    """
    setup = _find_whisper_cpp()
    if not setup:
        rprint("[red]未找到 whisper-cli 或模型文件[/red]")
        rprint("[yellow]安装: brew install whisper-cpp[/yellow]")
        rprint("[yellow]模型: 下载 ggml-large-v3-turbo.bin 到 ~/.local/share/whisper-cpp/[/yellow]")
        return None

    binary, model = setup
    if target_langs is None:
        target_langs = ["zh", "en"]

    base = os.path.splitext(os.path.basename(video_path))[0]
    model_name = os.path.splitext(os.path.basename(model))[0]
    rprint(f"[cyan]whisper-cpp transcribe[/cyan] {base} (model={model_name}, lang={lang})")

    # 0. 检测语音起始点（跳过课前静音段，避免 hallucination）
    rprint("  检测语音起始点...")
    speech_start = _detect_speech_start(video_path)
    if speech_start > 5:
        rprint(f"  [yellow]跳过开头 {speech_start:.0f}s 静音段[/yellow]")
    else:
        speech_start = 0

    # 1. 提取音频为 WAV（whisper.cpp 需要 16kHz mono WAV）
    rprint("  提取音频 (16kHz WAV)...")
    wav_path = _extract_audio_wav(video_path, audio_dir, trim_start=speech_start)
    full_duration = _get_video_duration(video_path) or 0
    audio_duration = _get_video_duration(wav_path) or 0
    duration = full_duration  # 用原始时长做记录
    rprint(f"  时长: {full_duration / 60:.1f} min (转录 {audio_duration / 60:.1f} min)")

    # 2. 运行 whisper-cli
    # 输出 JSON 到临时文件
    json_out = os.path.join(out_dir, base + ".whisper-cpp.json")

    cmd = [
        binary,
        "-m", model,
        "-l", lang,
        "-f", wav_path,
        "-oj",             # 输出 JSON
        "-of", os.path.join(out_dir, base + ".whisper-cpp"),  # 输出文件前缀（不含扩展名）
        "--print-progress",
        "-t", str(min(os.cpu_count() or 4, 8)),  # 线程数
    ]

    rprint(f"  开始转录...")
    start_time = time.time()

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=3600,  # 1 小时超时
        )
    except subprocess.TimeoutExpired:
        rprint("[red]whisper-cpp 转录超时（1小时）[/red]")
        return None

    elapsed = time.time() - start_time

    if result.returncode != 0:
        stderr = result.stderr.strip()
        rprint(f"[red]whisper-cpp 转录失败[/red]：{stderr[:500]}")
        return None

    # 3. 解析 JSON 输出
    if not os.path.exists(json_out):
        rprint(f"[red]whisper-cpp 未生成输出文件: {json_out}[/red]")
        return None

    segments = _parse_whisper_cpp_json(json_out)

    # 清理 whisper-cpp 的原始 JSON
    os.remove(json_out)

    if not segments:
        rprint("[red]whisper-cpp 输出无有效 segments[/red]")
        return None

    rprint(f"  转录耗时: {elapsed:.1f}s ({audio_duration / max(elapsed, 1):.1f}x 实时速度)")

    # 3.5. 偏移时间戳以对齐原始视频
    if speech_start > 0:
        for seg in segments:
            seg["start"] += speech_start
            seg["end"] += speech_start

    # 4. 过滤 hallucination
    segments = _filter_hallucination_segments(segments, target_langs)

    if not segments:
        rprint("[red]过滤后没有有效 segment[/red]")
        return None

    # 5. 质量检测
    full_text = " ".join(seg["text"] for seg in segments)
    is_good, issues = _check_transcript_quality(full_text, target_langs)
    if not is_good:
        rprint(f"[yellow]⚠ 转录质量警告: {'; '.join(issues)}[/yellow]")

    # 6. 保存标准格式
    js_data = {
        "path": video_path,
        "language": lang,
        "target_langs": target_langs,
        "duration": duration,
        "speech_start": round(speech_start, 1),
        "backend": "whisper-cpp",
        "model": model_name,
        "elapsed_seconds": round(elapsed, 1),
        "quality": {
            "passed": is_good,
            "issues": issues,
        },
        "segments": segments,
    }
    js_path = os.path.join(out_dir, base + ".json")
    with open(js_path, "w", encoding="utf-8") as f:
        json.dump(js_data, f, ensure_ascii=False, indent=2)

    srt_path = os.path.join(out_dir, base + ".srt")
    _write_srt(segments, srt_path)

    txt_path = os.path.join(out_dir, base + ".txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        for seg in segments:
            f.write(seg["text"].strip() + "\n")

    rprint(f"[green]转录完成[/green] {js_path}")
    rprint(f"  segments: {len(segments)}, duration: {duration:.0f}s, quality: {'✅' if is_good else '⚠️'}")
    return js_path


# ==================== 工具函数 ====================

def _write_srt(segments: List[Dict], srt_path: str):
    def fmt(t):
        ms = int((t - int(t)) * 1000)
        s = int(t)
        h, s = divmod(s, 3600)
        m, s = divmod(s, 60)
        return f"{h:02}:{m:02}:{s:02},{ms:03}"

    with open(srt_path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, 1):
            f.write(f"{i}\n{fmt(seg['start'])} --> {fmt(seg.get('end', seg['start'] + 5))}\n{seg['text'].strip()}\n\n")


# ==================== 主入口 ====================

def transcribe_paths(
    paths: List[str],
    audio_dir: str,
    out_dir: str,
    lang: str = "zh",
    backend: str = "",
    target_langs: Optional[List[str]] = None,
) -> List[str]:
    """
    转录视频文件列表。

    backend: "whisper-cpp"（默认推荐）或 "whisper-api"
    target_langs: 目标语言列表（用于质量检测），默认 ["zh", "en"]
    """
    os.makedirs(audio_dir, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)

    if not paths:
        rprint("[yellow]没有找到可转录的视频文件[/yellow]")
        return []

    if not backend:
        backend = os.environ.get("CB_TRANSCRIBER", "whisper-cpp")

    if target_langs is None:
        target_langs = ["zh", "en"]

    outputs = []
    for p in paths:
        base = os.path.splitext(os.path.basename(p))[0]
        existing_json = os.path.join(out_dir, base + ".json")
        if os.path.exists(existing_json):
            rprint(f"[dim]跳过已转录[/dim] {base}")
            outputs.append(existing_json)
            continue

        if backend == "whisper-api":
            result = _transcribe_whisper_api(p, audio_dir, out_dir, lang, target_langs)
        else:
            # 默认 whisper-cpp
            if backend not in ("whisper-cpp", ""):
                rprint(f"[yellow]未知后端 '{backend}'，使用 whisper-cpp[/yellow]")
            result = _transcribe_whisper_cpp(p, audio_dir, out_dir, lang, target_langs)

        if result:
            outputs.append(result)

    return outputs