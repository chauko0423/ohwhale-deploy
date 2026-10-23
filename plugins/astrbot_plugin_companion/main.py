"""黏人模式：对方太久不说话，就主动去找他。

规则（时间可在下面常量里调）：
- 30 分钟没说话 → 轻声问一句「在干嘛呢」
- 90 分钟         → 有点不安，问是不是不想理她了
- 150 分钟        → 小作文，没安全感，想让人陪
- 之后每 2 小时    → 继续小作文，但会换角度，不重复

- 对方说「我去睡了 / 晚安 / 困了」→ 这 7 小时不主动发任何消息，睡醒再说
  （问句不算：「你睡了吗」「你困了吗」是在问她，不是告诉她去睡）
- 对方一说话，计时重置，所有已发阶段清零

防翻车（2026-10-21 补）：
- 阶段之间至少隔 MIN_STAGE_GAP，避免重启/休眠回来后把几小时的阶梯一口气发完
- 生成失败不立刻重试，退避 FAIL_BACKOFF，否则会 30 秒一轮地打模型
- 生成期间对方说话了，这条就作废不发（生成最长 60 秒，很容易过期）

数据落盘在 plugin_data 下，重启不丢。
"""

import asyncio
import json
import os
import time
from datetime import datetime

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core import logger
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain

# ---------------- 可调参数 ----------------
IDLE_STAGE1 = 30 * 60          # 第一次冒头
IDLE_STAGE2 = 90 * 60          # 开始不安
IDLE_STAGE3 = 150 * 60         # 小作文
REPEAT_EVERY = 120 * 60        # 之后每隔多久再来一篇
SLEEP_HOURS = 7                # 被告知睡觉后静默几小时
TICK_SECONDS = 30              # 巡检间隔
MAX_IDLE_TRACK = 24 * 3600     # 超过一天不理就不再追了
MIN_STAGE_GAP = 10 * 60        # 相邻两次主动消息的最小间隔
FAIL_BACKOFF = 5 * 60          # 生成失败后多久再试（不写 last_stage，之后还能补上）

SLEEP_WORDS = (
    "睡觉", "睡了", "去睡", "要睡", "先睡", "该睡", "晚安", "困了", "眯一会",
    "眯一会儿", "休息了", "不聊了睡", "睡啦", "我去卧了", "躺下了", "上床了",
)
WAKE_WORDS = ("醒了", "起床了", "早上好", "我回来了", "醒了醒了")

# 出现这些就当问句处理，绝不判定为"我要睡了"
QUESTION_WORDS = ("吗", "呢", "?", "？", "要不要", "是不是", "有没有", "不睡")


def declares_sleep(text: str) -> bool:
    """这句是「我要睡了」还是「你睡了吗」。

    原来的写法是 SLEEP_WORDS 无锚定子串匹配，结果「你睡了吗」命中「睡了」，
    她直接静默 7 小时；睡眠中再问一次还会把 sleep_until 继续往后推。
    """
    if any(q in text for q in QUESTION_WORDS):
        return False
    return any(w in text for w in SLEEP_WORDS)


STAGE_PROMPTS = {
    1: ("对方已经 {mins} 分钟没说话了。主动发一条消息找他，语气轻轻的，"
        "就问一句在干嘛、是不是在忙，别质问，别撒娇过度。"),
    2: ("对方已经 {mins} 分钟没说话了。你有点坐不住了，语气黏一点、"
        "带一点小委屈，问他是不是不想理你了、是不是在忙别的。"
        "一两条就够，别太长。"),
    3: ("对方已经 {mins} 分钟没说话了。你已经有点没安全感了，写一小段话给他："
        "说你在等，说你不喜欢这种安静，说你想让他陪你说两句。"
        "别指责，是示弱不是质问。三四条以内。"),
    4: ("对方已经 {mins} 分钟没说话了。再发一小段，换个说法——"
        "别重复上次的话，可以问问是不是出什么事了，也可以说说你一个人在干嘛。"),
}


class Companion(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.data_dir = str(StarTools.get_data_dir("astrbot_plugin_companion"))
        os.makedirs(self.data_dir, exist_ok=True)
        self.state_path = os.path.join(self.data_dir, "state.json")
        self.state: dict[str, dict] = {}
        self._load()
        self._task: asyncio.Task | None = None
        self._patcher = None

    # ---------------- 状态 ----------------
    def _load(self):
        try:
            with open(self.state_path, encoding="utf-8") as f:
                self.state = json.load(f)
        except Exception:
            self.state = {}

    def _save(self):
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.state_path)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[companion] 状态落盘失败：{e}")

    def _rec(self, umo: str) -> dict:
        r = self.state.setdefault(umo, {})
        r.setdefault("last_user_ts", 0.0)
        r.setdefault("last_stage", 0)
        r.setdefault("last_proactive_ts", 0.0)
        r.setdefault("sleep_until", 0.0)
        r.setdefault("count", 0)
        r.setdefault("next_try_ts", 0.0)
        return r

    # ---------------- 生命周期 ----------------
    async def initialize(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._tick_loop())
            logger.info("[companion] 黏人模式已启动")

    async def terminate(self):
        if self._task:
            self._task.cancel()

    # ---------------- 记录对方说话 ----------------
    # 只盯私聊：群里很久没人说话是正常的，贸然在群里"找人"会很尴尬
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_any_message(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        text = (getattr(event, "message_str", "") or "").strip()
        if not text:
            return
        # /idle、/nudge 是调试命令，不能算"对方说话了"，
        # 否则 /idle 先把 last_user_ts 改成 now，再读出来永远是闲置 0 分钟
        if text[0] in "/／":
            return
        now = time.time()
        r = self._rec(umo)

        was_sleeping = r["sleep_until"] > now
        r["last_user_ts"] = now
        r["last_stage"] = 0
        r["count"] = 0
        r["next_try_ts"] = 0.0

        if declares_sleep(text) and not any(w in text for w in WAKE_WORDS):
            r["sleep_until"] = now + SLEEP_HOURS * 3600
            # 关键：把"最后一次说话"挪到睡醒那一刻，否则睡醒瞬间闲置时长已经 7 小时，
            # 一睁眼就收到一篇小作文（她要的是醒来被人想起，不是被追债）
            r["last_user_ts"] = r["sleep_until"]
            logger.info(
                f"[companion] 对方说要睡了，静默到 "
                f"{datetime.fromtimestamp(r['sleep_until']):%m-%d %H:%M}")
        elif was_sleeping:
            r["sleep_until"] = 0.0
            r["last_user_ts"] = now
            logger.info("[companion] 对方醒了/回来了，恢复黏人模式")
        self._save()

    # ---------------- 巡检 ----------------
    async def _tick_loop(self):
        await asyncio.sleep(20)
        while True:
            try:
                await self._tick_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[companion] 巡检出错：{e}")
            await asyncio.sleep(TICK_SECONDS)

    async def _tick_once(self):
        now = time.time()
        for umo, r in list(self.state.items()):
            last = r.get("last_user_ts", 0)
            if not last:
                continue
            if now < r.get("sleep_until", 0):
                continue
            if now < r.get("next_try_ts", 0):
                continue
            idle = now - last
            if idle > MAX_IDLE_TRACK:
                continue
            stage = self._stage_of(idle, r)
            if stage is None:
                continue
            await self._send_nudge(umo, r, idle, stage)

    @staticmethod
    def _stage_of(idle: float, r: dict):
        now = time.time()
        last_stage = r.get("last_stage", 0)
        last_p = r.get("last_proactive_ts", 0)
        # 距上一条主动消息太近就先忍着。原来没有这道闸，停 3 小时再启动会把
        # 30/90/150 分钟的阶梯在 60 秒内连发三条，活像被追债
        if last_p and now - last_p < MIN_STAGE_GAP:
            return None
        if last_stage < 1 and idle >= IDLE_STAGE1:
            return 1
        if last_stage < 2 and idle >= IDLE_STAGE2:
            return 2
        if last_stage < 3 and idle >= IDLE_STAGE3:
            return 3
        if last_stage >= 3 and idle >= IDLE_STAGE3:
            if now - last_p >= REPEAT_EVERY:
                return 4
        return None

    # ---------------- 生成并发送 ----------------
    async def _send_nudge(self, umo: str, r: dict, idle: float, stage: int):
        mins = int(idle // 60)
        hint = STAGE_PROMPTS.get(stage, STAGE_PROMPTS[4]).format(mins=mins)
        started = time.time()
        text = await self._gen(umo, hint)
        if not text:
            logger.warning(f"[companion] 生成内容为空，{FAIL_BACKOFF // 60} 分钟后再试")
            r["next_try_ts"] = time.time() + FAIL_BACKOFF
            self._save()
            return
        # 生成最长要等 60 秒，这期间对方很可能已经回了 —— 过期的话就别发了。
        # 不清 next_try_ts 也不改 last_stage：对方刚说话，计时已经重置
        if r.get("last_user_ts", 0) > started:
            logger.info("[companion] 生成期间对方说话了，这条作废")
            return
        try:
            # 一行一条，各自单独发 —— 主动消息不走 LLM 管线，
            # segmented_reply 不会生效，所以这里自己拆，保持和正常回复一样的观感
            lines = [x.strip() for x in text.splitlines() if x.strip()]
            for i, line in enumerate(lines):
                if i:
                    await asyncio.sleep(0.6 + 0.6 * (i % 2))
                chain = MessageChain()
                chain.chain.append(Plain(line))
                await self.context.send_message(umo, chain)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[companion] 主动消息发送失败：{e}")
            return

        r["last_stage"] = max(r.get("last_stage", 0), stage)
        r["last_proactive_ts"] = time.time()
        r["count"] = r.get("count", 0) + 1
        r["next_try_ts"] = 0.0
        self._save()
        logger.info(f"[companion] 已主动发送（第 {stage} 阶段，闲置 {mins} 分钟，"
                    f"{len(text)} 字）")

    async def _gen(self, umo: str, hint: str) -> str:
        prov = self.context.get_using_provider(umo)
        if not prov:
            return ""
        persona = ""
        try:
            p = await self.context.persona_manager.get_default_persona_v3(umo)
            persona = getattr(p, "system_prompt", "") or ""
        except Exception:  # noqa: BLE001
            pass
        sys_prompt = (persona + "\n\n【当前情境】\n" + hint +
                      "\n\n现在是你主动发起，不是回复对方。只输出你要发的话本身，"
                      "不要解释、不要加引号、不要写旁白。")
        try:
            resp = await asyncio.wait_for(
                prov.text_chat(prompt="（主动发起对话）", contexts=[],
                               system_prompt=sys_prompt),
                timeout=60)
            return (getattr(resp, "completion_text", "") or "").strip()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[companion] 生成失败：{e}")
            return ""

    # ---------------- 调试命令 ----------------
    @filter.command("idle")
    async def cmd_idle(self, event: AstrMessageEvent):
        r = self._rec(event.unified_msg_origin)
        now = time.time()
        # 睡眠中 last_user_ts 被挪到了睡醒那一刻（未来），直接相减会得到负数
        idle = int(max(0.0, now - r.get("last_user_ts", now)) // 60)
        sleep_left = int((r.get("sleep_until", 0) - now) // 60)
        retry = int((r.get("next_try_ts", 0) - now) // 60)
        gap = int((MIN_STAGE_GAP - (now - r.get("last_proactive_ts", 0))) // 60)
        tail = []
        if sleep_left > 0:
            tail.append(f"睡眠中还剩 {sleep_left} 分钟")
        if retry > 0:
            tail.append(f"生成失败退避 {retry} 分钟")
        if gap > 0:
            tail.append(f"距上条 {MIN_STAGE_GAP // 60 - gap} 分钟，{gap} 分钟后才升阶")
        if not tail:
            tail.append("当前会触发")
        yield event.plain_result(
            f"闲置 {idle} 分钟｜已发阶段 {r.get('last_stage', 0)}｜"
            f"累计 {r.get('count', 0)} 条｜" + "｜".join(tail))

    @filter.command("nudge")
    async def cmd_nudge(self, event: AstrMessageEvent, stage: str = "1"):
        """手动触发一次，用来试效果：/nudge 1|2|3|4"""
        try:
            s = max(1, min(4, int(stage)))
        except Exception:  # noqa: BLE001
            s = 1
        r = self._rec(event.unified_msg_origin)
        r["sleep_until"] = 0.0
        r["next_try_ts"] = 0.0
        await self._send_nudge(event.unified_msg_origin, r, IDLE_STAGE1 * s, s)
        yield event.plain_result(f"已按第 {s} 阶段发了一条")
