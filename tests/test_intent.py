import unittest

from course_buddy.intent import ACTION_ALL, ACTION_LIST, ACTION_NOTES, AskIntent, parse_rule_based_intent, parse_user_intent, resolve_course_id
from unittest.mock import patch


COURSES = {
    "87081": {"name": "数理统计", "aliases": ["数统", "统计"]},
    "88817": {"name": "现代操作系统", "aliases": ["操作系统", "现操", "OS"]},
}


class IntentTests(unittest.TestCase):
    def test_course_name_maps_to_course_id(self):
        self.assertEqual(resolve_course_id("现代操作系统", COURSES), "88817")
        self.assertEqual(resolve_course_id("现操", COURSES), "88817")
        self.assertEqual(resolve_course_id("数统", COURSES), "87081")

    def test_rule_based_all_with_course_name_and_chinese_time(self):
        intent = parse_rule_based_intent("帮我整理现代操作系统最近两周的回放笔记", COURSES)
        self.assertIsNotNone(intent)
        self.assertEqual(intent.action, ACTION_ALL)
        self.assertEqual(intent.course, "88817")
        self.assertEqual(intent.since, "2w")

    def test_rule_based_notes(self):
        intent = parse_rule_based_intent("给我总结数理统计的笔记", COURSES)
        self.assertIsNotNone(intent)
        self.assertEqual(intent.action, ACTION_NOTES)
        self.assertEqual(intent.course, "87081")

    def test_rule_based_list(self):
        intent = parse_rule_based_intent("列出现在有哪些课", COURSES)
        self.assertIsNotNone(intent)
        self.assertEqual(intent.action, ACTION_LIST)

    def test_llm_action_is_upgraded_when_rule_sees_all(self):
        with patch("course_buddy.intent.parse_llm_intent") as mock_llm:
            mock_llm.return_value = AskIntent(action="transcribe", course="87081", since="7d", source="llm")
            intent = parse_user_intent(
                "帮我下载并转录数理统计最近一周的回放，然后整理笔记",
                COURSES,
                ask_cfg={"llm": {"enabled": True}},
                prefer_llm=True,
            )
        self.assertIsNotNone(intent)
        self.assertEqual(intent.action, ACTION_ALL)
        self.assertEqual(intent.course, "87081")


if __name__ == "__main__":
    unittest.main()
