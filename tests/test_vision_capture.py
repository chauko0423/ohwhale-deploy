"""视觉采集的离线测试：缩放、窗口优先、动作多帧、失败回退。

不依赖真实摄像头 / QQ 通话窗口，全部用假对象替换。
"""
import base64
import importlib.util
import io
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image


def load_vision():
    spec = importlib.util.spec_from_file_location(
        "vision_capture_under_test",
        Path(__file__).parents[1] / "scripts" / "vision_capture.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeWindow:
    def __init__(self, size=(1920, 1080)):
        self.size = size

    def capture_as_image(self):
        return Image.new("RGB", self.size, (30, 60, 90))


class FakeRect:
    left, top, right, bottom = 100, 100, 2020, 1180


class VisionCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_vision()

    def _decode(self, b64):
        return Image.open(io.BytesIO(base64.b64decode(b64)))

    def test_shrink_limits_width_and_valid_jpeg(self):
        out = self.m._shrink(Image.new("RGB", (3840, 2160)), 1280, 70)
        img = self._decode(out)
        self.assertEqual(img.width, 1280)
        self.assertEqual(img.format, "JPEG")

    def test_grab_for_reply_uses_window_capture_first(self):
        with patch.object(self.m, "find_call_window", return_value=(FakeWindow(), FakeRect())), \
             patch.object(self.m, "grab_fullscreen_b64", lambda *a, **k: "FULLSCREEN"):
            out = self.m.grab_for_reply("看看我", source="call_window")
        self.assertEqual(len(out), 1)
        self.assertNotEqual(out[0], "FULLSCREEN")
        self.assertEqual(self._decode(out[0]).width, self.m.DEFAULT_MAX_WIDTH)

    def test_grab_for_reply_falls_back_to_fullscreen(self):
        with patch.object(self.m, "find_call_window", return_value=(None, None)), \
             patch.object(self.m, "grab_fullscreen_b64", lambda *a, **k: "FULLSCREEN"):
            out = self.m.grab_for_reply("看看我", source="call_window")
        self.assertEqual(out, ["FULLSCREEN"])

    def test_motion_words_take_two_frames(self):
        calls = {"n": 0}

        def fake_full(*_a, **_k):
            calls["n"] += 1
            return f"frame{calls['n']}"

        with patch.object(self.m, "find_call_window", return_value=(None, None)), \
             patch.object(self.m, "grab_fullscreen_b64", fake_full), \
             patch.object(self.m.time, "sleep", lambda *_: None):
            out = self.m.grab_for_reply("我在做什么动作", source="call_window",
                                        motion_frames=2)
        self.assertEqual(out, ["frame1", "frame2"])

    def test_plain_question_takes_one_frame(self):
        with patch.object(self.m, "find_call_window", return_value=(None, None)), \
             patch.object(self.m, "grab_fullscreen_b64", lambda *a, **k: "one"):
            out = self.m.grab_for_reply("我帅吗", source="call_window", motion_frames=2)
        self.assertEqual(out, ["one"])

    def test_no_screenshot_returns_empty_not_crash(self):
        with patch.object(self.m, "find_call_window", return_value=(None, None)), \
             patch.object(self.m, "grab_fullscreen_b64", lambda *a, **k: None):
            self.assertEqual(self.m.grab_for_reply("看看我"), [])

    def test_motion_word_list_covers_gesture_questions(self):
        for text in ("我在做什么动作", "比划一下手势", "你看到我挥手了吗"):
            self.assertTrue(self.m._looks_like_motion(text), text)
        self.assertFalse(self.m._looks_like_motion("今天好累"))


if __name__ == "__main__":
    unittest.main()
