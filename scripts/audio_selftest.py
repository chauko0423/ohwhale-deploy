"""控制实验：放一段已知的响声，验证「环回录音」这条路本身通不通。

如果放声时环回还是 0，说明问题在采集侧，不在 QQ/通话侧。
"""
from __future__ import annotations

import math
import os
import struct
import tempfile
import threading
import time
import wave

import numpy as np
import soundcard as sc

RATE = 48000
SECONDS = 3.0
CHUNK = int(RATE * 0.05)
WAV = os.path.join(tempfile.gettempdir(), "probe_tone.wav")


def make_tone() -> None:
    with wave.open(WAV, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        frames = bytearray()
        for i in range(int(RATE * SECONDS)):
            v = int(12000 * math.sin(2 * math.pi * 440 * i / RATE))
            frames += struct.pack("<h", v)
        w.writeframes(bytes(frames))


def record(dev, out: dict) -> None:
    peak = 0.0
    try:
        with dev.recorder(samplerate=RATE) as rec:
            t_end = time.time() + SECONDS + 1
            while time.time() < t_end:
                buf = rec.record(numframes=CHUNK)
                sig = buf.mean(axis=1) if buf.ndim > 1 else buf
                peak = max(peak, float(np.sqrt(np.mean(sig ** 2))))
    except Exception as e:  # noqa: BLE001
        out["err"] = str(e)[:60]
    out["peak"] = peak


def main() -> None:
    make_tone()
    dev = sc.get_microphone(id=sc.default_speaker().name, include_loopback=True)
    print("监听端点：", dev.name)
    out: dict = {"peak": 0.0, "err": ""}
    t = threading.Thread(target=record, args=(dev, out), daemon=True)
    t.start()
    time.sleep(0.4)
    print("放 440Hz 正弦，3 秒…")
    import winsound
    winsound.PlaySound(WAV, winsound.SND_FILENAME)
    t.join(timeout=SECONDS + 6)
    print(f"环回峰值 rms = {out['peak']:.5f}  {out.get('err','')}")
    print("判定：", "采集正常" if out["peak"] > 0.01 else "采集侧有问题（放声都收不到）")


if __name__ == "__main__":
    main()
