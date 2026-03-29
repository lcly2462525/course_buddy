import os
import re
from pathlib import Path
from typing import Any, Dict

import yaml
from dotenv import load_dotenv


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    return value


def _resolve_path(base_dir: Path, value: str) -> str:
    expanded = Path(os.path.expanduser(value))
    if expanded.is_absolute():
        return str(expanded)
    return str((base_dir / expanded).resolve())


_LEGACY_COURSE_NAMES = {
    "86789": "城市经济学导论",
    "87081": "数理统计",
    "88817": "现代操作系统",
    "88821": "计算机系统结构（A类）",
    "88884": "泛函分析",
    "88892": "时间序列分析",
    "88918": "微分方程数值解",
    "89538": "人工智能（B类）",
}


def _infer_course_name_from_file(path: Path) -> str | None:
    stem = path.stem
    if "_" in stem:
        stem = stem.split("_", 1)[1]
    stem = re.sub(r"\(第[^()]*讲\)$", "", stem).strip()
    return stem or None


def _infer_courses_from_data(root_dir: Path) -> Dict[str, Dict[str, Any]]:
    inferred: Dict[str, Dict[str, Any]] = {}
    course_roots = [
        root_dir / "notes",
        root_dir / "transcripts",
        root_dir / "downloads",
        root_dir / "audio",
    ]

    course_ids = set()
    for course_root in course_roots:
        if not course_root.is_dir():
            continue
        for child in course_root.iterdir():
            if child.is_dir() and child.name.isdigit():
                course_ids.add(child.name)

    for course_id in sorted(course_ids):
        course_name = None
        for course_root in course_roots:
            course_dir = course_root / course_id
            if not course_dir.is_dir():
                continue
            for child in sorted(course_dir.iterdir()):
                if child.is_file():
                    course_name = _infer_course_name_from_file(child)
                    if course_name:
                        break
            if course_name:
                break

        inferred[course_id] = {
            "name": course_name or _LEGACY_COURSE_NAMES.get(course_id) or f"课程 {course_id}",
            "aliases": [],
        }

    return inferred


def load_config(path: str) -> Dict[str, Any]:
    config_path = Path(path).expanduser().resolve()
    load_dotenv(config_path.parent / ".env", override=False)
    with config_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg = _expand_env(cfg)
    cfg["config_path"] = str(config_path)
    cfg["config_dir"] = str(config_path.parent)
    cfg["root_dir"] = _resolve_path(config_path.parent, cfg.get("root_dir", "data"))
    cfg["_courses_inferred"] = False

    cookies_path = cfg.get("cookies_path")
    if cookies_path:
        cfg["cookies_path"] = _resolve_path(config_path.parent, cookies_path)

    courses = cfg.get("courses") or {}
    if not courses:
        inferred = _infer_courses_from_data(Path(cfg["root_dir"]))
        if inferred:
            cfg["courses"] = inferred
            cfg["_courses_inferred"] = True

    for meta in cfg.get("courses", {}).values():
        aliases = meta.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        meta["aliases"] = aliases
    return cfg
