"""通话侧接入 AstrBot 的桥接层（并线的关键）

为什么不用直连 DeepSeek：
    AstrBot 里有她的人设、长期记忆，还有一堆 LLM 工具（bili_search / bili_detail /
    bili_share …）。直连 DeepSeek 就只剩一个裸模型，所以通话里她"不会逛 B 站"。
    走 AstrBot 的 /api/chat，这些能力全都回来了，聊天和通话还是同一个她。

流程：
    1. POST /api/auth/login          → 拿 token
    2. GET  /api/chat/new_session    → 拿 session_id（platform=webchat）
    3. POST /api/chat/send (SSE)     → 发一句、收流式回复

用法：
    python astrbot_bridge.py --test        # 自检：登录 + 发一句话
"""

from __future__ import annotations

from ohwhale.config import get as setting, path as config_path, project_path, state_path, log_path, secret, llm, require

import json
import os
import sys
import time

import httpx

BASE = setting("services.astrbot_url")
CFG = config_path("astrbot_config")

for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)
os.environ["NO_PROXY"] = "127.0.0.1,localhost"


DASHBOARD_PASSWORD = None


def load_dashboard_cred():
    return setting("services.dashboard_username", "astrbot"), secret("ASTRBOT_DASHBOARD_PASSWORD")


class AstrBotBridge:
    def __init__(self, base: str = BASE):
        self.base = base.rstrip("/")
        self.client = httpx.Client(timeout=120, trust_env=False)
        self.token: str | None = None
        self.session_id: str | None = None

    # ---------- 基础 ----------
    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def alive(self) -> bool:
        try:
            r = self.client.get(f"{self.base}/api/stat/start-time", timeout=3)
            return r.status_code < 500
        except Exception:  # noqa: BLE001
            return False

    def login(self) -> bool:
        user, pwd = load_dashboard_cred()
        r = self.client.post(f"{self.base}/api/auth/login",
                             json={"username": user, "password": pwd})
        if r.status_code != 200:
            print(f"  登录失败 HTTP {r.status_code}: {r.text[:120]}")
            return False
        data = r.json()
        body = data.get("data") or {}
        self.token = body.get("token") or body.get("access_token") or data.get("token")
        if self.token:
            self.client.headers["Authorization"] = f"Bearer {self.token}"
        return self.token is not None

    def new_session(self, platform_id: str = "webchat") -> str | None:
        r = self.client.get(f"{self.base}/api/chat/new_session",
                            params={"platform_id": platform_id}, headers=self._headers())
        if r.status_code != 200:
            print(f"  建会话失败 HTTP {r.status_code}: {r.text[:120]}")
            return None
        d = r.json().get("data") or {}
        self.session_id = d.get("session_id") or d.get("id") or d.get("conversation_id")
        return self.session_id

    def ensure(self) -> bool:
        if not self.token and not self.login():
            return False
        if not self.session_id and not self.new_session():
            return False
        return True

    # ---------- 对话 ----------
    def chat(self, message: str, timeout: float = 90.0) -> str:
        """发一句话，把 SSE 流里的文本拼起来返回。"""
        if not self.ensure():
            raise RuntimeError("AstrBot 不可用")

        payload = {"message": message, "session_id": self.session_id}
        parts: list[str] = []
        with self.client.stream("POST", f"{self.base}/api/chat/send",
                                json=payload, headers=self._headers(),
                                timeout=timeout) as r:
            if r.status_code != 200:
                raise RuntimeError(f"send HTTP {r.status_code}: {r.read()[:150]}")
            for line in r.iter_lines():
                if not line:
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line in ("[DONE]", ""):
                    continue
                try:
                    ev = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                # 注意：plain 事件是"思考过程"的流片，不是回复。
                # 真正的回复在 complete 事件里（整段文本）。
                t = ev.get("type") or ev.get("event")
                data = ev.get("data")
                if t == "complete":
                    if isinstance(data, str):
                        return data.strip()
                    if isinstance(data, dict):
                        return str(data.get("text") or data.get("content") or "").strip()
                elif t in ("plain", "delta", "chunk"):
                    if isinstance(data, str):
                        parts.append(data)      # 兜底：没有 complete 时才用
                elif t in ("end", "done"):
                    break
        return "".join(parts).strip()


def main():
    ap_test = "--test" in sys.argv
    b = AstrBotBridge()
    print("AstrBot 是否在线:", b.alive())
    if not b.alive():
        print("先启动 AstrBot（双击 启动小满QQ.bat）")
        return 1
    if not b.login():
        return 2
    print("登录成功")
    sid = b.new_session()
    print("会话:", sid)
    if ap_test:
        t0 = time.time()
        reply = b.chat("在干嘛呀")
        print(f"回复（{time.time()-t0:.1f}s）:", reply[:200])
    return 0


if __name__ == "__main__":
    sys.exit(main())
