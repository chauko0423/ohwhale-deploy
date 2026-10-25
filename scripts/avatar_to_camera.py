r"""把 avatar_live 的共享内存画面桥接成 CameraEmulator 会重新读取的 image.png。

背景（实测于 Windows 11 + QQNT 9.9.36 + CameraEmulator 0.1.0a1）：

1. CameraEmulatorSource.dll（运行在 Frame Server，session 0）只在能映射到
   ``Global\CameraEmulatorScreen`` 时才输出"屏幕"帧，否则退回默认占位图。
2. avatar_live 以普通用户运行时创建的是 ``Local\CameraEmulatorScreen``——
   会话级命名空间，Frame Server 看不到，于是摄像头永远显示占位图。
3. 摄像头源会**每帧重新读取** ``C:\ProgramData\CameraEmulator\image.png``
   （实测覆盖该文件后下一帧即生效），因此可以用它作为跨会话通道：
   共享内存 → image.png（本脚本，默认 10 fps）→ 摄像头。

本脚本同时兼容 Global / Local 两种映射，所以无论 avatar_live 用什么权限运行
都能把画面送进摄像头；不需要把 avatar_live 提权。

用法：
    python scripts/avatar_to_camera.py [--fps 10] [--target <image.png>]

默认目标路径与 CameraEmulator 的 ``camera-common.ps1`` 保持一致。
"""
from __future__ import annotations

import argparse
import ctypes
import io
import os
import sys
import time
from ctypes import wintypes

from PIL import Image

SECTION_GLOBAL = r"Global\CameraEmulatorScreen"
SECTION_LOCAL = r"Local\CameraEmulatorScreen"
HEADER_SIZE = 4096
MAGIC = 0x534D4543
MAX_W, MAX_H = 3840, 2160
MAP_BYTES = HEADER_SIZE + MAX_W * MAX_H * 4
DEFAULT_TARGET = r"C:\ProgramData\CameraEmulator\image.png"
OUT_W, OUT_H = 1280, 720

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.OpenFileMappingW.restype = wintypes.HANDLE
k32.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
k32.MapViewOfFile.restype = ctypes.c_void_p
k32.MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                              wintypes.DWORD, ctypes.c_size_t]
k32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
k32.CloseHandle.argtypes = [wintypes.HANDLE]
FILE_MAP_READ = 0x0004


class Section:
    """打开共享内存；优先 Global，退回 Local。"""

    def __init__(self) -> None:
        self.handle = None
        self.view = None
        self.name = ""

    def open(self) -> bool:
        for name in (SECTION_GLOBAL, SECTION_LOCAL):
            handle = k32.OpenFileMappingW(FILE_MAP_READ, False, name)
            if not handle:
                continue
            view = k32.MapViewOfFile(handle, FILE_MAP_READ, 0, 0, MAP_BYTES)
            if not view:
                k32.CloseHandle(handle)
                continue
            self.handle, self.view, self.name = handle, view, name
            return True
        return False

    def close(self) -> None:
        if self.view:
            k32.UnmapViewOfFile(self.view)
            self.view = None
        if self.handle:
            k32.CloseHandle(self.handle)
            self.handle = None

    def sequence(self) -> int:
        return ctypes.c_int32.from_buffer_copy(
            ctypes.string_at(self.view + 4, 4)).value

    def read(self):
        """返回 (w, h, bgrx)；读写竞争时重试，失败返回 None。"""
        for _ in range(3):
            header = ctypes.string_at(self.view, 16)
            magic, seq, w, h = (ctypes.c_uint32 * 4).from_buffer_copy(header)
            if magic != MAGIC or not (16 <= w <= MAX_W) or not (16 <= h <= MAX_H):
                return None
            data = ctypes.string_at(self.view + HEADER_SIZE, w * h * 4)
            if seq == self.sequence():
                return w, h, data
            time.sleep(0.004)
        return None


def write_png(target: str, w: int, h: int, bgrx: bytes, quality: int) -> None:
    img = Image.frombuffer("RGB", (w, h), bgrx, "raw", "BGRX", 0, 1)
    if (w, h) != (OUT_W, OUT_H):
        img = img.resize((OUT_W, OUT_H), Image.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    tmp = target + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(buf.getvalue())
    os.replace(tmp, target)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fps", type=float, default=10.0)
    parser.add_argument("--target", default=DEFAULT_TARGET)
    parser.add_argument("--quality", type=int, default=82)
    args = parser.parse_args()

    os.makedirs(os.path.dirname(args.target), exist_ok=True)
    section = Section()
    period = 1.0 / max(1.0, args.fps)
    last_seq = None
    frames = 0
    print(f"[avatar-bridge] target={args.target} fps={args.fps:g}", flush=True)

    try:
        while True:
            if section.view is None:
                if not section.open():
                    time.sleep(1.0)
                    continue
                print(f"[avatar-bridge] 已连接 {section.name}", flush=True)

            frame = section.read()
            if frame is None:
                time.sleep(0.2)
                continue

            seq = section.sequence()
            if seq == last_seq:
                time.sleep(period / 2)
                continue
            last_seq = seq

            w, h, data = frame
            try:
                write_png(args.target, w, h, data, args.quality)
                frames += 1
                if frames % 100 == 0:
                    print(f"[avatar-bridge] {frames} 帧", flush=True)
            except OSError as exc:
                print(f"[avatar-bridge] 写入失败: {exc}", flush=True)
                time.sleep(0.3)
            time.sleep(period)
    except KeyboardInterrupt:
        pass
    finally:
        section.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
