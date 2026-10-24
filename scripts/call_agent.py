"""QQ 通话自动接听 —— 基于 Windows UIA（CDP 被腾讯屏蔽了，走无障碍接口）

用法：
    python call_agent.py watch      # 观察：有窗口变化就打印，用来看来电时长什么样
    python call_agent.py answer     # 自动接听：轮询，出现"接听"类按钮就点

为什么用 UIA：
    QQ 9.9.35 带了 --remote-debugging-port 参数，但进程里没有 DevToolsActivePort，
    端口也不监听 —— 腾讯把这个口子关掉了。UIA 是系统级无障碍接口，绕不过去也封不掉。
"""

from __future__ import annotations

from ohwhale.config import get as setting, path as config_path, project_path, state_path, log_path, secret, llm, require

import re
import sys
import time
import traceback

from pywinauto import Desktop

# 按钮文字里出现这些就算"来电"
ANSWER_PAT = re.compile(r"接听|接受|接起")
# 排除误判（挂断/拒绝里也可能带"接"字）
REJECT_PAT = re.compile(r"挂断|拒绝|关闭")

LOG = log_path("call_agent.log")


def log(msg: str):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def named_buttons(window):
    """返回窗口里所有有文字的按钮，解析失败就给空。"""
    out = []
    try:
        for d in window.descendants(control_type="Button"):
            try:
                t = (d.window_text() or "").strip()
            except Exception:  # noqa: BLE001
                continue
            if t:
                out.append((t, d))
    except Exception:  # noqa: BLE001
        pass
    return out


def windows_snapshot():
    """{(title, class): 按钮文字列表}"""
    snap = {}
    try:
        for w in Desktop(backend="uia").windows():
            try:
                t = (w.window_text() or "").strip()
            except Exception:  # noqa: BLE001
                continue
            if not t:
                continue
            try:
                cls = w.class_name()
            except Exception:  # noqa: BLE001
                cls = ""
            snap[(t, cls)] = [b for b, _ in named_buttons(w)]
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    return snap


def watch():
    log("=== 观察模式：有窗口变化就打印（Ctrl+C 停）===")
    prev = {}
    while True:
        cur = windows_snapshot()
        for k, btns in cur.items():
            if k not in prev:
                log(f"[新窗口] {k[0]!r}  class={k[1]}")
                if btns:
                    log(f"          按钮: {' / '.join(btns)}")
            elif prev[k] != btns:
                new = [b for b in btns if b not in prev[k]]
                log(f"[按钮变化] {k[0]!r} -> 新增 {' / '.join(new)}")
        for k in prev:
            if k not in cur:
                log(f"[窗口关闭] {k[0]!r}")
        prev = cur
        time.sleep(0.5)


def self_enable_video() -> bool:
    """在通话窗口里点「开启视频」。"""
    for w in Desktop(backend="uia").windows():
        try:
            t = (w.window_text() or "").strip()
        except Exception:  # noqa: BLE001
            continue
        if "通话" not in t:
            continue
        try:
            btns = w.descendants(control_type="Button")
        except Exception:  # noqa: BLE001
            continue
        for b in btns:
            try:
                tx = (b.window_text() or "").strip()
            except Exception:  # noqa: BLE001
                continue
            if tx == "开启视频":
                try:
                    b.invoke()
                except Exception:  # noqa: BLE001
                    try:
                        b.click_input()
                    except Exception:  # noqa: BLE001
                        return False
                return True
    return False


def answer(once: bool = False):
    log("=== 自动接听：等待来电 ===")
    while True:
        try:
            for w in Desktop(backend="uia").windows():
                try:
                    title = (w.window_text() or "").strip()
                except Exception:  # noqa: BLE001
                    continue
                # 只钻通话窗口。
                # 遍历所有窗口的整棵树会慢到错过来电：WorkBuddy 一个窗口就有几百个元素，
                # 一轮扫描超过 20 秒，等轮到"语音通话"时早就超时了。
                if "通话" not in title:
                    continue
                # 视频来电窗口里同时有「视频接听」和「语音接听」，
                # 必须先挑视频的那个，否则会被当成语音电话接起来。
                cands = [(t, b) for t, b in named_buttons(w)
                         if ANSWER_PAT.search(t) and not REJECT_PAT.search(t)]
                if not cands:
                    continue

                def rank(item):
                    t = item[0]
                    if "视频" in t:
                        return 3
                    if t.strip() in ("接听", "接受", "接起"):
                        return 2
                    return 1        # 「语音接听」之类排最后

                cands.sort(key=rank, reverse=True)
                text, btn = cands[0]
                log(f"[来电] 窗口 {title!r} 发现 {[c[0] for c in cands]} -> 选 {text!r}")
                try:
                    btn.invoke()
                except Exception:  # noqa: BLE001
                    try:
                        btn.click_input()
                    except Exception as e:  # noqa: BLE001
                        log(f"  点击失败: {e}")
                        time.sleep(1)
                        continue
                log("  已点击")

                # 只有视频来电才开摄像头。语音来电开了就变成视频通话了，四季明确不要。
                # 判据：窗口标题带"视频"，或者接听选项里同时出现「接听」和「语音接听」
                #      （视频来电给两个选择，语音来电只有一个「接听」）。
                is_video = ("视频" in title) or len(cands) >= 2
                if is_video:
                    time.sleep(2.0)
                    if self_enable_video():
                        log("  视频来电 -> 已开启视频（使用 QQ 当前选择的摄像头；尚未核验是否为虚拟摄像头）")
                    else:
                        log("  视频来电 -> 没找到「开启视频」（可能已是开启状态）")
                else:
                    log("  语音来电 -> 保持语音，不开摄像头")

                if once:
                    return
                time.sleep(3)
        except Exception as e:  # noqa: BLE001
            log(f"轮询出错: {e}")
        time.sleep(0.6)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "watch"
    if cmd == "watch":
        watch()
    elif cmd == "answer":
        answer()
    elif cmd == "answer-once":
        answer(once=True)
    else:
        print(__doc__)
