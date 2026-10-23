# -*- coding: utf-8 -*-
"""qq_power 插件离线回归测试。

桩掉 astrbot + 假的 bot.call_action + 假的 HTTP 层，验证：
- 群管理参数映射对不对（不能把「禁言 10 分钟」送成 10 秒）
- 权限闸是否只放行主人
- 空间发说说/列说说/点赞的解析与错误处理
- 表情挑选、去重、文字兜底

用法（用 AstrBot 自己的 python，才有 PIL/httpx）：
  "python" test_qq_power.py
"""
import asyncio
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# ---------------- 桩掉 astrbot ----------------
def stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class _Filter:
    class EventMessageType:
        PRIVATE_MESSAGE = "private"
        GROUP_MESSAGE = "group"
        OTHER_MESSAGE = "other"
        ALL = "all"

    @staticmethod
    def _identity(*a, **k):
        return lambda f: f

    event_message_type = _identity
    command = _identity
    llm_tool = _identity
    on_decorating_result = _identity
    on_llm_request = _identity


class _Comp:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        self.type = self.__class__.__name__.lower()

    @classmethod
    def fromFileSystem(cls, path, **_):
        return cls(file=path, path=path)

    @classmethod
    def fromURL(cls, url, **_):
        return cls(file=url, url=url)


class Plain(_Comp):
    def __init__(self, text=""):
        super().__init__()
        self.text = text


class At(_Comp):
    def __init__(self, qq=""):
        super().__init__()
        self.qq = qq


class Image(_Comp):
    pass


class Record(_Comp):
    pass


class Reply(_Comp):
    def __init__(self, sender_id=0):
        super().__init__()
        self.sender_id = sender_id


class _Chain:
    def __init__(self):
        self.chain = []


class _Star:
    def __init__(self, context=None, config=None):
        self.context = context


class _StarTools:
    @staticmethod
    def get_data_dir(name):
        return os.path.join(tempfile.gettempdir(), "qq_power_test", name)


class _Logger:
    def info(self, *a):
        pass

    def warning(self, *a):
        pass

    def error(self, *a):
        pass


stub("astrbot")
stub("astrbot.api")
stub("astrbot.api.event", AstrMessageEvent=object, filter=_Filter)
stub("astrbot.api.star", Context=object, Star=_Star, StarTools=_StarTools)
stub("astrbot.core", logger=_Logger())
stub("astrbot.core.message")
stub("astrbot.core.message.components", Plain=Plain, At=At, Image=Image,
     Record=Record, Reply=Reply)
stub("astrbot.core.message.message_event_result", MessageChain=_Chain)


class MessageType:
    GROUP_MESSAGE = "GroupMessage"
    FRIEND_MESSAGE = "FriendMessage"
    OTHER_MESSAGE = "OtherMessage"


stub("astrbot.core.platform")
stub("astrbot.core.platform.message_type", MessageType=MessageType)

import main as M  # noqa: E402
import qzone as Q  # noqa: E402
import stickerkit as S  # noqa: E402
import daily as D  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"   {detail}" if detail else ""))


# ---------------- 假事件 / 假 bot ----------------
class FakeBot:
    def __init__(self, cookies="uin=o100002; skey=abc; p_skey=xyz123", token=987654,
                 with_bkn=True, role="admin", self_admin=True):
        self.calls = []
        self.with_bkn = with_bkn
        self.role = "member"          # 未列出的人一律当普通成员
        self.roles = {
            100002: "admin" if self_admin else "member",
            100001: "owner",
            3489566467: "admin",
            555: "member",
        }
        self.cookies = cookies
        self.token = token

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        if action == "get_cookies":
            # NapCat 的真实返回形状：{cookies, bkn}，bkn 对 qzone 用 p_skey 算
            if self.with_bkn:
                return {"cookies": self.cookies, "bkn": str(self.token)}
            return {"cookies": self.cookies}
        if action == "get_csrf_token":
            return {"token": self.token}
        if action == "get_group_member_info":
            uid = params.get("user_id")
            return {"user_id": uid, "role": self.roles.get(uid, self.role)}
        if action == "get_group_member_list":
            return [
                {"user_id": 100001, "nickname": "四季", "card": "", "role": "owner"},
                {"user_id": 999, "nickname": "路人", "card": "摸鱼的", "role": "member"},
            ]
        if action == "boom":
            raise RuntimeError("NapCat 掉了")
        return {"ok": True}


class FakeResult:
    def __init__(self, chain):
        self.chain = chain
        self._llm = True

    def is_llm_result(self):
        return self._llm


class FakeEvent:
    def __init__(self, sender="100001", group="123456", msgs=None, bot=None,
                 mid="msg_1"):
        self._sender = sender
        self._group = group
        self._msgs = msgs or [Plain("你好")]
        self.bot = bot or FakeBot()
        self.message_obj = types.SimpleNamespace(message_id=mid)
        self.unified_msg_origin = "qq:FriendMessage:100001"
        self.msg_type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
        self.message_str = "".join(getattr(c, "text", "") for c in self._msgs)
        self.sent = []
        self._result = None

    def get_sender_id(self):
        return self._sender

    def get_group_id(self):
        return self._group

    def get_self_id(self):
        return "100002"

    def get_message_type(self):
        return self.msg_type

    def get_messages(self):
        return self._msgs

    def get_result(self):
        return self._result

    def set_result(self, chain):
        self._result = FakeResult(chain)
        return self._result

    def plain_result(self, text):
        return text


def new_plugin(sticker_root=None):
    p = object.__new__(M.QQPower)
    p.data_dir = sticker_root or os.path.join(tempfile.gettempdir(), "qq_power_test", "qq_power")
    p.sticker_root = os.path.join(p.data_dir, "stickers")
    for emo in S.EMOTIONS:
        os.makedirs(os.path.join(p.sticker_root, emo), exist_ok=True)
    p.library = S.StickerLibrary(p.sticker_root, os.path.join(p.data_dir, "text_stickers"))
    p._last_sticker = {}
    p._qz = None
    p._qz_at = 0.0
    p._role_cache = {}
    import uuid as _uuid
    p._daily = D.DailyCounter(
        os.path.join(p.data_dir, f"daily_{_uuid.uuid4().hex[:8]}.json"))
    p._posting = False
    p._owners_file = os.path.join(p.data_dir, "owners.json")
    p.owners = {"100001"}
    p.owner = "100001"
    return p


sent_chains = []


class FakeCtx:
    async def send_message(self, umo, chain):
        sent_chains.append(chain)


def run(coro):
    return asyncio.run(coro)


# ================= 1. 群管理 =================
print("=== 1. 群管理参数映射 ===")
p = new_plugin()
p.context = FakeCtx()

ev = FakeEvent()
r = run(p.tool_group(ev, "mute", "999", "", 10))
called = ev.bot.calls[-1]
check("禁言调用 set_group_ban", called[0] == "set_group_ban", str(called[0]))
check("10 分钟 → 600 秒", called[1].get("duration") == 600, str(called[1]))
check("群号/对象正确", called[1].get("group_id") == 123456 and called[1].get("user_id") == 999)
check("回话人话", "10 分钟" in str(r), str(r))

ev = FakeEvent()
run(p.tool_group(ev, "unmute", "999", "", 10))
check("解除禁言 duration=0", ev.bot.calls[-1][1].get("duration") == 0)

ev = FakeEvent()
run(p.tool_group(ev, "kick", "999"))
check("踢人带 reject_add_request",
      ev.bot.calls[-1][0] == "set_group_kick"
      and ev.bot.calls[-1][1].get("reject_add_request") is False)

ev = FakeEvent()
run(p.tool_group(ev, "announce", "", "今晚九点开黑"))
check("公告走 _send_group_notice",
      ev.bot.calls[-1][0] == "_send_group_notice"
      and ev.bot.calls[-1][1].get("content") == "今晚九点开黑")

ev = FakeEvent()
run(p.tool_group(ev, "whole_mute"))
check("全员禁言 enable=True", ev.bot.calls[-1][1].get("enable") is True)

ev = FakeEvent()
run(p.tool_group(ev, "recall"))
check("撤回用消息 id", ev.bot.calls[-1][0] == "delete_msg"
      and ev.bot.calls[-1][1].get("message_id") == "msg_1")

ev = FakeEvent(msgs=[At("999"), Plain(" 在吗")])
run(p.tool_group(ev, "mute", "他", "", 3))
check("@ 能解析成 QQ 号", ev.bot.calls[-1][1].get("user_id") == 999,
      str(ev.bot.calls[-1][1]))

ev = FakeEvent(msgs=[Plain("在吗")])
r = run(p.tool_group(ev, "mute", "", "", 3))
check("没 @ 没引用时不瞎猜",
      "没认出" in str(r) and all(c[0] != "set_group_ban" for c in ev.bot.calls), str(r)[:40])

ev = FakeEvent(msgs=[At("100002"), Plain(" 禁言 "), At("100003"), Plain(" 三分钟")])
run(p.tool_group(ev, "mute", "Fugitive.(100003)", "", 3))
check("跳过 @ 自己的那个，认到真正的人",
      ev.bot.calls[-1][1].get("user_id") == 100003,
      str(ev.bot.calls[-1][1]))

ev = FakeEvent(msgs=[Reply(sender_id=100002), Plain("禁言")])
r = run(p.tool_group(ev, "mute", "", "", 3))
check("引用自己的消息不当目标", "没认出" in str(r), str(r)[:40])

ev = FakeEvent(msgs=[Reply(sender_id=100002), At("100003"), Plain(" 禁言")])
run(p.tool_group(ev, "mute", "", "", 3))
check("引用她 + @别人 时认 @ 的那个",
      ev.bot.calls[-1][1].get("user_id") == 100003)

ev = FakeEvent(msgs=[Reply(sender_id=777), Plain("禁言他")])
run(p.tool_group(ev, "mute", "", "", 5))
check("引用消息能解析出人", ev.bot.calls[-1][1].get("user_id") == 777,
      str(ev.bot.calls[-1][1]))

ev = FakeEvent(msgs=[Plain("禁言阿猫")])
r = run(p.tool_group(ev, "mute", "阿猫", "", 5))
check("给了名字但解析不了就报错", "没认出" in str(r), str(r)[:40])

ev = FakeEvent(msgs=[Plain("x")])
r = run(p.tool_group(ev, "mute", "100002", "", 5))
check("不动自己", "我自己" in str(r), str(r)[:30])

ev = FakeEvent(msgs=[Plain("x")])
r = run(p.tool_group(ev, "mute", "100001", "", 5))
check("不动主人", "主人" in str(r), str(r)[:30])

ev = FakeEvent()
r = run(p.tool_group(ev, "members"))
check("成员列表可读", "100001" in str(r) and "群主" in str(r), str(r)[:60])

ev = FakeEvent()
r = run(p.tool_group(ev, "mute", "999", "", 10))
check("私聊里拒绝群操作",
      "不是群消息" in run(p.tool_group(FakeEvent(group=""), "mute", "999")))

ev = FakeEvent(bot=FakeBot(self_admin=False))
p2a = new_plugin()
p2a.context = FakeCtx()
before = len(ev.bot.calls)
r = run(p2a.tool_group(ev, "mute", "999", "", 10))
check("不是管理员时说明是权限问题", "不是管理员" in str(r) and "不是我没有这个功能" in str(r), str(r)[:60])
check("不是管理员时不发禁言请求",
      all(c[0] != "set_group_ban" for c in ev.bot.calls))

ev = FakeEvent(sender="3489566467", bot=FakeBot())
p2b = new_plugin(); p2b.context = FakeCtx()
r = run(p2b.tool_group(ev, "mute", "999", "", 3))
check("群里的管理员能让她动手", "已禁言" in str(r), str(r)[:40])

ev = FakeEvent(sender="555", bot=FakeBot())
p2c = new_plugin(); p2c.context = FakeCtx()
r = run(p2c.tool_group(ev, "mute", "999", "", 3))
check("普通群成员不行", "不是主人" in str(r), str(r)[:40])

p2d = new_plugin(); p2d.context = FakeCtx()
p2d.owners.add("770305648")
ev = FakeEvent(sender="770305648", bot=FakeBot())
r = run(p2d.tool_group(ev, "mute", "999", "", 3))
check("主人名单里的第二个号也放行", "已禁言" in str(r), str(r)[:40])

# ================= 2. 权限 =================
print("\n=== 2. 权限闸 ===")
ev = FakeEvent(sender="888888")
r = run(p.tool_group(ev, "mute", "999", "", 10))
check("非主人不能禁言", "不是主人" in str(r), str(r)[:40])
check("非主人没触发任何 API 调用", all(c[0] != "set_group_ban" for c in ev.bot.calls))

r = run(p.tool_qzone(FakeEvent(sender="888888"), "post", "测试"))
check("非主人不能发说说", "不是主人" in str(r), str(r)[:40])

# ================= 3. QQ 空间 =================
print("\n=== 3. QQ 空间 ===")


import time as _time


def set_qz(plugin, responses):
    plugin._qz = make_qz(responses)
    plugin._qz_at = _time.time()          # 别让 _qzone() 以为缓存过期去重连
    return plugin._qz


def make_qz(responses):
    """把 _req 换掉，按 URL 片段返回预设内容。"""
    qz = Q.QzoneClient({"uin": "o100002", "skey": "abc", "p_skey": "xyz123"},
                       "100002", Q.gtk_from_skey("xyz123"))
    calls = []

    async def fake_req(method, url, **kw):
        calls.append((method, url, kw))
        for frag, payload in responses.items():
            if frag in url:
                return True, {"status": 200, "text": payload}, ""
        return True, {"status": 200, "text": "{}"}, ""

    qz._req = fake_req
    qz.calls = calls
    return qz


p2 = new_plugin()
p2.context = FakeCtx()
set_qz(p2, {"publish_v6": '{"code":0,"data":{"tid":"abc123"}}'})
r = run(p2.tool_qzone(FakeEvent(), "post", "今天也要好好的"))
check("发说说成功不谎报", "发出去了" in str(r), str(r))
check("内容进了表单(字段名 con)", p2._qz.calls[-1][2]["data"]["con"] == "今天也要好好的")
check("format 必须是 fs", p2._qz.calls[-1][2]["data"]["format"] == "fs")

set_qz(p2, {"publish_v6": '{"code":0,"data":{"tid":"p1"}}'})
r = run(p2.tool_qzone(FakeEvent(), "post", "只给自己看", "", 5, True))
check("仅自己可见 → ugc_right=64",
      p2._qz.calls[-1][2]["data"]["ugc_right"] == "64",
      str(p2._qz.calls[-1][2]["data"].get("ugc_right")))

set_qz(p2, {"publish_v6": '{"code":0,"data":{"tid":"p2"}}'})
run(p2.tool_qzone(FakeEvent(), "post", "公开的", "", 5, False))
check("默认发布是公开 ugc_right=1",
      p2._qz.calls[-1][2]["data"]["ugc_right"] == "1")

set_qz(p2, {"publish_v6": "<html>请先登录</html>"})
r = run(p2.tool_qzone(FakeEvent(), "post", "x"))
check("发说说失败不谎报", "没发出去" in str(r), str(r)[:50])

set_qz(p2, {"msglist_v6": '_preloadCallback({"code":0,"msglist":['
                                '{"tid":"t1","content":"第一条","created_time":1792500000,'
                                '"cmtnum":3,"like":{"cnt":5}},'
                                '{"tid":"t2","content":"第二条","created_time":1792510000}]})'})
r = run(p2.tool_qzone(FakeEvent(), "list", "", "", 5))
check("列说说解析 2 条", "t1" in str(r) and "t2" in str(r), str(r)[:80])
check("JSONP 壳被剥掉", "preloadCallback" not in str(r))

set_qz(p2, {"internal_dolike_app": '{"code":0,"data":{}}'})
r = run(p2.tool_qzone(FakeEvent(), "like", "", "t1"))
check("点赞成功", "成功了" in str(r), str(r))
check("点赞带 fid 和 like=true",
      p2._qz.calls[-1][2]["data"]["fid"] == "t1"
      and p2._qz.calls[-1][2]["data"]["like"] == "true")

set_qz(p2, {"re_feeds": '{"code":0,"data":{"commentid":1}}'})
r = run(p2.tool_qzone(FakeEvent(), "comment", "好看", "t1"))
check("评论成功", "成功了" in str(r), str(r))

r = run(p2.tool_qzone(FakeEvent(), "like", "", ""))
check("缺 tid 时给提示", "缺 tid" in str(r), str(r))

# 登录态解析
print("\n=== 4. 登录态解析 ===")
check("cookie 字符串解析", Q.parse_cookies("a=1; b=2") == {"a": "1", "b": "2"})
check("cookie list 解析",
      Q.parse_cookies([{"name": "uin", "value": "o123"}]).get("uin") == "o123")
check("g_tk 稳定", Q.gtk_from_skey("xyz123") == Q.gtk_from_skey("xyz123"))
check("g_tk 非负", 0 <= Q.gtk_from_skey("xyz123") < 2 ** 31)


async def login_test():
    bot = FakeBot()
    cli = await Q.QzoneClient.from_bot(bot, uin="100002")
    return cli


cli = run(login_test())
check("有 p_skey 时自己算 g_tk（不信返回的 bkn）",
      cli.uin == "100002" and cli.gtk == Q.gtk_from_skey("xyz123"),
      f"gtk={cli.gtk}")
check("uin 去了 o 前缀", cli.uin == "100002", cli.uin)


async def login_no_bkn():
    bot = FakeBot(with_bkn=False)
    return await Q.QzoneClient.from_bot(bot, uin="100002")


cli2 = run(login_no_bkn())
check("没给 bkn 也照样算得出",
      cli2.gtk == Q.gtk_from_skey("xyz123") and cli2.gtk > 0, f"gtk={cli2.gtk}")


# ================= 5. 表情 =================
print("\n=== 5. 表情包 ===")
for text, want in [
    ("哈哈哈哈笑死我了", "laugh"),
    ("好开心呀今天", "happy"),
    ("呜呜呜我想哭", "cry"),
    ("哼，不理你了", "angry"),
    ("困死了，先去睡", "sleepy"),
    ("嗯", None),
]:
    emo, sc = S.score_emotion(text)
    check(f"「{text}」→ {want}", emo == want, f"实际 {emo}({sc})")

check("命中越多概率越高", S.sticker_probability(3) > S.sticker_probability(1))
check("概率有上限", S.sticker_probability(9) <= 0.6)

lib = S.StickerLibrary(os.path.join(tempfile.gettempdir(), "qq_power_test", "empty"))
path = lib.text_sticker("angry")
check("图库空了能现画文字表情", bool(path) and os.path.exists(path), str(path))

# 自动表情钩子
p3 = new_plugin()
p3._last_sticker = {}
ev = FakeEvent()
res = ev.set_result([Plain("哈哈哈哈笑死我了")])
import random as _rnd  # noqa: E402
_real_random = _rnd.random
_rnd.random = lambda: 0.0      # 一定命中
try:
    run(p3.auto_sticker(ev))
finally:
    _rnd.random = _real_random
check("情绪命中时追加了表情", any(isinstance(c, M.Image) for c in res.chain),
      str([c.type for c in res.chain]))

ev = FakeEvent()
res = ev.set_result([Plain("哈哈哈哈笑死我了"), Record(file="a.mp3")])
run(p3.auto_sticker(ev))
check("已经是语音就不叠表情", not any(isinstance(c, M.Image) for c in res.chain))

ev = FakeEvent()
res = ev.set_result([Plain("嗯")])
run(p3.auto_sticker(ev))
check("没情绪就不配图", not any(isinstance(c, M.Image) for c in res.chain))

# ================= 7. 每日计数 =================
print("\n=== 7. 每天 30 条触发 ===")
import tempfile as _tf
dp = os.path.join(_tf.gettempdir(), "qq_power_test", "daily_x.json")
if os.path.exists(dp): os.remove(dp)
dc = D.DailyCounter(dp, threshold=30, max_posts=3)
fired = [dc.add(f"消息{i}") for i in range(1, 31)]
check("前 29 条不触发", not any(fired[:29]))
check("第 30 条触发", fired[29] is True)

dc.mark_posted()
check("发完一条，额度 +1", dc.state["posts"] == 1)
check("下一条在第 60 条", dc.next_at() == 60)

fired = [dc.add("x") for _ in range(30)]     # 31..60
check("第 60 条再次触发", fired[-1] is True)
dc.mark_posted(); dc.mark_posted()
check("满 3 条后不再触发",
      all(not dc.add("y") for _ in range(30)), f"posts={dc.state['posts']}")
check("额度上限生效", dc.state["posts"] == 3)

# 跨天重置
dc.state["date"] = "2020-01-01"
dc.add("新的一天")
check("跨天自动清零", dc.state["date"] != "2020-01-01" and dc.state["count"] == 1)

# 素材只留最近 12 条
dc2 = D.DailyCounter(os.path.join(_tf.gettempdir(), "qq_power_test", "daily_y.json"), 30, 3)
for i in range(20): dc2.add(f"第{i}条")
check("素材只留最近 12 条", len(dc2.recent()) == 12, str(len(dc2.recent())))

print("\n=== 8. 计数口径：私聊全算，群里只算 @她 ===")
pc = new_plugin(); pc.context = FakeCtx()

ev = FakeEvent(group="", msgs=[Plain("在干嘛")])            # 私聊，无 @
run(pc.count_messages(ev))
check("私聊每条都算", pc._daily.state["count"] == 1, str(pc._daily.state["count"]))

ev = FakeEvent(group="1129510432", msgs=[Plain("今天好热")])  # 群里，没 @
run(pc.count_messages(ev))
check("群里没 @ 她不算", pc._daily.state["count"] == 1, str(pc._daily.state["count"]))

ev = FakeEvent(group="1129510432", msgs=[At("100002"), Plain(" 你好")])
run(pc.count_messages(ev))
check("群里 @ 她就算", pc._daily.state["count"] == 2, str(pc._daily.state["count"]))

ev = FakeEvent(group="1129510432", msgs=[At("all"), Plain(" 开会了")])
run(pc.count_messages(ev))
check("@全体不算", pc._daily.state["count"] == 2, str(pc._daily.state["count"]))

ev = FakeEvent(group="", msgs=[Plain("/power daily")])
run(pc.count_messages(ev))
check("命令不算", pc._daily.state["count"] == 2, str(pc._daily.state["count"]))

print(f"\n通过 {len(PASS)} / 失败 {len(FAIL)}")
if FAIL:
    print("失败项：" + "；".join(FAIL))
sys.exit(1 if FAIL else 0)
