"""走 AstrBot 的 webchat 通道跑一次端到端：让模型自己决定调用插件工具。

用法：
    python chat_e2e.py "用 bili_dm 给我发一条私信，内容写：我学会私信了"
"""

from __future__ import annotations

from ohwhale.config import get as setting, path as config_path, project_path, state_path, log_path, secret, llm, require

import json
import sys
import urllib.request

BASE = setting("services.astrbot_url")
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(method: str, path: str, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with OP.open(req, timeout=120) as r:
        return json.loads(r.read().decode() or "{}")


def login() -> str:
    d = call("POST", "/api/auth/login",
             {"username": "astrbot", "password": secret("ASTRBOT_DASHBOARD_PASSWORD")})
    return d["data"]["token"]


def main() -> int:
    text = sys.argv[1] if len(sys.argv) > 1 else "在吗"
    tok = login()
    d = call("GET", "/api/chat/new_session?platform_id=webchat", token=tok)
    sid = (d.get("data") or {}).get("session_id")
    print("session:", sid)

    body = json.dumps({"message": text, "session_id": sid}).encode()
    req = urllib.request.Request(BASE + "/api/chat/send", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {tok}")
    final = []
    with OP.open(req, timeout=240) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload in ("[DONE]", ""):
                continue
            try:
                ev = json.loads(payload)
            except Exception:
                continue
            t = ev.get("type") or ev.get("event")
            if t == "complete":
                data = ev.get("data")
                final = [data if isinstance(data, str) else
                         str((data or {}).get("text") or "")]
                break
            if t in ("plain", "delta"):
                d2 = ev.get("data")
                if isinstance(d2, str):
                    final.append(d2)
    print("回复：", "".join(final)[:800])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
