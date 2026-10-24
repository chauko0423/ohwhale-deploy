"""The observed QQ session, not the system default, decides return routing."""
import ast
import contextlib
import io
from pathlib import Path
import unittest
from unittest.mock import Mock


class AudioReturnTests(unittest.TestCase):
    def check(self, capture, default="Microphone Array (Realtek)", hint="CABLE Output"):
        tree = ast.parse((Path(__file__).parents[1] / "scripts/audio_doctor.py").read_text("utf-8"))
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "check_return")
        results = []
        namespace = {"setting": lambda key: hint,
                     "default_capture": Mock(side_effect=default) if isinstance(default, Exception)
                        else lambda: (default, "fixture-id"),
                     "mark": lambda name, ok, detail: results.append((name, ok, detail))}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "audio_return", "exec"), namespace)
        with contextlib.redirect_stdout(io.StringIO()):
            namespace["check_return"]({"capture": set(capture)})
        return results

    def test_explicit_qq_virtual_mic_passes_with_physical_default(self):
        self.assertTrue(all(r[1] for r in self.check(["CABLE Output (VB-Audio Virtual Cable)"])))

    def test_qq_physical_mic_fails_even_when_system_default_is_virtual(self):
        self.assertFalse(self.check(["Realtek Microphone"], default="CABLE Output")[0][1])

    def test_no_qq_session_is_not_a_pass(self):
        self.assertFalse(self.check([], default="CABLE Output")[0][1])

    def test_default_endpoint_probe_failure_does_not_override_actual_session(self):
        self.assertTrue(self.check(["CABLE Output"], default=OSError("probe unavailable"))[0][1])

    def test_empty_device_hint_cannot_match_every_device(self):
        self.assertFalse(self.check(["Realtek Microphone"], hint="")[0][1])


if __name__ == "__main__":
    unittest.main()
