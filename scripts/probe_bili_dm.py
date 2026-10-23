"""B站 私信 / 评论@ 能力探测（只做实测，不改插件）。

用法：
    python probe_bili_dm.py send <mid> "内容"     # 真发一条私信
    python probe_bili_dm.py read <mid> [条数]      # 读与某人的最近私信
    python probe_bili_dm.py at-test <bvid> "内容"  # 在视频下发一条 @示例用户 的评论
"""

from __future__ import annotations

from ohwhale.config import get as setting, path as config_path, project_path, state_path, log_path, secret, llm, require

import json
import os
import sys
import time
import urllib.parse
import urllib.request
import uuid

COOKIE_FILE = state_path("bilibili/bili_cookie.json")
DEV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bili_dev_id.txt")
OWN_MID = 200002          # 小满的 B 站 uid
MASTER_MID = 200001              # 示例用户
MASTER_NAME = "示例用户"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def cookie() -> dict:
    with open(COOKIE_FILE, encoding="utf-8") as f:
        return json.load(f)


def headers() -> dict:
    c = cookie()
    return {
        "User-Agent": UA,
        "Referer": "https://message.bilibili.com/",
        "Cookie": "; ".join(f"{k}={c[k]}" for k in
                            ("SESSDATA", "bili_jct", "DedeUserID") if c.get(k)),
    }


def dev_id() -> str:
    """B站 私信要求 msg[dev_id]。固定一个，不每次变——变来变去像异常设备。"""
    if os.path.exists(DEV_FILE):
        return open(DEV_FILE, encoding="utf-8").read().strip()
    d = str(uuid.uuid4()).upper()
    with open(DEV_FILE, "w", encoding="utf-8") as f:
        f.write(d)
    return d


def post(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    h = headers()
    h["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    with OPENER.open(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def get(url: str) -> dict:
    req = urllib.request.Request(url, headers=headers())
    with OPENER.open(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def send_dm(mid: int, text: str) -> dict:
    c = cookie()
    now = int(time.time())
    data = {
        "msg[sender_uid]": str(OWN_MID),
        "msg[receiver_id]": str(mid),
        "msg[receiver_type]": "1",
        "msg[msg_type]": "1",
        "msg[msg_status]": "0",
        "msg[dev_id]": dev_id(),
        "msg[timestamp]": str(now),
        "msg[content]": json.dumps({"content": text}, ensure_ascii=False),
        "csrf": c.get("bili_jct", ""),
        "csrf_token": c.get("bili_jct", ""),
    }
    return post("https://api.vc.bilibili.com/web_im/v1/web_im/send_msg", data)


def read_dm(mid: int, n: int = 10) -> dict:
    qs = urllib.parse.urlencode({
        "talker_id": mid, "session_type": 1, "size": n,
        "begin_seqno": 0, "mobi_app": "web", "build": 0,
    })
    return get("https://api.vc.bilibili.com/svr_sync/v1/svr_sync/fetch_session_msgs?" + qs)


def reply_with_at(bvid: str, text: str, at_name: str, at_mid: int) -> dict:
    """发一条评论并在里面真正 @ 到人。

    关键：光在正文里写 @昵称 是纯文本，B站 不会通知对方；
    必须同时提交 at_name_to_mid={"昵称": mid}。
    """
    c = cookie()
    v = get("https://api.bilibili.com/x/web-interface/view?"
            + urllib.parse.urlencode({"bvid": bvid}))
    if v.get("code") != 0:
        raise SystemExit(f"取 aid 失败 code={v.get('code')} {v.get('message')}")
    oid = (v.get("data") or {}).get("aid")
    data = {
        "oid": str(oid),
        "type": "1",
        "root": "0",
        "parent": "0",
        "message": text,
        "plat": "1",
        "csrf": c.get("bili_jct", ""),
        "at_name_to_mid": json.dumps({at_name: at_mid}, ensure_ascii=False),
    }
    return post("https://api.bilibili.com/x/v2/reply/add", data)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    cmd = sys.argv[1]
    if cmd == "send":
        mid = int(sys.argv[2]) if len(sys.argv) > 2 else MASTER_MID
        text = sys.argv[3]
        print(json.dumps(send_dm(mid, text), ensure_ascii=False)[:600])
    elif cmd == "read":
        mid = int(sys.argv[2]) if len(sys.argv) > 2 else MASTER_MID
        n = int(sys.argv[3]) if len(sys.argv) > 3 else 10
        d = read_dm(mid, n)
        print("code", d.get("code"))
        for m in ((d.get("data") or {}).get("messages") or []):
            try:
                content = json.loads(m.get("content") or "{}")
            except Exception:
                content = {"raw": m.get("content")}
            print(time.strftime("%m-%d %H:%M", time.localtime(m.get("timestamp") or 0)),
                  "from", m.get("sender_uid"), "type", m.get("msg_type"),
                  str(content.get("content") or content)[:150])
    elif cmd == "at-test":
        print(json.dumps(reply_with_at(sys.argv[2], sys.argv[3],
                                       MASTER_NAME, MASTER_MID),
                         ensure_ascii=False)[:600])
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
