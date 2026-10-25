"""本地 ASR 服务（faster-whisper，离线，不走网络）。

背景：DashScope 的语音识别今天频繁卡 40 秒（WebSocket 建连 + 文件上传都挂），
call_loop 的超时兜底再好也只能"少等"，不能"快回"。这里用本地 whisper 常驻
提供 HTTP 接口，识别 2~5 秒的短句一般 1 秒内返回，网络波动完全无关。

用法（用 .venv-video 启动，模型只加载一次）：
    .venv-video\\Scripts\\python.exe scripts/asr_local_server.py --port 9890 --model small

接口：
    POST /asr   请求体 = 16kHz/16bit/单声道 WAV 或裸 PCM
                ?rate=16000&format=wav|pcm
    返回 JSON {"text": "..."}
    GET  /health -> {"ok": true, "model": "..."}
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np


class Config:
    model_name = "small"
    device = "cpu"
    compute_type = "int8"


_model = None
_model_lock = threading.Lock()


def get_model():
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel
            _model = WhisperModel(Config.model_name, device=Config.device,
                                  compute_type=Config.compute_type)
        return _model


def decode(body: bytes, fmt: str, rate: int) -> np.ndarray:
    if fmt == "wav":
        with wave.open(io.BytesIO(body), "rb") as wf:
            src_rate = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
        audio = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    else:
        src_rate = rate
        audio = np.frombuffer(body, dtype="<i2").astype(np.float32) / 32768.0
    if src_rate != 16000 and len(audio) > 1:
        n = int(round(len(audio) * 16000 / src_rate))
        audio = np.interp(np.linspace(0, len(audio) - 1, n),
                          np.arange(len(audio)), audio).astype(np.float32)
    return audio


def transcribe(audio: np.ndarray) -> str:
    model = get_model()
    segments, _info = model.transcribe(audio, language="zh", vad_filter=False,
                                       beam_size=1, condition_on_previous_text=False)
    return "".join(s.text for s in segments).strip()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        sys.stdout.write(f"[asr-local] {fmt % args}\n")
        sys.stdout.flush()

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/health"):
            self._json(200, {"ok": True, "model": Config.model_name})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.startswith("/asr"):
            self._json(404, {"error": "not found"})
            return
        from urllib.parse import parse_qs, urlsplit
        query = parse_qs(urlsplit(self.path).query)
        fmt = (query.get("format") or ["wav"])[0]
        rate = int((query.get("rate") or ["16000"])[0])
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if not body:
            self._json(400, {"error": "empty body"})
            return
        try:
            text = transcribe(decode(body, fmt, rate))
            self._json(200, {"text": text})
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9890)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--model", default="small")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--compute-type", default="int8")
    args = parser.parse_args()
    Config.model_name = args.model
    Config.device = args.device
    Config.compute_type = args.compute_type

    print(f"[asr-local] 加载模型 {Config.model_name} ...", flush=True)
    get_model()
    print(f"[asr-local] 就绪，监听 {args.host}:{args.port}", flush=True)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
