"""探测 AstrBot 定时任务(active_agent)的 session 字段：正控/负控。

只依赖标准库（urllib），不装 httpx。

用法：
    python probe_cron_session.py list
    python probe_cron_session.py plats
    python probe_cron_session.py create
    python probe_cron_session.py run <job_id>
    python probe_cron_session.py wait <job_id> [秒]
    python probe_cron_session.py patch <job_id> <session>
    python probe_cron_session.py del <job_id>
"""

from __future__ import annotations

from ohwhale.config import get as setting, path as config_path, project_path, state_path, log_path, secret, llm, require

import json
import os
import sys
import time
import urllib.request

BASE = setting("services.astrbot_url")
PWD = secret("ASTRBOT_DASHBOARD_PASSWORD")
QQ_UMO = setting("master.session", "")

# 直连回环，绕开系统代理
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _call(method: str, path: str, body: dict | None = None, token: str | None = None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with _OPENER.open(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {"raw": e.read().decode(errors="replace")[:400]}


def login() -> str:
    st, d = _call("POST", "/api/auth/login", {"username": "astrbot", "password": PWD})
    body = (d or {}).get("data") or {}
    tok = body.get("token") or body.get("access_token")
    if not tok:
        raise SystemExit(f"登录失败 {st}: {str(d)[:300]}")
    return tok


def jobs(tok):
    st, d = _call("GET", "/api/cron/jobs", token=tok)
    return (d.get("data") or []) if st == 200 else []


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    cmd = sys.argv[1]
    tok = login()

    if cmd == "list":
        for j in jobs(tok):
            p = j.get("payload") or {}
            print(f"{j['job_id']}  {j.get('name')}  type={j.get('job_type')} "
                  f"enabled={j.get('enabled')} session={p.get('session')!r} "
                  f"status={j.get('status')} err={(j.get('last_error') or '')[:40]}")
        return 0

    if cmd == "plats":
        for path in ("/api/platform/list", "/api/config/platform", "/api/config/get"):
            st, d = _call("GET", path, token=tok)
            print(path, st, str(d)[:300])
        return 0

    if cmd == "create":
        body = {
            "name": "小满接线自检",
            "note": "只做一件事：调用 send_message_to_user，给当前会话发一句"
                    "「小满接线自检·负控A」。不要调用其它工具，不要解释，发完就结束。",
            "session": "",
            "cron_expression": "0 0 1 1 *",
            "timezone": "Asia/Shanghai",
            "enabled": False,
        }
        st, d = _call("POST", "/api/cron/jobs", body, token=tok)
        print(st, json.dumps(d, ensure_ascii=False)[:500])
        return 0

    if cmd == "run":
        st, d = _call("POST", f"/api/cron/jobs/{sys.argv[2]}/run", token=tok)
        print("run:", st, str(d)[:200])
        return 0

    if cmd == "wait":
        job_id = sys.argv[2]
        limit = int(sys.argv[3]) if len(sys.argv) > 3 else 120
        t0 = time.time()
        while time.time() - t0 < limit:
            for j in jobs(tok):
                if j["job_id"] == job_id:
                    st = j.get("status")
                    if st in ("completed", "failed"):
                        print(f"[{time.time()-t0:.0f}s] status={st} "
                              f"last_error={(j.get('last_error') or '')[:200]}")
                        return 0
            time.sleep(2)
        print("超时未结束")
        return 1

    if cmd == "patch":
        body: dict = {"session": sys.argv[3]}
        if len(sys.argv) > 4:
            body["note"] = sys.argv[4]
        st, d = _call("PATCH", f"/api/cron/jobs/{sys.argv[2]}", body, token=tok)
        print("patch:", st, json.dumps(d, ensure_ascii=False)[:400])
        return 0

    if cmd == "del":
        st, d = _call("DELETE", f"/api/cron/jobs/{sys.argv[2]}", token=tok)
        print("del:", st, str(d)[:200])
        return 0

    print("未知命令")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
