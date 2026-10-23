"""QQ 通话闭环 v2：听 → 识别 → 思考 → 说话（支持打断 / 分句流式）

相对 v1 的四处改动：
  1. 电话模式人设：只回 1~2 句、30 字内、不分段 —— 单轮时长砍半
  2. VAD 加 0.3s 前置缓冲 + 自适应噪声底 —— 不再吞掉第一个字
  3. TTS 分句流式：第一句合成完就先播，后面的边播边合成 —— 首字延迟减半
  4. 支持打断：她说话时你一开口，立刻停

链路：
    你说话 → QQ 播放到 JBL → 环回录音 → VAD → DashScope ASR
      → DeepSeek（她的性格 + 电话模式）→ GPT-SoVITS → VB-Cable → QQ 麦克风

用法：
    python -u call_loop.py            # 一直循环
    python -u call_loop.py --once     # 只处理一句话
"""

from __future__ import annotations

from ohwhale.config import get as setting, path as config_path, project_path, state_path, log_path, secret, llm, require

import argparse
import json
import os
import queue
import re
import sqlite3
import sys
import threading
import time

# 控制台/重定向默认按 GBK 编码，她的回复里一出现 emoji（🍚 这种）就抛
# UnicodeEncodeError 直接把整个通话循环带走 —— 2026-10-23 12:14 就是这么挂的。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

import base64
import io
import socket

import httpx
import numpy as np

try:
    from astrbot_bridge import AstrBotBridge
except Exception:  # noqa: BLE001
    AstrBotBridge = None
import soundcard as sc
import sounddevice as sd
import soundfile as sf

HEARTBEAT = log_path("loop.heartbeat")
CFG = config_path("astrbot_config")
DB = config_path("astrbot_db")

RATE_IN = 16000
RATE_OUT = 48000
FRAME = 0.05            # 50ms 一帧
SILENCE_END = 0.90      # 静音多久算说完（0.55 太急，一句话中间换气就被截断）
MIN_SPEECH = 0.25
# 电话走不走 AstrBot：False = 直连模型（快、带电话模式），True = 走 AstrBot（有记忆/工具但慢）
PHONE_USE_BRIDGE = setting("audio.use_astrbot_bridge", False)
MAX_UTTER = 18.0
PREROLL = 0.45          # 前置缓冲，防止吞首字
THRESHOLD_FLOOR = 0.009
INTERRUPT_RATIO = 3.0   # 打断判定：能量超过阈值这么多倍（2.0 时环境噪音就误触发）

# 谁在往哪个端点送声音 —— 不能靠"默认扬声器"猜（他改过音频设备）
PROBE_PY = config_path("probe_python")
PROBE_SCRIPT = project_path("scripts/which_endpoint.py")
TTS_API = setting("services.tts_url")
TTS_PARAMS = {
    "text_lang": "zh",
    "ref_audio_path": config_path("voice_ref"),
    "prompt_text": setting("voice.prompt_text"),
    "prompt_lang": "zh",
    "text_split_method": "cut0",
    "media_type": "wav",
}

PHONE_MODE = """

【电话模式】
你现在在打语音电话，不是发文字消息。
· 每次只说 1~2 句，总共不超过 30 字
· 不要分段、不要换行、不要列点、不要括号动作
· 像真人接电话那样短、口语、可以带语气词
· 说完就停，把话头留给对方"""

# 把"正在说话的音量"发给 avatar.py 做口型，最高 30Hz 就够
AVATAR = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

# 流式合成：GPT-SoVITS 的 streaming_mode=3 + media_type=raw，边合成边出声。
# 原来要等整句 wav 合成完（约 1.3 s）才开始播，首字里最大的一块就是它。
TTS_STREAM = True
TTS_RAW_RATE = int(setting("audio.tts_raw_rate", 32000))
PLAY_START = 0.0        # 这一轮开口的时间点，用于"刚开口别被打断"的宽限
TTS_CLIENT = httpx.Client(timeout=120, trust_env=False,
                          limits=httpx.Limits(max_keepalive_connections=4))
VISION_MODEL = setting("llm.screen.model")
VISION_API = setting("llm.screen.base_url").rstrip("/") + "/chat/completions"
SEEWORDS = ("看", "屏幕", "画面", "这个", "那个", "图", "样子", "长啥样", "长得")

PLAYING = threading.Event()     # 她正在说话
INTERRUPT = threading.Event()   # 有人插话


def load_cfg():
    return {}


def get_keys(cfg):
    key, base, _ = llm("chat")
    return key, base, secret("DASHSCOPE_API_KEY")


def get_persona() -> str:
    with open(config_path("persona"), encoding="utf-8-sig") as f:
        base = f.read()
    base = re.sub(r"# 表情包\n(?:.*\n)*?(?=# |\Z)", "", base)
    return base + PHONE_MODE


def split_sentences(text: str) -> list[str]:
    """按标点切句，太碎的并到前一句。

    阈值取 4 字：`嗯，我在呢。` 只有 6 个字，如果按 8 字合并就会和下一句粘在一起，
    分句流式就失去意义了 —— 第一句必须尽早能播。
    """
    parts = re.split(r"(?<=[。！？!?；;\n])", text)
    out, buf = [], ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        buf += p
        if len(buf) >= 4:
            out.append(buf)
            buf = ""
    if buf:
        if out:
            out[-1] += buf
        else:
            out.append(buf)
    return out or [text.strip()]


def resample(x: np.ndarray, sr: int, target: int) -> np.ndarray:
    if sr == target:
        return x.astype("float32")
    n = int(round(len(x) * target / sr))
    return np.interp(np.linspace(0, len(x) - 1, n),
                     np.arange(len(x)), x).astype("float32")


class Speaker:
    def __init__(self):
        self.dev = None
        for i, d in enumerate(sd.query_devices()):
            if setting("audio.playback_hint") in d["name"] and d["max_output_channels"] >= 2:
                self.dev = i
        if self.dev is None:
            raise RuntimeError("找不到 VB-Cable 播放端点")

    def _play_into(self, st, data: np.ndarray, sr: int) -> bool:
        """往已打开的流里写一段；返回 True 表示被打断。"""
        data = resample(data, sr, RATE_OUT)
        stereo = np.column_stack([data, data]).astype("float32")
        chunk = 2048
        for i in range(0, len(stereo), chunk):
            if INTERRUPT.is_set():
                return True
            block = stereo[i:i + chunk]
            st.write(block)
            # 口型同步：这一段有多响，嘴就张多大（0.06 是经验缩放）
            try:
                rms = float(np.sqrt(np.mean(block ** 2)))
                AVATAR.sendto(f"{min(1.0, rms * 6.0):.3f}".encode(), ("127.0.0.1", int(setting("services.avatar_udp_port"))))
            except Exception:  # noqa: BLE001
                pass
        return INTERRUPT.is_set()

    def say_streaming(self, text: str, on_first_audio=None) -> bool:
        """分句合成 + 边收边播。返回 True 表示被打断。

        2026-10-23：改成流式（streaming_mode=3 / raw PCM），第一块音频到达就开播，
        不用等整句合成完。首字里的"合成"从 ~1.3 s 降到 ~0.3 s。
        """
        global PLAY_START
        parts = split_sentences(text)
        q_app: queue.Queue = queue.Queue()

        def worker():
            for p in parts:
                if INTERRUPT.is_set():
                    break
                try:
                    params = dict(TTS_PARAMS, text=p)
                    if TTS_STREAM:
                        params.update({"streaming_mode": 3, "media_type": "raw",
                                       "batch_size": 1})
                        with TTS_CLIENT.stream("GET", TTS_API, params=params) as r:
                            if r.status_code != 200:
                                raise RuntimeError(f"HTTP {r.status_code}")
                            buf = b""
                            for piece in r.iter_raw():
                                if INTERRUPT.is_set():
                                    break
                                buf += piece
                                cut = len(buf) - (len(buf) % 2)
                                if cut < 2:
                                    continue
                                pcm = np.frombuffer(buf[:cut], dtype="<i2")
                                buf = buf[cut:]
                                q_app.put(("raw", pcm.astype(np.float32) / 32768.0))
                    else:
                        r = TTS_CLIENT.get(TTS_API, params=params)
                        if r.status_code == 200 and len(r.content) > 800:
                            q_app.put(("wav", r.content))
                except Exception as e:  # noqa: BLE001
                    print(f"  TTS 失败: {e}")
            q_app.put(None)

        PLAYING.set()
        INTERRUPT.clear()
        PLAY_START = time.time()
        threading.Thread(target=worker, daemon=True).start()

        first = True
        interrupted = False
        with sd.OutputStream(samplerate=RATE_OUT, channels=2,
                             device=self.dev, dtype="float32") as st:
            while True:
                item = q_app.get()
                if item is None:
                    break
                kind, payload = item
                if kind == "raw":
                    data, sr = payload, TTS_RAW_RATE
                else:
                    import io
                    data, sr = sf.read(io.BytesIO(payload), dtype="float32")
                if data.ndim > 1:
                    data = data.mean(axis=1)
                if not len(data):
                    continue
                if first:
                    first = False
                    if on_first_audio:
                        on_first_audio()
                if self._play_into(st, data, sr):
                    interrupted = True
                    break
        PLAYING.clear()
        return interrupted


class StreamASR:
    """边说边识别。

    实测：整段发给 paraformer-realtime，`stop()` 要等 1.3 s 才拿到句末结果，
    但音频上传本身几乎不花时间。所以把"上传"摊到她还在说话的这几秒里做，
    说完时只剩尾包要等（约 0.3 s）。识别失败不抛，交给上层退回整段识别。
    """

    def __init__(self):
        self.text = ""
        self.ok = False
        self.rec = None
        self.cb = None
        try:
            from dashscope.audio.asr import Recognition, RecognitionCallback

            outer = self

            class _CB(RecognitionCallback):
                def __init__(self):
                    self.parts: list[tuple] = []

                def on_event(self, result):
                    s = result.get_sentence()
                    if not s or not s.get("text"):
                        return
                    key = s.get("begin_time") or len(self.parts)
                    self.parts = [p for p in self.parts if p[0] != key] + [(key, s["text"])]
                    outer.text = "".join(t for _, t in sorted(self.parts, key=lambda x: x[0]))

            self.cb = _CB()
            self.rec = Recognition(model=setting("audio.asr_model"), format="pcm",
                                   sample_rate=RATE_IN, callback=self.cb)
            self.rec.start()
            self.ok = True
        except Exception as e:  # noqa: BLE001
            print(f"  [ASR] 流式会话开不起来：{e}")

    def feed(self, frame: np.ndarray) -> None:
        if not self.ok:
            return
        try:
            pcm = (np.clip(frame, -1, 1) * 32767).astype("<i2").tobytes()
            self.rec.send_audio_frame(pcm)
        except Exception:  # noqa: BLE001
            self.ok = False

    def finish(self) -> str:
        if not self.ok:
            return ""
        try:
            self.rec.stop()
        except Exception:  # noqa: BLE001
            return ""
        return (self.text or "").strip()


class _Switch(Exception):
    """内部信号：换一个端点重新开录音。"""


class Ear:
    """环回录音 + VAD。她说的时候如果检测到你开口，就置 INTERRUPT。

    两条踩过的坑：
    1. 多路混流（每 50 ms 只留最响一路）等于把语音剪掉 2/3 → ASR 恒空。
       所以这里**一次只录一路干净的流**，另有 scout 每 5 秒确认声音还在不在这一路。
    2. `rec.record()` 在设备失效时会永久阻塞（不报错、也不给数据），进程看着活着其实聋了。
       所以采集放独立线程，主循环 2 秒收不到帧就重开。
    """

    def __init__(self, on_utterance):
        self.on_utterance = on_utterance
        self.candidates = self._candidates()
        self.current = self._initial()
        self.running = False
        self.noise = 0.005
        self._switch_to = None
        self.stat_peak = 0.0
        self.stat_utts = 0
        self._last_stat = 0.0
        self._asr = None

    @staticmethod
    def _candidates() -> list[str]:
        names: list[str] = []
        try:
            names.append(sc.default_speaker().name)
        except Exception:  # noqa: BLE001
            pass
        for hint in setting("audio.listen_hints", []):
            try:
                for s in sc.all_speakers():
                    if hint in s.name and s.name not in names:
                        names.append(s.name)
            except Exception:  # noqa: BLE001
                pass
        return names

    def _initial(self) -> str:
        try:
            return sc.default_speaker().name
        except Exception:  # noqa: BLE001
            return self.candidates[0]

    @staticmethod
    def _probe() -> dict:
        """问一句：QQ 现在往哪个端点播？（pycaw 跑在独立 venv 里，不往 AstrBot 环境装包）"""
        import subprocess
        if not (os.path.exists(PROBE_PY) and os.path.exists(PROBE_SCRIPT)):
            return {}
        try:
            r = subprocess.run([PROBE_PY, PROBE_SCRIPT, "qq"],
                               capture_output=True, timeout=25)
            return json.loads(r.stdout.decode("utf-8", "replace") or "{}")
        except Exception:  # noqa: BLE001
            return {}

    def _scout(self) -> None:
        """每 5 秒确认一次对方的声音落在哪个端点。她说话时不切，免得听自己。"""
        while self.running:
            time.sleep(5)
            if PLAYING.is_set():
                continue
            info = self._probe()
            if not info:
                continue
            target = (info.get("qq") or {}).get("endpoint") or info.get("loudest")
            if not target or ("VB-Audio Virtual Cable" in target and "CABLE In" not in target):
                continue
            if target != self.current:
                print(f"  [录音] 对方的声音在 {target}，从 {self.current} 切过去")
                self._switch_to = target

    def _listen(self, name: str) -> None:
        buf, speech, silence = [], 0.0, 0.0
        pre = []
        pre_len = max(1, int(PREROLL / FRAME))
        chunk = int(RATE_IN * FRAME)
        hot = 0

        q: queue.Queue = queue.Queue(maxsize=400)
        stop = threading.Event()

        def pump() -> None:
            try:
                d = sc.get_microphone(id=name, include_loopback=True)
                with d.recorder(samplerate=RATE_IN) as rec:
                    while self.running and not stop.is_set():
                        try:
                            q.put(rec.record(numframes=chunk), timeout=1.0)
                        except queue.Full:
                            try:
                                q.get_nowait()
                            except queue.Empty:
                                pass
            except Exception as e:  # noqa: BLE001
                print(f"  [录音] {name} 采集线程退出：{e}")

        threading.Thread(target=pump, daemon=True).start()

        while self.running:
            if self._switch_to and self._switch_to != name:
                stop.set()
                raise _Switch
            try:
                frame = q.get(timeout=2.0)
            except queue.Empty:
                stop.set()
                print(f"  [录音] {name} 连续 2 秒没数据，重开录音")
                raise _Switch
            sig = frame.mean(axis=1) if frame.ndim > 1 else frame
            if not len(sig):
                continue
            rms = float(np.sqrt(np.mean(sig ** 2)))
            dur = len(sig) / RATE_IN
            if rms > self.stat_peak:
                self.stat_peak = rms

            if rms < self.noise:
                self.noise = 0.98 * self.noise + 0.02 * rms
            thr = max(THRESHOLD_FLOOR, self.noise * 2.2)

            # 打断：要「明显比噪声大 + 持续 0.3 秒 + 她已经开口 0.6 秒以上」，
            # 否则环境里一声咳嗽、键盘敲一下都会把她打断。
            if (PLAYING.is_set()
                    and time.time() - PLAY_START > 0.6
                    and rms > max(thr * INTERRUPT_RATIO, 0.020)):
                hot += 1
                if hot >= 6:
                    INTERRUPT.set()
                    hot = 0
            else:
                hot = 0

            if rms > thr:
                if speech == 0.0:
                    buf = list(pre)
                    asr = StreamASR()
                    for f in pre:                 # 前置缓冲也补进去，别漏首字
                        asr.feed(f)
                    self._asr = asr
                    asr.feed(sig)
                else:
                    if self._asr:
                        self._asr.feed(sig)
                buf.append(sig.copy())
                speech += dur
                silence = 0.0
            elif speech > 0:
                buf.append(sig.copy())
                silence += dur
                if silence >= SILENCE_END:
                    if speech >= MIN_SPEECH and buf:
                        audio = np.concatenate(buf)
                        text = self._asr.finish() if self._asr else ""
                        self._asr = None
                        self.stat_utts += 1
                        threading.Thread(target=self.on_utterance,
                                         args=(audio, text), daemon=True).start()
                    buf, speech, silence = [], 0.0, 0.0
            if speech > MAX_UTTER:
                if buf:
                    audio = np.concatenate(buf)
                    text = self._asr.finish() if self._asr else ""
                    self._asr = None
                    self.stat_utts += 1
                    threading.Thread(target=self.on_utterance,
                                     args=(audio, text), daemon=True).start()
                buf, speech, silence = [], 0.0, 0.0

            pre.append(sig.copy())
            if len(pre) > pre_len:
                pre.pop(0)

            if time.time() - self._last_stat > 15:
                self._last_stat = time.time()
                print(f"  [{time.strftime('%H:%M:%S')}] [录音] {name}"
                      f"  近15秒峰值={self.stat_peak:.4f} 噪声底={self.noise:.4f}"
                      f" 切段={self.stat_utts}")
                self.stat_peak = 0.0
                self.stat_utts = 0
                try:
                    with open(HEARTBEAT, "w", encoding="utf-8") as hb:
                        hb.write(f"{time.time():.0f} {name}")
                except Exception:  # noqa: BLE001
                    pass

    def run(self) -> None:
        self.running = True
        print(f"  [录音] 监听端点：{self.current}"
              f"（备用：{[c for c in self.candidates if c != self.current]}）")
        threading.Thread(target=self._scout, daemon=True).start()
        while self.running:
            if self._switch_to:
                self.current, self._switch_to = self._switch_to, None
                self.noise = 0.005
            try:
                self._listen(self.current)
            except _Switch:
                continue
            except Exception as e:  # noqa: BLE001
                print(f"  [录音] {self.current} 出错：{e}")
                time.sleep(1.0)


def grab_screen_b64() -> str:
    """截当前屏幕，缩到 1280 宽压 jpg。"""
    from PIL import ImageGrab
    img = ImageGrab.grab()
    img = img.resize((1280, int(img.height * 1280 / img.width)))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=70)
    return base64.b64encode(buf.getvalue()).decode()


def wants_vision(text: str) -> bool:
    return any(w in text for w in SEEWORDS)


def shorten_for_phone(text: str, key: str, base: str) -> str:
    """电话里不能念小作文。

    AstrBot 那条路（webchat）用的是库里的人设，**不带 PHONE_MODE**，
    她一张口就是 100 多字、播 29 秒 —— 电话里没法听。这里再压一道：
    超过 45 字就改写成一句 30 字以内；改写失败就截到第一个句号。
    """
    t = (text or "").strip()
    if len(t) <= 45:
        return t
    try:
        with httpx.Client(timeout=12, trust_env=False) as cli:
            r = cli.post(f"{base.rstrip('/')}/chat/completions",
                         json={"model": setting("llm.chat.model"), "temperature": 0.6,
                               "max_tokens": 80,
                               "messages": [
                                   {"role": "system",
                                    "content": "把下面这句话改写成电话里说的**一句**中文"
                                               "（不超过 30 字），保留原意和她的语气，"
                                               "不要换行、不要解释，只输出这一句。"},
                                   {"role": "user", "content": t}]},
                         headers={"Authorization": f"Bearer {key}"})
            out = (r.json()["choices"][0]["message"].get("content") or "").strip()
        out = out.replace("\n", " ").strip().strip("“”\"")
        if 0 < len(out) <= 60:
            return out
    except Exception as e:  # noqa: BLE001
        print(f"  [缩短失败] {e}")
    for sep in ("。", "？", "！", "\n"):
        i = t.find(sep)
        if 0 < i <= 60:
            return t[:i + 1]
    return t[:60]


class Brain:
    def __init__(self, key, base, persona, vision_key=""):
        self.key, self.base, self.persona = key, base.rstrip("/"), persona
        self.vision_key = vision_key
        self.hist: list[dict] = []
        # 优先走 AstrBot（她的人设、长期记忆、B站等工具都在那边）
        # AstrBot 加载比本脚本慢，所以连不上就后台重试，一好就切过去
        # 电话默认直连模型：实测 ttft 0.1~1.0 s（AstrBot 那条 agent 链路要 3~5 s，
        # 而且 webchat 不带 PHONE_MODE，她会在电话里念小作文）。桥接只做兜底。
        self.bridge = None
        if PHONE_USE_BRIDGE:
            self._try_bridge()
        if PHONE_USE_BRIDGE and self.bridge is None and AstrBotBridge is not None:
            threading.Thread(target=self._bridge_retry, daemon=True).start()

    def _try_bridge(self) -> bool:
        if AstrBotBridge is None:
            return False
        try:
            b = AstrBotBridge()
            if b.alive() and b.login() and b.new_session():
                self.bridge = b
                print("已接入 AstrBot（人设/记忆/工具全带上）")
                return True
        except Exception as e:  # noqa: BLE001
            print(f"  连 AstrBot 失败({e})")
        return False

    def _bridge_retry(self):
        print("AstrBot 还没起来，先直连 DeepSeek，后台每 8 秒重试")
        for _ in range(45):
            time.sleep(8)
            if self.bridge is not None:
                return
            if self._try_bridge():
                print("AstrBot 已就绪，后续对话自动切过去")
                return

    def think(self, text: str, image_b64: str | None = None) -> str:
        # 带图的那一轮必须走视觉模型，AstrBot 这条路不支持图片，直接跳过
        if self.bridge is not None and image_b64 is None:
            try:
                reply = self.bridge.chat(text)
                if reply:
                    return shorten_for_phone(reply, self.key, self.base)
            except Exception as e:  # noqa: BLE001
                print(f"  AstrBot 出错({e})，回退直连")
        self.hist.append({"role": "user", "content": text})
        if image_b64 and self.vision_key:
            # 这一轮要看图：走视觉模型，把屏幕截图和话一起给它
            content = [
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                {"type": "text", "text": text},
            ]
            msgs = [{"role": "system", "content": self.persona}] + self.hist[:-1][-6:] + \
                   [{"role": "user", "content": content}]
            with httpx.Client(timeout=90, trust_env=False) as cli:
                r = cli.post(VISION_API,
                             json={"model": VISION_MODEL, "messages": msgs},
                             headers={"Authorization": f"Bearer {self.vision_key}"})
                d = r.json()
            reply = d["choices"][0]["message"].get("content", "").strip()
        else:
            msgs = [{"role": "system", "content": self.persona}] + self.hist[-8:]
            with httpx.Client(timeout=60, trust_env=False) as cli:
                r = cli.post(f"{self.base}/chat/completions",
                             json={"model": setting("llm.chat.model"), "messages": msgs,
                                   "temperature": 0.85},
                             headers={"Authorization": f"Bearer {self.key}"})
                d = r.json()
            reply = d["choices"][0]["message"].get("content", "").strip()
        self.hist.append({"role": "assistant", "content": reply})
        return reply


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    cfg = load_cfg()
    ds_key, ds_base, ali_key = get_keys(cfg)
    persona = get_persona()

    import dashscope
    from dashscope.audio.asr import Recognition, RecognitionCallback
    dashscope.api_key = ali_key

    speaker = Speaker()
    brain = Brain(ds_key, ds_base, persona, vision_key=ali_key)
    q: queue.Queue = queue.Queue()
    ear = Ear(lambda a, t: q.put((a, t)))
    threading.Thread(target=ear.run, daemon=True).start()
    print(f"人设 {len(persona)} 字（含电话模式）")
    print(f"[{time.strftime('%H:%M:%S')}] === 已在听 ===")

    def transcribe(audio: np.ndarray) -> str:
        pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()

        class CB(RecognitionCallback):
            def __init__(self):
                self.text = ""

            def on_event(self, result):
                s = result.get_sentence()
                if s and s.get("text"):
                    self.text = s["text"]

        cb = CB()
        rec = Recognition(model=setting("audio.asr_model"), format="pcm",
                          sample_rate=RATE_IN, callback=cb)
        rec.start()
        for i in range(0, len(pcm), 3200):
            rec.send_audio_frame(pcm[i:i + 3200])
        rec.stop()
        return cb.text.strip()

    try:
        while True:
            try:
                audio, text0 = q.get(timeout=1)
            except queue.Empty:
                continue
            t0 = time.time()
            dur = len(audio) / RATE_IN
            peak = float(np.sqrt(np.mean(audio ** 2))) if len(audio) else 0.0
            # 系统音量一旦被调小（2026-10-23 实测 JBL 只有 4%），环回拿到的声音
            # 峰值只剩 0.02，ASR 一律识别为空。这里做一次归一化，别把音量当成前提。
            amp = float(np.max(np.abs(audio))) if len(audio) else 0.0
            gain = 1.0
            if amp > 0.001:
                gain = min(8.0, 0.6 / amp)
                if gain > 1.2:
                    audio = np.clip(audio * gain, -1.0, 1.0)
            # 边录边识别：VAD 期间已经把音频喂给 paraformer 了，这里通常只剩尾包
            text = text0 or transcribe(audio)
            t_asr = time.time() - t0

            # 识别这一两秒里他又说了几句 → 说明刚才那句还没说完。
            # 合并后再识别一次，避免"听一半就抢答"，也避免同一句被回两遍。
            extra = []
            while True:
                try:
                    a2, _t2 = q.get_nowait()
                except queue.Empty:
                    break
                extra.append(a2)
            if extra:
                merged = np.concatenate([audio] + extra)
                if len(merged) / RATE_IN <= MAX_UTTER * 2:
                    amp2 = float(np.max(np.abs(merged)))
                    if amp2 > 0.001:
                        g2 = min(8.0, 0.6 / amp2)
                        if g2 > 1.2:
                            merged = np.clip(merged * g2, -1.0, 1.0)
                    t1 = time.time()
                    text2 = transcribe(merged)
                    print(f"  [{time.strftime('%H:%M:%S')}] 接上了后面 {len(extra)} 段"
                          f"（合并 {len(merged)/RATE_IN:.1f}s，ASR {time.time()-t1:.1f}s）")
                    if text2:
                        text = text2
                        dur = len(merged) / RATE_IN

            if not text:
                print(f"  [{time.strftime('%H:%M:%S')}] [没听清] ASR {t_asr:.1f}s"
                      f"｜音频 {dur:.1f}s 峰值 {peak:.4f} 增益 {gain:.1f}x")
                continue
            print(f"\n[{time.strftime('%H:%M:%S')}] 你：{text}"
                  f"  （音频 {dur:.1f}s 峰值 {peak:.3f}）")
            t1 = time.time()
            img = grab_screen_b64() if wants_vision(text) else None
            if img:
                print("  [看了一眼屏幕]")
            reply = brain.think(text, img)
            t_llm = time.time() - t1
            print(f"[{time.strftime('%H:%M:%S')}] 她：{reply}")

            t_play = time.time()
            first_at = []

            def mark():
                first_at.append(time.time() - t0)

            interrupted = speaker.say_streaming(reply, on_first_audio=mark)
            print(f"  （首字 {first_at[0]:.1f}s ＝ 识别 {t_asr:.1f} + 思考 {t_llm:.1f} + 合成"
                  f"｜播完 {time.time()-t_play:.1f}s"
                  f"｜{'被打断' if interrupted else '说完'}）")
            if args.once:
                break
    except KeyboardInterrupt:
        pass
    finally:
        ear.running = False


if __name__ == "__main__":
    main()
