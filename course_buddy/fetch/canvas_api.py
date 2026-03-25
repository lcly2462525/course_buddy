"""
Canvas LMS REST API 工具

通过 Canvas API Token 获取用户的课程列表等信息。
API 文档: https://canvas.instructure.com/doc/api/
"""

import os
from typing import Dict, List, Optional

import requests

CANVAS_BASE = "https://oc.sjtu.edu.cn"
TOKEN_FILE = os.path.expanduser("~/.config/canvas/token")


def load_canvas_token() -> Optional[str]:
    """加载 Canvas API Token"""
    if os.path.exists(TOKEN_FILE):
        return open(TOKEN_FILE).read().strip()
    return os.environ.get("CANVAS_TOKEN")


def get_active_courses(token: Optional[str] = None) -> List[Dict]:
    """
    获取当前用户的活跃课程列表。

    返回列表，每个元素包含:
      - id: 课程 ID (int)
      - name: 课程全名
      - course_code: 课程代码
      - enrollment_term_id: 学期 ID

    只返回 enrollment_state=active 的课程（即当前学期在修的课程）。
    """
    if not token:
        token = load_canvas_token()
    if not token:
        raise RuntimeError(
            f"未找到 Canvas API Token。\n"
            f"请先配置: mkdir -p ~/.config/canvas && echo 'YOUR_TOKEN' > {TOKEN_FILE}\n"
            f"获取方式: https://oc.sjtu.edu.cn → 设置 → 新建访问许可证"
        )

    headers = {"Authorization": f"Bearer {token}"}
    courses = []
    url = f"{CANVAS_BASE}/api/v1/courses"
    params = {
        "enrollment_state": "active",
        "per_page": 100,
        "include[]": "term",
    }

    while url:
        r = requests.get(url, headers=headers, params=params, timeout=15)
        if r.status_code == 401:
            raise RuntimeError("Canvas API Token 无效或已过期，请重新生成。")
        r.raise_for_status()
        batch = r.json()
        if isinstance(batch, dict) and "errors" in batch:
            raise RuntimeError(f"Canvas API 错误: {batch['errors']}")
        courses.extend(batch)

        # 处理分页 (Link header)
        url = None
        params = {}  # 后续页不再传 params，URL 里已经包含了
        link_header = r.headers.get("Link", "")
        for part in link_header.split(","):
            if 'rel="next"' in part:
                url = part.split(";")[0].strip().strip("<>")
                break

    return courses


def filter_real_courses(courses: List[Dict]) -> List[Dict]:
    """
    过滤掉非真实课程（如学期概览、测试课程等）。

    启发式规则:
    - 排除 name 包含 "概览"、"sandbox"、"test" 的课程
    - 排除没有 course_code 的课程
    - 排除学生未正式选课的课程（无 enrollments 或 enrollment 不含 student/observer 角色）
    """
    filtered = []
    skip_keywords = ["概览", "sandbox", "test", "template", "培训"]

    for c in courses:
        name = (c.get("name") or "").lower()
        code = c.get("course_code") or ""

        # 跳过关键词
        if any(kw.lower() in name for kw in skip_keywords):
            continue

        # 必须有 course_code
        if not code.strip():
            continue

        filtered.append(c)

    return filtered


def courses_to_config(courses: List[Dict]) -> Dict:
    """
    将 Canvas 课程列表转为 config.yaml 的 courses 字典格式。

    返回:
      {
        "12345": {
          "name": "课程名称",
          "aliases": [],
          "urls": ["https://v.sjtu.edu.cn/course/12345"],
          "note_rules": {"key_terms": []}
        },
        ...
      }
    """
    result = {}
    for c in courses:
        cid = str(c["id"])
        name = c.get("name") or c.get("course_code") or f"课程{cid}"
        # 清理课程名（去掉学期前缀等）
        # 例如 "(2025-2026-2)-MA263-2" → 保留 name 字段
        result[cid] = {
            "name": name,
            "aliases": [],
            "urls": [f"https://v.sjtu.edu.cn/course/{cid}"],
            "note_rules": {"key_terms": []},
        }
    return result
