"""把"她"的画面推给虚拟摄像头，让 QQ 视频通话里能看到她

原理：
    OBS 装好后会注册一个 `OBS Virtual Camera` 设备（DirectShow）。
    pyvirtualcam 能直接往这个设备写帧，不需要真的开 OBS 界面。
    QQ 视频通话里把摄像头选成 `OBS Virtual Camera`，看到的就是这里画的东西。

口型同步：
    call_loop 播放她的语音时，每 2048 个采样算一次 RMS，通过 UDP 发到 127.0.0.1:9999。
    本脚本按这个数值控制嘴的张开幅度；不说话时自然合上。

用法：
    python avatar.py                      # 用 avatar/jinger.png 当立绘
    python avatar.py --placeholder        # 没立绘时画一个占位卡（调试用）
    python avatar.py --preview            # 同时弹个小窗预览（方便调位置）
"""

from __future__ import annotations

from ohwhale.config import get as setting, path as config_path, project_path, state_path, log_path, secret, llm, require

import argparse
import os
import socket
import threading
import time

import numpy as np
import pyvirtualcam
from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H, FPS = 640, 480, 30
UDP_PORT = int(setting("services.avatar_udp_port"))
PORTRAIT = config_path("avatar_image")

# 嘴的位置（相对画面宽高的比例）—— 换立绘后要按脸调
MOUTH_X, MOUTH_Y = 0.5, 0.62
MOUTH_W, MOUTH_H_MAX = 52, 34

level = 0.0          # 0~1，当前说话音量
lock = threading.Lock()


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
            with lock:
                level = max(0.0, min(1.0, v))
            last = time.time()
        except socket.timeout:
            pass
        except Exception:  # noqa: BLE001
            continue
        # 1 秒没收到就当她没在说话
        if time.time() - last > 1.0:
            with lock:
                level *= 0.85


def load_font(size: int):
    for p in (r"C:/Windows/Fonts/msyhbd.ttc", r"C:/Windows/Fonts/msyh.ttc"):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def make_placeholder() -> Image.Image:
    """没有立绘时的占位：一张渐变卡 + 名字。"""
    img = Image.new("RGB", (W, H), (18, 24, 40))
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=(int(18 + 30 * t), int(24 + 40 * t), int(48 + 60 * t)))
    d.ellipse([W * 0.30, H * 0.22, W * 0.70, H * 0.78], fill=(38, 52, 86))
    d.text((W * 0.42, H * 0.30), "鲸鲸", font=load_font(46), fill=(200, 226, 255))
    d.text((W * 0.33, H * 0.86), "立绘占位（待替换）", font=load_font(20), fill=(130, 150, 180))
    return img


def load_portrait() -> Image.Image:
    if not os.path.exists(PORTRAIT):
        return make_placeholder()
    p = Image.open(PORTRAIT).convert("RGB")
    # 等比铺满，背景用放大模糊的同一张图（免得留黑边）
    scale = max(W / p.width, H / p.height)
    bg = p.resize((int(p.width * scale), int(p.height * scale)), Image.LANCZOS)
    left = (bg.width - W) // 2
    top = (bg.height - H) // 2
    bg = bg.crop((left, top, left + W, top + H)).filter(ImageFilter.GaussianBlur(12))
    fg = p.resize((int(p.width * (H / p.height)), H), Image.LANCZOS)
    x = (W - fg.width) // 2
    bg.paste(fg, (x, 0))
    return bg


def draw_mouth(img: Image.Image, lv: float) -> Image.Image:
    """按音量画一张嘴（张嘴幅度跟随音量）。"""
    d = ImageDraw.Draw(img, "RGBA")
    cx, cy = W * MOUTH_X, H * MOUTH_Y
    h = 6 + MOUTH_H_MAX * lv
    w = MOUTH_W
    d.ellipse([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2],
              fill=(120, 40, 55, 200), outline=(80, 26, 38, 220), width=2)
    return img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--placeholder", action="store_true")
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--camera", default=None, help="摄像头名，默认自动找 OBS Virtual Camera")
    args = ap.parse_args()

    base = make_placeholder() if args.placeholder else load_portrait()
    threading.Thread(target=udp_listener, daemon=True).start()

    # pyvirtualcam 0.15 没有 list_devices()，直接按名字开；obs 后端默认就叫这个
    cam_name = args.camera or "OBS Virtual Camera"
    print("使用摄像头:", cam_name)

    try:
        with pyvirtualcam.Camera(width=W, height=H, fps=FPS,
                                 fmt=pyvirtualcam.PixelFormat.RGB,
                                 device=cam_name, print_fps=False) as cam:
            print(f"推流中 {W}x{H}@{FPS}  →  QQ 里选「{cam_name}」")
            i = 0
            while True:
                with lock:
                    lv = level
                frame = base.copy()
                if lv > 0.04:
                    frame = draw_mouth(frame, lv)
                arr = np.asarray(frame)
                cam.send(arr)
                cam.sleep_until_next_frame()
                i += 1
    except KeyboardInterrupt:
        print("\n停止")


if __name__ == "__main__":
    main()
