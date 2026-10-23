"""让小满逛 B 站：搜索 / 详情 / 字幕 / 评论 / 点赞 / 发评论(@主人) / 私信 / 兴趣表 + 扫码登录。

设计原则：
- 只用标准库（qrcode/PIL 由 AstrBot 环境自带）
- 匿名可用：搜索、详情、评论
- 需要登录：字幕、点赞、发评论、私信（cookie 存在本机，不上传）
- 风控硬限流写在代码里，不靠模型自觉：点赞 <=10/天，评论 <=1/天，私信 <=3/天

2026-10-23 起：
- 评论可以真正 @ 到主人（靠 at_name_to_mid，光写 "@昵称" 只是纯文本、不通知任何人）
- 新增站内私信：给她一个能主动找主人的通道（B 站私信），并能读主人发来的话
"""

from ohwhale.config import get as setting, path as config_path, project_path, state_path, log_path, secret, llm, require

import asyncio
import hashlib
import json
import os
import random
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import astrbot.api.message_components as Comp
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core import logger
from astrbot.core.message.message_event_result import MessageChain

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
BASE_HEADERS = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

_TAG = re.compile(r"</?em[^>]*>")
_HOME = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = state_path("bilibili/bili_cookie.json")
STATE_FILE = state_path("bilibili/bili_state.json")
TASTE_FILE = state_path("bilibili/interests.json")

MAX_LIKE_PER_DAY = int(setting("bilibili.max_like_per_day", 10))
MAX_REPLY_PER_DAY = int(setting("bilibili.max_reply_per_day", 6))
# 私信额度要同时养两件事：主动分享 + 回他的话。
# 之前写 3，聊几句就没额度发视频了（04:40 那轮踩到），改成 6。
MAX_DM_PER_DAY = int(setting("bilibili.max_dm_per_day", 8))

# 逛的频率：2026-10-23 四季要「勤一些」，间隔从 55 分钟降到 25 分钟，
# 抽签 skip 率从 28% 降到 12%。
# 25 而不是 30：cron 每 30 分钟一发，跑一轮要几十秒，若判据写 30 会因为
# 「29.9 < 30」被挡掉一半的槽位（踩过）。
BROWSE_MIN_GAP = 25
BROWSE_SKIP_RATE = 0.12

# 私信秒回巡检
DM_WATCH_SECONDS = 60          # 多久看一次私信
DM_WATCH_MAX_PER_HOUR = 8      # 一小时内最多自动回几条（防对喷式刷屏）

# 「真看视频」：走独立 venv 里的 watch_video.py（ffmpeg 抽帧 + Pillow 图版 + 模型看图）
# 不把这些包装进 AstrBot 环境，避免污染它；一条视频约 30~60 秒、2~4k token，结果落盘缓存
TOOLS_DIR = config_path("scripts")
VW_PYTHON = config_path("video_python")
VW_SCRIPT = os.path.join(TOOLS_DIR, "watch_video.py")
VW_CACHE_DIR = config_path("video_cache")
VW_MAX_SECONDS = 900           # 超过 15 分钟的视频不整条看（下载+看图太贵）

# 分享目标（主人私聊）。换号只改这里。
TARGET_SESSION = setting("master.session", "")

# 主人的 B 站账号。私信投递和评论 @ 都要它。
# 200001「示例用户」与小满已互关 —— 互关后私信不受「对方回复前最多 1 条」限制。
MASTER_UID = int(setting("master.bili_uid", 0) or 0)
MASTER_NAME = setting("master.bili_name", "")

# 私信要求 msg[dev_id]，且换来换去像异常设备，固化成文件
DEV_ID_FILE = state_path("bilibili/bili_dev_id.txt")

# 是否用 QQ 结构化卡片分享。
# 实测：NapCat(QQ 9.9.33) 发的 com.tencent.structmsg 卡片，收端会显示
# 「发送者版本过低，无法展示内容」，而且 send_message 仍返回成功——
# 不会触发降级，属于"静默失败"。所以默认关闭，改用纯文本 + 裸链接，
# 由收端 QQ 自己生成链接预览。等 QQ 版本升级并验证过再打开。
# 2026-10-23：已切到 LL 那套（QQ 9.9.35.52892），比 NapCat 自带的 9.9.33 新，
# 之前「发送者版本过低」的静默失败可能已解决，打开实测。失败就改回 False。
CARD_ENABLED = False

DEFAULT_INTERESTS = [
    "本地大模型部署",
    "推理引擎 优化",
    "CUDA kernel 优化",
    "UE5 独立游戏开发",
    "量化 模型压缩",
]


# ---------------- 通用 ----------------
def _json(url: str, headers: dict | None = None, data: dict | None = None,
          timeout: int = 15) -> dict:
    h = dict(BASE_HEADERS)
    if headers:
        h.update(headers)
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _human(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    return f"{n / 10000:.1f}万" if n >= 10000 else str(n)


def _ts(t) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.localtime(int(t)))
    except (TypeError, ValueError, OSError):
        return "?"


def _clean(s, limit: int = 90) -> str:
    return _TAG.sub("", str(s or "")).replace("\n", " ").strip()[:limit]


def _load(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def _save(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _dev_id() -> str:
    """B 站私信要求 msg[dev_id]。固定一个，别每次变——变来变去像异常设备。"""
    try:
        with open(DEV_ID_FILE, encoding="utf-8") as f:
            v = f.read().strip()
            if v:
                return v
    except OSError:
        pass
    v = str(uuid.uuid4()).upper()
    try:
        with open(DEV_ID_FILE, "w", encoding="utf-8") as f:
            f.write(v)
    except OSError:
        pass
    return v


class BiliScroll(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._mixin_key = ""
        self._mixin_expire = 0.0
        self._watch_task: asyncio.Task | None = None

    # ---------------- 真看视频 ----------------
    def _watch_video_sync(self, bvid: str, per: int = 4, listen: bool = False) -> str:
        """调独立 venv 跑 watch_video.py：抽帧 → 图版 → 模型看图 → 分段描述（listen 时连声音）。

        结果按 BV 号缓存（视频内容不会变，重复问不再花钱）。
        """
        cache = os.path.join(VW_CACHE_DIR, bvid,
                             "summary.txt" if not listen else "report.txt")
        if os.path.exists(cache):
            try:
                with open(cache, encoding="utf-8") as f:
                    txt = f.read().strip()
                if txt:
                    return txt
            except OSError:
                pass
        if not os.path.exists(VW_PYTHON):
            raise RuntimeError(f"没找到看视频用的 python：{VW_PYTHON}")
        if not os.path.exists(VW_SCRIPT):
            raise RuntimeError(f"没找到 {VW_SCRIPT}")
        # 子进程默认按控制台代码页（本机 cp936）写管道，直接 utf-8 解会全是乱码；
        # 必须显式指定子进程的 stdout 编码，再兜底解一次 gb18030
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        p = subprocess.run(
            [VW_PYTHON, VW_SCRIPT, "full" if listen else "look", bvid, str(per)],
            capture_output=True, timeout=300, env=env,
        )
        def _dec(b: bytes) -> str:
            if not b:
                return ""
            try:
                return b.decode("utf-8")
            except UnicodeDecodeError:
                return b.decode("gb18030", "replace")
        out = _dec(p.stdout)
        err = _dec(p.stderr)
        if p.returncode != 0 or "===" not in out:
            raise RuntimeError(f"看视频失败（exit {p.returncode}）：{(err or out)[-300:]}")
        body = out.split("\n", 1)[1].strip() if "\n" in out else out
        # 光有 "===" 不算数：图密的时候模型会把 token 全烧在思考上、content 返回空，
        # 那时输出里只剩 "usage=3322" 这种空壳。这种不能缓存、也不能当成看过。
        if len(body) < 200 or "没读出内容" in body:
            raise RuntimeError("看图返回的是空壳（没有实际描述），不算看过")
        try:
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            with open(cache, "w", encoding="utf-8") as f:
                f.write(body)
        except OSError:
            pass
        return body

    @filter.llm_tool(name="bili_watch_video")
    async def bili_watch_video(self, event: AstrMessageEvent, bvid: str,
                               listen: bool = False):
        """真正「看」一条视频：抽 8 帧拼成图版交给模型看，字幕/界面/图表都能读出来。
        比 bili_subtitle 强在——没字幕的视频也能看，而且能看到画面上烧的字、图表、代码。
        listen=true 时还会下音轨跑一遍语音转写（多花约 15 秒），能回答"第几分钟说了什么"。
        耗时 30~60 秒，看完会缓存，再问同一条不再花时间。
        超过 15 分钟的长视频先别整条看，用 bili_subtitle 或 bili_comments 代替。

        Args:
            bvid(string): BV 号
            listen(bool): 要不要连声音一起听（默认否；要查"谁说了什么"才开）
        """
        try:
            d = await asyncio.to_thread(self._detail_sync, bvid)
        except Exception as e:  # noqa: BLE001
            return f"取 {bvid} 详情失败：{e}"
        dur = int(d.get("duration") or 0)
        if dur > VW_MAX_SECONDS:
            return (f"这条 {dur // 60} 分钟，整条看太贵。"
                    "先用 bili_subtitle / bili_comments 判断，真值得再拆段看。")
        try:
            txt = await asyncio.to_thread(self._watch_video_sync, bvid,
                                          4 if listen else 4, listen)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[bili] watch_video failed: {e}")
            return f"看视频失败：{e}"
        mode = "看图+听声" if listen else "抽帧看图"
        return (f"{bvid} 看完了（{mode}，不是读字幕）。原话：\n"
                + txt[:2600])

    # ---------------- 私信巡检（秒回） ----------------
    async def initialize(self):
        if not setting("features.bilibili", False):
            logger.info("[bili] 自动私信巡检未启用；请在 config.json 设置 features.bilibili")
            return
        if not TARGET_SESSION or not MASTER_UID or not MASTER_NAME:
            logger.warning("[bili] 请先填写 master.session / master.bili_uid / master.bili_name")
            return
        if self._watch_task is None or self._watch_task.done():
            self._watch_task = asyncio.create_task(self._dm_watch_loop())
            logger.info(f"[bili] 私信巡检已启动，每 {DM_WATCH_SECONDS} 秒看一次")

    async def terminate(self):
        if self._watch_task:
            self._watch_task.cancel()

    async def _dm_watch_loop(self):
        await asyncio.sleep(20)      # 等 AstrBot 与平台起稳，别抢启动
        while True:
            try:
                await self._dm_watch_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[bili] 私信巡检出错：{e}")
            await asyncio.sleep(DM_WATCH_SECONDS)

    async def _dm_watch_once(self):
        st = _load(STATE_FILE, {})
        if st.get("dm_watch_off"):
            return
        if not self._logged_in():
            return
        rows = await asyncio.to_thread(self._dm_read_sync, 6)
        if not rows:
            return

        last = str(st.get("dm_last_key") or "")
        if not last:
            # 首启只记账，不把历史消息全回一遍
            st["dm_last_key"] = rows[-1][2]
            _save(STATE_FILE, st)
            logger.info("[bili] 私信巡检首次启动，只记了位置，不回复历史")
            return

        new = [r for r in rows if r[2] != last]
        if not new:
            return
        ts, text, key = new[-1]          # 只回最新一条，堆积的旧消息不逐条回
        if not text or text.startswith("我们已互相关注"):
            st["dm_last_key"] = key
            _save(STATE_FILE, st)
            return

        now = time.time()
        if now < float(st.get("dm_retry_ts") or 0):
            return
        recent = [t for t in (st.get("dm_reply_ts") or []) if now - t < 3600]
        if len(recent) >= DM_WATCH_MAX_PER_HOUR:
            logger.warning("[bili] 私信巡检：一小时内已回"
                           f" {len(recent)} 条，暂停到窗口过去")
            return

        reply = await self._dm_compose(text)
        if not reply:
            st["dm_retry_ts"] = now + 300     # 生成失败，5 分钟后再试，别 60 秒一轮打模型
            _save(STATE_FILE, st)
            return

        await asyncio.to_thread(self._dm_send_sync, reply)
        st["dm_last_key"] = key
        st["dm_reply_ts"] = recent + [now]
        st["dm_retry_ts"] = 0
        _save(STATE_FILE, st)
        logger.info(f"[bili] 私信秒回：收到「{text[:40]}」→ 回了 {len(reply)} 字")

    async def _dm_compose(self, text: str) -> str:
        """用她的人设回一句 B 站私信。不要求长，一两句就够。"""
        try:
            prov = self.context.get_using_provider(TARGET_SESSION)
        except Exception:  # noqa: BLE001
            prov = None
        if not prov:
            return ""
        persona = ""
        try:
            p = await self.context.persona_manager.get_default_persona_v3(TARGET_SESSION)
            persona = getattr(p, "system_prompt", "") or ""
        except Exception:  # noqa: BLE001
            pass

        ctx_note = ""
        m = re.search(r"BV[0-9A-Za-z]{10}", text)
        if m:
            bvid = m.group(0)
            watched = ""
            try:
                d = await asyncio.to_thread(self._detail_sync, bvid)
                title = _clean(d.get("title"), 50)
                owner = (d.get("owner") or {}).get("name", "?")
                desc = _clean(d.get("desc"), 160)
                dur = int(d.get("duration") or 0)
                ctx_note = (f"\n\n（他发来的是一条视频：{title}｜UP {owner}｜"
                            f"时长 {dur // 60} 分 {dur % 60} 秒｜简介 {desc}）")
                if 0 < dur <= VW_MAX_SECONDS:
                    # 真看一眼：抽帧看图，比只读简介知道得多
                    try:
                        watched = await asyncio.to_thread(
                            self._watch_video_sync, bvid, 4)
                        watched = _clean(watched, 700)
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"[bili] 秒回时看视频失败：{e}")
                if watched:
                    ctx_note += ("\n（你已经抽帧看过了，画面里看到的是："
                                 + watched + "）")
            except Exception:  # noqa: BLE001
                ctx_note = f"\n\n（他发来一条视频 {bvid}，你这边没取到详情）"

        sys_prompt = (
            (persona + "\n\n" if persona else "")
            + "【当前情境】主人正在 B 站的私信里跟你说话，不是 QQ。"
            + ctx_note
            + "\n\n只输出你要回的话：**一到三句**，口语，像在聊天框里一句一句发（用换行分句）。"
              "活泼一点：可以有语气词、可以逗他、可以吐槽，但要短，别写成小作文。"
              "不要解释你是 AI，不要提「自动回复」，不要复述他说的话。"
              "如果他发了视频，说一句你看到的、具体的（别夸空话）。"
        )
        try:
            resp = await asyncio.wait_for(
                prov.text_chat(prompt=text, contexts=[], system_prompt=sys_prompt),
                timeout=150)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[bili] 私信生成失败：{e}")
            return ""
        out = (getattr(resp, "completion_text", "") or "").strip()
        out = re.sub(r"^[\"'「『]|[\"'」』]$", "", out).strip()
        return out[:180]

    # ---------------- 登录态 ----------------
    def _cookie(self) -> dict:
        return _load(COOKIE_FILE, {})

    def _cookie_header(self) -> dict:
        c = self._cookie()
        if not c.get("SESSDATA"):
            return {}
        parts = [f"{k}={c[k]}" for k in ("SESSDATA", "bili_jct", "DedeUserID") if c.get(k)]
        return {"Cookie": "; ".join(parts)}

    def _logged_in(self) -> bool:
        return bool(self._cookie().get("SESSDATA"))

    # ---------------- WBI 签名 ----------------
    def _refresh_mixin_key(self) -> str:
        nav = _json("https://api.bilibili.com/x/web-interface/nav")
        wbi = ((nav.get("data") or {}).get("wbi_img")) or {}
        img = (wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
        sub = (wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
        if not img or not sub:
            raise RuntimeError("拿不到 WBI 签名密钥")
        raw = img + sub
        self._mixin_key = "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]
        self._mixin_expire = time.time() + 3600
        return self._mixin_key

    def _mixin(self) -> str:
        if not self._mixin_key or time.time() > self._mixin_expire:
            return self._refresh_mixin_key()
        return self._mixin_key

    def _signed_qs(self, params: dict) -> str:
        p = {k: v for k, v in params.items() if v is not None and v != ""}
        p["wts"] = int(time.time())
        q = urllib.parse.urlencode(sorted(p.items()))
        p["w_rid"] = hashlib.md5((q + self._mixin()).encode()).hexdigest()
        return urllib.parse.urlencode(p)

    # ---------------- 数据层 ----------------
    def _search_sync(self, keyword: str, limit: int) -> list:
        qs = self._signed_qs({
            "search_type": "video", "keyword": keyword, "page": 1,
            "page_size": max(limit, 10), "order": "pubdate",
        })
        d = _json("https://api.bilibili.com/x/web-interface/wbi/search/type?" + qs)
        if d.get("code") != 0:
            raise RuntimeError(f"搜索 code={d.get('code')} msg={d.get('message')}")
        out = [v for v in ((d.get("data") or {}).get("result") or []) if v.get("bvid")]
        return out[:limit]

    def _detail_sync(self, bvid: str) -> dict:
        d = _json("https://api.bilibili.com/x/web-interface/view?"
                  + urllib.parse.urlencode({"bvid": bvid}))
        if d.get("code") != 0:
            raise RuntimeError(f"详情 code={d.get('code')} msg={d.get('message')}")
        return d.get("data") or {}

    def _comments_sync(self, aid: int, limit: int) -> list:
        qs = urllib.parse.urlencode(
            {"type": 1, "oid": aid, "sort": 1, "ps": max(limit, 5), "pn": 1})
        d = _json("https://api.bilibili.com/x/v2/reply?" + qs)
        if d.get("code") != 0:
            raise RuntimeError(f"评论 code={d.get('code')} msg={d.get('message')}")
        return ((d.get("data") or {}).get("replies") or [])[:limit]

    def _subtitle_sync(self, bvid: str) -> tuple[str, str]:
        """返回 (字幕正文, 备注)。备注里带统计信息，方便模型自己判断可信度。"""
        if not self._logged_in():
            raise RuntimeError("没有登录，读不了字幕")
        d = self._detail_sync(bvid)
        cid = d.get("cid")
        duration = int(d.get("duration") or 0)
        p = _json(
            "https://api.bilibili.com/x/player/v2?"
            + urllib.parse.urlencode({"bvid": bvid, "cid": cid}),
            headers=self._cookie_header(),
        )
        subs = (((p.get("data") or {}).get("subtitle") or {}).get("subtitles")) or []
        # 关键：接口会返回 subtitle_url 为空的占位轨，必须过滤掉
        subs = [s for s in subs if (s.get("subtitle_url") or "").strip()]
        if not subs:
            raise RuntimeError("这条视频没有可用字幕")
        subs.sort(key=lambda s: 0 if "zh" in (s.get("lan") or "") else 1)

        body = []
        for s in subs:
            url = s["subtitle_url"]
            if url.startswith("//"):
                url = "https:" + url
            try:
                body = _json(url).get("body") or []
            except Exception:  # noqa: BLE001
                body = []
            if body:
                break
        if not body:
            raise RuntimeError("字幕接口返回空内容")

        lines = [b.get("content", "") for b in body if b.get("content")]
        text = "\n".join(lines)[:4000]

        # 歌词判据：按条数比例算，不能只看有没有 ♪
        music_lines = sum(1 for x in lines if "♪" in x or "♫" in x)
        ratio = music_lines / max(len(lines), 1)
        if ratio >= 0.3:
            note = (f"这条字幕 {len(lines)} 条里有 {music_lines} 条是歌词/音乐符号，"
                    "不是人声讲解，别拿它当视频内容。")
        else:
            note = (f"字幕 {len(text)} 字 / 视频 {duration} 秒。"
                    "B 站 AI 字幕常只覆盖部分片段，拿不准时以简介和评论为准。")
        return text, note

    # ---------------- 写操作（带限流） ----------------
    def _bump_state(self, kind: str) -> tuple[bool, str]:
        st = _load(STATE_FILE, {})
        today = time.strftime("%Y-%m-%d")
        if st.get("date") != today:
            # 只重置计数，别把 last_browse_ts 一起清了——
            # 早先写法整条覆盖成 {"date","like","reply"}，导致每天第一次点赞/评论后
            # should_browse 的「距上次逛多久」判据失效（间隔永远 9999 分钟）
            st["date"] = today
            for k in ("like", "reply", "dm"):
                st[k] = 0
        limit = {"like": MAX_LIKE_PER_DAY,
                 "reply": MAX_REPLY_PER_DAY,
                 "dm": MAX_DM_PER_DAY}.get(kind, 0)
        verb = {"like": "点过", "reply": "发过", "dm": "发过"}.get(kind, "做过")
        if st.get(kind, 0) >= limit:
            return False, f"今天已经{verb} {limit} 次了，歇着吧"
        st[kind] = st.get(kind, 0) + 1
        _save(STATE_FILE, st)
        return True, ""

    def _like_sync(self, bvid: str) -> dict:
        c = self._cookie()
        d = _json(
            "https://api.bilibili.com/x/web-interface/archive/like",
            headers=self._cookie_header(),
            data={"bvid": bvid, "like": 1, "csrf": c.get("bili_jct", "")},
        )
        if d.get("code") != 0:
            raise RuntimeError(f"点赞 code={d.get('code')} msg={d.get('message')}")
        return d

    def _reply_sync(self, aid: int, message: str, at_name: str = "",
                    at_mid: int = 0) -> dict:
        """发评论。at_name/at_mid 非空时带 at_name_to_mid。

        B 站只有在请求里带 at_name_to_mid 时，正文里的 "@昵称" 才算真 @（会通知对方）；
        光写文本 "@昵称" 只是四个字，对方什么也收不到。
        """
        c = self._cookie()
        data = {"oid": aid, "type": 1, "message": message,
                "plat": 1, "csrf": c.get("bili_jct", "")}
        if at_name and at_mid:
            data["at_name_to_mid"] = json.dumps({at_name: at_mid}, ensure_ascii=False)
        d = _json(
            "https://api.bilibili.com/x/v2/reply/add",
            headers=self._cookie_header(),
            data=data,
        )
        if d.get("code") != 0:
            raise RuntimeError(f"评论 code={d.get('code')} msg={d.get('message')}")
        return d

    # ---------------- 私信 ----------------
    def _dm_ready(self) -> tuple[bool, str]:
        if not self._logged_in():
            return False, "还没登录 B 站，发不了私信。先用 /bili_login 扫码。"
        if not (self._cookie().get("DedeUserID") or "").strip():
            return False, "cookie 里没有 DedeUserID，重新扫码登录一次。"
        return True, ""

    def _dm_send_sync(self, text: str) -> dict:
        c = self._cookie()
        data = {
            "msg[sender_uid]": str(c.get("DedeUserID") or ""),
            "msg[receiver_id]": str(MASTER_UID),
            "msg[receiver_type]": "1",     # 1 = 发给个人
            "msg[msg_type]": "1",          # 1 = 文字
            "msg[msg_status]": "0",
            "msg[dev_id]": _dev_id(),
            "msg[timestamp]": str(int(time.time())),
            "msg[content]": json.dumps({"content": text}, ensure_ascii=False),
            "csrf": c.get("bili_jct", ""),
            "csrf_token": c.get("bili_jct", ""),
        }
        d = _json(
            "https://api.vc.bilibili.com/web_im/v1/web_im/send_msg",
            headers=self._cookie_header(),
            data=data,
        )
        if d.get("code") != 0:
            raise RuntimeError(f"私信 code={d.get('code')} msg={d.get('message')}")
        return d

    def _dm_read_sync(self, limit: int = 10) -> list:
        """按时间正序返回与主人的会话（只保留主人发来的文字，以及分享卡片）。

        返回 [(ts, text, msg_key)]；分享类消息 text 形如 "[分享] 标题"。
        """
        qs = urllib.parse.urlencode({
            "talker_id": MASTER_UID, "session_type": 1,
            "size": max(limit, 5), "begin_seqno": 0,
            "mobi_app": "web", "build": 0,
        })
        d = _json(
            "https://api.vc.bilibili.com/svr_sync/v1/svr_sync/fetch_session_msgs?" + qs,
            headers=self._cookie_header(),
        )
        if d.get("code") != 0:
            raise RuntimeError(f"读私信 code={d.get('code')} msg={d.get('message')}")
        msgs = ((d.get("data") or {}).get("messages")) or []
        out = []
        for m in reversed(msgs):  # 接口按新→旧返回，翻成正序
            if int(m.get("sender_uid") or 0) != MASTER_UID:
                continue          # 自己发的不算"他说话"
            try:
                content = json.loads(m.get("content") or "{}")
            except Exception:  # noqa: BLE001
                content = {}
            mt = m.get("msg_type")
            if mt == 1:
                text = (content.get("content") or "").strip()
            elif mt == 7:          # 分享卡片（手机/B站内分享视频给对方）
                text = "[分享] " + (content.get("title") or content.get("desc") or "")
                if content.get("id"):
                    text += f" {content.get('id')}"   # 分享卡片的 id 通常是 BV 号
            else:
                continue
            if not text:
                continue
            out.append((int(m.get("timestamp") or 0), text, str(m.get("msg_key") or "")))
        return out[-limit:]

    # ---------------- 工具 ----------------
    @filter.llm_tool(name="bili_search")
    async def bili_search(self, event: AstrMessageEvent, keyword: str, limit: int = 8):
        """按关键词搜索 B 站视频，按发布时间倒序，返回最新清单（BV号/标题/UP/播放/时长/日期）。
        想知道「最近有什么新东西」就用它。

        Args:
            keyword(string): 关键词，多个词用空格分开
            limit(number): 返回几条，默认 8，最多 20
        """
        limit = max(1, min(int(limit or 8), 20))
        try:
            rows = await asyncio.to_thread(self._search_sync, keyword, limit)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[bili] search failed: {e}")
            return f"搜「{keyword}」失败了：{e}。如实说没搜到，别编。"
        if not rows:
            return f"搜「{keyword}」没有结果。"
        lines = [f"搜「{keyword}」，按发布时间倒序，{len(rows)} 条："]
        for i, v in enumerate(rows, 1):
            lines.append(
                f"{i}. {v.get('bvid')} | {_clean(v.get('title'), 60)} | "
                f"UP:{_clean(v.get('author'), 20)} | 播放{_human(v.get('play'))} | "
                f"时长{v.get('duration', '?')} | {_ts(v.get('pubdate'))}")
        return "\n".join(lines)

    @filter.llm_tool(name="bili_detail")
    async def bili_detail(self, event: AstrMessageEvent, bvid: str):
        """看一条视频的详情：标题、简介、UP、播放/点赞/收藏、时长、分P。
        用来判断这条视频到底讲什么、值不值得看。

        Args:
            bvid(string): BV 号，例如 BV1xx411c7mD
        """
        try:
            d = await asyncio.to_thread(self._detail_sync, bvid)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[bili] detail failed: {e}")
            return f"取 {bvid} 详情失败：{e}。如实说。"
        st = d.get("stat") or {}
        return (
            f"标题：{_clean(d.get('title'), 80)}\n"
            f"UP：{(d.get('owner') or {}).get('name', '?')}\n"
            f"发布：{_ts(d.get('pubdate'))} | 时长 {d.get('duration', '?')} 秒 | "
            f"分P {len(d.get('pages') or [])}\n"
            f"数据：播放 {_human(st.get('view'))} / 点赞 {_human(st.get('like'))} / "
            f"收藏 {_human(st.get('favorite'))} / 弹幕 {_human(st.get('danmaku'))}\n"
            f"简介：{_clean(d.get('desc'), 300) or '（无简介）'}\n"
            f"链接：https://www.bilibili.com/video/{bvid}")

    @filter.llm_tool(name="bili_comments")
    async def bili_comments(self, event: AstrMessageEvent, bvid: str, limit: int = 5):
        """读热门评论（按赞排序）。评论往往比视频本身更能看出坑和真假。

        Args:
            bvid(string): BV 号
            limit(number): 读几条，默认 5，最多 10
        """
        limit = max(1, min(int(limit or 5), 10))
        try:
            aid = (await asyncio.to_thread(self._detail_sync, bvid)).get("aid")
            rows = await asyncio.to_thread(self._comments_sync, aid, limit)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[bili] comments failed: {e}")
            return f"取 {bvid} 评论失败：{e}。如实说，别说「没有评论」。"
        if not rows:
            return f"{bvid} 目前没有评论。"
        lines = [f"{bvid} 的热门评论："]
        for r in rows:
            lines.append(f"- {(r.get('member') or {}).get('uname', '?')}"
                         f"（赞{r.get('like', 0)}）："
                         f"{_clean((r.get('content') or {}).get('message'), 120)}")
        return "\n".join(lines)

    @filter.llm_tool(name="bili_subtitle")
    async def bili_subtitle(self, event: AstrMessageEvent, bvid: str):
        """读视频字幕原文（需要已登录 B 站）。用于真正了解视频讲了什么。
        返回值可能带一句可信度提示——有提示时说明字幕不全或是歌词，
        这种情况下不要拿字幕当结论，改用简介和评论判断。

        Args:
            bvid(string): BV 号
        """
        try:
            text, note = await asyncio.to_thread(self._subtitle_sync, bvid)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[bili] subtitle failed: {e}")
            return f"读 {bvid} 字幕失败：{e}。"
        return f"{bvid} 字幕（{note}）：\n{text}"

    @filter.llm_tool(name="bili_like")
    async def bili_like(self, event: AstrMessageEvent, bvid: str):
        """给视频点赞。只给真正看完、觉得值得的视频点，别刷。
        每天最多 10 个，超了会被拒绝。

        Args:
            bvid(string): BV 号
        """
        if not self._logged_in():
            return "还没登录 B 站，点不了赞。"
        ok, why = self._bump_state("like")
        if not ok:
            return why
        try:
            await asyncio.to_thread(self._like_sync, bvid)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[bili] like failed: {e}")
            return f"点赞失败：{e}"
        return f"给 {bvid} 点了个赞。"

    @filter.llm_tool(name="bili_reply")
    async def bili_reply(self, event: AstrMessageEvent, bvid: str, message: str):
        """在视频下发一条评论，**一定会 @ 主人**（他会在 B 站「消息 → @我的」看到）。
        每天最多 4 条。**要说得像人话，别一句话就走**——四季要看得出你真的看了：
        说清哪儿有意思、哪句让你停住、你信不信，可以带情绪和吐槽，40~140 字正好。
        不许写链接、不许透露任何私人信息（账号、路径、聊天内容）。
        视频分享请直接用 bili_share 的 comment 参数，不用调这个。

        Args:
            bvid(string): BV 号
            message(string): 评论正文，口语化 40~140 字。不要自己写 @，工具会自动加
        """
        if not self._logged_in():
            return "还没登录 B 站，发不了评论。"
        message = (message or "").strip()
        if not message:
            return "评论内容为空。"
        if "@" in message:
            return "正文里别自己写 @，@ 由工具加。改掉再发。"
        if "http" in message:
            return "评论里不许带链接。改掉再发。"
        if len(message) > 160:
            return f"评论太长（{len(message)} 字），压到 160 字以内再说。"
        st0 = _load(STATE_FILE, {})
        if bvid in (st0.get("replied_bvids") or []):
            return f"{bvid} 你已经评过一次了，同一条视频别刷第二遍。换一条。"
        ok, why = self._bump_state("reply")
        if not ok:
            return why
        # @ 主人不做成开关：他要在评论区看到，关掉就等于白发。
        # （2026-10-23：早先给 at_master 参数，她有好几次传 false，评论全成了没通知的纯文本）
        at_master = True
        tail = f"@{MASTER_NAME} "
        full = tail + message
        try:
            d = await asyncio.to_thread(self._detail_sync, bvid)
            r = await asyncio.to_thread(
                self._reply_sync, d.get("aid"), full,
                MASTER_NAME if at_master else "", MASTER_UID if at_master else 0)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[bili] reply failed: {e}")
            return f"评论失败：{e}"
        rpid = (r.get("data") or {}).get("rpid")
        st1 = _load(STATE_FILE, {})
        seen = st1.get("replied_bvids") or []
        if bvid not in seen:
            seen.append(bvid)
            st1["replied_bvids"] = seen[-200:]   # 只留最近 200 个，别无限长
            _save(STATE_FILE, st1)
        who = f"并 @ 了{MASTER_NAME}" if at_master else "（没 @ 人）"
        return f"在 {bvid} 下面留了言{who}：{full}（rpid={rpid}）"

    @filter.llm_tool(name="bili_dm")
    async def bili_dm(self, event: AstrMessageEvent, text: str):
        """给主人发一条 B 站站内私信。用来在 B 站上直接找他（他手机上会收到红点）。
        每天最多 3 条，别拿它当聊天框——聊天走 QQ，这里只发"我看到一个东西想给你看"。
        不许透露账号、路径、你们聊天内容里的私事。

        Args:
            text(string): 私信内容，1-200 字。可以带一条视频链接
        """
        ok, why = self._dm_ready()
        if not ok:
            return why
        text = (text or "").strip()
        if not text:
            return "私信内容为空。"
        if len(text) > 200:
            return f"私信太长（{len(text)} 字），压到 200 字以内。"
        ok, why = self._bump_state("dm")
        if not ok:
            return why
        try:
            d = await asyncio.to_thread(self._dm_send_sync, text)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[bili] dm failed: {e}")
            return f"私信发送失败：{e}"
        key = (d.get("data") or {}).get("msg_key")
        return f"已私信给主人（msg_key={key}）：{text}"

    @filter.llm_tool(name="bili_dm_read")
    async def bili_dm_read(self, event: AstrMessageEvent, limit: int = 10):
        """读主人发来的 B 站私信（含他分享给你的视频卡片）。
        她会先看有没有新消息，有新消息才需要理；没新消息就别硬找话。

        Args:
            limit(number): 读最近几条，默认 10，最多 20
        """
        ok, why = self._dm_ready()
        if not ok:
            return why
        limit = max(1, min(int(limit or 10), 20))
        try:
            rows = await asyncio.to_thread(self._dm_read_sync, limit)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[bili] dm_read failed: {e}")
            return f"读私信失败：{e}"
        if not rows:
            return "主人还没给你发过私信。"
        st = _load(STATE_FILE, {})
        last = str(st.get("dm_last_key") or "")
        lines = [f"与主人的最近 {len(rows)} 条私信（正序）："]
        for ts, text, key in rows:
            flag = "  ← 新" if key and key != last else ""
            lines.append(f"- [{time.strftime('%m-%d %H:%M', time.localtime(ts))}] "
                         f"{text}{flag}")
        return "\n".join(lines) + "\n（回他的话直接用 bili_dm；已读位置由 bili_dm_mark_read 记）"

    @filter.llm_tool(name="bili_dm_mark_read")
    async def bili_dm_mark_read(self, event: AstrMessageEvent):
        """把"读到哪儿了"记下来：下次 bili_dm_read 就不会再把旧消息标成新。
        读完并处理完主人的私信后调一次。
        """
        ok, why = self._dm_ready()
        if not ok:
            return why
        try:
            rows = await asyncio.to_thread(self._dm_read_sync, 5)
        except Exception as e:  # noqa: BLE001
            return f"读私信失败：{e}"
        if not rows:
            return "没有私信可记。"
        st = _load(STATE_FILE, {})
        st["dm_last_key"] = rows[-1][2]
        _save(STATE_FILE, st)
        return f"已记到 msg_key={st['dm_last_key']}。"

    @filter.llm_tool(name="bili_taste")
    async def bili_taste(self, event: AstrMessageEvent, add: str = ""):
        """读自己的兴趣表；传入 add 就追加一条兴趣。
        逛 B 站前先看兴趣表，按它去搜，而不是随便搜。

        Args:
            add(string): 要追加的兴趣，留空则只读取
        """
        t = _load(TASTE_FILE, {"interests": list(DEFAULT_INTERESTS), "history": []})
        if add and add.strip():
            topic = add.strip()[:30]
            if topic not in t["interests"]:
                t["interests"].append(topic)
                _save(TASTE_FILE, t)
            return f"记住了：{topic}。现在兴趣表：{'、'.join(t['interests'])}"
        return ("我的兴趣表：" + "、".join(t["interests"])
                + f"\n最近看过：{len(t.get('history', []))} 条")

    @filter.llm_tool(name="bili_share")
    async def bili_share(self, event: AstrMessageEvent, bvid: str, reason: str,
                         comment: str = "", via: str = "both"):
        """把一条视频分享给主人——一次做完三件事：发 QQ、发 B 站私信、**在视频评论区留一条
        @ 主人的评论**（他会在 B 站「消息 → @我的」里看到）。

        **默认就该带 comment**：分享不评论，他只在私信里能看见，评论区里找不到你。
        comment 写成"对视频说的话"，工具会自动在最前面 @ 主人，你别自己写 @、也别写链接。
        每天评论额度有限（4 条），额度用完时工具会告诉你，那就只发 QQ + 私信。

        Args:
            bvid(string): BV 号
            reason(string): 发给他的私信/QQ 里的理由，两三句话，别念清单
            comment(string): 评论区那条（40~140 字，口语，可以吐槽）。留空 = 不评论
            via(string): 投递方式，both（默认）/ qq / dm
        """
        try:
            d = await asyncio.to_thread(self._detail_sync, bvid)
        except Exception as e:  # noqa: BLE001
            return f"取 {bvid} 详情失败，做不了分享卡片：{e}"

        title = _clean(d.get("title"), 60)
        owner = (d.get("owner") or {}).get("name", "?")
        cover = d.get("pic") or ""
        if cover.startswith("//"):
            cover = "https:" + cover
        url = f"https://www.bilibili.com/video/{bvid}"
        dur = int(d.get("duration") or 0)
        dur_s = f"{dur // 60}:{dur % 60:02d}" if dur else "?"
        reason = (reason or "").strip()[:300]

        card = {
            "app": "com.tencent.structmsg",
            "desc": "新闻",
            "view": "news",
            "ver": "0.0.0.1",
            "prompt": f"[分享] {title}",
            "meta": {"news": {
                "title": title,
                "desc": f"UP主：{owner} · {dur_s}",
                "jumpUrl": url,
                "preview": cover,
                "tag": "哔哩哔哩",
                "musicUrl": "",
            }},
        }
        text = f"{reason}\n{title}\n{url}"
        via = (via or "both").strip().lower()
        if via not in ("qq", "dm", "both"):
            via = "both"
        done = []

        if via in ("qq", "both"):
            got = await self._share_qq(card, text)
            if "失败" in got:
                return f"QQ 这一路没成：{got}"
            done.append(got)

        if via in ("dm", "both"):
            okc, why = self._dm_ready()
            if not okc:
                done.append(f"B 站私信跳过：{why}")
            else:
                okc2, why2 = self._bump_state("dm")
                if not okc2:
                    done.append(f"B 站私信跳过：{why2}")
                else:
                    try:
                        dd = await asyncio.to_thread(self._dm_send_sync, text)
                        done.append(f"已私信给主人（msg_key="
                                    f"{(dd.get('data') or {}).get('msg_key')}）")
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"[bili] share dm failed: {e}")
                        done.append(f"B 站私信失败：{e}")

        # 第三投：评论区 @ 主人。他只看私信是找不到"评论区那句"的。
        ctext = (comment or "").strip()
        if ctext:
            if "@" in ctext:
                done.append("评论区跳过：comment 里不要自己写 @")
            elif len(ctext) > 160:
                done.append(f"评论区跳过：太长（{len(ctext)} 字）")
            elif bvid in (_load(STATE_FILE, {}).get("replied_bvids") or []):
                done.append("评论区跳过：这条已经评过了")
            else:
                okc3, why3 = self._bump_state("reply")
                if not okc3:
                    done.append(f"评论区跳过：{why3}")
                else:
                    full = f"@{MASTER_NAME} {ctext}"
                    try:
                        r = await asyncio.to_thread(
                            self._reply_sync, d.get("aid"), full,
                            MASTER_NAME, MASTER_UID)
                        rpid = (r.get("data") or {}).get("rpid")
                        st1 = _load(STATE_FILE, {})
                        seen = st1.get("replied_bvids") or []
                        if bvid not in seen:
                            seen.append(bvid)
                            st1["replied_bvids"] = seen[-200:]
                            _save(STATE_FILE, st1)
                        done.append(f"已在评论区 @ 他（rpid={rpid}）")
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"[bili] share comment failed: {e}")
                        done.append(f"评论区失败：{e}")

        return f"{bvid} 分享完成 —— " + "；".join(done)

    async def _share_qq(self, card: dict, text: str) -> str:
        if not CARD_ENABLED:
            try:
                ok = await self.context.send_message(
                    TARGET_SESSION, MessageChain([Comp.Plain(text)]))
                if not ok:
                    raise RuntimeError("没找到可用的平台会话")
                return "已发 QQ（文字+链接）"
            except Exception as e:  # noqa: BLE001
                return f"分享失败：{e}"
        try:
            ok = await self.context.send_message(
                TARGET_SESSION,
                MessageChain([Comp.Json(card), Comp.Plain(text)]),
            )
            if not ok:
                raise RuntimeError("没找到可用的平台会话")
            return "已发 QQ（卡片）"
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[bili] share card failed: {e}, fallback to plain")
            try:
                await self.context.send_message(
                    TARGET_SESSION, MessageChain([Comp.Plain(text)]))
                return f"卡片发送失败（{e}），已改用纯文字带链接"
            except Exception as e2:  # noqa: BLE001
                return f"分享失败：{e2}"

    @filter.llm_tool(name="bili_should_browse")
    async def bili_should_browse(self, event: AstrMessageEvent,
                                 min_gap_minutes: int = BROWSE_MIN_GAP):
        """掷骰子决定这一轮要不要真的去逛。必须先调它：返回 go 才去逛，
        返回 skip 就立刻结束、什么都别做、也别发消息。
        随机性必须靠这个工具在代码里掷，不要自己估摸。

        Args:
            min_gap_minutes(number): 距上次逛至少隔几分钟，默认 30
        """
        st = _load(STATE_FILE, {})
        now = time.time()
        last = float(st.get("last_browse_ts") or 0)
        gap = ((now - last) / 60) if last else 9999.0
        if gap < float(min_gap_minutes or BROWSE_MIN_GAP):
            return f"skip：上次逛是 {gap:.0f} 分钟前，还没到间隔。这轮什么都别做。"
        if random.random() < BROWSE_SKIP_RATE:
            return "skip：这轮抽到不去。什么都别做，也别发消息。"
        st["last_browse_ts"] = now
        _save(STATE_FILE, st)
        return "go：这轮去逛。"

    # ---------------- 命令 ----------------
    @filter.command("bili_login")
    async def bili_login(self, event: AstrMessageEvent):
        """扫码登录 B 站（只有管理员该用）"""
        d = await asyncio.to_thread(
            _json, "https://passport.bilibili.com/x/passport-login/web/qrcode/generate")
        if d.get("code") != 0:
            yield event.plain_result(f"生成二维码失败：{d}")
            return
        url = d["data"]["url"]
        key = d["data"]["qrcode_key"]

        import qrcode as _qr

        png = state_path("bilibili/bili_qrcode.png")
        _qr.make(url, box_size=10, border=3).save(png)
        yield event.image_result(png)
        yield event.plain_result(
            "用 B 站 App 扫码，扫完手机上点确认。3 分钟内有效。\n"
            "扫不了就把这条链接发到手机上打开：" + url)

        import http.cookiejar

        for _ in range(60):
            await asyncio.sleep(3)
            try:
                cj = http.cookiejar.CookieJar()
                op = urllib.request.build_opener(
                    urllib.request.HTTPCookieProcessor(cj))
                req = urllib.request.Request(
                    "https://passport.bilibili.com/x/passport-login/web/qrcode/poll?"
                    + urllib.parse.urlencode({"qrcode_key": key}),
                    headers=BASE_HEADERS)
                with op.open(req, timeout=15) as resp:
                    r = json.loads(resp.read().decode("utf-8", "replace"))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[bili] poll error: {e}")
                continue

            code = (r.get("data") or {}).get("code")
            if code == 86038:
                yield event.plain_result("二维码过期了，再发一次 /bili_login。")
                return
            if code != 0:
                continue

            jar = {c.name: c.value for c in cj}
            q = urllib.parse.parse_qs(
                urllib.parse.urlparse((r.get("data") or {}).get("url", "")).query)
            for k in ("SESSDATA", "bili_jct", "DedeUserID"):
                if k in q:
                    jar[k] = q[k][0]
            if not jar.get("SESSDATA"):
                yield event.plain_result("登录成功但没拿到 SESSDATA，重试一次。")
                return
            _save(COOKIE_FILE, {
                "SESSDATA": jar["SESSDATA"],
                "bili_jct": jar.get("bili_jct", ""),
                "DedeUserID": jar.get("DedeUserID", ""),
                "saved_at": int(time.time()),
            })
            yield event.plain_result("登录好了，现在能读字幕、点赞、发评论了。")
            return

    @filter.command("bili_whoami")
    async def bili_whoami(self, event: AstrMessageEvent):
        """看当前用的是哪个 B 站账号"""
        if not self._logged_in():
            yield event.plain_result("还没登录。发 /bili_login")
            return
        try:
            d = await asyncio.to_thread(
                _json, "https://api.bilibili.com/x/web-interface/nav",
                self._cookie_header())
            data = d.get("data") or {}
            st = _load(STATE_FILE, {})
            yield event.plain_result(
                f"当前账号：{data.get('uname', '?')}（uid {data.get('mid', '?')}）\n"
                f"今天已点赞 {st.get('like', 0)}/{MAX_LIKE_PER_DAY}，"
                f"已评论 {st.get('reply', 0)}/{MAX_REPLY_PER_DAY}，"
                f"已私信 {st.get('dm', 0)}/{MAX_DM_PER_DAY}")
        except Exception as e:  # noqa: BLE001
            yield event.plain_result(f"查询失败：{e}")

    @filter.command("bili_watch")
    async def bili_watch(self, event: AstrMessageEvent, action: str = ""):
        """私信秒回巡检的开关与状态：/bili_watch · /bili_watch off · /bili_watch on
        · /bili_watch test  （test 会拿一句话走一遍生成+发送，不读你的真实消息）"""
        st = _load(STATE_FILE, {})
        act = (action or "").strip().lower()
        if act == "off":
            st["dm_watch_off"] = True
            _save(STATE_FILE, st)
            yield event.plain_result("私信秒回已关。要开回来发 /bili_watch on")
            return
        if act == "on":
            st["dm_watch_off"] = False
            _save(STATE_FILE, st)
            yield event.plain_result("私信秒回已开。")
            return
        if act == "test":
            text = "在吗，有点想你了，你今天都干嘛了"
            reply = await self._dm_compose(text)
            if not reply:
                yield event.plain_result("生成失败，看日志。")
                return
            try:
                await asyncio.to_thread(self._dm_send_sync, reply)
            except Exception as e:  # noqa: BLE001
                yield event.plain_result(f"发送失败：{e}")
                return
            yield event.plain_result(f"自测发送成功：{reply}")
            return
        running = bool(self._watch_task) and not self._watch_task.done()
        yield event.plain_result(
            f"巡检：{'运行中' if running else '未运行'}，每 {DM_WATCH_SECONDS} s 一次｜"
            f"开关：{'关' if st.get('dm_watch_off') else '开'}｜"
            f"已读到 msg_key {st.get('dm_last_key') or '（还没记）'}｜"
            f"近一小时自动回复 {len([t for t in (st.get('dm_reply_ts') or []) if time.time() - t < 3600])} 条")
