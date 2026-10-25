"""9880 端口上的 GPT-SoVITS 兼容 TTS 服务，后端换成阿里云百炼 CosyVoice。

为什么要这层：OhWhale 的 voice_picker / call_loop / AstrBot gsv 提供商都按
GPT-SoVITS 的 HTTP 约定调用 ``http://127.0.0.1:9880/tts``（GET 查询串或
POST JSON，返回 wav 字节）。本脚本实现同一接口，把请求转给 CosyVoice，
因此换音色只要改环境变量，不用动上层任何代码。

用法：
    python scripts/tts_cosyvoice_shim.py [--port 9880] [--voice longfeifei_v2]

环境变量：
    COSY_TTS_MODEL   默认 cosyvoice-v2
    COSY_TTS_VOICE   默认 longfeifei_v2（龙菲菲·甜美娇气女）
    COSY_TTS_RATE    语速倍率，默认 1.0
    COSY_TTS_PITCH   音调倍率，默认 1.0
    DASHSCOPE_API_KEY 从项目 .env 读取（也支持进程环境变量）

契约（与 GPT-SoVITS api_v2 对齐）：
    GET  /tts?text=...&text_lang=zh&media_type=wav
    POST /tts   {"text": "...", ...}
    返回 200 + audio/wav 字节；失败 500 + 纯文本错误。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_env() -> None:
    """读取项目 .env 里的 DASHSCOPE_API_KEY（不覆盖已有环境变量）。"""
    if os.environ.get("DASHSCOPE_API_KEY"):
        return
    env_file = ROOT / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "DASHSCOPE_API_KEY" and value.strip():
            os.environ["DASHSCOPE_API_KEY"] = value.strip()
            return


def fix_wav_sizes(wav: bytes) -> bytes:
    """CosyVoice 返回的 WAV 长度字段是占位符（RIFF/data 都是 0x7FFFFFxx）。

    soundfile / ffmpeg 能播，但时长元数据是错的（有些调用方会按 nframes 判断
    是否播放），这里按真实字节数改写 RIFF 和 data 两个长度字段。
    """
    if len(wav) < 44 or wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
        return wav
    import struct

    out = bytearray(wav)
    struct.pack_into("<I", out, 4, len(out) - 8)          # RIFF chunk size
    pos = 12
    while pos + 8 <= len(out):
        chunk_id = bytes(out[pos:pos + 4])
        chunk_size = struct.unpack_from("<I", out, pos + 4)[0]
        if chunk_id == b"data":
            struct.pack_into("<I", out, pos + 4, len(out) - pos - 8)
            break
        pos += 8 + chunk_size + (chunk_size & 1)
    return bytes(out)


def _decode(raw: bytes) -> str:
    """请求体按 UTF-8 解码，失败退 GBK（旧脚本/控制台可能不是 UTF-8）。"""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


BACKUP_TTS_URL = os.environ.get("COSY_TTS_BACKUP", "http://127.0.0.1:9881")
# call_loop 把 raw 响应按 config.audio.tts_raw_rate（默认 32000）解释，
# 再由它重采样到 48000 播放。这里必须输出同样的 32000，否则会被当成
# 48k→重采样→播放慢 1.5 倍，听起来就是"很粗的男人声音"。
RAW_TARGET_RATE = int(os.environ.get("COSY_TTS_RAW_RATE", "32000"))


def wav_to_raw_pcm(wav: bytes, target_sr: int = 48000) -> bytes:
    """把 wav 转成目标采样率的 16 bit 单声道裸 PCM。

    call_loop 的流式播放按 ``streaming_mode=3 / media_type=raw`` 请求，
    把响应体直接当 PCM（RATE_OUT=48000）播放；如果这里回 wav，
    RIFF 头会被当成采样点播出来（爆音），而且 24k 当 48k 播会快一倍、
    变成尖细的机器音。所以必须按它要的格式回。
    """
    import io as _io

    import numpy as np

    with _io.BytesIO(wav) as buf:
        import wave as _wave
        with _wave.open(buf, "rb") as wf:
            src_sr = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
    pcm = np.frombuffer(frames, dtype="<i2").astype(np.float32)
    if len(pcm):
        # 备份（GPT-SoVITS）输出常常很轻，峰值只有 0.01~0.05；
        # 顺带做一次归一化，避免电话里听不清。
        peak = float(np.abs(pcm).max()) / 32768.0
        if 0 < peak < 0.15:
            pcm = pcm * min(6.0, 0.6 / peak)
    if src_sr != target_sr and len(pcm) > 1:
        n = int(round(len(pcm) * target_sr / src_sr))
        idx = np.linspace(0, len(pcm) - 1, n)
        pcm = np.interp(idx, np.arange(len(pcm)), pcm)
    return np.clip(pcm, -32768, 32767).astype("<i2").tobytes()


class Config:
    model = os.environ.get("COSY_TTS_MODEL", "cosyvoice-v2")
    voice = os.environ.get("COSY_TTS_VOICE", "longfeifei_v2")
    rate = float(os.environ.get("COSY_TTS_RATE", "1.0"))
    pitch = float(os.environ.get("COSY_TTS_PITCH", "1.0"))


_lock = threading.Lock()


def synthesize(text: str) -> bytes:
    """调用 CosyVoice 合成 wav 字节。

    到百炼的 WebSocket 偶发 5s 建连超时（实测约 10~20% 概率），
    这里每次重新建连接并重试 3 次；仍失败才把错误抛给上层。
    """
    import time
    from dashscope.audio.tts_v2 import AudioFormat, SpeechSynthesizer

    last: Exception | None = None
    with _lock:
        for attempt in range(4):
            try:
                synth = SpeechSynthesizer(
                    model=Config.model,
                    voice=Config.voice,
                    format=AudioFormat.WAV_24000HZ_MONO_16BIT,
                    speech_rate=Config.rate,
                    pitch_rate=Config.pitch,
                )
                data = synth.call(text)
                if not data:
                    raise RuntimeError("CosyVoice returned empty audio")
                return fix_wav_sizes(data)
            except Exception as exc:  # 建连超时通常重试即好
                last = exc
                time.sleep(0.25 + attempt * 0.35)
    raise RuntimeError(f"CosyVoice 连续 4 次失败: {last}")


def _force_wav_query(query: str) -> str:
    """把备份请求强制改成 media_type=wav、非流式，方便自己按目标采样率转码。

    GPT-SoVITS api_v2 直出 raw 时是它自己的采样率（v2Pro 是 32000），
    而 call_loop 一律按 48000 播放——直接转发会变成慢速低音（听起来像粗男人）。
    """
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    kept = [(k, v) for k, v in pairs if k not in ("media_type", "streaming_mode")]
    kept += [("media_type", "wav"), ("streaming_mode", "0")]
    return urllib.parse.urlencode(kept)


def proxy_backup(path_query: str, method: str, body: bytes | None) -> tuple[int, bytes, str]:
    """CosyVoice 连续失败时，把原始请求转给本地 GPT-SoVITS 备份（9881）。

    原请求里的 ref_audio_path / prompt_text / streaming_mode / media_type 原样转发，
    所以备份按它自己的约定返回（wav 或 raw 分块），上层无需感知音色切换。
    """
    import urllib.request
    url = BACKUP_TTS_URL.rstrip("/") + "/tts" + (("?" + path_query) if path_query else "")
    req = urllib.request.Request(url, data=body, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read()
        return resp.status, data, resp.headers.get("Content-Type", "audio/wav")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    _raw_query = ""
    _raw_body = None

    def log_message(self, fmt: str, *args) -> None:  # 精简访问日志
        sys.stdout.write(f"[tts-shim] {self.address_string()} {fmt % args}\n")
        sys.stdout.flush()

    def _reply(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, text: str, extra: dict) -> None:
        text = (text or "").strip()
        if not text:
            self._reply(400, b"text is required", "text/plain; charset=utf-8")
            return
        raw_mode = False
        try:
            mts = extra.get("media_type") or []
            sts = extra.get("streaming_mode") or []
            if isinstance(mts, str):
                mts = [mts]
            if isinstance(sts, str):
                sts = [sts]
            raw_mode = ("raw" in [str(x).lower() for x in mts]
                        or any(str(x) == "3" for x in sts))
        except Exception:  # noqa: BLE001
            raw_mode = False
        try:
            wav = synthesize(text)
            if raw_mode:
                audio = wav_to_raw_pcm(wav, RAW_TARGET_RATE)
                self._reply(200, audio, "application/octet-stream")
                return
        except Exception as exc:  # CosyVoice 不可用 -> 本地 GPT-SoVITS 兜底
            try:
                status, data, ctype = proxy_backup(
                    _force_wav_query(self._raw_query), self.command, None)
                if status == 200 and data:
                    print(f"[tts-shim] CosyVoice 失败，已用备份后端: {exc}", flush=True)
                    if raw_mode:
                        self._reply(200, wav_to_raw_pcm(data, RAW_TARGET_RATE),
                                    "application/octet-stream")
                    else:
                        self._reply(200, data, ctype or "audio/wav")
                    return
            except Exception as exc2:  # noqa: BLE001
                print(f"[tts-shim] 备份后端也失败: {exc2}", flush=True)
            self._reply(500, f"tts failed: {exc}".encode("utf-8"),
                        "text/plain; charset=utf-8")
            return
        self._reply(200, wav, "audio/wav")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path.rstrip("/") not in ("/tts", ""):
            self._reply(404, b"not found", "text/plain; charset=utf-8")
            return
        query = urllib.parse.parse_qs(parsed.query)
        self._raw_query = parsed.query
        self._raw_body = None
        self._handle(query.get("text", [""])[0], query)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path.rstrip("/") != "/tts":
            self._reply(404, b"not found", "text/plain; charset=utf-8")
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        self._raw_query = parsed.query
        self._raw_body = raw
        text = ""
        extra: dict = {}
        ctype = (self.headers.get("Content-Type") or "").lower()
        try:
            if "json" in ctype:
                payload = json.loads(_decode(raw) or "{}")
                text = str(payload.get("text", ""))
                extra = payload
            else:
                query = urllib.parse.parse_qs(_decode(raw))
                text = query.get("text", [""])[0]
                extra = query
        except Exception as exc:
            self._reply(400, f"bad request: {exc}".encode("utf-8"),
                        "text/plain; charset=utf-8")
            return
        self._handle(text, extra)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9880)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--voice", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--rate", type=float, default=None)
    parser.add_argument("--pitch", type=float, default=None)
    args = parser.parse_args()

    load_env()
    if not os.environ.get("DASHSCOPE_API_KEY"):
        print("[tts-shim] 缺少 DASHSCOPE_API_KEY（.env 或环境变量）", flush=True)
        return 1
    if args.voice:
        Config.voice = args.voice
    if args.model:
        Config.model = args.model
    if args.rate is not None:
        Config.rate = args.rate
    if args.pitch is not None:
        Config.pitch = args.pitch

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[tts-shim] listening {args.host}:{args.port} "
          f"model={Config.model} voice={Config.voice} "
          f"rate={Config.rate} pitch={Config.pitch}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
