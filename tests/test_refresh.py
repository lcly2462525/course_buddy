import tempfile
import unittest
from pathlib import Path

import yaml

from course_buddy.cli import _refresh_courses, _load_or_init_config
from course_buddy.fetch.canvas_api import courses_to_config


class RefreshTests(unittest.TestCase):
    def test_refresh_replaces_non_current_courses_and_preserves_user_meta(self):
        existing_courses = {
            "87081": {
                "name": "数理统计",
                "aliases": ["数统"],
                "urls": ["https://custom.example/course/87081"],
                "note_rules": {"key_terms": ["极大似然"]},
            },
            "99999": {
                "name": "上学期课程",
                "aliases": ["旧课"],
                "note_rules": {"key_terms": ["old"]},
            },
        }
        new_courses = courses_to_config([
            {"id": 87081, "name": "数理统计", "course_code": "MA263"},
            {"id": 88884, "name": "泛函分析", "course_code": "MA301"},
        ])

        refreshed, added, updated, removed = _refresh_courses(existing_courses, new_courses)

        self.assertEqual(set(refreshed), {"87081", "88884"})
        self.assertEqual(refreshed["87081"]["aliases"], ["数统"])
        self.assertEqual(refreshed["87081"]["urls"], ["https://custom.example/course/87081"])
        self.assertEqual(refreshed["87081"]["note_rules"], {"key_terms": ["极大似然"]})
        self.assertEqual(added, [("88884", "泛函分析")])
        self.assertEqual(updated, [("87081", "数理统计")])
        self.assertEqual(removed, [("99999", "上学期课程")])

    def test_load_or_init_config_uses_example_when_config_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            (config_dir / "config.yaml.example").write_text(
                "root_dir: data\ncourses:\n  \"1\":\n    name: 示例课\n",
                encoding="utf-8",
            )

            cfg = _load_or_init_config(config_dir / "config.yaml", config_dir)

            self.assertEqual(cfg["root_dir"], "data")
            self.assertEqual(cfg["courses"], {})

    def test_load_or_init_config_reads_existing_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            config_path = config_dir / "config.yaml"
            config_path.write_text("root_dir: data\ncourses: {}\n", encoding="utf-8")

            cfg = _load_or_init_config(config_path, config_dir)

            self.assertEqual(cfg, yaml.safe_load(config_path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
