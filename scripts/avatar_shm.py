"""把她的画面实时写进 CameraEmulator 的共享内存（30fps，带口型）

原理（照 src/common/ScreenShare.h 复刻）：
    Global\\CameraEmulatorScreen 这块内存布局是
        [0, 4096)         ScreenHeader：magic / sequence / width / height / lastUpdateTick
        [4096, ...)       BGRX 像素，top-down，stride = width * 4
    写入方用 seqlock：写前把 sequence 加一变成奇数，写完再加一变成偶数。
    读取方（帧服务器）看到 sequence 是偶数且变化了，才认为这一帧完整。

为什么不用 --update image.png：
    那样每次都要起进程、解码 PNG、拷文件，最多 5fps，做不了口型。
    直接写内存可以 30fps，而且尺寸由我们决定（720x1280 竖屏）。

口型来源：
    call_loop 播放她的语音时，每 2048 个采样通过 UDP 发一次音量到 127.0.0.1:9999。

用法：
    python avatar_shm.py            # 一直写
    python avatar_shm.py --probe    # 只检查共享内存能否打开/创建
"""

from __future__ import annotations

from ohwhale.config import get as setting, path as config_path, project_path, state_path, log_path, secret, llm, require

import argparse
import ctypes
import socket
import struct
import sys
import threading
import time
from ctypes import wintypes

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# ---- 和 C++ 侧完全一致的常量 ----
SECTION_NAME = "Global\\CameraEmulatorScreen"
SECTION_NAME_LOCAL = "Local\\CameraEmulatorScreen"
MAGIC = 0x534D4543          # "CEMS"
HEADER_SIZE = 4096
MAX_W, MAX_H = 3840, 2160
SECTION_SIZE = HEADER_SIZE + MAX_W * MAX_H * 4

W, H = 1280, 720            # 16:9 横屏（QQ 的容器就是横的，竖屏会被它加边）
FPS = 30
UDP_PORT = int(setting("services.avatar_udp_port"))

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.OpenFileMappingW.restype = wintypes.HANDLE
k32.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
k32.CreateFileMappingW.restype = wintypes.HANDLE
k32.CreateFileMappingW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                   wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR]
k32.MapViewOfFile.restype = ctypes.c_void_p
k32.MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                              wintypes.DWORD, ctypes.c_size_t]
k32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
k32.GetTickCount64.restype = ctypes.c_ulonglong

FILE_MAP_WRITE = 0x0002
FILE_MAP_READ = 0x0004
PAGE_READWRITE = 0x04

level = 0.0
level_lock = threading.Lock()


def udp_listener():
    """收 call_loop 发来的音量。"""
    global level
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", UDP_PORT))
    s.settimeout(0.2)
    last = time.time()
    while True:
        try:
            data, _ = s.recvfrom(64)
            v = float(data.decode("ascii", "ignore").strip() or 0)
            with level_lock:
                level = max(0.0, min(1.0, v))
            last = time.time()
        except socket.timeout:
            pass
        except Exception:  # noqa: BLE001
            continue
        if time.time() - last > 0.6:
            with level_lock:
                level *= 0.75


def open_section():
    """优先打开帧服务器创建的段；没有就自己建一个（带同样的 SDDL）。"""
    h = k32.OpenFileMappingW(FILE_MAP_WRITE, False, SECTION_NAME)
    if h:
        print("已打开现有共享内存", SECTION_NAME)
        return h, False

    # 自建时需要 SDDL，否则帧服务器（LOCAL SERVICE）读不到
    sddl = "D:(A;;GA;;;SY)(A;;GA;;;LS)(A;;GA;;;BA)(A;;GRGW;;;IU)S:(ML;;NW;;;LW)"
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.ULONG)]
    sd = ctypes.c_void_p()
    size = wintypes.ULONG()
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(sd), ctypes.byref(size)):
        raise OSError(f"构造安全描述符失败: {ctypes.get_last_error()}")

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD),
                    ("lpSecurityDescriptor", ctypes.c_void_p),
                    ("bInheritHandle", wintypes.BOOL)]

    sa = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), sd, False)
    hi = SECTION_SIZE >> 32
    lo = SECTION_SIZE & 0xFFFFFFFF
    h = k32.CreateFileMappingW(wintypes.HANDLE(-1), ctypes.byref(sa),
                               PAGE_READWRITE, hi, lo, SECTION_NAME)
    if not h:
        err = ctypes.get_last_error()
        print(f"创建 Global 段失败({err})，退回 Local")
        h = k32.CreateFileMappingW(wintypes.HANDLE(-1), ctypes.byref(sa),
                                   PAGE_READWRITE, hi, lo, SECTION_NAME_LOCAL)
        if not h:
            raise OSError(f"创建共享内存失败: {ctypes.get_last_error()}")
        print("已创建", SECTION_NAME_LOCAL)
        return h, True
    print("已创建", SECTION_NAME)
    return h, True


def render_variants() -> list[np.ndarray]:
    """用最终场景底图 + 9 档口型，预渲染成 BGRX，运行时只做 memcpy。"""
    import json
    scene = Image.open(project_path("assets/avatar/scene_base.jpg")).convert("RGB")
    if scene.size != (W, H):
        scene = scene.resize((W, H), Image.LANCZOS)
    anchor = json.load(open(project_path("assets/avatar/mouth_anchor.json")))
    cx, cy = anchor["x"], anchor["y"]

    # 立绘很大，嘴按脸的比例给尺寸（太大会像贴了个黑洞）
    MW = 19.0
    MH_MAX = 15.0

    out = []
    for i in range(9):
        lv = i / 8
        img = scene.copy()
        d = ImageDraw.Draw(img, "RGBA")
        if lv <= 0.03:
            d.line([(cx - MW * 0.22, cy), (cx + MW * 0.22, cy)], fill=(146, 92, 96, 255), width=2)
        else:
            h = max(7.0, 2.5 + MH_MAX * lv)
            w = MW * (0.55 + 0.45 * lv)
            d.ellipse([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2],
                      fill=(104, 44, 52, 255))
            if h >= 10:
                d.ellipse([cx - w / 2 + 2, cy - h / 2 + 3, cx + w / 2 - 2, cy + h / 2 - 1.5],
                          fill=(188, 104, 110, 255))
        arr = np.asarray(img, dtype=np.uint8)
        bgrx = np.empty((H, W, 4), dtype=np.uint8)
        bgrx[:, :, 0] = arr[:, :, 2]
        bgrx[:, :, 1] = arr[:, :, 1]
        bgrx[:, :, 2] = arr[:, :, 0]
        bgrx[:, :, 3] = 255
        out.append(np.ascontiguousarray(bgrx))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    args = ap.parse_args()

    h, created = open_section()
    view = k32.MapViewOfFile(h, FILE_MAP_WRITE, 0, 0, SECTION_SIZE)
    if not view:
        print("MapViewOfFile 失败:", ctypes.get_last_error())
        return 1
    print(f"映射成功，段大小 {SECTION_SIZE/1024/1024:.1f} MB")

    if args.probe:
        print("probe 模式，退出")
        return 0

    variants = render_variants()
    print(f"预渲染 {len(variants)} 档口型 ({W}x{H})")
    threading.Thread(target=udp_listener, daemon=True).start()

    buf = (ctypes.c_char * SECTION_SIZE).from_address(view)
    header = struct.Struct("<I i I I Q")     # magic, seq, w, h, tick

    seq = 2
    frame = variants[0].reshape(-1)
    period = 1.0 / FPS
    print("开始写帧（Ctrl+C 停）")
    try:
        while True:
            t0 = time.time()
            with level_lock:
                lv = level
            idx = min(len(variants) - 1, int(round(lv * (len(variants) - 1))))
            data = variants[idx].reshape(-1).tobytes()

            seq += 1                              # 变奇数：写入中
            ctypes.memmove(ctypes.addressof(buf) + 0,
                           header.pack(MAGIC, seq, W, H, k32.GetTickCount64()), header.size)
            ctypes.memmove(ctypes.addressof(buf) + HEADER_SIZE, data, len(data))
            seq += 1                              # 变偶数：这一帧完整
            ctypes.memmove(ctypes.addressof(buf) + 4,
                           struct.pack("<i", seq), 4)

            dt = time.time() - t0
            if dt < period:
                time.sleep(period - dt)
    except KeyboardInterrupt:
        print("\n停止")
    finally:
        k32.UnmapViewOfFile(view)
    return 0


if __name__ == "__main__":
    sys.exit(main())
