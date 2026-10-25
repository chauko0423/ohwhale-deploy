"""把「她看到的东西」抓成图片：优先 QQ 通话窗口，退全屏。

背景：项目原本只有 ``grab_screen_b64()``（截全屏）。视频通话时，对方的画面
其实已经在 QQ 通话窗口里——直接截那个窗口，她就能"看到"你的表情和动作，
而且不用额外开摄像头、不占用 CameraEmulator。

对外只暴露两件事：

- ``grab_for_reply(text)``：按话里的关键词决定抓几帧，返回 base64 JPEG 列表
- ``find_call_window()``：给调试/测试用，返回 (window, rect) 或 (None, None)

所有失败都退化成"没有图"，绝不让视觉把通话循环带崩。
"""
from __future__ import annotations

import base64
import io
import re
import time

from PIL import Image

# 想看「动作/手势」时多抓一帧，让模型能比较前后差异
MOTION_WORDS = ("动作", "手势", "在干嘛", "在做什么", "姿势", "比划", "挥手", "点头", "摇头")
CALL_WINDOW_KEYWORDS = ("通话",)
DEFAULT_MAX_WIDTH = 1280
DEFAULT_QUALITY = 70
DEFAULT_MOTION_FRAMES = 2


def _env_int(name: str, default: int) -> int:
    try:
        import os
        return int(os.environ.get(name, default))
    except Exception:  # noqa: BLE001
        return default


def find_call_window():
    """返回 (window, (left, top, right, bottom))；找不到返回 (None, None)。"""
    try:
        from pywinauto import Desktop
    except Exception:  # noqa: BLE001
        return None, None
    try:
        windows = Desktop(backend="uia").windows()
    except Exception:  # noqa: BLE001
        return None, None
    for win in windows:
        try:
            title = (win.window_text() or "").strip()
        except Exception:  # noqa: BLE001
            continue
        if not any(key in title for key in CALL_WINDOW_KEYWORDS):
            continue
        try:
            rect = win.rectangle()
            if rect.right - rect.left < 200 or rect.bottom - rect.top < 120:
                continue
            return win, (rect.left, rect.top, rect.right, rect.bottom)
        except Exception:  # noqa: BLE001
            continue
    return None, None


def _shrink(img: Image.Image, max_width: int, quality: int) -> str:
    if img.width > max_width:
        img = img.resize((max_width, max(1, int(img.height * max_width / img.width))),
                         Image.BILINEAR)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode()


def _grab_window(win, rect, max_width: int, quality: int) -> str | None:
    """优先 PrintWindow（窗口被遮挡也能抓），失败退屏幕区域截图。"""
    try:
        img = win.capture_as_image()
        if img is not None and img.width > 0:
            return _shrink(img, max_width, quality)
    except Exception:  # noqa: BLE001
        pass
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab(bbox=rect)
        return _shrink(img, max_width, quality)
    except Exception:  # noqa: BLE001
        return None


def grab_fullscreen_b64(max_width: int = DEFAULT_MAX_WIDTH,
                        quality: int = DEFAULT_QUALITY) -> str | None:
    try:
        from PIL import ImageGrab
        return _shrink(ImageGrab.grab(), max_width, quality)
    except Exception:  # noqa: BLE001
        return None


def _looks_like_motion(text: str) -> bool:
    return any(word in text for word in MOTION_WORDS)


def grab_for_reply(text: str, source: str = "call_window",
                   max_width: int = DEFAULT_MAX_WIDTH,
                   quality: int = DEFAULT_QUALITY,
                   motion_frames: int = DEFAULT_MOTION_FRAMES,
                   interval: float = 0.8) -> list:
    """按配置抓图；返回 base64 列表（可能为空）。

    source: "call_window" 优先截通话窗口；"screen" 直接截全屏。
    通话窗口找不到时自动退全屏，保证「至少能看到点什么」。
    """
    frames = motion_frames if _looks_like_motion(text) else 1
    out = []
    for index in range(max(1, frames)):
        if index:
            time.sleep(interval)
        shot = None
        if source != "screen":
            win, rect = find_call_window()
            if win is not None and rect is not None:
                shot = _grab_window(win, rect, max_width, quality)
        if shot is None:
            shot = grab_fullscreen_b64(max_width, quality)
        if shot is None:
            break
        out.append(shot)
    return out


def describe_source(source: str = "call_window") -> str:
    """给日志用的一句话。"""
    if source == "screen":
        return "全屏"
    win, _rect = find_call_window()
    return "通话窗口" if win is not None else "全屏(未找到通话窗口)"
