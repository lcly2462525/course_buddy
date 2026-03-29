import unittest

from course_buddy.cli import build_parser


class CliParserTests(unittest.TestCase):
    def test_explicit_fetch_arguments_parse_normally(self):
        parser = build_parser()
        args = parser.parse_args(["fetch", "--course", "87081", "--since", "7d"])
        self.assertEqual(args.mode, "fetch")
        self.assertEqual(args.course, "87081")
        self.assertEqual(args.since, "7d")

    def test_config_works_before_subcommand(self):
        parser = build_parser()
        # --config is a subparser-level option, must come after subcommand
        args = parser.parse_args(["fetch", "--config", "foo.yaml", "--course", "87081"])
        self.assertEqual(args.config, "foo.yaml")

    def test_ask_arguments_parse_normally(self):
        parser = build_parser()
        args = parser.parse_args(["ask", "帮我整理", "现代操作系统", "最近两周的笔记"])
        self.assertEqual(args.mode, "ask")
        self.assertEqual(" ".join(args.text), "帮我整理 现代操作系统 最近两周的笔记")

    def test_refresh_arguments_parse_normally(self):
        parser = build_parser()
        args = parser.parse_args(["refresh", "--config", "foo.yaml"])
        self.assertEqual(args.mode, "refresh")
        self.assertEqual(args.config, "foo.yaml")


if __name__ == "__main__":
    unittest.main()
