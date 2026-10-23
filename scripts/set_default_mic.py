"""把 Windows 的「默认通信麦克风」指到 CABLE Output（她说话的那根虚拟线）。

背景：她的 TTS 走 VB-Cable（扬声器 (VB-Audio Virtual Cable) → CABLE Output）。
如果 QQ 通話用的是系统默认麦克风（而默认麦克风是 JBL / SteelSeries 之类），
对方就一个字都收不到 —— 但她在本地一切正常，日志里也看不出问题。

用法：
  python set_default_mic.py show                 # 只看
  python set_default_mic.py set "CABLE Output..."  # 设成某个录音端点（通信+控制台两个角色）
  python set_default_mic.py set-default          # 设成 CABLE Output
  python set_default_mic.py restore "<id>"       # 回滚到指定端点
"""
from __future__ import annotations

from ohwhale.config import get as setting, path as config_path, project_path, state_path, log_path, secret, llm, require

import sys

from comtypes import CLSCTX_ALL, CoCreateInstance, CoInitialize, CoUninitialize, GUID
from pycaw.api.policyconfig import IPolicyConfig
from pycaw.pycaw import AudioUtilities

CLSID_POLICY = GUID("{870AF99C-171D-4F9E-AF0D-E63DF40C2BC9}")
ROLES = {0: "Console", 1: "Multimedia", 2: "Communications"}


def friendly(dev) -> str:
    try:
        return dev.FriendlyName
    except Exception:
        return "?"


def device_id(dev) -> str | None:
    if dev is None:
        return None
    for attr in ("GetId", "id"):
        try:
            v = getattr(dev, attr)
            return v() if callable(v) else v
        except Exception:
            continue
    return None


def show() -> None:
    mic = AudioUtilities.GetMicrophone()
    spk = AudioUtilities.GetSpeakers()
    print("默认录音（通信）:", friendly(mic))
    print("  端点 id:", device_id(mic))
    print("默认播放:", friendly(spk))
    print("  端点 id:", device_id(spk))


def find_id(name_part: str) -> str:
    for d in AudioUtilities.GetAllDevices():
        if "Active" not in str(d.state):
            continue
        if name_part.lower() in str(d.FriendlyName).lower():
            return d.id
    raise SystemExit(f"没找到名字含「{name_part}」的活动端点")


def set_default(dev_id: str) -> None:
    pc = CoCreateInstance(CLSID_POLICY, interface=IPolicyConfig, clsctx=CLSCTX_ALL)
    for role, label in ROLES.items():
        try:
            pc.SetDefaultEndpoint(dev_id, role)
            print(f"  {label} → ok")
        except Exception as e:  # noqa: BLE001
            print(f"  {label} → 失败 {e}")


def main() -> None:
    CoInitialize()
    try:
        cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
        if cmd == "show":
            show()
        elif cmd == "set-default":
            dev_id = find_id(setting("audio.capture_hint"))
            print("目标:", dev_id)
            set_default(dev_id)
            print("--- 改后 ---")
            show()
        elif cmd == "set":
            dev_id = find_id(sys.argv[2])
            print("目标:", dev_id)
            set_default(dev_id)
            show()
        elif cmd == "restore":
            set_default(sys.argv[2])
            show()
        else:
            print(__doc__)
    finally:
        CoUninitialize()


if __name__ == "__main__":
    main()
