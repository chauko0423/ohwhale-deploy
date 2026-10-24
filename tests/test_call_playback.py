"""Exercise real playback control flow with fake TTS and audio devices."""
import contextlib
import importlib.util
import io
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np


class PlaybackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "call_playback_under_test", Path(__file__).parents[1] / "scripts/call_loop.py")
        cls.mod = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {n: Mock() for n in
                        ("soundcard", "sounddevice", "soundfile", "astrbot_bridge")}):
            spec.loader.exec_module(cls.mod)
        cls.mod.TTS_CLIENT.close()

    def setUp(self):
        self.m = self.mod
        self.m.INTERRUPT.clear()
        self.m.PLAYING.clear()
        self.m.PLAY_START = 0.0
        self.m.TTS_STREAM = True
        self.speaker = self.m.Speaker.__new__(self.m.Speaker)
        self.speaker.dev = 1
        self.writes = []
        self.stream = SimpleNamespace(write=lambda block: self.writes.append(block.copy()))
        self.m.sd.OutputStream = Mock(side_effect=lambda **kw: contextlib.nullcontext(self.stream))
        self.pcm = np.array([2000, -2000, 1000, -1000], dtype="<i2").tobytes()
        self.m.TTS_CLIENT = SimpleNamespace(stream=lambda *a, **k: self.response([self.pcm]))

    def response(self, pieces, status=200):
        return contextlib.nullcontext(SimpleNamespace(status_code=status, iter_raw=lambda: iter(pieces)))

    def assert_clean(self):
        self.assertFalse(self.m.PLAYING.is_set())
        self.assertEqual(self.m.PLAY_START, 0.0)

    def test_first_audio_means_successful_device_write(self):
        seen = []
        def first():
            self.assertTrue(self.writes)
            self.assertGreater(self.m.PLAY_START, 0)
            seen.append(True)
        self.assertFalse(self.speaker.say_streaming("你好，这是测试。", first))
        self.assertEqual(seen, [True])
        self.assert_clean()

    def test_empty_tts_is_explicit_failure_and_next_turn_works(self):
        self.m.TTS_CLIENT.stream = lambda *a, **k: self.response([])
        with self.assertRaisesRegex(self.m.SpeechPlaybackError, "未返回可播放音频"):
            self.speaker.say_streaming("你好")
        self.assert_clean()
        self.m.TTS_CLIENT.stream = lambda *a, **k: self.response([self.pcm])
        self.assertFalse(self.speaker.say_streaming("再试一次"))

    def test_http_failure_is_not_reported_as_speech(self):
        self.m.TTS_CLIENT.stream = lambda *a, **k: self.response([], 500)
        first = Mock()
        with self.assertRaisesRegex(self.m.SpeechPlaybackError, "HTTP 500"):
            self.speaker.say_streaming("你好", first)
        first.assert_not_called()
        self.assert_clean()

    def test_interrupt_before_first_audio_does_not_crash(self):
        def response(*a, **k):
            self.assertEqual(self.m.PLAY_START, 0.0)
            self.m.INTERRUPT.set()
            return self.response([self.pcm])
        self.m.TTS_CLIENT.stream = response
        first = Mock()
        self.assertTrue(self.speaker.say_streaming("你好", first))
        first.assert_not_called()
        self.assert_clean()

    def test_failed_device_write_does_not_mark_first_audio(self):
        self.stream.write = Mock(side_effect=OSError("device unavailable"))
        first = Mock()
        with self.assertRaises(OSError):
            self.speaker.say_streaming("你好", first)
        first.assert_not_called()
        self.assert_clean()

    def test_device_open_failure_resets_playback_state(self):
        self.m.sd.OutputStream.side_effect = OSError("device unavailable")
        with self.assertRaises(OSError):
            self.speaker.say_streaming("你好")
        self.assert_clean()

    def test_odd_network_chunks_preserve_all_pcm_samples(self):
        self.m.TTS_CLIENT.stream = lambda *a, **k: self.response(
            [self.pcm[:1], self.pcm[1:3], self.pcm[3:]])
        self.m.TTS_RAW_RATE = self.m.RATE_OUT
        try:
            self.speaker.say_streaming("你好")
            actual = np.concatenate(self.writes)[:, 0]
            np.testing.assert_allclose(actual, np.frombuffer(self.pcm, "<i2") / 32768)
        finally:
            self.m.TTS_RAW_RATE = 32000

    def test_no_audio_reporting_never_indexes_empty_time_list(self):
        for interrupted in (False, True):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.m.speak_reply(Mock(say_streaming=Mock(return_value=interrupted)), "你好",
                                   started_at=time.time(), asr_seconds=0, llm_seconds=0)
            self.assertIn("未播放", out.getvalue())
            self.assertNotIn("说完", out.getvalue())

    def test_playback_failure_keeps_next_turn_available(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.m.speak_reply(Mock(say_streaming=Mock(side_effect=OSError("private path"))), "你好",
                               started_at=time.time(), asr_seconds=0, llm_seconds=0)
        self.assertIn("继续监听", out.getvalue())
        self.assertNotIn("private path", out.getvalue())
        self.assertFalse(self.speaker.say_streaming("下一句"))


if __name__ == "__main__":
    unittest.main()
