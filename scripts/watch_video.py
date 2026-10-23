"""看 B 站视频：下载 → 抽帧 → 拼图版 → 交模型看（+ 可选出 ASR 用的音轨）。

这条链是视频里那套的"便宜版"：
- 量测/抽帧交给 ffmpeg（本地、零 token）
- 拼图版用 Pillow（本机 ffmpeg 是精简构建，没有 tile 滤镜）
- 最后一步才调模型（deepseek-flash 已实测能读图，封面 1016 prompt tokens）

用法：
    python watch_video.py probe BV1GSnk6aEUz              # 只下载+抽帧+出图版，不看
    python watch_video.py look  BV1GSnk6aEUz [每版帧数]    # 出图版并让模型描述
    python watch_video.py audio BV1GSnk6aEUz              # 下音轨转 16k mono wav（ASR 用）
"""

from __future__ import annotations

from ohwhale.config import get as setting, path as config_path, project_path, state_path, log_path, secret, llm, require

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = config_path("video_cache")
FFMPEG = config_path("ffmpeg")
FFPROBE = config_path("ffprobe")
COOKIE_FILE = state_path("bilibili/bili_cookie.json")
ASTRBOT_CFG = config_path("astrbot_config")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]
_MIX = {"k": "", "exp": 0.0}


def headers() -> dict:
    try:
        c = json.load(open(COOKIE_FILE, encoding="utf-8"))
    except OSError:
        c = {}
    return {"User-Agent": UA, "Referer": "https://www.bilibili.com/",
            "Cookie": "; ".join(f"{k}={c[k]}" for k in
                                ("SESSDATA", "bili_jct", "DedeUserID") if c.get(k))}


def get_json(url: str) -> dict:
    with OP.open(urllib.request.Request(url, headers=headers()), timeout=25) as r:
        return json.loads(r.read().decode())


def mixin() -> str:
    if _MIX["k"] and time.time() < _MIX["exp"]:
        return _MIX["k"]
    nav = get_json("https://api.bilibili.com/x/web-interface/nav")
    wbi = (nav.get("data") or {}).get("wbi_img") or {}
    raw = (wbi["img_url"].rsplit("/", 1)[-1].split(".")[0]
           + wbi["sub_url"].rsplit("/", 1)[-1].split(".")[0])
    _MIX["k"] = "".join(raw[i] for i in MIXIN_TAB)[:32]
    _MIX["exp"] = time.time() + 3600
    return _MIX["k"]


def signed(params: dict) -> str:
    p = dict(params)
    p["wts"] = int(time.time())
    q = urllib.parse.urlencode(sorted(p.items()))
    p["w_rid"] = hashlib.md5((q + mixin()).encode()).hexdigest()
    return urllib.parse.urlencode(p)


def detail(bvid: str) -> dict:
    d = get_json("https://api.bilibili.com/x/web-interface/view?"
                 + urllib.parse.urlencode({"bvid": bvid}))
    if d.get("code") != 0:
        raise SystemExit(f"detail 失败 {d.get('code')} {d.get('message')}")
    return d["data"]


def playurl(bvid: str, cid: int) -> dict:
    d = get_json("https://api.bilibili.com/x/player/wbi/playurl?"
                 + signed({"bvid": bvid, "cid": cid, "fnval": 16, "qn": 64,
                           "fourk": 0}))
    if d.get("code") != 0:
        raise SystemExit(f"playurl 失败 {d.get('code')} {d.get('message')}")
    return d["data"]


def download(url: str, path: str) -> int:
    req = urllib.request.Request(url, headers=headers())
    n = 0
    with OP.open(req, timeout=120) as r, open(path, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            n += len(chunk)
    return n


def pick_video(dash: dict) -> dict:
    """挑一个能看清又不大的：优先 720P 档（id 64），画质再高对"看懂在讲什么"没帮助。"""
    vids = dash.get("video") or []
    if not vids:
        raise SystemExit("没有视频流")
    vids = sorted(vids, key=lambda v: v.get("bandwidth") or 0)
    for q in (64, 32, 80):
        cand = [v for v in vids if v.get("id") == q]
        if cand:
            return cand[0]
    return vids[0]


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        raise SystemExit("命令失败：" + " ".join(cmd) + "\n"
                         + p.stderr.decode("utf-8", "replace")[-800:])
    return p


def ensure_work(bvid: str) -> dict:
    d = detail(bvid)
    dur = int(d.get("duration") or 0)
    cid = d.get("cid")
    out = os.path.join(WORK, bvid)
    os.makedirs(out, exist_ok=True)
    vpath = os.path.join(out, "video.mp4")
    if not (os.path.exists(vpath) and os.path.getsize(vpath) > 100_000):
        pu = playurl(bvid, cid)
        v = pick_video(pu.get("dash") or {})
        n = download(v["baseUrl"], vpath)
        print(f"[下载] {v.get('width')}x{v.get('height')} "
              f"{v.get('codecs')} → {n/1e6:.1f} MB")
    return {"dir": out, "dur": dur, "detail": d, "video": vpath}


def extract_frames(vpath: str, outdir: str, dur: int, count: int) -> list[str]:
    os.makedirs(outdir, exist_ok=True)
    for f in os.listdir(outdir):
        if f.endswith(".png"):
            os.remove(os.path.join(outdir, f))
    fps = count / max(dur, 1)
    run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-i", vpath, "-vf", f"fps={fps:.6f},scale=336:-1",
         "-frames:v", str(count), os.path.join(outdir, "f_%02d.png")])
    files = sorted(os.path.join(outdir, f) for f in os.listdir(outdir)
                   if f.endswith(".png"))
    return files


def drop_frames(files: list[str]) -> None:
    """拼完图版就把 PNG 删了：一条视频 16 张 336px 的图约 3 MB，留着没用。"""
    for f in files:
        try:
            os.remove(f)
        except OSError:
            pass


def prune(keep_dir: str, max_mb: int = 1500) -> None:
    """缓存不能无限长：先删 48 小时前的 video.mp4，再按总量删最旧的整目录。

    一条 720P 视频 3~30 MB，逛得勤的话一天能攒上百 MB，必须自己收。
    """
    now = time.time()
    for root, _dirs, files in os.walk(WORK):
        for f in files:
            if f == "video.mp4":
                p = os.path.join(root, f)
                try:
                    if now - os.path.getmtime(p) > 48 * 3600:
                        os.remove(p)
                except OSError:
                    pass
    total = 0
    dirs = []
    for d in os.listdir(WORK):
        p = os.path.join(WORK, d)
        if not os.path.isdir(p):
            continue
        size = sum(os.path.getsize(os.path.join(r, f))
                   for r, _ds, fs in os.walk(p) for f in fs)
        dirs.append((os.path.getmtime(p), size, p))
        total += size
    for _mt, size, p in sorted(dirs):
        if total <= max_mb * 1024 * 1024:
            break
        if os.path.abspath(p) == os.path.abspath(keep_dir):
            continue
        shutil.rmtree(p, ignore_errors=True)
        total -= size
        print(f"[缓存] 清理 {os.path.basename(p)}（{size/1e6:.1f} MB）")


def tile(files: list[str], cols: int, cell: int, sheet_path: str) -> str:
    from PIL import Image
    rows = (len(files) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell, rows * cell), (18, 18, 18))
    for i, f in enumerate(files):
        im = Image.open(f).convert("RGB")
        im.thumbnail((cell, cell))
        x = (i % cols) * cell + (cell - im.width) // 2
        y = (i // cols) * cell + (cell - im.height) // 2
        sheet.paste(im, (x, y))
    sheet.save(sheet_path, quality=88)
    return sheet_path


def llm_key() -> tuple[str, str]:
    key, base, _ = llm("vision")
    return key, base


def ask_image(sheet_path: str, prompt: str, model: str = setting("llm.vision.model"),
              tries: int = 2) -> tuple[str, dict]:
    """要图；content 为空就放大预算重试一次。

    deepseek-flash 是思考型：图密（代码/终端截图）时 reasoning 能吃掉两三千 token，
    content 直接返回空。第一次踩这个坑时"调用成功"但什么都没读到。
    """
    key, base = llm_key()
    b64 = base64.b64encode(open(sheet_path, "rb").read()).decode()
    last_usage: dict = {}
    for i, cap in enumerate((4000, 8000)[:tries]):
        body = {"model": model, "max_tokens": cap, "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]}]}
        req = urllib.request.Request(
            base + "/chat/completions", data=json.dumps(body).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"})
        with OP.open(req, timeout=300) as r:
            out = json.loads(r.read().decode())
        ch = out["choices"][0]
        usage = out.get("usage") or {}
        usage["finish_reason"] = ch.get("finish_reason")
        last_usage = usage
        text = ch["message"].get("content") or ""
        if text.strip():
            return text, usage
    return "", last_usage


def cmd_full(bvid: str, per: int = 4) -> int:
    """看图 + 听声音，合成一份报告。"""
    ctx = ensure_work(bvid)
    d = ctx["detail"]
    dur = ctx["dur"]
    print(f"[视频] {d.get('title')} | {dur}s | {(d.get('owner') or {}).get('name')}")

    files = extract_frames(ctx["video"], os.path.join(ctx["dir"], "frames"),
                           dur, per * 2)
    sheets = []
    for i in range(0, len(files), per):
        p = os.path.join(ctx["dir"], f"sheet{i // per + 1}.jpg")
        sheets.append(tile(files[i:i + per], 4, 252, p))
    drop_frames(files)
    step = dur / max(len(files), 1)
    vision = []
    for idx, sp in enumerate(sheets):
        t0, t1 = idx * per * step, min(dur, (idx + 1) * per * step)
        prompt = (f"这是 {dur} 秒视频按时间顺序抽的 {per} 帧（左上→右下，间隔约 "
                  f"{step:.0f}s，覆盖 {t0:.0f}–{t1:.0f}s）。只讲画面看得到的："
                  "每格的关键信息（界面/图表/字幕原样摘）+ 这一段在讲什么。看不到就说看不到。")
        txt, usage = ask_image(sp, prompt)
        vision.append(f"[画面 {t0:.0f}s–{t1:.0f}s · {usage.get('total_tokens')} tok]\n{txt}")

    # 听：走独立进程，避免把 faster-whisper 塞进这个脚本的主流程
    audio_txt = ""
    try:
        pu = playurl(bvid, d["cid"])
        auds = ((pu.get("dash") or {}).get("audio")) or []
        if auds:
            a = sorted(auds, key=lambda x: x.get("bandwidth") or 0)[-1]
            apath = os.path.join(ctx["dir"], "audio.m4a")
            if not os.path.exists(apath):
                download(a["baseUrl"], apath)
            wav = os.path.join(ctx["dir"], "audio16k.wav")
            if not os.path.exists(wav):
                run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                     "-i", apath, "-ac", "1", "-ar", "16000",
                     "-c:a", "pcm_s16le", wav])
            env = dict(os.environ)
            env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
            env.setdefault("HF_HUB_DISABLE_XET", "1")
            code = (
                "import sys,json\n"
                "from faster_whisper import WhisperModel\n"
                "m=WhisperModel('small',device='cpu',compute_type='int8')\n"
                "segs,_=m.transcribe(sys.argv[1],language='zh',vad_filter=True,beam_size=1)\n"
                "print(json.dumps([[round(s.start,1),round(s.end,1),s.text.strip()] "
                "for s in segs],ensure_ascii=False))\n"
            )
            r = subprocess.run([sys.executable, "-c", code, wav],
                               capture_output=True, env=env, timeout=900)
            if r.returncode == 0:
                segs = json.loads(r.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
                audio_txt = "\n".join(f"[{a0:6.1f}→{a1:6.1f}] {t}" for a0, a1, t in segs)
            else:
                audio_txt = "（ASR 失败：" + r.stderr.decode("utf-8", "replace")[-200:] + "）"
    except Exception as e:  # noqa: BLE001
        audio_txt = f"（音频这一段没走通：{e}）"

    print("\n=== 画面 ===")
    print("\n\n".join(vision))
    print("\n=== 声音（whisper small，CPU）===")
    print(audio_txt[:4000])
    print("\n=== 提醒 ===")
    print("ASR 有同音错字；画面里能看到的字以画面为准。两边打架就标「不确定」。")
    out = os.path.join(ctx["dir"], "report.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n\n".join(vision) + "\n\n=== 声音 ===\n" + audio_txt)
    print(f"\n[报告] {out}")
    return 0


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    cmd, bvid = sys.argv[1], sys.argv[2]
    prune(os.path.join(WORK, bvid))
    ctx = ensure_work(bvid)
    d = ctx["detail"]
    print(f"[视频] {d.get('title')} | {ctx['dur']}s | "
          f"{(d.get('owner') or {}).get('name')}")

    if cmd == "probe":
        files = extract_frames(ctx["video"], os.path.join(ctx["dir"], "frames"),
                              ctx["dur"], 16)
        s1 = tile(files[:8], 4, 252, os.path.join(ctx["dir"], "sheet1.jpg"))
        s2 = tile(files[8:], 4, 252, os.path.join(ctx["dir"], "sheet2.jpg"))
        drop_frames(files)
        print(f"[抽帧] {len(files)} 帧 → {s1} / {s2}")
        return 0

    if cmd == "look":
        per = int(sys.argv[3]) if len(sys.argv) > 3 else 8
        files = extract_frames(ctx["video"], os.path.join(ctx["dir"], "frames"),
                              ctx["dur"], per * 2)
        sheets = []
        for i in range(0, len(files), per):
            p = os.path.join(ctx["dir"], f"sheet{i // per + 1}.jpg")
            sheets.append(tile(files[i:i + per], 4, 252, p))
        drop_frames(files)
        dur = ctx["dur"]
        step = dur / max(len(files), 1)
        for idx, sp in enumerate(sheets):
            t0 = idx * per * step
            t1 = min(dur, (idx + 1) * per * step)
            prompt = (
                f"这是一条 {dur} 秒视频按时间顺序抽的 {per} 帧拼图（左上→右下，"
                f"每格间隔约 {step:.0f} 秒，本张覆盖 {t0:.0f}s–{t1:.0f}s）。"
                "请只根据画面说话：1) 这是什么类型的视频（录屏/实拍/动画/纯文字）；"
                "2) 每一格看到的关键信息（界面、图表、字幕文字原样摘）；"
                "3) 这一段在讲什么。看不到的地方就说看不到，不要猜。"
            )
            text, usage = ask_image(sp, prompt)
            print(f"\n=== 图版 {idx + 1}（{t0:.0f}s–{t1:.0f}s）usage={usage.get('total_tokens')} ===")
            print(text.strip() or "（这一版没读出内容：模型把 token 全用在思考上了）")
        return 0

    if cmd == "full":
        per = int(sys.argv[3]) if len(sys.argv) > 3 else 4
        return cmd_full(bvid, per)

    if cmd == "audio":
        pu = playurl(bvid, d["cid"])
        auds = ((pu.get("dash") or {}).get("audio")) or []
        if not auds:
            raise SystemExit("没有音频流")
        a = sorted(auds, key=lambda x: x.get("bandwidth") or 0)[-1]
        apath = os.path.join(ctx["dir"], "audio.m4a")
        download(a["baseUrl"], apath)
        wav = os.path.join(ctx["dir"], "audio16k.wav")
        run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", apath,
             "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav])
        print(f"[音频] {os.path.getsize(apath)/1e6:.1f} MB → {wav} "
              f"({os.path.getsize(wav)/1e6:.1f} MB, 16k mono)")
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
