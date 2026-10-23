"""通话时盯着看：QQ 到底开了哪个麦克风，它收到的电平有多大。

用途：她的话（TTS）走的是 VB-Cable，如果 QQ 的麦克风不是 CABLE Output，
对方就一个字都听不到 —— 而这件事只有"通话进行中"才看得出来。

用法：
  python audio_watch.py 240     # 盯 4 分钟，结果写 logs/audio_watch.log
"""
from __future__ import annotations

import os
import sys
import time

import psutil
from comtypes import CoInitialize, CoUninitialize
from pycaw.pycaw import AudioUtilities, IAudioMeterInformation, IAudioSessionControl2

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "audio_watch.log")
DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 240.0
STATE = {0: "inactive", 1: "active", 2: "expired"}


def snapshot() -> tuple[list[str], dict]:
    """返回（QQ 所在的录音端点列表, 各端点 QQ 峰值）"""
    caps, peaks = [], {}
    for d in AudioUtilities.GetAllDevices():
        if "Active" not in str(d.state):
            continue
        is_cap = str(d.id).startswith("{0.0.1.")
        try:
            se = d.AudioSessionManager.GetSessionEnumerator()
        except Exception:  # noqa: BLE001
            continue
        for k in range(se.GetCount()):
            ctl = se.GetSession(k)
            c2 = ctl.QueryInterface(IAudioSessionControl2)
            pid = c2.GetProcessId()
            if not pid:
                continue
            try:
                pn = psutil.Process(pid).name()
            except Exception:
                continue
            if "qq" not in pn.lower():
                continue
            pk = ctl.QueryInterface(IAudioMeterInformation).GetPeakValue()
            tag = ("录音" if is_cap else "播放")
            key = f"{tag} · {d.FriendlyName}"
            peaks[key] = max(peaks.get(key, 0.0), pk)
            if is_cap:
                caps.append(key)
    return caps, peaks


def main() -> None:
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    CoInitialize()
    t_end = time.time() + DURATION
    last_sig = None
    try:
        with open(OUT, "a", encoding="utf-8", buffering=1) as f:
            f.write(f"\n===== {time.strftime('%H:%M:%S')} 开始监听 {DURATION:.0f}s =====\n")
            f.flush()
            while time.time() < t_end:
                caps, peaks = snapshot()
                sig = tuple(sorted(caps))
                ts = time.strftime("%H:%M:%S")
                if sig and sig != last_sig:
                    f.write(f"[{ts}] QQ 打开录音端点：{sorted(set(caps))}\n")
                    f.flush()
                    last_sig = sig
                hot = {k: v for k, v in peaks.items() if v > 0.0005}
                if hot:
                    f.write(f"[{ts}] " + "  ".join(f"{k}={v:.3f}" for k, v in hot.items()) + "\n")
                    f.flush()
                time.sleep(1.0)
            f.write(f"===== {time.strftime('%H:%M:%S')} 结束 =====\n")
    finally:
        CoUninitialize()


if __name__ == "__main__":
    main()
