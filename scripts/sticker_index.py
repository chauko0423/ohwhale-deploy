"""给表情包建索引：拼图版 → 模型读字 + 判情绪 → JSONL。

用法：
    python sticker_index.py sheet <起始> [张数]     # 只拼版，不调模型（看效果）
    python sticker_index.py run  [起始] [张数]     # 正式跑，写 stickers_raw.jsonl
    python sticker_index.py one  <路径>            # 单张试识别
"""

from __future__ import annotations

from ohwhale.config import get as setting, path as config_path, project_path, state_path, log_path, secret, llm, require

import base64
import hashlib
import json
import os
import sys
import time
from PIL import Image

SRC_DIRS = [config_path("sticker_source")]
WORK = config_path("sticker_work")
SHEET_DIR = os.path.join(WORK, "sheets")
RAW = os.path.join(WORK, "stickers_raw.jsonl")
ASTRBOT_CFG = config_path("astrbot_config")
CELL = 420
COLS = 3

EMOTIONS = ("开心", "生气", "委屈", "无语", "害羞", "困累", "饿想吃",
            "撒娇", "惊讶", "嫌弃", "安慰", "得意")


def list_images() -> list[str]:
    out = []
    for d in SRC_DIRS:
        for f in sorted(os.listdir(d)):
            if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                out.append(os.path.join(d, f))
    return out


def make_sheet(paths: list[str], out: str) -> None:
    rows = (len(paths) + COLS - 1) // COLS
    sheet = Image.new("RGB", (COLS * CELL, rows * CELL), (30, 30, 30))
    for i, p in enumerate(paths):
        im = Image.open(p).convert("RGB")
        im.thumbnail((CELL - 8, CELL - 8))
        x = (i % COLS) * CELL + (CELL - im.width) // 2
        y = (i // COLS) * CELL + (CELL - im.height) // 2
        sheet.paste(im, (x, y))
    sheet.save(out, quality=90)


def llm_key() -> tuple[str, str]:
    key, base, _ = llm("vision")
    return key, base


def ask(sheet_path: str, n: int, prompt: str, cap: int = 4000) -> str:
    """要模型看图。空 content 就放大预算重试——思考型模型会把 token 全烧在 reasoning 上。"""
    import urllib.request
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    key, base = llm_key()
    b64 = base64.b64encode(open(sheet_path, "rb").read()).decode()
    last = ""
    for c in (cap, cap * 2, cap * 4):
        body = {"model": setting("llm.vision.model"), "max_tokens": c, "messages": [{
            "role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}]}
        req = urllib.request.Request(base + "/chat/completions",
                                     data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {key}",
                                              "Content-Type": "application/json"})
        with op.open(req, timeout=300) as r:
            out = json.loads(r.read().decode())
        txt = out["choices"][0]["message"].get("content") or ""
        if txt.strip():
            return txt
        last = txt
    return last


PROMPT = """这是一张 {n} 格的表情包拼图（3 列，左上→右下编号 1..{n}）。
逐格判断，只输出一个 JSON 数组，不要任何解释、不要 markdown 代码块。

每格输出：
{{"i": 序号, "text": "图上出现的文字（原样抄；没有文字就写空字符串）",
  "emotion": "从这些里选一个最贴的：{emos}；判断不了就写 无",
  "usable": true/false}}

usable 的判据：**这张图能不能当作"她自己的表情反应"发出去**。
- 图上文字是她能说的话（如"真当我是便宜货啊""困死了"）→ true
- 只是信息图/账单/海报/纯装饰/看不清 → false
- 有攻击性、涉及真人身份、或明显是别人的水印/宣传 → false"""


def parse_json_array(txt: str, n: int) -> list[dict]:
    s = txt.strip()
    if s.startswith("```"):
        s = s.split("```")[1] if "```" in s[3:] else s.strip("`")
        s = s.lstrip("json").strip()
    a, b = s.find("["), s.rfind("]")
    if a < 0 or b < 0:
        raise ValueError("没找到 JSON 数组")
    return json.loads(s[a:b + 1])


def cmd_one(path: str) -> None:
    tmp = os.path.join(WORK, "one.jpg")
    make_sheet([path], tmp)
    print(ask(tmp, 1, PROMPT.format(n=1, emos="、".join(EMOTIONS))))


def cmd_sheet(start: int, count: int) -> None:
    imgs = list_images()
    batch = imgs[start:start + count]
    os.makedirs(SHEET_DIR, exist_ok=True)
    out = os.path.join(SHEET_DIR, f"sheet_{start:04d}.jpg")
    make_sheet(batch, out)
    print(f"{len(batch)} 张 → {out}  ({os.path.getsize(out)/1e3:.0f} KB)")


def cmd_run(start: int = 0, count: int = 10**9, per: int = 9,
            step: int = 1, workers: int = 6) -> None:
    imgs = list_images()[start:start + count][::step]
    os.makedirs(SHEET_DIR, exist_ok=True)
    done = set()
    if os.path.exists(RAW):
        for line in open(RAW, encoding="utf-8"):
            try:
                done.add(json.loads(line)["path"])
            except Exception:
                pass
    import threading
    from concurrent.futures import ThreadPoolExecutor
    t0 = time.time()
    lock = threading.Lock()
    stat = {"ok": 0, "fail": 0, "rows": 0}
    todo = []
    for i in range(0, len(imgs), per):
        batch = imgs[i:i + per]
        if all(p in done for p in batch):
            continue
        todo.append((i, batch))
    print(f"待处理 {len(todo)} 版 / {workers} 并发", flush=True)

    def work(item):
        i, batch = item
        sp = os.path.join(SHEET_DIR, f"b{start + i:04d}.jpg")
        make_sheet(batch, sp)
        try:
            txt = ask(sp, len(batch),
                      PROMPT.format(n=len(batch), emos="、".join(EMOTIONS)),
                      cap=8000)
            rows = parse_json_array(txt, len(batch))
        except Exception as e:  # noqa: BLE001
            with lock:
                stat["fail"] += 1
                print(f"[skip] {start+i}: {e}", flush=True)
            return
        out = []
        for j, p in enumerate(batch):
            r = next((x for x in rows if int(x.get("i", 0)) == j + 1), {})
            out.append({"path": p,
                        "text": (r.get("text") or "").strip(),
                        "emotion": (r.get("emotion") or "无").strip(),
                        "usable": bool(r.get("usable"))})
        with lock:
            with open(RAW, "a", encoding="utf-8") as f:
                for o in out:
                    f.write(json.dumps(o, ensure_ascii=False) + "\n")
            stat["ok"] += 1
            stat["rows"] += len(out)
            el = time.time() - t0
            print(f"版 {stat['ok']}/{len(todo)} 行 {stat['rows']} 失败 {stat['fail']} "
                  f"{el:.0f}s", flush=True)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, todo))
    print(f"完成：{stat}", flush=True)



LIB = config_path("stickers")
PLUGIN_INDEX = config_path("sticker_index")


def cmd_build(max_side: int = 480, quality: int = 85) -> int:
    """把 stickers_raw.jsonl 变成能用的表情库：转码落盘 + 生成 index.json。"""
    import hashlib
    from PIL import Image as _I
    rows = []
    if not os.path.exists(RAW):
        print("没有 raw 数据，先跑 run")
        return 1
    for line in open(RAW, encoding="utf-8"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if not r.get("usable"):
            continue
        emo = (r.get("emotion") or "无").strip()
        if emo not in EMOTIONS:
            continue
        rows.append(r)
    # 安全过滤：露骨/攻击性/超长（信息图不像表情反应）
    BAD = ("性关系", "强奸", "约炮", "裸", "色情", "做爱", "睡我", "操你",
           "傻逼", "死全家", "去死", "杀了你")
    kept = []
    for r in rows:
        t = (r.get("text") or "")
        if any(w in t for w in BAD):
            continue
        if len(t) > 80:
            continue
        kept.append(r)
    print(f"安全过滤：{len(rows)} → {len(kept)}")
    rows = kept

    # 去重（同一张图可能出现在两个包里）
    seen = set()
    uniq = []
    for r in rows:
        try:
            h = hashlib.sha1(open(r["path"], "rb").read()).hexdigest()[:16]
        except OSError:
            continue
        if h in seen:
            continue
        seen.add(h)
        r["hash"] = h
        uniq.append(r)

    moods: dict[str, list] = {}
    made = 0
    for r in uniq:
        d = os.path.join(LIB, r["emotion"])
        os.makedirs(d, exist_ok=True)
        dst = os.path.join(d, r["hash"] + ".jpg")
        if not os.path.exists(dst):
            try:
                im = _I.open(r["path"]).convert("RGB")
                im.thumbnail((max_side, max_side))
                im.save(dst, "JPEG", quality=quality)
            except Exception as e:  # noqa: BLE001
                print("转码失败", r["path"], e)
                continue
        moods.setdefault(r["emotion"], []).append(
            {"path": os.path.relpath(dst, LIB).replace("\\", "/"), "text": r.get("text") or ""})
        made += 1

    idx = {"moods": moods, "total": made}
    os.makedirs(os.path.dirname(PLUGIN_INDEX), exist_ok=True)
    with open(PLUGIN_INDEX, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=1)
    print(f"入库 {made} 张 → {LIB}")
    for k, v in sorted(moods.items(), key=lambda kv: -len(kv[1])):
        print(f"  {k:<6} {len(v)}")
    print("索引:", PLUGIN_INDEX)
    return 0


def main() -> int:
    os.makedirs(WORK, exist_ok=True)
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    c = sys.argv[1]
    if c == "sheet":
        cmd_sheet(int(sys.argv[2]) if len(sys.argv) > 2 else 0,
                  int(sys.argv[3]) if len(sys.argv) > 3 else 9)
    elif c == "one":
        cmd_one(sys.argv[2])
    elif c == "build":
        return cmd_build()
    elif c == "run":
        cmd_run(int(sys.argv[2]) if len(sys.argv) > 2 else 0,
                int(sys.argv[3]) if len(sys.argv) > 3 else 10**9,
                step=int(sys.argv[4]) if len(sys.argv) > 4 else 1,
                workers=int(sys.argv[5]) if len(sys.argv) > 5 else 6)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
