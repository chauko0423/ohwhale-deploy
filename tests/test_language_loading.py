"""A denied optional native import must not prevent Chinese startup."""
import builtins
import importlib.util
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class LanguageLoadingTests(unittest.TestCase):
    def setUp(self):
        self.attempts = []
        original_import = builtins.__import__

        def restricted_import(name, *args, **kwargs):
            if name in {"fast_langdetect", "split_lang"}:
                self.attempts.append(name)
                raise ImportError("Application control denied native dependency")
            return original_import(name, *args, **kwargs)

        self.guard = patch("builtins.__import__", side_effect=restricted_import)
        self.guard.start()
        self.addCleanup(self.guard.stop)
        # Only silence jieba; the language module itself is the shipped source.
        with patch.dict("sys.modules", {"jieba": types.SimpleNamespace(setLogLevel=lambda level: None)}):
            source = Path(__file__).resolve().parents[1] / "third-party/gpt-sovits-overrides/GPT_SoVITS/text/LangSegmenter/langsegmenter.py"
            spec = importlib.util.spec_from_file_location("language_loading_fixture", source)
            self.module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.module)

    def test_chinese_startup_never_imports_native_detector(self):
        text = "你好，今天是2026年。我们开始聊天吧！"
        self.assertEqual(self.module.LangSegmenter.getTexts(text), [{"lang": "zh", "text": text}])
        self.assertEqual(self.attempts, [])

    def test_existing_fast_path_preserves_explicit_language(self):
        text = "今日はいい天気です。"
        self.assertEqual(self.module.LangSegmenter.getTexts(text, "ja"), [{"lang": "ja", "text": text}])
        self.assertEqual(self.attempts, [])

    def test_mixed_language_reports_failure_without_silent_fallback(self):
        with self.assertRaisesRegex(RuntimeError, "Multilingual segmentation is unavailable") as error:
            self.module.LangSegmenter.getTexts("你好，Hello", "zh")
        self.assertIsInstance(error.exception.__cause__, ImportError)
        self.assertEqual(self.attempts, ["fast_langdetect"])


if __name__ == "__main__":
    unittest.main()
