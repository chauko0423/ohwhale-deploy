"""在打电话的时候跑：同时盯所有环回端点，看哪一路真的有声音。

用法（保持通话，然后跑）：
  python audio_probe.py 8
"""
from __future__ import annotations

import sys
import threading
import time

import numpy as np
import soundcard as sc

SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0
RATE = 48000
CHUNK = int(RATE * 0.05)

stats: dict[str, dict] = {}
lock = threading.Lock()


def watch(dev) -> None:
    key = dev.name
    peak = 0.0
    frames = 0
    try:
        with dev.recorder(samplerate=RATE) as rec:
            t_end = time.time() + SECONDS
            while time.time() < t_end:
                buf = rec.record(numframes=CHUNK)
                sig = buf.mean(axis=1) if buf.ndim > 1 else buf
                r = float(np.sqrt(np.mean(sig ** 2)))
                peak = max(peak, r)
                frames += 1
    except Exception as e:  # noqa: BLE001
        with lock:
            stats[key] = {"err": str(e)[:60], "peak": 0.0, "frames": 0}
        return
    with lock:
        stats[key] = {"err": "", "peak": peak, "frames": frames}


def main() -> None:
    devs = [m for m in sc.all_microphones(include_loopback=True)
            if getattr(m, "isloopback", False)]
    print(f"默认扬声器：{sc.default_speaker().name}")
    print(f"并行监听 {len(devs)} 个环回端点，{SECONDS:.0f} 秒…请继续说话\n")
    ths = [threading.Thread(target=watch, args=(d,), daemon=True) for d in devs]
    for d, t in zip(devs, ths):
        stats[d.name] = {"err": "", "peak": 0.0, "frames": 0}
    for t in ths:
        t.start()
    for t in ths:
        t.join(timeout=SECONDS + 6)

    print(f"{'峰值 rms':>10}  {'帧数':>6}  端点")
    for name, s in sorted(stats.items(), key=lambda kv: -kv[1]["peak"]):
        bar = "#" * min(40, int(s["peak"] * 400))
        err = f"  [{s['err']}]" if s["err"] else ""
        print(f"{s['peak']:>10.5f}  {s['frames']:>6}  {name} {bar}{err}")


if __name__ == "__main__":
    main()
