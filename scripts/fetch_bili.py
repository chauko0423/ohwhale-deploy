"""抓 B 站视频的元数据 / 字幕 / 评论 / UP主空间，只用标准库。

复用 astrbot_plugin_bili_scroll 的 cookie（data/bilibili/bili_cookie.json）。

用法：
    python fetch_bili.py info BV1GSnk6aEUz
    python fetch_bili.py sub  BV1GSnk6aEUz [BV...]
    python fetch_bili.py cmt  BV1GSnk6aEUz [n]
"""

from __future__ import annotations

from ohwhale.config import get as setting, path as config_path, project_path, state_path, log_path, secret, llm, require

import hashlib
import json
import os
import sys
import time
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
HDR = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}
COOKIE_FILE = state_path("bilibili/bili_cookie.json")
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

_MIXIN = {"key": "", "exp": 0.0}


def cookie_header() -> dict:
    try:
        with open(COOKIE_FILE, encoding="utf-8") as f:
            c = json.load(f)
    except Exception:
        return {}
    if not c.get("SESSDATA"):
        return {}
    parts = [f"{k}={c[k]}" for k in ("SESSDATA", "bili_jct", "DedeUserID") if c.get(k)]
    return {"Cookie": "; ".join(parts)}


def get(url: str, headers: dict | None = None) -> dict:
    h = dict(HDR)
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with OPENER.open(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def mixin() -> str:
    if _MIXIN["key"] and time.time() < _MIXIN["exp"]:
        return _MIXIN["key"]
    nav = get("https://api.bilibili.com/x/web-interface/nav")
    wbi = ((nav.get("data") or {}).get("wbi_img")) or {}
    img = (wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
    sub = (wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
    raw = img + sub
    _MIXIN["key"] = "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]
    _MIXIN["exp"] = time.time() + 3600
    return _MIXIN["key"]


def signed(params: dict) -> str:
    p = {k: v for k, v in params.items() if v not in (None, "")}
    p["wts"] = int(time.time())
    q = urllib.parse.urlencode(sorted(p.items()))
    p["w_rid"] = hashlib.md5((q + mixin()).encode()).hexdigest()
    return urllib.parse.urlencode(p)


def detail(bvid: str) -> dict:
    d = get("https://api.bilibili.com/x/web-interface/view?"
            + urllib.parse.urlencode({"bvid": bvid}))
    if d.get("code") != 0:
        raise SystemExit(f"detail {bvid} code={d.get('code')} {d.get('message')}")
    return d["data"]


def cmd_info(bvid: str) -> None:
    d = detail(bvid)
    st = d.get("stat") or {}
    print(f"BV   : {d.get('bvid')}  aid={d.get('aid')} cid={d.get('cid')}")
    print(f"标题 : {d.get('title')}")
    print(f"UP   : {(d.get('owner') or {}).get('name')} (mid={(d.get('owner') or {}).get('mid')})")
    print(f"分区 : {d.get('tname')} | 发布 {time.strftime('%Y-%m-%d %H:%M', time.localtime(d.get('pubdate', 0)))}"
          f" | 时长 {d.get('duration')}s | 分P {len(d.get('pages') or [])}")
    print(f"数据 : 播放 {st.get('view')} 点赞 {st.get('like')} 收藏 {st.get('favorite')} "
          f"投币 {st.get('coin')} 弹幕 {st.get('danmaku')} 评论 {st.get('reply')}")
    print(f"链接 : https://www.bilibili.com/video/{bvid}")
    print("简介 :")
    print(d.get("desc") or "（无）")


def cmd_sub(*bvids: str) -> None:
    ck = cookie_header()
    print("cookie:", "有 SESSDATA" if ck else "无（字幕多半读不到）")
    for bvid in bvids:
        d = detail(bvid)
        print("=" * 70)
        print(f"{bvid} {d.get('title')}  (cid={d.get('cid')})")
        p = get("https://api.bilibili.com/x/player/wbi/v2?"
                + signed({"bvid": bvid, "cid": d.get("cid")}), ck)
        if p.get("code") != 0:
            print("  player/wbi/v2 code=", p.get("code"), p.get("message"))
            continue
        subs = (((p.get("data") or {}).get("subtitle") or {}).get("subtitles")) or []
        print("  字幕轨:", [(s.get("lan"), s.get("lan_doc"),
                           bool(s.get("subtitle_url"))) for s in subs])
        subs = [s for s in subs if (s.get("subtitle_url") or "").strip()]
        if not subs:
            print("  → 没有可用字幕")
            continue
        subs.sort(key=lambda s: 0 if "zh" in (s.get("lan") or "") else 1)
        for s in subs[:2]:
            url = s["subtitle_url"]
            if url.startswith("//"):
                url = "https:" + url
            body = get(url).get("body") or []
            lines = [b.get("content", "") for b in body if b.get("content")]
            print(f"  ── 轨 {s.get('lan')} ({s.get('lan_doc')})，{len(lines)} 条，{sum(map(len, lines))} 字")
            print("\n".join(lines))


def cmd_cmt(bvid: str, n: int = 10) -> None:
    d = detail(bvid)
    qs = urllib.parse.urlencode({"type": 1, "oid": d.get("aid"), "sort": 1,
                                 "ps": max(n, 5), "pn": 1})
    r = get("https://api.bilibili.com/x/v2/reply?" + qs)
    if r.get("code") != 0:
        raise SystemExit(f"reply code={r.get('code')} {r.get('message')}")
    rows = ((r.get("data") or {}).get("replies") or [])[:n]
    print(f"{bvid} 热门评论 {len(rows)} 条：")
    for x in rows:
        m = x.get("member") or {}
        print(f"- {m.get('uname')}（赞{x.get('like', 0)}）："
              f"{(x.get('content') or {}).get('message', '').replace(chr(10), ' ')[:300]}")



def cmd_cmt_wbi(bvid: str, n: int = 20) -> None:
    """用 wbi/main 查评论（含"折叠/新号"的评论）。

    为什么要这个：旧的 /x/v2/reply 会漏掉一部分评论——2026-10-23 就因此误判
    「她的评论没显示」，实际在页面上是能看到的。
    """
    d = detail(bvid)
    qs = signed({"oid": d["aid"], "type": 1, "mode": 2, "plat": 1,
                 "web_location": 1315875})
    r = get("https://api.bilibili.com/x/v2/reply/wbi/main?" + qs)
    if r.get("code") != 0:
        raise SystemExit(f"wbi/main code={r.get('code')} {r.get('message')}")
    reps = ((r.get("data") or {}).get("replies")) or []
    total = ((r.get("data") or {}).get("cursor") or {}).get("all_count")
    print(f"{bvid} 评论 {len(reps)} 条（总数 {total}）")
    for rep in reps[:n]:
        c = rep.get("content") or {}
        at = c.get("at_name_to_mid")
        mark = f"  @{list(at.keys())}" if at else ""
        print(f"  {time.strftime('%m-%d %H:%M', time.localtime(rep.get('ctime') or 0))} "
              f"{(rep.get('member') or {}).get('uname', '?')}："
              f"{str(c.get('message', ''))[:60]}{mark}")


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    cmd, rest = sys.argv[1], sys.argv[2:]
    if cmd == "info":
        for b in rest:
            cmd_info(b)
            print()
    elif cmd == "sub":
        cmd_sub(*rest)
    elif cmd == "cmtw":
        cmd_cmt_wbi(rest[0], int(rest[1]) if len(rest) > 1 else 20)
    elif cmd == "cmt":
        cmd_cmt(rest[0], int(rest[1]) if len(rest) > 1 else 10)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
