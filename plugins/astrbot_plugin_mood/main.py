"""给鲸鲸加一层"小状态"：好感、心情、他刚说过的话。

只做一件事：每次请求大模型前，把 data/mood.json 压成一行 [状态] 追加到 system prompt。
状态由通话侧（call_loop）和本插件共同维护，两边看到的是同一个她。

原则：只影响语气，不控制行为；任何异常都不能影响回复。
"""
from __future__ import annotations

import importlib.util
import os

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core import logger

_HOME = os.environ.get("OHWHALE_HOME", r"C:\OhWhale")


def _load_mood():
    """按绝对路径加载 scripts/mood_state.py，不依赖 sys.path。"""
    for path in (os.path.join(_HOME, "scripts", "mood_state.py"),
                 os.path.join(os.getcwd(), "scripts", "mood_state.py")):
        if os.path.isfile(path):
            spec = importlib.util.spec_from_file_location("ohwhale_mood_state", path)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                return module
    return None


class MoodState(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.mood = _load_mood()
        if self.mood is None:
            logger.warning("[mood] 找不到 scripts/mood_state.py，状态注入关闭")

    @filter.on_llm_request()
    async def inject_state(self, event: AstrMessageEvent, req):
        if self.mood is None:
            return
        try:
            text = (getattr(event, "message_str", "") or "").strip()
            if text:
                self.mood.update_from_user(text)
            req.system_prompt = (req.system_prompt or "") + "\n\n" + self.mood.summary()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[mood] 状态注入失败：{exc}")
