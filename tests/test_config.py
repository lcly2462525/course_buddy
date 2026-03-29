import tempfile
import textwrap
import unittest
from pathlib import Path

from course_buddy.config import load_config


class ConfigTests(unittest.TestCase):
    def test_load_config_resolves_paths_relative_to_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            (config_dir / "cookies.json").write_text("[]", encoding="utf-8")
            (config_dir / "config.yaml").write_text(
                textwrap.dedent(
                    """
                    root_dir: data
                    cookies_path: ./cookies.json
                    courses:
                      "87081":
                        name: 数理统计
                    """
                ).strip(),
                encoding="utf-8",
            )
            cfg = load_config(str(config_dir / "config.yaml"))
            self.assertEqual(cfg["root_dir"], str((config_dir / "data").resolve()))
            self.assertEqual(cfg["cookies_path"], str((config_dir / "cookies.json").resolve()))

    def test_load_config_recovers_courses_from_local_data_when_config_is_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            (config_dir / "config.yaml").write_text(
                textwrap.dedent(
                    """
                    root_dir: data
                    courses: {}
                    """
                ).strip(),
                encoding="utf-8",
            )
            notes_dir = config_dir / "data" / "notes" / "87081"
            notes_dir.mkdir(parents=True)
            (notes_dir / "2026-03-20_数理统计(第9讲).md").write_text("# note", encoding="utf-8")

            cfg = load_config(str(config_dir / "config.yaml"))

            self.assertTrue(cfg["_courses_inferred"])
            self.assertEqual(cfg["courses"]["87081"]["name"], "数理统计")
            self.assertEqual(cfg["courses"]["87081"]["aliases"], [])

    def test_load_config_keeps_existing_courses_without_inference(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            (config_dir / "config.yaml").write_text(
                textwrap.dedent(
                    """
                    root_dir: data
                    courses:
                      "87081":
                        name: 数理统计
                    """
                ).strip(),
                encoding="utf-8",
            )
            notes_dir = config_dir / "data" / "notes" / "88884"
            notes_dir.mkdir(parents=True)
            (notes_dir / "2026-03-23_泛函分析(第12讲).md").write_text("# note", encoding="utf-8")

            cfg = load_config(str(config_dir / "config.yaml"))

            self.assertFalse(cfg["_courses_inferred"])
            self.assertEqual(set(cfg["courses"]), {"87081"})


if __name__ == "__main__":
    unittest.main()
