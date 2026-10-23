"""按内容决定发语音还是发文字（替代 AstrBot 内置的纯随机 TTS）。

规则：
- 越短越可能发语音（短句像随口一说，长段落像打字）
- 情绪越浓越可能发语音（感叹号、语气词、情绪词）
- 其余情况发文字
- 每条独立掷骰子，所以会出现"上一条语音、下一条文字"的自然混排

部署前要注意：必须把 provider_tts_settings.enable 设成 false，
否则内置 TTS 会先转一遍，这里再转一遍就会出现两条语音。

命令：
  /say <文本>   强制把一段文字合成为语音发出来（用于试听/对比）
"""

from ohwhale.config import get as setting, path as config_path, project_path, state_path, log_path, secret, llm, require

import asyncio
import random
import re
import subprocess
import time

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core import logger
from astrbot.core.message.components import Record

try:
    from astrbot.core.message.components import Plain
except ImportError:  # 兼容旧版本
    from astrbot.api.message_components import Plain

# 情绪词：出现越多，越发语音
EMO_WORDS = (
    "！", "？", "…", "~", "～",
    "哼", "嘛", "呀", "啦", "诶", "欸", "哎", "嗷", "唔",
    "讨厌", "喜欢", "累", "难过", "开心", "气", "笑", "哭",
    "想", "怕", "好烦", "真好", "可惜", "居然", "竟然",
)

# TTS 单次最多送多少字（QQ 语音太长也会被截）
MAX_TTS_CHARS = 150

# 合成超时（秒）。Edge TTS 到微软的网络不稳，实测波动 1.5s ~ 37s，
# 超时太久不如直接发文字——让人等到沉默比没有语音更糟。
TTS_TIMEOUT = 20.0

# ---------------- 双线路由 ----------------
# 本地 GPT-SoVITS 优先（音色是"大肥鱼"本人），GPU 忙或失败时自动切云端。
LOCAL_TTS_ID = setting("voice.local_provider_id", "gpt_sovits")
# 云端用阿里云百炼 CosyVoice（复刻的大肥鱼音色），和本地音色一致，
# 切换时不会有"换人"感。edge_tts 留作最后兜底。
CLOUD_TTS_ID = setting("voice.cloud_provider_id", "")
GPU_FREE_MIN_MB = 2600        # 本地推理大约要这么多空闲显存
LOCAL_TIMEOUT = 25.0          # 本地超过这个时间没出音频，就当它不可用
                             # 2026-10-21 从 8s 放宽：多行长句（79 字）8 秒根本不够，
                             # 一超时就把本地标停 60 秒，接着兜底到 edge_tts，音色直接换人
LOCAL_COOLDOWN_S = 60         # 本地失败后 60s 内不再尝试（本地模型冷启要约 45s）
GPU_CACHE_S = 15.0            # 显存查询缓存

# GPT-SoVITS v2Pro 的老毛病：目标文本太短（实测 ≤16 字）时 AR 会提前吐 EOS，
# 合成出来只有 0.34s 白噪声（t2s_model.py 里 idx<11 那段就是硬下限 0.4s）。
# 短句交给云端 CosyVoice——同一个人的复刻音色，切换听不出换人。
LOCAL_MIN_CHARS = 18


def audio_duration(path: str) -> float:
    """读 wav 时长（秒）。优先读文件头，读不了再问 ffmpeg。读不到返回 0。"""
    try:
        import wave
        with wave.open(path, "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:  # noqa: BLE001
        pass
    try:
        out = subprocess.run(["ffmpeg", "-i", path], capture_output=True,
                             text=True, errors="ignore", timeout=5).stderr
        m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", out)
        if not m:
            return 0.0
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:  # noqa: BLE001
        return 0.0


def too_short(path: str, text: str) -> bool:
    """GPT-SoVITS 偶尔会吐一段不到半秒的残句，听感上就是"断气"。
    按 6 字/秒 估个下限，明显偏短就判失败。"""
    d = audio_duration(path)
    if d <= 0:
        return False  # 读不出时长就别误杀
    return d < max(0.7, 0.06 * len(text))


def bad_audio(path: str, text: str) -> bool:
    """比 too_short 更严：既抓"断气"，也抓"跑飞"。

    2026-10-21 加。实测本地偶尔会念到 14.7 秒（原文只有 34 字，正常 6~9 秒），
    这种又长又拖的听着像在念经，比没有更难受。按 6 字/秒 估上限。
    """
    d = audio_duration(path)
    if d <= 0:
        return False
    lo = max(0.7, 0.055 * len(text))
    hi = max(6.0, 0.30 * len(text))
    return d < lo or d > hi


def user_wants_voice(text: str) -> bool:
    """对方是否在明确要求发语音。"""
    t = text or ""
    wants = ("语音", "声音", "说给我听", "念一下", "念给我", "读一下", "读给我", "出声", "发条音")
    negate = ("不用语音", "别发语音", "不要语音")
    if any(n in t for n in negate):
        return False
    return any(w in t for w in wants)


def voice_probability(text: str) -> float:
    """给一段文本打分：返回发语音的概率 0~1。"""
    t = (text or "").strip()
    n = len(t)
    if n <= 0:
        return 0.0
    if n <= 4:
        p = 0.45          # "好的""嗯嗯"这种纯应答，多数还是打字
    elif n <= 20:
        p = 0.8
    elif n <= 40:
        p = 0.6
    elif n <= 70:
        p = 0.38
    elif n <= 110:
        p = 0.18
    else:
        p = 0.07

    hits = sum(1 for w in EMO_WORDS if w in t)
    if hits:
        p += min(0.25, 0.07 * hits)
    # 纯陈述句（没有标点语气）降权
    if not re.search(r"[！？…~～]", t):
        p -= 0.1
    return max(0.0, min(0.95, p))


class VoicePicker(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._gpu_free = None
        self._gpu_at = 0.0
        self._local_down_until = 0.0

    # ---------- 双线探测 ----------
    def _gpu_free_mb(self) -> int | None:
        """查当前空闲显存（MB）。查不到返回 None（当作未知，允许尝试本地）。"""
        now = time.time()
        if self._gpu_free is not None and now - self._gpu_at < GPU_CACHE_S:
            return self._gpu_free
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.free",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3,
            )
            self._gpu_free = int(out.stdout.strip().splitlines()[0])
        except Exception:  # noqa: BLE001
            self._gpu_free = None
        self._gpu_at = now
        return self._gpu_free

    def _inst(self, pid: str):
        pm = getattr(self.context, "provider_manager", None)
        if pm is None:
            return None
        return (getattr(pm, "inst_map", None) or {}).get(pid)

    def _local_ok(self, text: str = "") -> bool:
        if len(text) < LOCAL_MIN_CHARS:
            return False
        if time.time() < self._local_down_until:
            return False
        free = self._gpu_free_mb()
        if free is None:
            return True
        return free >= GPU_FREE_MIN_MB

    async def _tts(self, umo: str, text: str) -> tuple[str | None, str]:
        """返回 (音频路径, 走的哪条线)。

        2026-10-21 修正结论：
        昨晚"本地声音怪"的真正原因是**对生产 9880 调了 /set_gpt_weights，
        把微调的 e15 存档留在了线上**，而 e15 在短句上几乎必挂（实测 20~30%）。
        yaml 里默认的原版 s1v3 GPT 稳得多（约 80~90%），所以恢复本地优先。

        本地最多试 3 次（每次换随机种子，等于重摇一次），
        用 bad_audio 同时挡"断气"和"跑飞"，都不行才切云端。
        """
        local = self._inst(LOCAL_TTS_ID)
        cloud = self._inst(CLOUD_TTS_ID)

        # 本地优先（yaml 里用的是原版 s1v3 GPT，短句比微调存档稳得多）
        if local and self._local_ok(text):
            for attempt in (1, 2, 3):
                try:
                    audio = await asyncio.wait_for(
                        local.get_audio(text), timeout=LOCAL_TIMEOUT)
                    if audio and not bad_audio(audio, text):
                        return audio, "local"
                    if audio:
                        logger.warning(
                            f"[voice-picker] 本地第 {attempt} 次音频不合格"
                            f"（{audio_duration(audio):.2f}s / {len(text)}字），重试")
                except Exception as e:  # noqa: BLE001
                    self._local_down_until = time.time() + LOCAL_COOLDOWN_S
                    logger.warning(
                        f"[voice-picker] 本地 TTS 不可用（{type(e).__name__}），"
                        f"{LOCAL_COOLDOWN_S}s 内不再试本地")
                    break

        if cloud:
            for _ in (1, 2):
                try:
                    audio = await asyncio.wait_for(
                        cloud.get_audio(text), timeout=TTS_TIMEOUT)
                    if audio and not bad_audio(audio, text):
                        return audio, "cloud"
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[voice-picker] 云端 TTS 失败：{e}")
                    break

        # 最后兜底：只允许克隆音色这两条线。
        # 绝不能用 provider_manager 里随便哪个 TTS——那里还挂着 edge_tts，
        # 用上就是"换了个陌生人说话"，比不发语音更糟。宁可返回 None 让对方看到文字。
        pm = getattr(self.context, "provider_manager", None)
        for inst in (getattr(pm, "tts_provider_insts", None) or []):
            cfg = getattr(inst, "provider_config", None) or {}
            pid = cfg.get("id") if isinstance(cfg, dict) else ""
            if pid not in (LOCAL_TTS_ID, CLOUD_TTS_ID):
                continue
            try:
                audio = await asyncio.wait_for(
                    inst.get_audio(text), timeout=TTS_TIMEOUT)
                if audio and not bad_audio(audio, text):
                    return audio, f"retry:{pid}"
            except Exception:  # noqa: BLE001
                continue
        return None, "none"

    @filter.on_decorating_result()
    async def pick_voice_or_text(self, event: AstrMessageEvent):
        result = event.get_result()
        if result is None or not result.is_llm_result():
            return

        chain = getattr(result, "chain", None) or []
        if any(isinstance(c, Record) for c in chain):
            return  # 已经是语音了，别重复处理

        plains = [c for c in chain if isinstance(c, Plain)]
        text = "".join(c.text for c in plains).strip()
        if len(text) < 2:
            return

        # 送给 TTS 前要洗一遍：换行→逗号。
        # 聊天里"一个气泡一行"是为了阅读节奏，真念出来必须是一句连贯的话；
        # 带着 \n 丢给 GPT-SoVITS 会念得断续甚至直接跑飞（2026-10-21 实测）。
        tts_text = re.sub(r"\s*\n+\s*", "，", text).strip() or text

        p = voice_probability(text)
        roll = random.random()
        forced = user_wants_voice(getattr(event, "message_str", "") or "")
        if forced:
            logger.info("[voice-picker] 对方明确要语音，强制合成")
        elif roll > p:
            logger.debug(f"[voice-picker] 文字 (p={p:.2f}, roll={roll:.2f})")
            return

        audio, via = await self._tts(event.unified_msg_origin, tts_text[:MAX_TTS_CHARS])
        if not audio:
            logger.info(f"[voice-picker] 两条线都没出音频，这条改发文字"
                        f"（p={p:.2f} len={len(tts_text)}）")
            return
        logger.info(f"[voice-picker] 语音 via={via} p={p:.2f} len={len(tts_text)}")

        new_chain = []
        replaced = False
        for c in chain:
            if isinstance(c, Plain):
                if not replaced and c.text.strip():
                    new_chain.append(Record(file=audio, url=audio, text=c.text))
                    replaced = True
                # 其余文本段丢弃：语音已经把话说完，再发文字就重复了
            else:
                new_chain.append(c)
        result.chain = new_chain

    @filter.command("say")
    async def say(self, event: AstrMessageEvent):
        """强制把后面的文字合成语音发出来，用于试听"""
        text = (event.message_str or "").strip()
        # 去掉命令本身
        text = re.sub(r"^/?say\s*", "", text).strip()
        if not text:
            yield event.plain_result("用法：/say 想试听的那句话")
            return
        audio, via = await self._tts(event.unified_msg_origin, text[:MAX_TTS_CHARS])
        if not audio:
            yield event.plain_result("合成失败：两条线都没出音频。")
            return
        logger.info(f"[voice-picker] /say via={via}")
        yield event.chain_result([Record(file=audio, url=audio, text=text)])
