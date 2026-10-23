"""把「默认麦克风」钉在 CABLE Output 上，防止切设备时又把回程弄断。

为什么需要它：她会说话靠的是 VB-Cable 这根虚拟线；只要默认麦克风被系统换回
物理麦（插拔耳机、切默认播放设备时 Windows 常干这事），对方就一个字都听不到，
而本地一切正常、日志也正常 —— 2026-10-23 就是这么坏掉的。

策略（避免影响别的语音软件）：
- 每 5 秒看一次默认录音设备
- 只有在「没有别的程序正在用麦克风（活跃录音会话）」时才改
- 改了会写日志，方便回溯
"""
from __future__ import annotations

from ohwhale.config import get as setting, path as config_path, project_path, state_path, log_path, secret, llm, require

import os
import time

from comtypes import CLSCTX_ALL, CoCreateInstance, CoInitialize, CoUninitialize, GUID
from pycaw.api.policyconfig import IPolicyConfig
from pycaw.pycaw import AudioUtilities, IAudioSessionControl2

CLSID_POLICY = GUID("{870AF99C-171D-4F9E-AF0D-E63DF40C2BC9}")
HERE = os.path.dirname(os.path.abspath(__file__))
LOG = log_path("mic_guard.log")
INTERVAL = 5.0


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def cable_id() -> str | None:
    for d in AudioUtilities.GetAllDevices():
        if "Active" in str(d.state) and setting("audio.capture_hint") in str(d.FriendlyName):
            return d.id
    return None


def default_capture_id() -> str | None:
    dev = AudioUtilities.GetMicrophone()
    if dev is None:
        return None
    return dev.id if hasattr(dev, "id") else dev.GetId()


def busy_by_others() -> bool:
    """有没有别的程序正在录音（她自己不算）。"""
    for d in AudioUtilities.GetAllDevices():
        if "Active" not in str(d.state) or not str(d.id).startswith("{0.0.1."):
            continue
        try:
            se = d.AudioSessionManager.GetSessionEnumerator()
        except Exception:  # noqa: BLE001
            continue
        for k in range(se.GetCount()):
            ctl = se.GetSession(k)
            c2 = ctl.QueryInterface(IAudioSessionControl2)
            if c2.GetState() != 1:          # 1 = active
                continue
            pid = c2.GetProcessId()
            if not pid:
                continue
            try:
                import psutil
                pn = psutil.Process(pid).name().lower()
            except Exception:
                continue
            if "python" in pn or "qq" in pn:
                continue
            return True
    return False


def pin(dev_id: str) -> None:
    pc = CoCreateInstance(CLSID_POLICY, interface=IPolicyConfig, clsctx=CLSCTX_ALL)
    for role in (0, 1, 2):
        pc.SetDefaultEndpoint(dev_id, role)


def main() -> None:
    CoInitialize()
    try:
        target = cable_id()
        if not target:
            log("找不到 CABLE Output，退出")
            return
        log(f"开始看守，目标端点 {target}")
        fixed = 0
        while True:
            cur = default_capture_id()
            if cur and cur.lower() != target.lower():
                if busy_by_others():
                    log(f"默认麦克风被改成 {cur}，但别的程序正在用，先不动")
                else:
                    pin(target)
                    fixed += 1
                    log(f"默认麦克风被改成 {cur} → 已拉回 CABLE Output（累计 {fixed} 次）")
            time.sleep(INTERVAL)
    except KeyboardInterrupt:
        pass
    finally:
        CoUninitialize()


if __name__ == "__main__":
    main()
