"""给鲸鲸一双手：群管理、QQ 空间、图片和表情包。

三件事，三条不同的路：
1. 群管理 —— NapCat 原生就有（set_group_ban / set_group_kick / _send_group_notice …），
   以前"有权限但不会用"，是因为模型没有工具可以调。这里补上。
2. 图片 / 表情包 —— 表情包按情绪从本地图库挑；图库空了就用 Pillow 现画文字表情。
   走 on_decorating_result 钩子自动追加，正常对话里不用模型操心。
3. QQ 空间 —— NapCat 没有空间接口，只能拿 get_cookies 的登录态去调网页版 CGI。
   发说说 / 列说说 / 点赞 / 评论 / 删除。

安全：群操作和空间操作只认 OWNER_QQ（四季本人），别人在群里喊她禁言谁也不会照做。
"""

from __future__ import annotations

from ohwhale.config import get as setting, path as config_path, project_path, state_path, log_path, secret, llm, require

import asyncio
import json
import os
import random
import sys
import time
from datetime import datetime

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core import logger
from astrbot.core.message.components import At, Image, Plain, Record, Reply
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.message_type import MessageType

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

# AstrBot 热重载只会重新执行 main.py，同目录的 qzone/stickerkit 还留在 sys.modules 里，
# 改完这两个文件不重启就永远跑旧代码（2026-10-21 踩过）。这里强制丢掉缓存。
for _mod in ("qzone", "stickerkit", "daily"):
    sys.modules.pop(_mod, None)

from qzone import QzoneClient, QzoneError  # noqa: E402
from daily import DailyCounter  # noqa: E402
from stickerkit import (  # noqa: E402
    EMOTIONS,
    FALLBACK_TEXT,
    StickerLibrary,
    score_emotion,
    sticker_probability,
)

# ---------------- 可调参数 ----------------
OWNER_QQ = str(setting("master.qq", ""))        # 只有这个号能让她动群和空间
STICKER_COOLDOWN = 90          # 同一会话两条表情之间至少隔多久（秒）
QZONE_LOGIN_TTL = 300          # 登录态缓存多久（秒）
ROLE_TTL = 60                  # 群身份缓存多久（秒）。管理权限常是"后给的"，缓存久了会误判
MAX_MEMBERS_SHOWN = 25

# 能力说明，注入到 system_prompt 末尾。
# 不写这段，模型会嘴上答应"好呀我去禁言他"，然后什么也不做。
CAPABILITY_PROMPT = (
    "\n\n【你有的手】你不是只会打字。以下事情让你做，就直接调用工具做完再回话，"
    "不要说「我做不到」「你自己弄」：\n"
    "- 群管理（禁言/解禁/踢人/改名片/头衔/群公告/全员禁言/撤回/改群名/设管理/看成员）："
    "你是这个群的管理员，有权限\n"
    "- QQ 空间（发说说/看最近说说/点赞/评论/删说说）：工具是 qq_qzone，你真的能发。"
    "以前跟他说过「发不了」，那是旧版本的事了，现在别再推给他让他自己贴。\n"
    "- 发图片和表情包\n"
    "群管理工具就是 qq_group，你确实能禁言、踢人、改名片、发公告；"
    "如果以前在某个群说过「我没有这个功能」，那是说错了，现在照常调，"
    "失败只会是权限或对象的问题，不是功能没有。"
    "禁言或踢人时直接调 qq_group 并把 target 填成他说的那个人；"
    "别先去查成员列表找 QQ 号，工具会自己从 @ 或引用里认人。\n"
    "发说说必须有正文：他没说要发什么就先问一句，别凭空编，也别答「我发不出去」。\n"
    "工具失败了就照实说哪里没成，别假装成功。"
)


class QQPower(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.data_dir = str(StarTools.get_data_dir("astrbot_plugin_qq_power"))
        os.makedirs(self.data_dir, exist_ok=True)
        self.sticker_root = os.path.join(self.data_dir, "stickers")
        for emo in EMOTIONS:
            os.makedirs(os.path.join(self.sticker_root, emo), exist_ok=True)
        self.library = StickerLibrary(
            self.sticker_root, os.path.join(self.data_dir, "text_stickers"))
        self._last_sticker: dict[str, float] = {}
        self._qz: QzoneClient | None = None
        self._qz_at: float = 0.0
        self._role_cache: dict[tuple[str, str], tuple[str, float]] = {}
        self._daily = DailyCounter(os.path.join(self.data_dir, "daily.json"))
        self._posting = False
        self._owners_file = os.path.join(self.data_dir, "owners.json")
        self.owners: set[str] = self._load_owners()
        if not self.owners:
            logger.warning("[qq-power] 请先填写 config.json 的 master.qq；主人权限默认关闭")
        self.owner = str(OWNER_QQ)          # 兼容旧引用，等于默认主人
        self._call_log = os.path.join(self.data_dir, "tool_calls.jsonl")
        # 说说评论巡检：她主动看别人在自己说说下面的评论
        self._bot = None
        self._owner_umo = ""
        self._comment_task = None
        self._comment_state_path = os.path.join(self.data_dir, "comment_state.json")
        self._comment_state = self._load_comment_state()

    # ================= 说说评论巡检 =================
    def _load_comment_state(self) -> dict:
        try:
            with open(self._comment_state_path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                return {
                    "seen": [str(x) for x in (d.get("seen") or [])][-200:],
                    "last_check": float(d.get("last_check") or 0),
                }
        except Exception:  # noqa: BLE001
            pass
        return {"seen": [], "last_check": 0.0}

    def _save_comment_state(self):
        try:
            tmp = self._comment_state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._comment_state, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self._comment_state_path)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[qq-power] 评论巡检状态落盘失败：{e}")

    async def initialize(self):
        """启动评论巡检后台任务。间隔由 config.json 的 qzone.comment_poll_minutes 控制。"""
        try:
            self._comment_task = asyncio.create_task(self._comment_loop())
            logger.info("[qq-power] 说说评论巡检已启动")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[qq-power] 评论巡检启动失败：{e}")

    async def terminate(self):
        if self._comment_task:
            self._comment_task.cancel()
            self._comment_task = None

    async def _comment_loop(self):
        """后台轮询：每隔 N 分钟看她最新说说的评论，有新评论就主动找主人说一句。"""
        # 启动后先等 90 秒，避免和启动阶段抢资源
        await asyncio.sleep(90)
        while True:
            try:
                if not self.owner:
                    await asyncio.sleep(600)
                    continue
                minutes = 60
                try:
                    minutes = int(setting("qzone.comment_poll_minutes", 60))
                except Exception:  # noqa: BLE001
                    minutes = 60
                minutes = max(10, min(1440, minutes))
                enabled = True
                try:
                    enabled = bool(setting("features.qzone_comment_poll", True))
                except Exception:  # noqa: BLE001
                    enabled = True
                if enabled:
                    await self._check_comments()
                await asyncio.sleep(minutes * 60)
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[qq-power] 评论巡检出错：{e}")
                await asyncio.sleep(300)

    async def _check_comments(self):
        """看最新说说下面的评论；只报告上次检查之后新出现的。"""
        if not self._bot or not self._owner_umo:
            return
        try:
            cli = await QzoneClient.from_bot(self._bot, self.owner)
        except QzoneError:
            return
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[qq-power] 评论巡检取空间登录态失败：{e}")
            return

        r = await cli.list_self(3)
        if not r.get("ok"):
            return
        baseline = not self._comment_state.get("seen")
        fresh = []
        for item in r.get("items") or []:
            tid = str(item.get("tid") or "")
            if not tid:
                continue
            cr = await cli.comments(tid, 20)
            if not cr.get("ok"):
                continue
            for it in cr.get("items") or []:
                text = (it.get("text") or "").strip()
                if not text:
                    continue
                sig = f"{tid}|{it.get('uin') or it.get('who')}|{text[:60]}"
                if sig in self._comment_state["seen"]:
                    continue
                self._comment_state["seen"].append(sig)
                fresh.append({"who": it.get("who") or "某人", "text": text, "t": item.get("内容", "")})

        self._comment_state["seen"] = self._comment_state["seen"][-200:]
        self._comment_state["last_check"] = time.time()
        self._save_comment_state()

        # 第一次运行只做基线，不打扰；之后才报告新评论
        if baseline or not fresh:
            return

        lines = "\n".join(f"- {x['who']}：{x['text']}" for x in fresh[:5])
        try:
            persona = ""
            try:
                p = await self.context.persona_manager.get_default_persona_v3(self._owner_umo)
                persona = getattr(p, "system_prompt", "") or ""
            except Exception:  # noqa: BLE001
                pass
            prov = self.context.get_using_provider()
            prompt = (
                f"有人在你 QQ 空间的说说下面评论了：\n{lines}\n\n"
                "你要在 QQ 上主动跟他说一句（不是回复评论，是私聊他）。"
                "1~2 句，短，口语，可以损他、可以嘴硬，别客服腔、别解释。"
            )
            reply = await prov.text_chat(prompt=prompt, contexts=[], system_prompt=persona)
            text = (getattr(reply, "completion_text", "") or "").strip() or f"有人在我说说下面说话：\n{lines}"
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[qq-power] 评论巡检生成回复失败：{e}")
            text = f"有人在我说说下面说话：\n{lines}"
        try:
            chain = MessageChain()
            chain.message(text)
            await self.context.send_message(self._owner_umo, chain)
            self._log_call("qzone_comment_poll", {"n": len(fresh)}, "sent")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[qq-power] 评论巡检通知发送失败：{e}")

    # ================= 主人名单 =================
    def _load_owners(self) -> set[str]:
        """主人 QQ 名单。默认只有 OWNER_QQ，可在 data/owners.json 里加。

        四季至少有三个号在跟她说话（私聊 100001、群里还有别的），
        写死一个就会出现"这个群能禁言、那个群不能"。
        """
        base = {q.strip() for q in str(OWNER_QQ).split(",") if q.strip()}
        try:
            with open(self._owners_file, encoding="utf-8") as f:
                extra = json.load(f)
            if isinstance(extra, list):
                base |= {str(x).strip() for x in extra if str(x).strip().isdigit()}
        except Exception:  # noqa: BLE001
            pass
        return base

    def _save_owners(self):
        try:
            with open(self._owners_file, "w", encoding="utf-8") as f:
                json.dump(sorted(self.owners), f, ensure_ascii=False, indent=1)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[qq-power] 主人名单落盘失败：{e}")

    # ================= 权限 =================
    async def _denied(self, event: AstrMessageEvent,
                      allow_group_admin: bool = False) -> str | None:
        """返回 None 表示放行，返回字符串表示拒绝理由。

        群里的操作：主人 + 本群群主/管理员都放行（她都听群管的，不然给了管理也用不了）。
        空间是她的私人地盘，只认主人。
        """
        who = str(event.get_sender_id())
        if who in self.owners:
            return None
        gid = str(event.get_group_id() or "")
        if allow_group_admin and gid:
            role = await self._role_of(event, gid, who)
            if role in ("owner", "admin"):
                return None
            self._log_call("perm", {"gid": gid, "uid": who},
                           f"deny role={role!r}")
        return (f"没执行：{who} 不是主人，也不是本群管理员，按规矩不能替他动群。"
                f"注意——禁言/踢人这些工具本身是好的，只是这个人没资格，"
                f"换个有权限的人（主人或群管）来就行，别说自己没这个功能。")

    # ================= 小工具 =================
    async def _call(self, event: AstrMessageEvent, action: str, **params):
        """调 NapCat API，统一兜异常，并留一条现场记录。"""
        try:
            r = await event.bot.call_action(action, **params)
            if action in ("set_group_ban", "set_group_kick", "set_group_card",
                          "set_group_whole_ban", "_send_group_notice",
                          "set_group_name", "set_group_admin", "delete_msg"):
                self._log_call(action, params, "ok")
            return True, r
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[qq-power] {action} 失败：{e}")
            self._log_call(action, params, f"FAIL {e}")
            return False, str(e)

    def _log_call(self, tool: str, args: dict, result: str):
        """把每次工具调用写进 jsonl。

        AstrBot 的日志只走控制台不落盘，出问题时翻不到。这个文件是唯一的现场，
        上限约 200KB，超了就清空重来（只保留最近的，够定位就够）。
        """
        try:
            if os.path.exists(self._call_log) and os.path.getsize(self._call_log) > 200_000:
                os.remove(self._call_log)
            with open(self._call_log, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "t": datetime.now().strftime("%m-%d %H:%M:%S"),
                    "tool": tool, "args": args,
                    "result": str(result)[:300],
                }, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass

    def _resolve_target(self, event: AstrMessageEvent, target: str) -> tuple[str | None, str | None]:
        """把「他」解析成 QQ 号。返回 (uid, 错误)。

        顺序：显式 QQ 号 → 消息里的 @ → 被引用的那条消息的发送者。

        两个必须跳过的情况（2026-10-21 23:50 踩到）：
        - **@ 到自己**：在群里叫醒她必然带一个 @鲸鲸，取第一个 At 就会取到她，
          于是「禁言 @某人」变成「禁言我自己」，回一句「那是我自己，别闹」
        - **引用的是她自己的消息**：Reply.sender_id 还是她

        解析不出来就报错，绝不退回"说话人自己"——否则一句「禁言他」
        没 @ 到人，就把说话的人禁了。
        """
        me = str(event.get_self_id())
        t = (target or "").strip().lstrip("@")
        if t.isdigit():
            # 显式写了号码就照办（哪怕是她自己，让上层去提示"别闹"）
            return t, None

        msgs = []
        try:
            msgs = event.get_messages() or []
        except Exception:  # noqa: BLE001
            pass

        for comp in msgs:
            if not isinstance(comp, At):
                continue
            qq = str(getattr(comp, "qq", "") or "")
            if qq.isdigit() and qq not in ("0", me):
                return qq, None
        for comp in msgs:
            if isinstance(comp, Reply):
                sid = str(getattr(comp, "sender_id", "") or "")
                if sid.isdigit() and sid not in ("0", me):
                    return sid, None

        who = "对方" if not t else f"「{target}」"
        return None, f"没认出{who}是谁——@ 一下他（别只 @ 我），或者引用他刚发的那条消息"

    async def _qzone(self, event: AstrMessageEvent) -> QzoneClient:
        now = time.time()
        if self._qz and now - self._qz_at < QZONE_LOGIN_TTL:
            return self._qz
        cli = await QzoneClient.from_bot(event.bot, uin=str(event.get_self_id()))
        self._qz, self._qz_at = cli, now
        return cli

    async def _role_of(self, event: AstrMessageEvent, gid: str, uid: str) -> str:
        """某人在这个群是什么身份。缓 5 分钟。

        两个用途：查我自己能不能动别人（QQ 只允许管理员/群主禁言踢人），
        以及判断下指令的人是不是群管。
        """
        key = (gid, str(uid))
        now = time.time()
        hit = self._role_cache.get(key)
        if hit and now - hit[1] < ROLE_TTL:
            return hit[0]
        ok, me = await self._call(event, "get_group_member_info",
                                  group_id=int(gid), user_id=int(uid))
        role = me.get("role", "") if ok and isinstance(me, dict) else ""
        if role:
            self._role_cache[key] = (role, now)
        return role

    async def _self_role(self, event: AstrMessageEvent, gid: str) -> str:
        return await self._role_of(event, gid, str(event.get_self_id()))

    # ================= 群管理 =================
    @filter.llm_tool(name="qq_group")
    async def tool_group(self, event: AstrMessageEvent, action: str,
                         target: str = "", value: str = "",
                         minutes: int = 10) -> str:
        """管理当前 QQ 群。你是该群管理员，可以直接操作。

        Args:
            action (string): mute=禁言 / unmute=解除禁言 / kick=踢人 / card=改群名片 / title=设专属头衔 / announce=发群公告 / whole_mute=全员禁言 / whole_unmute=解除全员禁言 / recall=撤回对方刚发的消息 / rename=改群名 / admin_on=设为管理员 / admin_off=撤销管理员 / members=看群成员列表
            target (string): 要操作的人。直接填昵称或他说的人就行，留空也行——插件会自动用消息里 @ 的那个人、或他引用的那条消息的发送者。不要为了找 QQ 号先去调 members
            value (string): 文本参数：群名片、头衔、公告内容、新群名
            minutes (number): 禁言时长（分钟），0 表示立即解除
        """
        deny = await self._denied(event, allow_group_admin=True)
        if deny:
            self._log_call("qq_group", {"action": action, "target": target}, deny)
            return deny

        gid = str(event.get_group_id() or "")
        if not gid:
            return "这条不是群消息，群操作只能在群里做。"

        a = (action or "").strip().lower()

        # 需要管理员身份的动作，先确认自己是不是管理员——不是的话直接说人话
        need_admin = a in ("mute", "unmute", "kick", "title", "announce",
                           "whole_mute", "whole_unmute", "recall", "rename",
                           "admin_on", "admin_off")
        if need_admin:
            role = await self._self_role(event, gid)
            if role and role not in ("admin", "owner"):
                # 权限常是"后给的"。这里必须把缓存打掉，否则接下来 60 秒还会照着旧身份拒人
                self._role_cache.pop((gid, str(event.get_self_id())), None)
                self._log_call("self_role", {"gid": gid, "action": a}, f"not_admin role={role!r}")
                return (f"我在这个群不是管理员（查到的是 {role}），QQ 不让我禁言。"
                        f"这是权限问题，不是我没有这个功能——给我个管理员就行。")

        # 只有这几个动作需要"对谁"；公告/全员/撤回/群名/成员列表不需要，别让它们被解析拦住
        need_target = a in ("mute", "unmute", "kick", "card", "title", "admin_on", "admin_off")
        uid = ""
        if need_target:
            uid, uerr = self._resolve_target(event, target)
            if uerr:
                return uerr
            if str(uid) == str(event.get_self_id()):
                return "那是我自己，别闹"
            if str(uid) == self.owner and a in ("mute", "kick", "admin_off"):
                return "他是主人，这个我不做"

        try:
            if a in ("mute", "unmute"):
                secs = 0 if a == "unmute" else max(1, int(minutes)) * 60
                ok, r = await self._call(event, "set_group_ban",
                                         group_id=int(gid), user_id=int(uid),
                                         duration=secs)
                if not ok:
                    return f"禁言没成：{r}"
                return (f"已解除 {uid} 的禁言" if secs == 0
                        else f"已禁言 {uid} {max(1, int(minutes))} 分钟")

            if a == "kick":
                ok, r = await self._call(event, "set_group_kick",
                                         group_id=int(gid), user_id=int(uid),
                                         reject_add_request=False)
                return f"已把 {uid} 请出群" if ok else f"踢人没成：{r}"

            if a == "card":
                if not value.strip():
                    return "要改成什么名片？value 是空的。"
                ok, r = await self._call(event, "set_group_card",
                                         group_id=int(gid), user_id=int(uid),
                                         card=value.strip())
                return f"名片改成「{value.strip()}」了" if ok else f"改名没成：{r}"

            if a == "title":
                ok, r = await self._call(event, "set_group_special_title",
                                         group_id=int(gid), user_id=int(uid),
                                         special_title=value.strip())
                return f"头衔设成「{value.strip()}」了" if ok else f"设头衔没成：{r}"

            if a == "announce":
                if not value.strip():
                    return "公告内容不能为空。"
                ok, r = await self._call(event, "_send_group_notice",
                                         group_id=int(gid), content=value.strip())
                return "群公告发出去了" if ok else f"发公告没成：{r}"

            if a in ("whole_mute", "whole_unmute"):
                enable = a == "whole_mute"
                ok, r = await self._call(event, "set_group_whole_ban",
                                         group_id=int(gid), enable=enable)
                return ("已全员禁言" if enable else "已解除全员禁言") if ok else f"操作没成：{r}"

            if a == "recall":
                mid = getattr(getattr(event, "message_obj", None), "message_id", None)
                if not mid:
                    return "找不到要撤回的消息 id。"
                ok, r = await self._call(event, "delete_msg", message_id=mid)
                return "撤回了" if ok else f"撤回没成：{r}"

            if a == "rename":
                if not value.strip():
                    return "新群名不能为空。"
                ok, r = await self._call(event, "set_group_name",
                                         group_id=int(gid), group_name=value.strip())
                return f"群名改成「{value.strip()}」了" if ok else f"改群名没成：{r}"

            if a in ("admin_on", "admin_off"):
                ok, r = await self._call(event, "set_group_admin",
                                         group_id=int(gid), user_id=int(uid),
                                         enable=(a == "admin_on"))
                return ("已设为管理员" if a == "admin_on" else "已撤销管理员") if ok else f"设置没成：{r}"

            if a == "members":
                ok, r = await self._call(event, "get_group_member_list", group_id=int(gid))
                if not ok or not isinstance(r, list):
                    return f"拿群成员失败：{r}"
                lines = []
                for m in r[:MAX_MEMBERS_SHOWN]:
                    role = {"owner": "群主", "admin": "管理"}.get(m.get("role"), "")
                    name = m.get("card") or m.get("nickname") or ""
                    lines.append(f"{m.get('user_id')} {name}{('/' + role) if role else ''}")
                more = "" if len(r) <= MAX_MEMBERS_SHOWN else f"（共 {len(r)} 人，只列前 {MAX_MEMBERS_SHOWN}）"
                return "群成员：" + "；".join(lines) + more

            return f"不认识这个操作：{action}。可用的是 mute/unmute/kick/card/title/announce/whole_mute/whole_unmute/recall/rename/admin_on/admin_off/members"
        except ValueError:
            return f"参数格式不对：target={target!r} minutes={minutes!r}"
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[qq-power] 群操作异常：{e}")
            return f"操作出错了：{e}"

    # ================= QQ 空间 =================
    @filter.llm_tool(name="qq_qzone")
    async def tool_qzone(self, event: AstrMessageEvent, action: str,
                         text: str = "", tid: str = "", num: int = 5,
                         private: bool = False, uin: str = "") -> str:
        """操作 QQ 空间（说说）。发说说、看最近的说说、给说说点赞或评论。

        Args:
            action (string): post=发说说 / list=看自己最近的说说 / comments=看某条说说的评论 / friends=看好友动态 / user=看某个好友的说说(配 uin) / like=点赞 / comment=评论 / delete=删说说
            text (string): 发说说时的正文，或评论内容
            tid (string): 说说 id。点赞/评论/删除时用，先 list 拿到
            uin (string): 对方的 QQ 号。想看/评论「他发的说说」时必填（先用 friends 或 known QQ）
            num (number): 列表条数，默认 5
            private (boolean): 发说说时是否仅自己可见。他说「仅自己可见/只给自己看」就传 true，默认 false（公开）
        """
        deny = await self._denied(event)
        if deny:
            self._log_call("qq_qzone", {"action": action}, deny)
            return deny

        a = (action or "").strip().lower()
        try:
            cli = await self._qzone(event)
        except QzoneError as e:
            return f"空间登不上：{e}。多半是 NapCat 没登录或 cookie 取不到。"

        try:
            if a == "post":
                if not text.strip():
                    msg = "想说点什么呀？先告诉我内容，人家再发。"
                    self._log_call("qq_qzone", {"action": "post"}, "empty_text")
                    return msg
                r = await cli.publish(text.strip(), ugc_right=64 if private else 1)
                if r.get("ok"):
                    scope = "仅自己可见" if private else "公开"
                    self._log_call("qq_qzone", {"action": "post", "len": len(text)},
                                   f"ok tid={r.get('tid')}")
                    return f"说说发出去了（{scope}）"
                self._log_call("qq_qzone", {"action": "post"}, f"FAIL {r.get('err')}")
                return f"没发出去：{r.get('err')}"

            if a == "list":
                r = await cli.list_self(int(num))
                if not r.get("ok"):
                    return f"看不掉：{r.get('err')}"
                if not r.get("count"):
                    return "最近没有说说。"
                return _fmt_items("最近的说说", r["items"])

            if a == "friends":
                r = await cli.list_friends(int(num))
                if not r.get("ok"):
                    return f"好友动态没拉到：{r.get('err')}"
                if not r.get("count"):
                    return "好友动态是空的（这个接口本来就不太稳）。"
                return _fmt_items("好友动态", r["items"])

            if a == "user":
                if not uin.strip():
                    return "要看谁的空间？把对方的 QQ 号给我（uin）。"
                r = await cli.list_user(uin.strip(), int(num))
                if not r.get("ok"):
                    return f"他的说说没拉到：{r.get('err')}"
                if not r.get("count"):
                    return "他最近没发说说，或者不让你看。"
                self._log_call("qq_qzone", {"action": "user", "uin": uin}, f"ok n={r.get('count')}")
                return _fmt_items("他的说说", r["items"])

            if a == "comments":
                if not tid.strip():
                    return "先 list 看一眼，把 tid 给我。"
                r = await cli.comments(tid.strip(), int(num) if num else 20)
                if not r.get("ok"):
                    return f"评论没读出来：{r.get('err')}"
                if not r.get("count"):
                    return "这条下面还没有评论。"
                self._log_call("qq_qzone", {"action": "comments", "tid": tid},
                               f"ok n={r.get('count')}")
                lines = ["这条说说下面的评论："]
                for it in r["items"]:
                    who = it.get("who") or it.get("uin") or "某人"
                    lines.append(f"- {who}：{it.get('text')}（{it.get('time')}）")
                return "\n".join(lines)

            if a in ("like", "comment", "delete"):
                if not tid.strip():
                    return "缺 tid，先用 list 看一眼再操作。"
                if a == "like":
                    r = await cli.like(tid.strip())
                elif a == "comment":
                    if not text.strip():
                        return "评论内容不能为空。"
                    # 评论别人的说说必须带 hostuin，否则会被空间当成评论自己的
                    r = await cli.comment(tid.strip(), text.strip(), hostuin=uin.strip())
                else:
                    r = await cli.delete(tid.strip())
                self._log_call("qq_qzone", {"action": a, "tid": tid, "uin": uin},
                               ("ok" if r.get("ok") else f"FAIL {r.get('err')}"))
                if r.get("ok"):
                    if a == "comment":
                        target = f"（评在 {uin} 的说说下）" if uin.strip() else ""
                        return f"评论发出去了{target}，空间已确认能读到。"
                    return f"{a} 成功了"
                return f"{a} 没成：{r.get('err')}"

            return f"不认识这个操作：{action}。可用 post/list/comments/friends/user/like/comment/delete"
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[qq-power] 空间操作异常：{e}")
            return f"空间操作出错了：{e}"

    # ================= 图片 / 表情 =================
    @filter.llm_tool(name="qq_media")
    async def tool_media(self, event: AstrMessageEvent, action: str,
                         target: str = "") -> str:
        """发图片或表情包。想主动发一张图、或者配个表情时用。

        Args:
            action (string): sticker=发表情包 / image=发图片
            target (string): sticker 时填情绪（开心/笑/害羞/喜欢/难过/哭/生气/困/疑惑/得意 或 happy/laugh/shy/love/sad/cry/angry/sleepy/confuse/proud）；image 时填本地路径或图片 URL
        """
        a = (action or "").strip().lower()
        umo = event.unified_msg_origin

        if a == "sticker":
            emo = _normalize_emotion(target)
            if not emo:
                return f"没有这个情绪：{target}。可用：{'、'.join(EMOTIONS)}"
            path = self.library.get(emo, umo)
            if not path:
                return "手里一张表情都没有，也没有字体能画，先不发了。"
            await self._send_image(umo, path)
            return f"发了个「{FALLBACK_TEXT.get(emo, emo)}」的表情"

        if a == "image":
            src = (target or "").strip()
            if not src:
                return "要发哪张图？给个路径或链接。"
            if not (src.startswith("http") or os.path.exists(src)):
                return f"找不到这张图：{src}"
            await self._send_image(umo, src)
            return "图发出去了"

        return f"不认识这个操作：{action}。可用 sticker/image"

    async def _send_image(self, umo: str, src: str):
        if src.startswith("http"):
            comp = Image.fromURL(src)
        else:
            comp = Image.fromFileSystem(src)
        chain = MessageChain()
        chain.chain.append(comp)
        try:
            await self.context.send_message(umo, chain)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[qq-power] 图片发送失败：{e}")

    # ================= 自动表情 =================
    @filter.on_decorating_result()
    async def auto_sticker(self, event: AstrMessageEvent):
        """按回复的情绪，在消息链末尾追加一张表情。"""
        try:
            result = event.get_result()
            if result is None or not result.is_llm_result():
                return
            chain = list(getattr(result, "chain", None) or [])
            if not chain:
                return
            # 已经是语音/图了就不再叠，免得一条消息炸成三样
            if any(isinstance(c, (Record, Image)) for c in chain):
                return

            text = "".join(c.text for c in chain if isinstance(c, Plain))
            emo, score = score_emotion(text)
            if not emo:
                return

            umo = event.unified_msg_origin
            now = time.time()
            if now - self._last_sticker.get(umo, 0) < STICKER_COOLDOWN:
                return
            if random.random() > sticker_probability(score):
                return

            path = self.library.get(emo, umo)
            if not path:
                return
            chain.append(Image.fromFileSystem(path))
            result.chain = chain
            self._last_sticker[umo] = now
            logger.info(f"[qq-power] 配了个表情 emo={emo} score={score}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[qq-power] 表情钩子出错：{e}")

    # ================= 每日感受 =================
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def count_messages(self, event: AstrMessageEvent):
        """每收一条消息就计数。满 30 条就让她写一条说说。

        私聊和群都算——"每天累计接收到30条消息"是全局口径。
        命令、空消息、她自己发出的消息都不计。
        """
        text = (getattr(event, "message_str", "") or "").strip()
        if not text or text[0] in "/／":
            return
        # 记住主人的会话（私聊），评论巡检要用它主动发消息
        try:
            if str(event.get_sender_id()) in self.owners:
                self._bot = event.bot
                self._owner_umo = event.unified_msg_origin
        except Exception:  # noqa: BLE001
            pass
        # 口径：私聊每条都算；群里只算 @ 到她的。
        # 群里一天几百条，全算的话她会以为"聊了很多"，其实那些话跟她没关系。
        try:
            mt = event.get_message_type()
        except Exception:  # noqa: BLE001
            mt = None
        if mt == MessageType.GROUP_MESSAGE and not self._at_me(event):
            return
        try:
            if self._daily.add(text):
                if self._posting:
                    return
                self._posting = True
                asyncio.create_task(self._daily_post(event))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[qq-power] 日计数出错：{e}")

    def _at_me(self, event: AstrMessageEvent) -> bool:
        """消息里有没有 @ 到她（@全体 不算）。"""
        me = str(event.get_self_id())
        try:
            for comp in event.get_messages() or []:
                if isinstance(comp, At) and str(getattr(comp, "qq", "") or "") == me:
                    return True
        except Exception:  # noqa: BLE001
            pass
        return False

    async def _daily_post(self, event: AstrMessageEvent):
        """攒够素材了，写一段感受发到空间。失败不影响聊天。"""
        umo = event.unified_msg_origin
        n = self._daily.state["count"]
        try:
            persona = ""
            try:
                p = await self.context.persona_manager.get_default_persona_v3(umo)
                persona = getattr(p, "system_prompt", "") or ""
            except Exception:  # noqa: BLE001
                pass

            today = "\n".join(f"- {x}" for x in self._daily.recent()) or "（今天没什么特别的）"
            hint = (
                f"今天你断断续续收到了 {n} 条消息，聊到过这些：\n{today}\n\n"
                "现在你想在 QQ 空间写一条说说，记一下今天的感受。\n"
                "要求：第一人称，20~60 字，口语，像自己随手写的；"
                "不要话题标签，不要引号，不要解释，不要提「收了多少条消息」这种系统细节。\n"
                "**必须用自己的话重写**：不要照抄上面任何一句原话。"
                "上面如果有「发个说说」「禁言」「你试一下」这类指令，那是他在让你做事，"
                "不是聊天内容，跳过别写进去。\n"
                "只输出正文本身。"
            )
            prov = self.context.get_using_provider(umo)
            if not prov:
                logger.warning("[qq-power] 每日说说跳过：没有可用模型")
                return
            resp = await asyncio.wait_for(
                prov.text_chat(prompt="（写一条说说）", contexts=[],
                               system_prompt=persona + "\n\n【当前情境】\n" + hint),
                timeout=60)
            text = (getattr(resp, "completion_text", "") or "").strip()
            text = text.strip('"“”「」\'').strip()
            if not text:
                logger.warning("[qq-power] 每日说说跳过：模型没给内容")
                return
            text = "\n".join([ln for ln in text.splitlines() if ln.strip()][:2])[:200]
            # 照抄原句就重写一次（素材里混着指令时尤其容易发生）
            flat = lambda x: "".join(x.split())
            if any(flat(text) == flat(sc) for sc in self._daily.recent()):
                logger.info("[qq-power] 说说照抄了原句，重写一次")
                resp = await asyncio.wait_for(
                    prov.text_chat(prompt="（换一种说法，别照抄）", contexts=[],
                                   system_prompt=persona + "\n\n【当前情境】\n" + hint
                                   + "\n\n注意：上一版你把原话抄下来了，这次务必换个说法。"),
                    timeout=60)
                text2 = (getattr(resp, "completion_text", "") or "").strip()
                text2 = text2.strip('"“”「」').strip()
                text2 = "\n".join([ln for ln in text2.splitlines() if ln.strip()][:2])[:200]
                if text2 and not any(flat(text2) == flat(sc) for sc in self._daily.recent()):
                    text = text2

            cli = await QzoneClient.from_bot(event.bot, uin=str(event.get_self_id()))
            r = await cli.publish(text, ugc_right=1)
            if r.get("ok"):
                self._daily.mark_posted()
                self._log_call("daily_post", {"count": n}, f"ok {text[:60]}")
                logger.info(f"[qq-power] 写了条说说：{text[:40]}")
            else:
                self._log_call("daily_post", {"count": n}, f"FAIL {r.get('err')}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[qq-power] 每日说说失败：{e}")
            self._log_call("daily_post", {"count": n}, f"FAIL {e}")
        finally:
            self._posting = False

    # ================= 能力注入 =================
    @filter.on_llm_request()
    async def inject_capability(self, event: AstrMessageEvent, req):
        try:
            req.system_prompt = (req.system_prompt or "") + CAPABILITY_PROMPT
            # 现场记录：管线这次到底把哪几个工具发给了模型。
            # 模型张嘴说「我没有发说说的手」时，只有这行能证明是真没有还是她自己不认。
            try:
                ft = getattr(req, "func_tool", None)
                names = []
                if ft is not None:
                    names = [getattr(t, "name", str(t)) for t in (list(ft) if not isinstance(ft, str) else [])]
                self._log_call("llm_request", {"umo": event.unified_msg_origin},
                               "tools=" + ",".join(n for n in names if n.startswith("qq_")))
            except Exception:  # noqa: BLE001
                pass
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[qq-power] 能力注入失败：{e}")

    # ================= 调试命令 =================
    @filter.command("power")
    async def cmd_power(self, event: AstrMessageEvent):
        # 主人名单管理：/power owner add 770305648
        raw = (getattr(event, "message_str", "") or "").strip()
        toks = [t for t in raw.replace("/", " ").split() if t]

        # /power daily —— 看今天攒了多少条；post 立刻写一条（不等 30 条）
        if len(toks) >= 2 and toks[1] == "daily":
            if len(toks) >= 3 and toks[2] == "post":
                if str(event.get_sender_id()) not in self.owners:
                    yield event.plain_result("只有主人能催她写")
                    return
                yield event.plain_result("行，这就让她写一条")
                asyncio.create_task(self._daily_post(event))
                return
            yield event.plain_result(self._daily.summary())
            return

        # /power reset —— 只清当前这个会话的对话记录（换人设、被自己历史带偏时用）
        if len(toks) >= 2 and toks[1] in ("reset", "clear"):
            if str(event.get_sender_id()) not in self.owners:
                yield event.plain_result("只有主人能清")
                return
            try:
                umo = event.unified_msg_origin
                cm = self.context.conversation_manager
                before = await cm.get_curr_conversation_id(umo)
                try:
                    await cm.delete_conversations_by_user_id(umo)
                except Exception:  # noqa: BLE001
                    await cm.delete_conversation(umo)
                yield event.plain_result(
                    f"清掉了这个会话的记录（原 id {str(before)[:8]}）。"
                    f"下一句就是全新的开始，她不会再拿前面的话当依据。")
            except Exception as e:  # noqa: BLE001
                yield event.plain_result(f"清不动：{e}")
            return

        # /power whoami —— 当场自证：你是什么号、在这个群什么身份、我是什么身份
        if len(toks) >= 2 and toks[1] in ("whoami", "who"):
            who = str(event.get_sender_id())
            gid = str(event.get_group_id() or "")
            parts = [f"你={who}", "在名单" if who in self.owners else "不在名单"]
            if gid:
                parts.append(f"你的群身份={await self._role_of(event, gid, who) or '查不到'}")
                parts.append(f"我的群身份={await self._self_role(event, gid) or '查不到'}")
                parts.append(f"群={gid}")
            else:
                parts.append("当前是私聊")
            parts.append("主人名单=" + "、".join(sorted(self.owners)))
            yield event.plain_result("｜".join(parts))
            return

        if len(toks) >= 2 and toks[1] == "owner":
            if str(event.get_sender_id()) not in self.owners:
                yield event.plain_result("只有主人能改这个名单")
                return
            if len(toks) == 2:
                yield event.plain_result("主人名单：" + "、".join(sorted(self.owners)))
                return
            act, qq = toks[2], (toks[3] if len(toks) > 3 else "")
            if not qq.isdigit():
                yield event.plain_result("给个 QQ 号，例如 /power owner add 770305648")
                return
            if act == "add":
                self.owners.add(qq)
                self._save_owners()
                yield event.plain_result(f"加了 {qq}。现在名单：{'、'.join(sorted(self.owners))}")
            elif act in ("del", "remove", "rm"):
                if qq == str(OWNER_QQ):
                    yield event.plain_result("默认主人踢不掉")
                    return
                self.owners.discard(qq)
                self._save_owners()
                yield event.plain_result(f"删了 {qq}。现在名单：{'、'.join(sorted(self.owners))}")
            else:
                yield event.plain_result("用法：/power owner add|del <QQ号>")
            return

        n = self.library.total()
        dirs = sum(1 for d in os.listdir(self.sticker_root)
                   if os.path.isdir(os.path.join(self.sticker_root, d))
                   and any(f.lower().endswith((".png", ".jpg", ".gif", ".webp"))
                           for f in os.listdir(os.path.join(self.sticker_root, d))))
        lines = [f"表情库 {n} 张，覆盖 {dirs}/{len(EMOTIONS)} 种情绪（目录 {self.sticker_root}）"]
        try:
            cli = await self._qzone(event)
            r = await cli.list_self(1)
            keys = ",".join(sorted(cli.cookies.keys()))
            state = "OK" if r.get("ok") else f"异常 → {r.get('err')}"
            lines.append(f"空间：cookie {len(cli.cookies)} 项({keys})｜g_tk "
                         f"{'有' if cli.gtk else '缺'}｜{state}")
        except QzoneError as e:
            lines.append(f"空间登录：失败 → {e}")
        lines.append(f"群管理：{'群 ' + str(event.get_group_id()) if event.get_group_id() else '当前是私聊'}")
        lines.append("主人：" + "、".join(sorted(self.owners)))
        yield event.plain_result("｜".join(lines))


def _normalize_emotion(word: str) -> str | None:
    w = (word or "").strip()
    if w in EMOTIONS:
        return w
    for emo, text in FALLBACK_TEXT.items():
        if w == text or (w and w in text):
            return emo
    alias = {"笑": "laugh", "开心": "happy", "害羞": "shy", "喜欢": "love",
             "难过": "sad", "哭": "cry", "生气": "angry", "困": "sleepy",
             "疑惑": "confuse", "得意": "proud"}
    return alias.get(w)


def _fmt_items(title: str, items: list[dict]) -> str:
    out = [f"{title}（{len(items)} 条）："]
    for i, it in enumerate(items, 1):
        who = f"{it.get('作者')}：" if it.get("作者") else ""
        out.append(f"{i}. [{it.get('时间', '')}] {who}{it.get('内容', '')}"
                   f"（tid={it.get('tid', '')}，评论 {it.get('评论数', 0)}）")
    return "\n".join(out)
