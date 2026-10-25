"""心情/好感状态机的离线测试：规则、边界、持久化、摘要。"""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


def load_mood():
    spec = importlib.util.spec_from_file_location(
        "mood_state_under_test",
        Path(__file__).parents[1] / "scripts" / "mood_state.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MoodStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_mood()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "mood.json")
        self.addCleanup(self.tmp.cleanup)

    def test_default_when_file_missing(self):
        data = self.m.load(self.path)
        self.assertEqual(data["affection"], 50)
        self.assertEqual(data["mood"], 0)
        self.assertEqual(data["last"], [])

    def test_praise_raises_affection_and_mood(self):
        self.m.update_from_user("你今天好可爱啊", self.path)
        data = self.m.load(self.path)
        self.assertGreater(data["affection"], 50)
        self.assertGreater(data["mood"], 0)

    def test_insult_lowers_affection(self):
        self.m.update_from_user("你真笨", self.path)
        self.assertLess(self.m.load(self.path)["affection"], 50)

    def test_sad_message_does_not_lower_affection(self):
        self.m.update_from_user("我今天好累，难受", self.path)
        data = self.m.load(self.path)
        self.assertGreaterEqual(data["affection"], 50)
        self.assertLess(data["mood"], 0)

    def test_clamped_to_range(self):
        for _ in range(50):
            self.m.update_from_user("滚，你真蠢", self.path)
        data = self.m.load(self.path)
        self.assertGreaterEqual(data["affection"], 0)
        self.assertGreaterEqual(data["mood"], -5)
        for _ in range(50):
            self.m.update_from_user("你真可爱，我喜欢你", self.path)
        data = self.m.load(self.path)
        self.assertLessEqual(data["affection"], 100)
        self.assertLessEqual(data["mood"], 5)

    def test_last_lines_keep_only_five(self):
        for index in range(8):
            self.m.update_from_user(f"第{index}句", self.path)
        self.assertEqual(len(self.m.load(self.path)["last"]), 5)

    def test_summary_mentions_affection_and_last_line(self):
        self.m.update_from_user("我好累啊", self.path)
        line = self.m.summary(self.path)
        self.assertIn("好感", line)
        self.assertIn("我好累啊", line)

    def test_save_is_valid_json_even_after_many_writes(self):
        for index in range(20):
            self.m.update_from_user(f"测试{index}", self.path)
        with open(self.path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertIn("affection", data)
        self.assertIn("updated", data)


if __name__ == "__main__":
    unittest.main()
