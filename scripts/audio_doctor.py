"""通话链路体检：一条命令说清「她聋没聋、对方听不听得见她」。

拆成五段，每段单独给 PASS / FAIL，坏在哪一段一眼能看出来：
  1. 进程    call_loop 在不在跑，心跳有多新
  2. 耳朵    电话的声音落在哪个播放端点，她现在听的是不是那一个
  3. 嘴      她的 TTS 能不能进 VB-Cable
  4. 回程    QQ 实际打开的麦克风是不是 CABLE Output
  5. 设备    列出所有端点，标出物理/虚拟

用法：
  python audio_doctor.py            # 全量体检
  python audio_doctor.py --quick    # 跳过 TTS 试音（不动喇叭）
"""
from __future__ import annotations

from ohwhale.config import get as setting, path as config_path, project_path, state_path, log_path, secret, llm, require

import argparse
import contextlib
import io
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request

import numpy as np

# comtypes 必须在 soundcard 之前导入：soundcard 会用自己那套初始化 COM，
# 之后 comtypes 再 CoInitializeEx 会报 "无法在设置线程模式后对其加以更改"。
from comtypes import CoInitialize, CoUninitialize  # noqa: I001
from pycaw.pycaw import AudioUtilities, IAudioMeterInformation, IAudioSessionControl2
import soundcard as sc  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
HEARTBEAT = log_path("loop.heartbeat")
TTS = setting("services.tts_url")
TTS_PARAMS = {
    "text": "体检试音，一二三。",
    "text_lang": "zh",
    "ref_audio_path": config_path("voice_ref"),
    "prompt_text": setting("voice.prompt_text"),
    "prompt_lang": "zh",
    "text_split_method": "cut0",
    "media_type": "wav",
}
RESULTS: list[tuple[str, bool, str]] = []


def mark(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}：{detail}")


def qq_endpoints() -> dict:
    """QQ 当前在哪些端点上有会话（播放/录音分开）。"""
    out = {"render": set(), "capture": set(), "peak": {}}
    for d in AudioUtilities.GetAllDevices():
        if "Active" not in str(d.state):
            continue
        kind = "capture" if str(d.id).startswith("{0.0.1.") else "render"
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
                import psutil
                pn = psutil.Process(pid).name().lower()
            except Exception:
                continue
            if "qq" not in pn:
                continue
            key = f"{kind} · {d.FriendlyName}"
            out[kind].add(d.FriendlyName)
            pk = ctl.QueryInterface(IAudioMeterInformation).GetPeakValue()
            out["peak"][key] = max(out["peak"].get(key, 0.0), pk)
    return out


def default_capture() -> tuple[str, str]:
    dev = AudioUtilities.GetMicrophone()
    did = dev.id if hasattr(dev, "id") else dev.GetId()
    name = "?"
    for d in AudioUtilities.GetAllDevices():
        if str(d.id).lower() == str(did).lower():
            name = d.FriendlyName
            break
    return name, did


def check_process() -> None:
    print("\n① 进程")
    if not os.path.exists(HEARTBEAT):
        mark("call_loop 心跳", False, "没有心跳文件 —— 通话循环没在跑")
        return
    age = time.time() - os.path.getmtime(HEARTBEAT)
    text = open(HEARTBEAT, encoding="utf-8", errors="replace").read().strip()
    mark("call_loop 心跳", age < 30, f"{age:.0f} 秒前（{text}）")


def check_ear(qq: dict) -> None:
    print("\n② 耳朵（她听谁）")
    try:
        default_spk = sc.default_speaker().name
    except Exception:  # noqa: BLE001
        default_spk = "?"
    if qq["render"]:
        target = sorted(qq["render"])[0]
        mark("QQ 的播放端点", True, f"{target}（默认扬声器是 {default_spk}）")
        try:
            heartbeat = open(HEARTBEAT, encoding="utf-8").read().strip()
            timestamp, listening = heartbeat.split(" ", 1)
            fresh = 0 <= time.time() - float(timestamp) < 30
        except (OSError, ValueError):
            listening, fresh = "未知", False
        mark("她听的是不是这一路", fresh and listening in qq["render"],
             f"实际监听 {listening} / QQ 在 {sorted(qq['render'])}")
    else:
        mark("QQ 的播放端点", False, "当前没有 QQ 通话，无法验证端到端回程")


def measure(seconds: float = 3.0) -> float:
    """播一句她的声音，量 CABLE Output 收到多大。"""
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = TTS + "?" + urllib.parse.urlencode(TTS_PARAMS)
    with op.open(urllib.request.Request(url), timeout=90) as r:
        data = r.read()
    tmp = os.path.join(os.environ.get("TEMP", HERE), "doctor_tts.wav")
    with open(tmp, "wb") as f:
        f.write(data)

    import wave
    with wave.open(tmp) as w:
        sr, ch = w.getframerate(), w.getnchannels()
        raw = w.readframes(w.getnframes())
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)

    cap = [m for m in sc.all_microphones(include_loopback=False)
           if setting("audio.capture_hint") in m.name]
    if not cap:
        raise RuntimeError("找不到 CABLE Output")
    peak = [0.0]
    stop = threading.Event()

    def rec() -> None:
        with cap[0].recorder(samplerate=16000) as r:
            while not stop.is_set():
                b = r.record(numframes=800)
                s = b.mean(axis=1) if b.ndim > 1 else b
                if len(s):
                    peak[0] = max(peak[0], float(np.sqrt(np.mean(s ** 2))))

    threading.Thread(target=rec, daemon=True).start()
    time.sleep(0.4)
    spk = [s for s in sc.all_speakers() if setting("audio.playback_hint") in s.name]
    if not spk:
        raise RuntimeError("找不到 VB-Cable 播放端点")
    with spk[0].player(samplerate=sr, channels=1) as p:
        p.play(x)
    time.sleep(0.5)
    stop.set()
    return peak[0]


def check_mouth(full: bool) -> None:
    print("\n③ 嘴（她的声音进不进虚拟线）")
    if not full:
        mark("TTS → CABLE Output", False, "SKIPPED: --quick 未执行试音")
        return
    try:
        peak = measure()
        mark("TTS → CABLE Output", peak > 0.05, f"实测峰值 {peak:.3f}（>0.05 算正常）")
    except Exception as e:  # noqa: BLE001
        mark("TTS → CABLE Output", False, f"试音失败：{e}")


def check_return(qq: dict) -> None:
    print("\n④ 回程（对方听不听得到她）")
    # QQ 可以单独选择设备。系统默认仍是物理麦克风，并不意味着 QQ 回程失败。
    try:
        name, _ = default_capture()
        print(f"       系统默认麦克风（仅供参考）：{name}")
    except Exception:  # noqa: BLE001
        print("       系统默认麦克风信息不可用；以 QQ 实际会话为准")
    if qq["capture"]:
        hint = setting("audio.capture_hint")
        mark("QQ 打开的麦克风", bool(hint) and any(hint in n for n in qq["capture"]), f"{sorted(qq['capture'])}")
        print("       ↑ 通话中这一行最关键：必须是 CABLE Output，否则对方只会听到房间")
    else:
        mark("QQ 打开的麦克风", False, "未观察到 QQ 录音会话；接通电话后重测，不能仅凭系统默认设备判成功")


def check_devices() -> None:
    print("\n⑤ 设备清单")
    for s in sc.all_speakers():
        tag = "虚拟" if any(k in s.name for k in ("VB-Audio", "SteelSeries", "Sonar")) else "物理"
        print(f"   [播放/{tag}] {s.name}")
    for m in sc.all_microphones(include_loopback=False):
        tag = "虚拟" if any(k in m.name for k in ("VB-Audio", "SteelSeries", "Sonar")) else "物理"
        print(f"   [录音/{tag}] {m.name}")


def run_checks() -> int:
    full = "--quick" not in sys.argv
    CoInitialize()
    try:
        print(f"===== 通话链路体检 {time.strftime('%Y-%m-%d %H:%M:%S')} =====")
        qq = qq_endpoints()
        check_process()
        check_ear(qq)
        check_mouth(full)
        check_return(qq)
        check_devices()
    finally:
        CoUninitialize()

    bad = [r for r in RESULTS if not r[1]]
    print("\n===== 结论 =====")
    if not bad:
        print("全部 PASS：她在听、她的声音也进得了麦克风。")
    else:
        print(f"{len(bad)} 项 FAIL：")
        for name, _, detail in bad:
            print(f"  · {name} —— {detail}")
    return 0 if not bad else 1


def main() -> int:
    RESULTS.clear()
    if "--json" not in sys.argv:
        return run_checks()
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            code = run_checks()
        except Exception as exc:
            RESULTS.append(("audio_doctor", False, str(exc)))
            code = 1
    print(json.dumps({"ok": code == 0, "checks": [
        {"name": n, "ok": ok, "detail": detail} for n, ok, detail in RESULTS
    ]}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
