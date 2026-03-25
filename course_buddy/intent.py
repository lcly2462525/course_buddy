import json
import os
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional


ACTION_LIST = "list"
ACTION_FETCH = "fetch"
ACTION_TRANSCRIBE = "transcribe"
ACTION_NOTES = "notes"
ACTION_ALL = "all"
VALID_ACTIONS = {
    ACTION_LIST,
    ACTION_FETCH,
    ACTION_TRANSCRIBE,
    ACTION_NOTES,
    ACTION_ALL,
}

CN_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}

TIME_UNIT_MAP = {
    "天": "d",
    "日": "d",
    "d": "d",
    "day": "d",
    "days": "d",
    "周": "w",
    "星期": "w",
    "礼拜": "w",
    "w": "w",
    "week": "w",
    "weeks": "w",
    "月": "m",
    "m": "m",
    "month": "m",
    "months": "m",
}

LIST_KEYWORDS = ("列出", "列表", "有哪些课", "有什么课", "课程列表", "看看课程", "所有课程")
FETCH_KEYWORDS = ("下载", "抓取", "拉取", "回放", "视频")
TRANSCRIBE_KEYWORDS = ("转录", "字幕", "听写", "asr", "transcribe")
NOTES_KEYWORDS = ("笔记", "总结", "摘要", "整理", "梳理", "要点")


@dataclass
class AskIntent:
    action: str
    course: Optional[str] = None
    since: str = "7d"
    urls: List[str] = field(default_factory=list)
    source: str = "rule"
    raw_text: str = ""


def normalize_text(text: str) -> str:
    lowered = text.lower().strip()
    return re.sub(r"[\s\-_./:：,，、\"'“”‘’()（）【】\[\]]+", "", lowered)


def chinese_to_int(text: str) -> Optional[int]:
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if text in CN_DIGITS and text != "十":
        return CN_DIGITS[text]
    if text == "十":
        return 10
    if len(text) == 2 and text[0] == "十" and text[1] in CN_DIGITS:
        return 10 + CN_DIGITS[text[1]]
    if len(text) == 2 and text[1] == "十" and text[0] in CN_DIGITS:
        return CN_DIGITS[text[0]] * 10
    if len(text) == 3 and text[1] == "十" and text[0] in CN_DIGITS and text[2] in CN_DIGITS:
        return CN_DIGITS[text[0]] * 10 + CN_DIGITS[text[2]]
    return None


def normalize_since(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    raw = value.strip().lower()
    raw = raw.removeprefix("最近").removeprefix("近").removeprefix("过去")
    raw = raw.removeprefix("within")
    raw = raw.replace("个", "")
    raw = re.sub(r"\s+", "", raw)
    match = re.fullmatch(r"(\d+)(天|日|d|周|星期|礼拜|w|月|m|day|days|week|weeks|month|months)", raw)
    if match:
        num = int(match.group(1))
        unit = TIME_UNIT_MAP[match.group(2)]
        return f"{num}{unit}"

    match = re.fullmatch(r"([零一二两三四五六七八九十]+)(天|日|周|星期|礼拜|月)", raw)
    if match:
        num = chinese_to_int(match.group(1))
        if num is None:
            return None
        unit = TIME_UNIT_MAP[match.group(2)]
        return f"{num}{unit}"
    return None


def extract_since(text: str) -> Optional[str]:
    compact = re.sub(r"\s+", "", text.lower())
    patterns = [
        r"(?:最近|近|过去)(\d+)(天|日|d|周|星期|礼拜|w|月|m)",
        r"(?:最近|近|过去)([零一二两三四五六七八九十]+)(天|日|周|星期|礼拜|月)",
    ]
    for pat in patterns:
        match = re.search(pat, compact)
        if match:
            return normalize_since("".join(match.groups()))
    return None


def extract_urls(text: str) -> List[str]:
    return re.findall(r"https?://[^\s]+", text)


def _course_candidates(course_id: str, meta: Dict[str, Any]) -> List[str]:
    names = [course_id, str(meta.get("name", "")).strip()]
    names.extend(str(alias).strip() for alias in meta.get("aliases", []) if str(alias).strip())
    return [name for name in names if name]


def resolve_course_id(query: str, courses: Dict[str, Dict[str, Any]]) -> Optional[str]:
    if not query:
        return None
    stripped = query.strip()
    if stripped in courses:
        return stripped

    normalized_query = normalize_text(stripped)
    if not normalized_query:
        return None

    exact_matches: List[str] = []
    contains_matches: List[str] = []
    scored: List[tuple[float, str]] = []
    for course_id, meta in courses.items():
        for candidate in _course_candidates(course_id, meta):
            normalized_candidate = normalize_text(candidate)
            if normalized_query == normalized_candidate:
                exact_matches.append(course_id)
            elif normalized_query in normalized_candidate or normalized_candidate in normalized_query:
                contains_matches.append(course_id)
            else:
                score = SequenceMatcher(None, normalized_query, normalized_candidate).ratio()
                scored.append((score, course_id))

    if exact_matches:
        return sorted(set(exact_matches))[0]
    if len(set(contains_matches)) == 1:
        return contains_matches[0]
    if scored:
        scored.sort(reverse=True)
        best_score, best_course = scored[0]
        if best_score >= 0.72:
            return best_course
    return None


def _detect_action(text: str) -> Optional[str]:
    lowered = text.lower()
    if any(keyword in lowered for keyword in LIST_KEYWORDS):
        return ACTION_LIST

    has_fetch = any(keyword in lowered for keyword in FETCH_KEYWORDS)
    has_transcribe = any(keyword in lowered for keyword in TRANSCRIBE_KEYWORDS)
    has_notes = any(keyword in lowered for keyword in NOTES_KEYWORDS)

    if has_fetch and (has_transcribe or has_notes):
        return ACTION_ALL
    if has_transcribe and has_notes:
        return ACTION_ALL
    if has_notes:
        return ACTION_NOTES
    if has_transcribe:
        return ACTION_TRANSCRIBE
    if has_fetch:
        return ACTION_FETCH
    return None


def parse_rule_based_intent(text: str, courses: Dict[str, Dict[str, Any]]) -> Optional[AskIntent]:
    cleaned = text.strip()
    action = _detect_action(cleaned)
    urls = extract_urls(cleaned)
    if action == ACTION_LIST:
        return AskIntent(action=ACTION_LIST, source="rule", raw_text=cleaned)

    course_id = None
    id_match = re.search(r"\b(\d{4,8})\b", cleaned)
    if id_match:
        course_id = resolve_course_id(id_match.group(1), courses)
    if not course_id:
        course_id = resolve_course_id(cleaned, courses)

    if not action and course_id:
        action = ACTION_ALL
    if not action:
        return None

    return AskIntent(
        action=action,
        course=course_id,
        since=extract_since(cleaned) or "7d",
        urls=urls,
        source="rule",
        raw_text=cleaned,
    )


def _build_llm_client(llm_cfg: Dict[str, Any]):
    try:
        from openai import OpenAI
    except Exception:
        return None

    api_key = llm_cfg.get("api_key") or os.getenv(llm_cfg.get("api_key_env", "OPENAI_API_KEY"))
    if not api_key:
        return None

    kwargs: Dict[str, Any] = {"api_key": api_key}
    base_url = llm_cfg.get("base_url") or os.getenv("OPENAI_BASE_URL")
    if base_url:
        kwargs["base_url"] = base_url
    return OpenAI(**kwargs)


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def parse_llm_intent(
    text: str,
    courses: Dict[str, Dict[str, Any]],
    ask_cfg: Dict[str, Any],
) -> Optional[AskIntent]:
    llm_cfg = ask_cfg.get("llm", {})
    if not llm_cfg.get("enabled", True):
        return None

    client = _build_llm_client(llm_cfg)
    if client is None:
        return None

    catalog = [
        {
            "course_id": course_id,
            "name": meta.get("name", ""),
            "aliases": meta.get("aliases", []),
        }
        for course_id, meta in courses.items()
    ]
    system_prompt = (
        "你是 course-buddy 的命令解析器。"
        "你的任务是把中文自然语言解析成 JSON 命令。"
        "只允许 action 为 list/fetch/transcribe/notes/all。"
        "课程只能从给定目录里选；如果用户说了课程名字，请尽量匹配到对应 course_id。"
        "如果用户没说时间范围，since 设为 7d。"
        "时间范围统一输出成 7d/2w/1m 这种格式。"
        "只输出 JSON，不要解释。"
    )
    user_prompt = json.dumps(
        {
            "user_text": text,
            "course_catalog": catalog,
            "output_schema": {
                "action": "list|fetch|transcribe|notes|all",
                "course_id": "string or null",
                "course_query": "string or null",
                "since": "7d | 2w | 1m | null",
                "urls": ["optional list of urls"],
            },
        },
        ensure_ascii=False,
    )

    try:
        response = client.chat.completions.create(
            model=llm_cfg.get("model", "gpt-4o-mini"),
            temperature=llm_cfg.get("temperature", 0),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
    except Exception:
        return None

    content = response.choices[0].message.content or ""
    payload = _extract_json_object(content)
    if not payload:
        return None

    action = str(payload.get("action") or "").strip().lower()
    if action not in VALID_ACTIONS:
        return None

    course_id = payload.get("course_id")
    if isinstance(course_id, str):
        course_id = resolve_course_id(course_id, courses)

    if not course_id:
        query = payload.get("course_query")
        if isinstance(query, str):
            course_id = resolve_course_id(query, courses)

    urls = payload.get("urls") if isinstance(payload.get("urls"), list) else []
    urls = [str(url).strip() for url in urls if str(url).strip()]

    return AskIntent(
        action=action,
        course=course_id,
        since=normalize_since(payload.get("since")) or extract_since(text) or "7d",
        urls=urls or extract_urls(text),
        source="llm",
        raw_text=text,
    )


def parse_user_intent(
    text: str,
    courses: Dict[str, Dict[str, Any]],
    ask_cfg: Optional[Dict[str, Any]] = None,
    prefer_llm: bool = True,
) -> Optional[AskIntent]:
    ask_cfg = ask_cfg or {}
    rule_intent = parse_rule_based_intent(text, courses)
    if prefer_llm:
        llm_intent = parse_llm_intent(text, courses, ask_cfg)
        if llm_intent and (llm_intent.action == ACTION_LIST or llm_intent.course):
            if rule_intent:
                if rule_intent.action == ACTION_ALL and llm_intent.action != ACTION_ALL:
                    llm_intent.action = ACTION_ALL
                if not llm_intent.course and rule_intent.course:
                    llm_intent.course = rule_intent.course
                if not llm_intent.urls and rule_intent.urls:
                    llm_intent.urls = rule_intent.urls
                if llm_intent.since == "7d" and rule_intent.since != "7d":
                    llm_intent.since = rule_intent.since
            return llm_intent
    return rule_intent
