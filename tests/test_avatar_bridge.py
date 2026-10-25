"""avatar_to_camera 桥接的离线测试：帧解析与写盘。"""
import importlib.util
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image


def load_bridge():
    spec = importlib.util.spec_from_file_location(
        "avatar_bridge_under_test",
        Path(__file__).parents[1] / "scripts/avatar_to_camera.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AvatarBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_bridge()

    def make_section(self, w, h, seq=7, magic=None):
        magic = self.m.MAGIC if magic is None else magic
        header = struct.pack("<I i I I Q", magic, seq, w, h, 12345678)
        header = header.ljust(self.m.HEADER_SIZE, b"\0")
        pixels = bytes([10, 20, 30, 255]) * (w * h)
        section = self.m.Section()
        section.view = 0
        section.name = "fake"
        payload = {0: header, self.m.HEADER_SIZE: pixels, 4: struct.pack("<i", seq)}
        section._payload = payload
        return section

    def test_read_parses_header_and_pixels(self):
        section = self.make_section(16, 16)
        with patch.object(self.m.ctypes, "string_at",
                          side_effect=lambda ptr, size: section._payload[ptr]):
            self.assertEqual(section.read(), (16, 16, bytes([10, 20, 30, 255]) * 256))

    def test_read_rejects_wrong_magic(self):
        section = self.make_section(16, 16, magic=0xDEADBEEF)
        with patch.object(self.m.ctypes, "string_at",
                          side_effect=lambda ptr, size: section._payload.get(ptr, b"\0")):
            self.assertIsNone(section.read())

    def test_write_png_scales_to_720p(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "image.png")
            self.m.write_png(target, 16, 9, bytes([0, 0, 255, 255]) * (16 * 9), 80)
            with Image.open(target) as img:
                self.assertEqual(img.size, (self.m.OUT_W, self.m.OUT_H))
            self.assertFalse(os.path.exists(target + ".tmp"))

    def test_default_target_matches_camera_storage(self):
        self.assertEqual(self.m.DEFAULT_TARGET,
                         r"C:\ProgramData\CameraEmulator\image.png")


if __name__ == "__main__":
    unittest.main()
