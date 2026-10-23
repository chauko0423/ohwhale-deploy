"""插件改动的最小单测：不启动 AstrBot，只验新代码的请求形状。

用一个 shim 顶替 Star 实例（这几个方法只用到 self._cookie / _cookie_header）。
运行：
  PYTHONPATH=<astrbot site-packages> python test_bili_patch.py
"""

from __future__ import annotations

from ohwhale.config import get as setting, path as config_path, project_path, state_path, log_path, secret, llm, require

import importlib.util
import json
import os
import sys
import types

PLUGIN = os.environ.get("BILI_PLUGIN") or (
    project_path("plugins/astrbot_plugin_bili_scroll/main.py"))


def _stub_astrbot() -> None:
    """被改的这几个方法完全不碰 astrbot，把依赖打桩就能单测。

    不这么做的话，导入 astrbot 会一路拉到 mcp → pywintypes，
    在非 AstrBot 进程里缺 DLL 就炸——那是环境问题，不是插件问题。
    """

    def deco(*_a, **_k):
        def wrap(fn):
            return fn
        return wrap

    class _Comp:
        def __init__(self, *a, **k):
            self.args = a

    class Star:
        def __init__(self, ctx=None, cfg=None):
            self.context = ctx

    mods = {
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": types.ModuleType("astrbot.api"),
        "astrbot.api.message_components": types.ModuleType("astrbot.api.message_components"),
        "astrbot.api.event": types.ModuleType("astrbot.api.event"),
        "astrbot.api.star": types.ModuleType("astrbot.api.star"),
        "astrbot.core": types.ModuleType("astrbot.core"),
        "astrbot.core.message": types.ModuleType("astrbot.core.message"),
        "astrbot.core.message.message_event_result":
            types.ModuleType("astrbot.core.message.message_event_result"),
    }
    mc = mods["astrbot.api.message_components"]
    mc.Plain = _Comp
    mc.Json = _Comp
    ev = mods["astrbot.api.event"]
    ev.AstrMessageEvent = type("AstrMessageEvent", (), {})
    ev.filter = types.SimpleNamespace(llm_tool=deco, command=deco)
    st = mods["astrbot.api.star"]
    st.Context = type("Context", (), {})
    st.Star = Star
    mods["astrbot.core"].logger = types.SimpleNamespace(
        warning=lambda *a, **k: None, info=lambda *a, **k: None,
        error=lambda *a, **k: None)
    mods["astrbot.core.message.message_event_result"].MessageChain = _Comp
    mods["astrbot"].api = mods["astrbot.api"]
    mods["astrbot"].core = mods["astrbot.core"]
    mods["astrbot.api"].message_components = mc
    mods["astrbot.api"].event = ev
    mods["astrbot.api"].star = st
    mods["astrbot.core"].message = mods["astrbot.core.message"]
    mods["astrbot.core.message"].message_event_result = \
        mods["astrbot.core.message.message_event_result"]
    sys.modules.update(mods)


_stub_astrbot()

from importlib.machinery import SourceFileLoader

spec = importlib.util.spec_from_loader(
    "bili_main", SourceFileLoader("bili_main", PLUGIN))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.MASTER_UID = 200001
mod.MASTER_NAME = "测试用户"
mod.TARGET_SESSION = "qq:FriendMessage:100001"

# 单测不许碰线上状态：把 STATE_FILE 指到临时文件
import tempfile
_TEST_DIR = tempfile.TemporaryDirectory(prefix="ohwhale-bili-test-")
mod.STATE_FILE = os.path.join(_TEST_DIR.name, "state.json")
mod.DEV_ID_FILE = os.path.join(_TEST_DIR.name, "device.txt")
mod.COOKIE_FILE = os.path.join(_TEST_DIR.name, "cookie.json")
with open(mod.COOKIE_FILE, "w", encoding="utf-8") as f:
    json.dump({"bili_jct": "offline-csrf", "SESSDATA": "offline-session", "DedeUserID": "100002"}, f)

captured: list[dict] = []


def fake_json(url, headers=None, data=None, timeout=15):
    captured.append({"url": url, "data": data})
    if "reply/add" in url:
        return {"code": 0, "data": {"rpid": 12345}}
    if "send_msg" in url:
        return {"code": 0, "data": {"msg_key": 999888}}
    return {"code": 0, "data": {}}


class Shim:
    """只提供被调用方法需要的东西。"""

    _cookie = mod.BiliScroll._cookie
    _cookie_header = mod.BiliScroll._cookie_header
    _logged_in = mod.BiliScroll._logged_in
    _bump_state = mod.BiliScroll._bump_state

    def __init__(self):
        self.calls = []


def main() -> int:
    real = mod._json
    mod._json = fake_json
    fails = []
    try:
        s = Shim()

        # 1. 带 @ 的评论：必须出现 at_name_to_mid
        captured.clear()
        mod.BiliScroll._reply_sync(s, 117480240515503, "@示例用户 测试",
                                   mod.MASTER_NAME, mod.MASTER_UID)
        d = captured[-1]["data"]
        assert d.get("at_name_to_mid") == json.dumps(
            {mod.MASTER_NAME: mod.MASTER_UID}, ensure_ascii=False), d
        assert d.get("message").startswith("@示例用户")
        print("P1 评论带 at_name_to_mid ... PASS")

        # 2. 不带 @ 的评论：不能带该字段（否则 B 站会莫名 @ 人）
        captured.clear()
        mod.BiliScroll._reply_sync(s, 1, "纯文字评论")
        d = captured[-1]["data"]
        assert "at_name_to_mid" not in d, d
        print("P2 不 @ 时不带该字段 ... PASS")

        # 3. 私信形状
        captured.clear()
        mod.BiliScroll._dm_send_sync(s, "测试私信")
        d = captured[-1]["data"]
        assert d["msg[receiver_id]"] == str(mod.MASTER_UID), d
        assert d["msg[receiver_type]"] == "1" and d["msg[msg_type]"] == "1"
        assert json.loads(d["msg[content]"])["content"] == "测试私信"
        assert d["csrf"] and d["csrf"] == d["csrf_token"]
        assert d["msg[dev_id]"], d
        print("P3 私信请求形状 ... PASS")

        # 4. dev_id 稳定
        a = mod._dev_id()
        b = mod._dev_id()
        assert a == b and len(a) == 36, (a, b)
        print("P4 dev_id 稳定 ... PASS")

        # 5. 限流计数：额度用满后再发必须被拒（额度从模块读，不写死）
        state = mod.STATE_FILE
        backup = None
        try:
            with open(state, encoding="utf-8") as f:
                backup = f.read()
        except OSError:
            pass
        cap = mod.MAX_DM_PER_DAY
        mod._save(state, {"date": __import__("time").strftime("%Y-%m-%d"),
                          "dm": cap})
        ok, why = s._bump_state("dm")
        assert not ok and f"{cap} 次" in why, (ok, why, cap)
        print(f"P5 私信限流（额度 {cap}，用满即拒）... PASS")
        # 6. 跨天重置只清计数、保留 last_browse_ts
        mod._save(state, {"date": "1999-01-01", "dm": 9, "last_browse_ts": 123})
        ok, _ = s._bump_state("like")
        st = json.load(open(state, encoding="utf-8"))
        assert ok and st.get("last_browse_ts") == 123, st
        print("P6 跨天重置保留 last_browse_ts ... PASS")
        if backup is not None:
            with open(state, "w", encoding="utf-8") as f:
                f.write(backup)
    except AssertionError as e:
        fails.append(str(e))
        print("FAIL:", e)
    finally:
        mod._json = real
    print("\n结论：", "全过" if not fails else f"{len(fails)} 处失败")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
