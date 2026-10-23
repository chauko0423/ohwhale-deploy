"""实时合成她的画面：房间背景 + 立绘 + 三种小动作 + 口型 → 共享内存

动作（汤姆猫式，只有她会动）：
    · 身子左右摆：整体正弦横移 ±6px，4 秒一个来回
    · 歪头：头部图层绕脖子旋转 ±4°，5 秒一个周期
    · 眨眼：每 3.2~4.6 秒闭眼 110ms
    · 口型：跟随 call_loop 通过 UDP 发来的音量，9 档

性能：头和身子分开贴，头只预旋转 13 个角度（1.5° 一档），
      每帧只做「拷贝底图 + 两次贴图 + 画嘴」，稳定 30fps。
"""

from __future__ import annotations

from ohwhale.config import get as setting, path as config_path, project_path, state_path, log_path, secret, llm, require

import ctypes
import json
import math
import os
import random
import socket
import struct
import sys
import threading
import time
from ctypes import wintypes

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

# ---------- 画面参数 ----------
W, H = 1280, 720
ZOOM = 1.06          # 裁掉边缘一整圈，人物自然变大
CHAR_H = 660         # 立绘高度（原来 720）
COVER = 200          # 她的下半身被桌子挡住多少
DESK_LOOKUP = (450, 700)

# ---------- 共享内存（和 C++ 侧一致）----------
SECTION_NAME = "Global\\CameraEmulatorScreen"
SECTION_NAME_LOCAL = "Local\\CameraEmulatorScreen"
MAGIC = 0x534D4543
HEADER_SIZE = 4096
MAX_W, MAX_H = 3840, 2160
SECTION_SIZE = HEADER_SIZE + MAX_W * MAX_H * 4
FPS = 30
UDP_PORT = int(setting("services.avatar_udp_port"))

LAYERS = os.path.join(config_path("avatar"), "layers")
BASE_BG = config_path("avatar_image")
OUT_BASE = project_path("assets/avatar/scene_base_v2.jpg")
OUT_ANCHOR = project_path("assets/avatar/mouth_anchor_v2.json")

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.OpenFileMappingW.restype = wintypes.HANDLE
k32.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
k32.CreateFileMappingW.restype = wintypes.HANDLE
k32.CreateFileMappingW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                   wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR]
k32.MapViewOfFile.restype = ctypes.c_void_p
k32.MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                              wintypes.DWORD, ctypes.c_size_t]
k32.GetTickCount64.restype = ctypes.c_ulonglong
FILE_MAP_WRITE = 0x0002
PAGE_READWRITE = 0x04

level = 0.0
level_lock = threading.Lock()

# 严格照 PSD 的层序。整条链是：
#   back_hair → neck → footwear → handwear → topwear
#   → earwear → headwear → face → mouth
#   → eyewhite → irides → eyebrow → eyelash → front_hair
#   → 桌子
# 分成 4 组只是为了能一次旋转整颗头，组内顺序仍然是 PSD 顺序；
# 嘴单独画在 face 和 eyewhite 之间，不放在最上层。
BACK_LAYERS = ["back_hair"]
BODY_LAYERS = ["neck", "footwear", "handwear", "topwear"]
HEAD_A = ["earwear", "headwear", "face"]                    # 嘴之前
HEAD_B = ["eyewhite", "irides", "eyebrow", "eyelash", "front_hair"]   # 嘴之后
MOUTH_XY = (535, 520)


def udp_listener():
    global level
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", UDP_PORT))
    s.settimeout(0.2)
    last = time.time()
    while True:
        try:
            data, _ = s.recvfrom(64)
            with level_lock:
                level = max(0.0, min(1.0, float(data.decode("ascii", "ignore").strip() or 0)))
            last = time.time()
        except socket.timeout:
            pass
        except Exception:  # noqa: BLE001
            continue
        if time.time() - last > 0.6:
            with level_lock:
                level *= 0.8


def detect_desk(img: Image.Image) -> int:
    a = np.asarray(img.convert("RGB")).astype(int)
    diff = np.abs(np.diff(a, axis=0)).mean(axis=(1, 2))
    lo, hi = DESK_LOOKUP
    best = max(range(lo, min(hi, len(diff))), key=lambda y: diff[y])
    return int(best)


def zoom_crop(img: Image.Image) -> Image.Image:
    """裁掉边缘一圈再放大回 1280x720，等于整体推近。"""
    w, h = img.size
    cw, ch = int(w / ZOOM), int(h / ZOOM)
    left, top = (w - cw) // 2, (h - ch) // 2
    return img.crop((left, top, left + cw, top + ch)).resize((W, H), Image.LANCZOS)


def alpha_bbox(img: Image.Image, thr: int = 32, border: int = 16):
    """PSD 图层四角常有 1px 杂点，先清掉边框再算包围盒。"""
    a = np.asarray(img)[:, :, 3].copy()
    if border:
        a[:border, :] = 0
        a[-border:, :] = 0
        a[:, :border] = 0
        a[:, -border:] = 0
    ys, xs = np.where(a > thr)
    if len(ys) == 0:
        return (0, 0, img.width, img.height)
    return (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)


def build_sprites():
    """返回（场景底图, 身体精灵, 头部精灵, 锚点）"""
    bg = Image.open(BASE_BG).convert("RGB")
    if bg.size != (W, H):
        bg = bg.resize((W, H), Image.LANCZOS)
    bg = zoom_crop(bg)
    desk_y = detect_desk(bg)

    def compose(names):
        img = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
        for n in names:
            p = os.path.join(LAYERS, f"{n}.png")
            if os.path.exists(p):
                img.alpha_composite(Image.open(p).convert("RGBA"))
        return img

    back = compose(BACK_LAYERS)
    body = compose(BODY_LAYERS)
    head_a = compose(HEAD_A)
    head_b = compose(HEAD_B)

    # 四组共用同一个裁剪框，保证贴回去还对齐
    boxes = [alpha_bbox(x) for x in (back, body, head_a, head_b)]
    bb = (min(b[0] for b in boxes), min(b[1] for b in boxes),
          max(b[2] for b in boxes), max(b[3] for b in boxes))
    scale = CHAR_H / (bb[3] - bb[1])
    tw, th = int((bb[2] - bb[0]) * scale), int((bb[3] - bb[1]) * scale)

    # 闭眼版：只留压扁的睫毛（去掉眼白和瞳孔，像真闭眼）
    blink_img = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    lash_p = os.path.join(LAYERS, "eyelash.png")
    if os.path.exists(lash_p):
        lash = Image.open(lash_p).convert("RGBA")
        lb = alpha_bbox(lash)
        piece = lash.crop(lb)
        piece = piece.resize((piece.width, max(6, int(piece.height * 0.20))), Image.LANCZOS)
        blink_img.alpha_composite(piece, (lb[0], lb[1] + (lb[3] - lb[1]) // 2 - piece.height // 2))
    fh_p = os.path.join(LAYERS, "front_hair.png")
    if os.path.exists(fh_p):
        blink_img.alpha_composite(Image.open(fh_p).convert("RGBA"))

    back_c = back.crop(bb).resize((tw, th), Image.LANCZOS)
    body_c = body.crop(bb).resize((tw, th), Image.LANCZOS)
    head_a_c = head_a.crop(bb).resize((tw, th), Image.LANCZOS)
    head_b_c = head_b.crop(bb).resize((tw, th), Image.LANCZOS)
    blink_c = blink_img.crop(bb).resize((tw, th), Image.LANCZOS)

    x = W // 2 - tw // 2
    y = (desk_y + COVER) - th

    scene = bg.convert("RGBA")          # 干净背景，人物每帧现贴
    desk_overlay = bg.crop((0, desk_y, W, H)).convert("RGBA")

    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(sh)
    d.ellipse([x - 30, desk_y - 8, x + body_c.width + 30, desk_y + 30], fill=(0, 0, 0, 100))
    scene.alpha_composite(sh.filter(ImageFilter.GaussianBlur(14)))

    scene.convert("RGB").save(OUT_BASE, quality=95)

    mx = x + int((MOUTH_XY[0] - bb[0]) * scale)
    my = y + int((MOUTH_XY[1] - bb[1]) * scale)
    # 眼睛在 PSD 画布里的范围（eyewhite 图层量出来的）
    eye_l = (430, 441, 530, 501)
    eye_r = (546, 441, 642, 501)
    def to_screen(b):
        # 以嘴（mx,my）为原点换算，不能用精灵左上角
        return [mx + int((b[0] - MOUTH_XY[0]) * scale), my + int((b[1] - MOUTH_XY[1]) * scale),
                max(2, int((b[2] - b[0]) * scale)), max(2, int((b[3] - b[1]) * scale))]
    # 取脸颊肤色，眨眼时用它盖住眼睛（比写死颜色自然）
    # 取不透明像素的中位色当肤色（单点取样可能落在透明处）
    arr = np.asarray(head_a_c)
    m_ = arr[:, :, 3] > 200
    if m_.any():
        skin = tuple(int(v) for v in np.median(arr[m_][:, :3], axis=0))
    else:
        skin = (250, 226, 214)
    anchor = {"x": mx, "y": my, "desk_y": desk_y,
              "scale": scale,
              "skin": list(skin),
              "eyes": [to_screen(eye_l), to_screen(eye_r)],
              "pos": [x, y, body_c.width, body_c.height],
              "pivot": [x + body_c.width // 2, y + int(body_c.height * 0.60)]}
    json.dump(anchor, open(OUT_ANCHOR, "w"), indent=1)
    print(f"桌沿 y={desk_y}  人物@({x},{y}) {tw}x{th}  嘴=({mx},{my})")
    return scene, back_c, body_c, head_a_c, head_b_c, blink_c, desk_overlay, anchor


def make_tilted_heads(head: Image.Image, pivot, angles):
    """预先把头旋转出若干角度，运行时直接取用（省 CPU）。"""
    out = {}
    pad = 80
    canvas = Image.new("RGBA", (head.width + pad * 2, head.height + pad * 2), (0, 0, 0, 0))
    canvas.alpha_composite(head, (pad, pad))
    px, py = pivot[0] + pad, pivot[1] + pad
    for ang in angles:
        out[ang] = (canvas.rotate(ang, resample=Image.BICUBIC, center=(px, py)), pad)
    return out


def draw_mouth(img: Image.Image, cx: int, cy: int, lv: float):
    """她原本的嘴是一条很淡的小线，所以这里也画得很克制。"""
    d = ImageDraw.Draw(img, "RGBA")
    MW, MH = 11.0, 9.0
    if lv <= 0.03:
        d.line([(cx - 4, cy), (cx + 4, cy)], fill=(150, 96, 100, 235), width=2)
        return
    h = max(4.0, 1.8 + MH * lv)
    w = MW * (0.55 + 0.45 * lv)
    d.ellipse([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], fill=(122, 62, 68, 235))
    if h >= 6:
        d.ellipse([cx - w / 2 + 1.2, cy - h / 2 + 2, cx + w / 2 - 1.2, cy + h / 2 - 1],
                  fill=(196, 128, 132, 235))


def main():
    h, created = 0, False
    h = k32.OpenFileMappingW(FILE_MAP_WRITE, False, SECTION_NAME)
    if not h:
        # 帧服务器还没建，自己建（带同样的 SDDL）
        sddl = "D:(A;;GA;;;SY)(A;;GA;;;LS)(A;;GA;;;BA)(A;;GRGW;;;IU)S:(ML;;NW;;;LW)"
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
        sd = ctypes.c_void_p()
        size = wintypes.ULONG()
        advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(sd), ctypes.byref(size))
        class SA(ctypes.Structure):
            _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                        ("bInheritHandle", wintypes.BOOL)]
        sa = SA(ctypes.sizeof(SA), sd, False)
        h = k32.CreateFileMappingW(wintypes.HANDLE(-1), ctypes.byref(sa), PAGE_READWRITE,
                                   SECTION_SIZE >> 32, SECTION_SIZE & 0xFFFFFFFF, SECTION_NAME)
        created = True
    view = k32.MapViewOfFile(h, FILE_MAP_WRITE, 0, 0, SECTION_SIZE)
    if not view:
        print("映射失败:", ctypes.get_last_error())
        return 1
    print("共享内存", "已创建" if created else "已打开")

    scene, back, body, head_a, head_b, blink_b, desk_overlay, anchor = build_sprites()
    angles = [round(a * 0.5, 1) for a in range(-3, 4)]      # -1.5 ~ +1.5 度（幅度要小，只是微微动）
    backs = make_tilted_heads(back, anchor["pivot"], angles)
    heads_b = make_tilted_heads(head_b, anchor["pivot"], angles)
    blinks = make_tilted_heads(blink_b, anchor["pivot"], angles)
    bx, by, bw, bh = anchor["pos"]
    hx, hy = bx, by
    mx, my = anchor["x"], anchor["y"]

    # 嘴画在「未旋转的头部A」上，再整块旋转 —— 这样嘴永远长在脸上
    pad = 80
    base_canvas = Image.new("RGBA", (head_a.width + pad * 2, head_a.height + pad * 2), (0, 0, 0, 0))
    base_canvas.alpha_composite(head_a, (pad, pad))
    rel_mx, rel_my = mx - bx + pad, my - by + pad
    piv = (anchor["pivot"][0] - bx + pad, anchor["pivot"][1] - by + pad)
    heads_a = {}
    for ang in angles:
        for i in range(9):
            c = base_canvas.copy()
            draw_mouth(c, rel_mx, rel_my, i / 8)
            heads_a[(ang, i)] = c.rotate(ang, resample=Image.BICUBIC, center=piv)
    print(f"预渲染 {len(angles)}×9 = {len(heads_a)} 个头/口型组合")

    threading.Thread(target=udp_listener, daemon=True).start()

    buf = (ctypes.c_char * SECTION_SIZE).from_address(view)
    header = struct.Struct("<I i I I Q")
    seq = 2

    # 眨眼节奏
    next_blink = time.time() + random.uniform(2.0, 3.5)
    blink_until = 0.0

    scene_rgb = scene.convert("RGB")
    print("开始推帧（30fps，带摆动/歪头/眨眼/口型）")
    t_start = time.time()
    frames = 0
    try:
        while True:
            t0 = time.time()
            now = t0 - t_start

            # 摆动：±6px，4 秒周期；歪头：±4.5°，5 秒周期
            # 常态是轻轻律动；每隔七八秒来一次幅度大的摆动
            small = 2.0 * math.sin(now * 2 * math.pi / 4.5)
            env = max(0.0, math.sin(now * 2 * math.pi / 11.0)) ** 3
            big = env * 3.5 * math.sin(now * 2 * math.pi / 2.2)
            sway = int(round(small + big))
            tilt = 1.5 * math.sin(now * 2 * math.pi / 5.0)
            ang = min(angles, key=lambda a: abs(a - tilt))

            # 眨眼
            if now + t_start >= next_blink and now + t_start > blink_until:
                blink_until = t0 + 0.18
                next_blink = t0 + random.uniform(3.2, 4.6)
            blinking = t0 < blink_until

            img = scene_rgb.copy()

            with level_lock:
                lv = level
            lv_i = min(8, max(0, int(round(lv * 8))))

            bk, padb = backs[ang]
            img.paste(bk, (hx - padb + sway, hy - padb), bk)
            img.paste(body, (bx + sway, by), body)

            ha = heads_a[(ang, 0 if blinking else lv_i)]      # 嘴已烤在里面
            img.paste(ha, (hx - pad + sway, hy - pad), ha)

            hb_, padh = blinks[ang] if blinking else heads_b[ang]
            img.paste(hb_, (hx - padh + sway, hy - padh), hb_)

            img.paste(desk_overlay, (0, anchor["desk_y"]), desk_overlay)


            arr = np.asarray(img, dtype=np.uint8)
            bgrx = np.empty((H, W, 4), dtype=np.uint8)
            bgrx[:, :, 0] = arr[:, :, 2]
            bgrx[:, :, 1] = arr[:, :, 1]
            bgrx[:, :, 2] = arr[:, :, 0]
            bgrx[:, :, 3] = 255

            seq += 1
            ctypes.memmove(ctypes.addressof(buf),
                           header.pack(MAGIC, seq, W, H, k32.GetTickCount64()), header.size)
            ctypes.memmove(ctypes.addressof(buf) + HEADER_SIZE, bgrx.tobytes(), W * H * 4)
            seq += 1
            ctypes.memmove(ctypes.addressof(buf) + 4, struct.pack("<i", seq), 4)

            frames += 1
            if frames % 90 == 0:
                print(f"  {frames} 帧  {frames/(time.time()-t_start):.1f} fps")

            dt = time.time() - t0
            if dt < 1.0 / FPS:
                time.sleep(1.0 / FPS - dt)
    except KeyboardInterrupt:
        print("\n停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
