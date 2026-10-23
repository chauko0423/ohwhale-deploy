"""列出每个播放端点上的音频会话：哪个进程、是否在响、峰值多少。

回答一个具体问题：打电话时，对方的声音被 Windows 播到哪个端点去了。
"""
from __future__ import annotations

import psutil
from comtypes import CLSCTX_ALL, CoInitialize, CoUninitialize
from pycaw.pycaw import (AudioUtilities, IAudioMeterInformation,
                         IAudioSessionControl2)


def main() -> None:
    CoInitialize()
    try:
        devs = [d for d in AudioUtilities.GetAllDevices()
                if "Render" in d.id or True]
        print(f"音频设备 {len(devs)} 个（只显示有会话或正在响的）\n")
        for d in devs:
            if str(d.state) not in ("AudioDeviceState.Active",):
                continue
            print(f"— {d.FriendlyName}  <{d.state}>")
            try:
                mgr = d.AudioSessionManager
                se = mgr.GetSessionEnumerator()
                total = se.GetCount()
                shown = 0
                for k in range(total):
                    ctl = se.GetSession(k)
                    c2 = ctl.QueryInterface(IAudioSessionControl2)
                    pid = c2.GetProcessId()
                    try:
                        pname = psutil.Process(pid).name() if pid else "system"
                    except Exception:
                        pname = "?"
                    meter = ctl.QueryInterface(IAudioMeterInformation)
                    peak = meter.GetPeakValue()
                    if pid and (peak > 0.0005 or "QQ" in pname):
                        print(f"    {pname:<12}({pid:<6}) state={c2.GetState()} peak={peak:.4f}")
                        shown += 1
                if not shown:
                    print(f"    （{total} 个会话，都在静音）")
            except Exception as e:  # noqa: BLE001
                print(f"    读取失败: {str(e)[:70]}")
            print()
    finally:
        CoUninitialize()


if __name__ == "__main__":
    main()
