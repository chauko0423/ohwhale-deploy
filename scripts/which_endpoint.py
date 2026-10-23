"""查 QQ 正在往哪个播放端点送声音，以及各端点当前有没有声音。

给 call_loop 用：他改过音频设备，不能靠"默认扬声器"猜。
输出一行 JSON，方便子进程调用。
"""
from __future__ import annotations

import json
import sys

import psutil
from comtypes import CLSCTX_ALL, CoInitialize, CoUninitialize
from pycaw.pycaw import (AudioUtilities, IAudioMeterInformation,
                         IAudioSessionControl2)


def main() -> None:
    pid_filter = sys.argv[1].lower() if len(sys.argv) > 1 else "qq"
    CoInitialize()
    try:
        out = {"endpoints": [], "qq": None, "loudest": None}
        for d in AudioUtilities.GetAllDevices():
            if "Active" not in str(d.state):
                continue
            if not str(d.id).startswith("{0.0.0.00000000}"):
                continue          # 只看播放端点
            peaks = []
            try:
                se = d.AudioSessionManager.GetSessionEnumerator()
                for k in range(se.GetCount()):
                    ctl = se.GetSession(k)
                    c2 = ctl.QueryInterface(IAudioSessionControl2)
                    pid = c2.GetProcessId()
                    if not pid:
                        continue
                    try:
                        name = psutil.Process(pid).name()
                    except Exception:
                        name = "?"
                    peak = ctl.QueryInterface(IAudioMeterInformation).GetPeakValue()
                    peaks.append((name, peak, c2.GetState()))
            except Exception:  # noqa: BLE001
                pass
            live = [p for p in peaks if p[2] == 1]
            out["endpoints"].append({
                "name": d.FriendlyName,
                "sessions": len(peaks),
                "peak": round(max([p[1] for p in live], default=0.0), 4),
            })
            for name, peak, state in peaks:
                if pid_filter in name.lower():
                    if out["qq"] is None or peak > out["qq"]["peak"]:
                        out["qq"] = {"endpoint": d.FriendlyName, "peak": round(peak, 4)}
        live = [e for e in out["endpoints"] if e["peak"] > 0.001]
        if live:
            out["loudest"] = max(live, key=lambda e: e["peak"])["name"]
        print(json.dumps(out, ensure_ascii=False))
    finally:
        CoUninitialize()


if __name__ == "__main__":
    main()
