"""Synthesize on this GPU without QQ login, credentials or an audio device."""
from __future__ import annotations

import array
import io
import json
import os
import socket
import subprocess
import time
import wave
from pathlib import Path

import httpx

from .config import get, path, root


def audio_stats(pcm: bytes, rate: int) -> dict:
    if len(pcm) < 2 or len(pcm) % 2:
        raise ValueError("Incomplete PCM audio")
    samples = array.array("h", pcm)
    peak = max(abs(x) for x in samples)
    duration = len(samples) / rate
    if peak < 10 or duration < 0.5:
        raise ValueError("Audio is silent or unexpectedly short")
    return {"audio_duration_s": round(duration, 3), "sample_rate": rate, "peak": peak}


def main():
    repo = root()
    data = repo / "data"
    data.mkdir(exist_ok=True)
    result = {"schema": 1, "scope": "this-machine-tts-only", "ok": False,
              "full_acceptance_passed": False, "listening_review": False,
              "qq_call_latency_measured": False, "streaming_trials": []}
    process = None
    log = None
    try:
        # Refuse to operate on another installation through custom path overrides.
        for key in ("tts_python", "gpt_sovits", "gpt_weights", "sovits_weights", "pretrained_dir", "voice_ref"):
            if not Path(path(key)).resolve().is_relative_to(repo):
                raise ValueError(f"Smoke-test path must be inside this installation: {key}")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        config = {"custom": {"device": "cuda", "is_half": True, "version": "v2Pro",
                  "t2s_weights_path": path("gpt_weights"), "vits_weights_path": path("sovits_weights"),
                  "bert_base_path": str(Path(path("pretrained_dir")) / "chinese-roberta-wwm-ext-large"),
                  "cnhuhbert_base_path": str(Path(path("pretrained_dir")) / "chinese-hubert-base")}}
        config_path = data / "tts-smoke.yaml"
        config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        gpu = subprocess.run([path("tts_python"), "-c",
            "import torch,json; print(json.dumps(dict(torch=torch.__version__,cuda=torch.cuda.is_available(),gpu=torch.cuda.get_device_name(0),vram_bytes=torch.cuda.get_device_properties(0).total_memory)))"],
            capture_output=True, text=True, encoding="utf-8", env=env, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if gpu.returncode:
            raise RuntimeError("CUDA probe failed: " + gpu.stderr[-2000:])
        result["hardware"] = json.loads(gpu.stdout)
        log = (data / "tts-smoke.log").open("wb")
        started = time.monotonic()
        process = subprocess.Popen([path("tts_python"), "api_v2.py", "-a", "127.0.0.1",
            "-p", str(port), "-c", str(config_path)], cwd=path("gpt_sovits"), env=env,
            stdout=log, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        url = f"http://127.0.0.1:{port}"
        with httpx.Client(timeout=240, trust_env=False) as client:
            for _ in range(300):
                if process.poll() is not None:
                    raise RuntimeError(f"TTS exited ({process.returncode}); see data/tts-smoke.log")
                try:
                    if client.get(url + "/docs", timeout=2).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(1)
            else:
                raise TimeoutError("TTS did not become ready within 300 seconds")
            result["startup_s"] = round(time.monotonic() - started, 3)
            base = {"text_lang": "zh", "ref_audio_path": path("voice_ref"),
                    "prompt_text": get("voice.prompt_text"), "prompt_lang": "zh"}
            before = time.monotonic()
            response = client.post(url + "/tts", json=dict(base,
                text="你好，这里是独立部署环境的语音测试。我们再说第二句话，检查音频是否完整。",
                text_split_method="cut5", media_type="wav", streaming_mode=False,
                temperature=0.7, top_p=0.9))
            response.raise_for_status()
            elapsed = time.monotonic() - before
            with wave.open(io.BytesIO(response.content)) as wav:
                if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                    raise ValueError("Unexpected WAV format")
                stats = audio_stats(wav.readframes(wav.getnframes()), wav.getframerate())
            (data / "tts-smoke.wav").write_bytes(response.content)
            result["cold_nonstreaming"] = dict(stats, total_s=round(elapsed, 3))
            for number, text in enumerate(("你好，我已经准备好了，我们开始聊天吧。",
                    "今天的天气怎么样？出门的时候记得带上水。",
                    "我们来检查一下语音是否连贯，每一句话能不能说完整。"), 1):
                before = time.monotonic()
                first = None
                chunks = []
                with client.stream("GET", url + "/tts", params=dict(base, text=text,
                        text_split_method="cut0", streaming_mode=3, media_type="raw", batch_size=1)) as response:
                    response.raise_for_status()
                    for chunk in response.iter_raw():
                        if chunk:
                            if first is None:
                                first = time.monotonic() - before
                            chunks.append(chunk)
                elapsed = time.monotonic() - before
                pcm = b"".join(chunks)
                stats = audio_stats(pcm, 32000)
                with wave.open(str(data / f"tts-stream-{number}.wav"), "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(32000)
                    wav.writeframes(pcm)
                result["streaming_trials"].append(dict(stats, trial=number,
                    first_pcm_s=round(first, 3), total_s=round(elapsed, 3), chunks=len(chunks)))
            result["ok"] = True
    except Exception as exc:
        result["error"] = str(exc)
    finally:
        # Only terminate the exact subprocess launched by this test.
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if log is not None:
            log.close()
        report = json.dumps(result, ensure_ascii=False, indent=2)
        (data / "tts-smoke.json").write_text(report, encoding="utf-8")
        print(report, flush=True)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
