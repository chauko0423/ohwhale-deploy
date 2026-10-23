"""每日消息计数：每收满 N 条，就攒够了一次"写点感受"的理由。

只做计数和素材留存，不碰网络、不碰 LLM——这样能离线测。
落盘在 `daily.json`，进程重启不清零，跨天自动重置。
"""

from __future__ import annotations

import json
import os
from datetime import date

THRESHOLD = 30      # 每累计多少条触发一次
MAX_POSTS = 3       # 每天最多发几条，防止刷屏
BUFFER = 12         # 留多少条素材给 LLM 参考
SNIPPET = 40        # 每条素材截多长


class DailyCounter:
    def __init__(self, path: str, threshold: int = THRESHOLD,
                 max_posts: int = MAX_POSTS):
        self.path = path
        self.threshold = max(1, int(threshold))
        self.max_posts = max(1, int(max_posts))
        self.state = {"date": "", "count": 0, "posts": 0, "recent": []}
        self._load()
        self._roll(date.today().isoformat())

    # ---------------- 落盘 ----------------
    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                self.state.update({
                    "date": str(d.get("date") or ""),
                    "count": int(d.get("count") or 0),
                    "posts": int(d.get("posts") or 0),
                    "recent": [str(x) for x in (d.get("recent") or [])][-BUFFER:],
                })
        except Exception:  # noqa: BLE001
            pass

    def _save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 跨天 ----------------
    def _roll(self, today: str):
        if self.state.get("date") != today:
            self.state = {"date": today, "count": 0, "posts": 0, "recent": []}
            self._save()

    # ---------------- 计数 ----------------
    def add(self, text: str = "") -> bool:
        """收一条消息。返回 True 表示"够 30 条了，该写一条"。

        注意：这里**不**增加 posts，得等真的发出去了再 mark_posted()。
        否则发布失败也会烧掉当天的额度。
        """
        self._roll(date.today().isoformat())
        self.state["count"] += 1
        t = (text or "").strip()
        if t:
            self.state["recent"].append(t[:SNIPPET])
            self.state["recent"] = self.state["recent"][-BUFFER:]
        self._save()

        if self.state["posts"] >= self.max_posts:
            return False
        return self.state["count"] % self.threshold == 0

    def mark_posted(self):
        self.state["posts"] += 1
        self._save()

    # ---------------- 查询 ----------------
    def recent(self) -> list[str]:
        return list(self.state.get("recent") or [])

    def next_at(self) -> int:
        c = self.state["count"]
        return (c // self.threshold + 1) * self.threshold

    def summary(self) -> str:
        s = self.state
        return (f"今天已收 {s['count']} 条｜已写 {s['posts']}/{self.max_posts} 条说说"
                f"｜下一条在第 {self.next_at()} 条")
