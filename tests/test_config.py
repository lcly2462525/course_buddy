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


if __name__ == "__main__":
    unittest.main()
