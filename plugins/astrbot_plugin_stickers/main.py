"""表情包：让她在合适的时候发一张自己的图。

设计：
- 索引由 `tools/sticker_index.py` 生成（模型逐张读字 + 判情绪），这里只负责挑和发
- 按情绪挑，不随机乱发；同一张不连着用；有冷却与每日上限
- 只发本地文件，不发 URL
"""

from __future__ import annotations

from ohwhale.config import get as setting, path as config_path, project_path, state_path, log_path, secret, llm, require

import json
import os
import random
import time

import astrbot.api.message_components as Comp
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core import logger
from astrbot.core.message.message_event_result import MessageChain

COOLDOWN_SECONDS = 45        # 两次表情包至少隔这么久
MAX_PER_DAY = 25
INDEX_NAME = "index.json"


class StickerPack(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.data_dir = str(StarTools.get_data_dir("astrbot_plugin_stickers"))
        os.makedirs(self.data_dir, exist_ok=True)
        self.index_path = config_path("sticker_index")
        self.state_path = os.path.join(self.data_dir, "state.json")
        self._index: dict | None = None

    # ---------------- 索引 ----------------
    def _load_index(self) -> dict:
        if self._index is not None:
            return self._index
        try:
            with open(self.index_path, encoding="utf-8") as f:
                self._index = json.load(f)
            for entries in self._index.get("moods", {}).values():
                for entry in entries:
                    candidate = os.path.realpath(os.path.join(config_path("stickers"), entry["path"]))
                    if os.path.commonpath([candidate, os.path.realpath(config_path("stickers"))]) != os.path.realpath(config_path("stickers")):
                        raise ValueError("表情索引路径超出素材目录")
                    entry["path"] = candidate
        except Exception:  # noqa: BLE001
            self._index = {"moods": {}, "total": 0}
        return self._index

    def _state(self) -> dict:
        try:
            with open(self.state_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            return {}

    def _save_state(self, st: dict) -> None:
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.state_path)

    # ---------------- 工具 ----------------
    @filter.llm_tool(name="send_sticker")
    async def send_sticker(self, event: AstrMessageEvent, mood: str = ""):
        """发一张自己的表情包（鲸鱼娘的图，带字的那种）。
        说话的时候如果情绪明显——被逗笑、无语、委屈、困了、想吃、得意、想撒娇——
        可以配一张，比纯文字更像真人。
        别每句都发：一天十来张就够，同一张不要连着用。

        Args:
            mood(string): 想要的情绪，可填：开心 / 生气 / 委屈 / 无语 / 害羞 / 困累 / 饿想吃 /
                撒娇 / 惊讶 / 嫌弃 / 安慰 / 得意。留空则随机挑一张
        """
        idx = self._load_index()
        moods = idx.get("moods") or {}
        if not moods:
            return "表情包库还没建好，先别发。"
        st = self._state()
        now = time.time()
        today = time.strftime("%Y-%m-%d")
        if st.get("date") != today:
            st = {"date": today, "count": 0, "recent": [], "last_ts": 0}
        if now - float(st.get("last_ts") or 0) < COOLDOWN_SECONDS:
            return "刚发过一张，等一下再发。"
        if int(st.get("count") or 0) >= MAX_PER_DAY:
            return "今天的表情包额度用完了，明天再发。"

        key = (mood or "").strip()
        pool = moods.get(key) or []
        if not pool:
            # 没这个情绪就退到最接近的一类；再不行就全库随机
            for alt in ("开心", "无语", "撒娇", "得意"):
                if moods.get(alt):
                    pool = moods[alt]
                    key = alt
                    break
            if not pool:
                pool = [x for v in moods.values() for x in v]
                key = "随机"
        recent = set(st.get("recent") or [])
        fresh = [x for x in pool if x["path"] not in recent] or pool
        pick = random.choice(fresh)

        try:
            chain = MessageChain()
            chain.chain.append(Comp.Image.fromFileSystem(pick["path"]))
            ok = await self.context.send_message(event.unified_msg_origin, chain)
            if not ok:
                raise RuntimeError("没找到可用的平台会话")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[sticker] 发送失败：{e}")
            return f"表情包没发出去：{e}"

        st["count"] = int(st.get("count") or 0) + 1
        st["last_ts"] = now
        st["recent"] = (list(recent) + [pick["path"]])[-12:]
        self._save_state(st)
        logger.info(f"[sticker] 发了一张 {key}：{os.path.basename(pick['path'])}")
        return f"发了一张{key}的表情包（图上是「{(pick.get('text') or '')[:20]}」）。继续说话。"

    @filter.llm_tool(name="sticker_moods")
    async def sticker_moods(self, event: AstrMessageEvent):
        """看一眼表情包库里有哪些情绪、各有多少张。想发但不确定该填什么 mood 时用。"""
        idx = self._load_index()
        moods = idx.get("moods") or {}
        if not moods:
            return "表情包库还没建好。"
        parts = [f"{k} {len(v)} 张" for k, v in
                 sorted(moods.items(), key=lambda kv: -len(kv[1]))]
        return f"共 {idx.get('total', 0)} 张：\n" + "\n".join(parts)
