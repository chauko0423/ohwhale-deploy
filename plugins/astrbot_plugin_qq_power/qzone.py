"""QQ 空间客户端（走网页版 HTTP 接口，不经过 NapCat）。

为什么不用 NapCat：NapCat 只实现了 OneBot 标准接口 + 少量扩展，
空间（说说/点赞/评论）不在里面。但 NapCat 能拿到登录态——
`get_cookies(domain="qzone.qq.com")` 给 cookie，`get_csrf_token` 直接给 bkn(=g_tk)。
拿到这两样，就能用网页版那套 CGI 干活。

接口清单（都是 user.qzone.qq.com 的 proxy 转发）：
- 发说说  emotion_cgi_publish_v6
- 列说说  emotion_cgi_msglist_v6      （自己的，JSON 干净）
- 列动态  feeds3_html_more            （好友的，勉强能解析，尽力而为）
- 点赞    likes/internal_dolike_app
- 评论    emotion_cgi_re_feeds
- 删除    emotion_cgi_del_v6

设计原则：任何一步失败都不要抛异常给 LLM，返回 {"ok": False, "err": "人话"}，
并把原始返回塞进 raw 字段，方便在 /空间 日志里查。
"""

from __future__ import annotations

import json
import re
from datetime import datetime

BASE = "https://user.qzone.qq.com/proxy/domain"
PUBLISH_URL = f"{BASE}/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"
MSGLIST_URL = f"{BASE}/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6"
REFEEDS_URL = f"{BASE}/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"
DEL_URL = f"{BASE}/taotao.qzone.qq.com/cgi-bin/emotion_cgi_delete_v6"
LIKE_URL = f"{BASE}/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app"
DETAIL_URL = f"{BASE}/taotao.qq.com/cgi-bin/emotion_cgi_msgdetail_v6"
FEEDS_URL = f"{BASE}/ic2.qzone.qq.com/cgi-bin/feeds/feeds3_html_more"
INFOCENTER_URL = "https://user.qzone.qq.com/{uin}"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

TIMEOUT = 15.0

# 空间返回码 → 人话。实测未登录就是 -3000「请先登录空间」
CODE_HINTS = {
    -3000: "空间没认登录态（cookie 或 g_tk 不对，也可能是缺 qzonetoken）",
    -10000: "内容被空间拦截了",
    -4001: "参数不对，空间拒了",
    1003: "登录态已失效，需要重新登录 QQ",
}


class QzoneError(Exception):
    pass


# ---------------- 登录态 ----------------
def gtk_from_skey(skey: str) -> int:
    """经典 g_tk：从 skey/p_skey 算 bkn。"""
    h = 5381
    for ch in skey or "":
        h += (h << 5) + ord(ch)
        h &= 0x7FFFFFFF
    return h & 0x7FFFFFFF


def parse_cookies(raw) -> dict[str, str]:
    """NapCat 返回的 cookie 可能是 str / list / dict，统一成字典。"""
    out: dict[str, str] = {}
    if not raw:
        return out
    if isinstance(raw, dict):
        cookies = raw.get("cookies", raw)
        if isinstance(cookies, str):
            return parse_cookies(cookies)
        if isinstance(cookies, list):
            for item in cookies:
                if isinstance(item, dict) and item.get("name"):
                    out[str(item["name"])] = str(item.get("value", ""))
                elif isinstance(item, str):
                    out.update(parse_cookies(item))
            return out
        return out
    if isinstance(raw, list):
        return parse_cookies({"cookies": raw})
    for part in str(raw).split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            k, v = k.strip(), v.strip()
            if k:
                out[k] = v
    return out


def _strip_jsonp(text: str) -> str:
    """去掉 _preloadCallback(...) / _Callback(...) / HTML <script> 这层壳。"""
    t = (text or "").strip()
    if not t:
        return t
    if t[0] in "{[":
        return t
    # 发说说接口会返回 <html><body><script>...frameElement.callback({...})...</script>
    if "<" in t:
        t = re.sub(r"<[^>]+>", "", t).strip()
    if t[:1] in "{[":
        return t
    m = re.search(r"^[^(]*\((.*)\)[;\s]*$", t, re.S)
    return m.group(1) if m else t


def _loads(text: str):
    """从各种壳里把 JSON 抠出来。

    空间有三种返回：纯 JSON、`_preloadCallback({...})`、以及发说说那种
    `<html><script>var cb;try{...}catch(e){...} frameElement.callback({...})</script>`。
    最后一种里面还有 catch(e) 带括号，正则抠不干净，只能扫括号配对。
    """
    t = _strip_jsonp(text)
    try:
        return json.loads(t)
    except Exception:
        pass
    start = t.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(t)):
            ch = t[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(t[start:i + 1])
                    except Exception:
                        break
        start = t.find("{", start + 1)
    return None


def _snippet(text: str, n: int = 160) -> str:
    s = re.sub(r"\s+", " ", (text or "")).strip()
    return s[:n]


class QzoneClient:
    def __init__(self, cookies: dict[str, str], uin: str, gtk: int):
        self.cookies = cookies or {}
        self.uin = str(uin)
        self.gtk = int(gtk or 0)
        self.skey = self.cookies.get("p_skey") or self.cookies.get("skey") or ""
        self.qzonetoken = ""

    # ---------------- 构造 ----------------
    @classmethod
    async def from_bot(cls, bot, uin: str = "") -> "QzoneClient":
        """从 NapCat 拿登录态。bot 是 aiocqhttp 的 CQHttp 实例。

        NapCat 的 get_cookies 返回 {cookies, bkn}，而且**对 *.qzone.qq.com 是拿 p_skey
        算 bkn 的**——这正是空间要的 g_tk。别去用 get_csrf_token：那个只按 skey 算，
        域名不对，发说说会被空间当未登录拒掉（2026-10-21 实测踩过）。
        """
        if bot is None:
            raise QzoneError("拿不到 QQ 连接对象（NapCat 可能没连上）")
        try:
            raw = await bot.call_action("get_cookies", domain="qzone.qq.com")
        except Exception as e:  # noqa: BLE001
            raise QzoneError(f"get_cookies 失败：{e}") from e

        cookies = parse_cookies(raw)
        if not cookies:
            raise QzoneError("NapCat 没返回 cookie，可能没登录")

        actual_uin = (uin or cookies.get("uin", "").lstrip("o")
                      or cookies.get("p_uin", "").lstrip("o"))

        # g_tk 一律优先用 p_skey 自己算。
        # NapCat 对 qzone 域给的 bkn 恰好就是 p_skey 版，但不保证：
        # LLOneBot 的 get_cookies 是用 skey 算的（源码 GetCookie.ts），
        # 用它调空间会被判 -3000。所以别信返回值，自己算最稳。
        gtk = 0
        pskey = cookies.get("p_skey") or ""
        if pskey:
            gtk = gtk_from_skey(pskey)
        if not gtk and isinstance(raw, dict):
            try:
                gtk = int(raw.get("bkn") or 0)
            except (TypeError, ValueError):
                gtk = 0
        if not gtk:
            gtk = gtk_from_skey(cookies.get("skey") or "")
        if not gtk:
            try:
                tok = await bot.call_action("get_csrf_token")
                gtk = int(tok.get("token") or 0) if isinstance(tok, dict) else int(tok or 0)
            except Exception:  # noqa: BLE001
                gtk = 0

        if not actual_uin:
            raise QzoneError("cookie 里没有 uin，登录态不完整")
        if not gtk:
            raise QzoneError("拿不到 g_tk（cookie 里缺 p_skey/skey），空间接口调不了")
        return cls(cookies, actual_uin, gtk)

    # ---------------- 底层请求 ----------------
    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": UA,
            "Referer": f"https://user.qzone.qq.com/{self.uin}",
            "Origin": "https://user.qzone.qq.com",
            "Accept": "*/*",
        }

    def _base_form(self) -> dict[str, str]:
        return {
            "uin": self.uin,
            "hostuin": self.uin,
            "format": "json",
            "qzreferrer": f"https://user.qzone.qq.com/{self.uin}",
        }

    async def _req(self, method: str, url: str, **kw) -> tuple[bool, dict, str]:
        try:
            import httpx
        except Exception as e:  # noqa: BLE001
            return False, {}, f"没装 httpx：{e}"
        params = {"g_tk": str(self.gtk)}
        # 一定要无条件 pop：传进来的是空 dict 时，若只 pop 非空的，
        # **kw 里还留着 params，和下面的 params= 撞成 "multiple values"（2026-10-21 实测踩到）
        extra = kw.pop("params", None)
        if extra:
            params.update(extra)
        try:
            async with httpx.AsyncClient(
                timeout=TIMEOUT, follow_redirects=True, headers=self._headers(),
                cookies=self.cookies,
            ) as cli:
                resp = await cli.request(method, url, params=params, **kw)
            return True, {"status": resp.status_code, "text": resp.text}, ""
        except Exception as e:  # noqa: BLE001
            return False, {}, f"请求失败：{e}"

    # ---------------- qzonetoken ----------------
    async def _fetch_qzonetoken(self) -> str:
        """从空间首页抠 qzonetoken。发说说有时候没有它会被判 -3000。"""
        if self.qzonetoken:
            return self.qzonetoken
        try:
            import httpx
        except Exception:  # noqa: BLE001
            return ""
        try:
            async with httpx.AsyncClient(
                timeout=TIMEOUT, follow_redirects=True, headers=self._headers(),
                cookies=self.cookies,
            ) as cli:
                resp = await cli.get(INFOCENTER_URL.format(uin=self.uin))
            for pat in (r'window\.g_qzonetoken\s*=\s*"([^"]+)"',
                        r'qzonetoken\s*[:=]\s*"([^"]+)"'):
                m = re.search(pat, resp.text)
                if m:
                    self.qzonetoken = m.group(1)
                    return self.qzonetoken
        except Exception:  # noqa: BLE001
            pass
        return ""

    # ---------------- 发说说 ----------------
    async def publish(self, text: str, ugc_right: int = 1) -> dict:
        text = (text or "").strip()
        if not text:
            return {"ok": False, "err": "内容是空的"}
        form = self._base_form()
        # 字段名是 con 不是 content，format 要 fs 不是 json —— 写成 content 会被
        # 空间判成「您未输入内容」(-10005)。2026-10-21 实测踩过。
        form.pop("format", None)
        form.update({
            "syn_tweet_verson": "1",
            "paramstr": "1",
            "pic_template": "",
            "richtype": "",
            "richval": "",
            "special_url": "",
            "subrichtype": "",
            "who": "1",
            "con": text,
            "feedversion": "1",
            "ver": "1",
            "ugc_right": str(ugc_right),   # 1 公开, 64 仅自己可见
            "to_sign": "0",
            "to_tweet": "0",
            "code_version": "1",
            "format": "fs",
        })
        for attempt in range(2):
            tok = await self._fetch_qzonetoken()
            params = {"qzonetoken": tok} if tok else {}
            if tok:
                form["qzonetoken"] = tok
            ok, payload, err = await self._req("POST", PUBLISH_URL, data=form,
                                               params=params)
            if not ok:
                return {"ok": False, "err": err}
            data = _loads(payload["text"])
            if not isinstance(data, dict):
                return {"ok": False, "err": "返回不是 JSON（可能被风控或登录失效）",
                        "raw": _snippet(payload["text"])}
            code = data.get("code", data.get("ret", -1))
            if str(code) in ("0", "None"):
                tid = ((data.get("data") or {}).get("tid")
                       or (data.get("data") or {}).get("feedid")
                       or data.get("tid") or "")
                return {"ok": True, "tid": tid, "data": data}
            # 第一次 -3000 且拿到了 token，重试一次
            if attempt == 0 and str(code) == "-3000" and tok:
                continue
            return {"ok": False, "err": _code_text(code, data), "raw": data}
        return {"ok": False, "err": "空间连试两次都没过"}

    # ---------------- 列说说 ----------------
    async def list_self(self, num: int = 10) -> dict:
        params = {
            "uin": self.uin, "ftype": "0", "sort": "0", "pos": "0",
            "num": str(max(1, min(40, num))), "replynum": "0",
            "callback": "_preloadCallback", "code_version": "1",
            "format": "json", "need_private_comment": "1",
        }
        ok, payload, err = await self._req("GET", MSGLIST_URL, params=params)
        if not ok:
            return {"ok": False, "err": err}
        data = _loads(payload["text"])
        if not isinstance(data, dict):
            return {"ok": False, "err": "返回不是 JSON（登录态可能失效）",
                    "raw": _snippet(payload["text"])}
        code = data.get("code", data.get("ret", -1))
        if str(code) not in ("0", "None") and data.get("msglist") is None:
            return {"ok": False, "err": _code_text(code, data), "raw": data}
        items = []
        for m in (data.get("msglist") or [])[:num]:
            items.append({
                "tid": m.get("tid", ""),
                "内容": _snippet(m.get("content", ""), 80),
                "时间": _fmt_ts(m.get("created_time")),
                "评论数": m.get("cmtnum", 0),
                "赞": (m.get("like") or {}).get("cnt", 0) if isinstance(m.get("like"), dict) else 0,
                "unikey": m.get("unikey", ""),
                "curlikekey": m.get("curlikekey", ""),
            })
        return {"ok": True, "count": len(items), "items": items}

    async def list_friends(self, num: int = 10) -> dict:
        """好友动态。这个接口返回的是给前端渲染的混合结构，只能尽力解析。"""
        params = {
            "uin": self.uin, "scope": "0", "view": "1", "flag": "1",
            "applist": "all", "fupdate": "1", "p": "1",
            "count": str(max(1, min(30, num))), "callback": "_Callback",
        }
        ok, payload, err = await self._req("GET", FEEDS_URL, params=params)
        if not ok:
            return {"ok": False, "err": err}
        data = _loads(payload["text"])
        if not isinstance(data, dict):
            return {"ok": False, "err": "好友动态返回无法解析（这个接口本来就不稳）",
                    "raw": _snippet(payload["text"])}
        items = []
        for it in (data.get("data") or {}).get("data", []) or []:
            if not isinstance(it, dict):
                continue
            tid = it.get("tid") or it.get("id") or ""
            if not tid:
                continue
            user = it.get("userinfo") or it.get("user") or {}
            author_uin = (str(it.get("uin") or it.get("hostuin") or it.get("hostUin")
                              or user.get("uin") or "")).strip()
            items.append({
                "tid": str(tid),
                "作者": it.get("nickname") or it.get("name") or user.get("nickname") or "",
                "uin": author_uin,
                "内容": _snippet(it.get("content") or it.get("summary") or "", 80),
                "时间": _fmt_ts(it.get("abstime") or it.get("create_time")),
                "unikey": it.get("key") or it.get("unikey") or "",
                "curlikekey": it.get("curlikekey") or "",
            })
            if len(items) >= num:
                break
        return {"ok": True, "count": len(items), "items": items}

    async def list_user(self, uin: str, num: int = 10) -> dict:
        """看某个好友的说说（用他的 uin 拉 feed，权限不够会失败）。"""
        uin = str(uin or "").strip()
        if not uin:
            return {"ok": False, "err": "缺 uin（好友的 QQ 号）"}
        params = {
            "uin": self.uin, "hostuin": uin, "ftype": "0", "sort": "0",
            "pos": "0", "num": str(max(1, min(40, num))), "replynum": "0",
            "callback": "_preloadCallback", "code_version": "1",
            "format": "json", "need_private_comment": "1",
        }
        ok, payload, err = await self._req("GET", MSGLIST_URL, params=params)
        if not ok:
            return {"ok": False, "err": err}
        data = _loads(payload["text"])
        if not isinstance(data, dict):
            return {"ok": False, "err": "返回不是 JSON（可能没有权限看这个人的说说）",
                    "raw": _snippet(payload["text"])}
        code = data.get("code", data.get("ret", -1))
        if str(code) not in ("0", "None") and data.get("msglist") is None:
            return {"ok": False, "err": _code_text(code, data), "raw": data}
        items = []
        for m in (data.get("msglist") or [])[:num]:
            items.append({
                "tid": m.get("tid", ""),
                "uin": uin,
                "内容": _snippet(m.get("content", ""), 80),
                "时间": _fmt_ts(m.get("created_time")),
                "评论数": m.get("cmtnum", 0),
                "赞": (m.get("like") or {}).get("cnt", 0) if isinstance(m.get("like"), dict) else 0,
            })
        return {"ok": True, "count": len(items), "items": items}


    # ---------------- 点赞 ----------------
    async def like(self, tid: str, unikey: str = "", curlikekey: str = "",
                   cancel: bool = False) -> dict:
        tid = str(tid or "").strip()
        if not tid:
            return {"ok": False, "err": "缺 tid（先用 list 拿）"}
        feedkey = unikey or f"{tid}_{self.uin}"
        form = {
            "qzonetoken": "",
            "feedkey": feedkey,
            "otype": "0",
            "fid": tid,
            "from": "",
            "appid": "311",
            "curlikekey": curlikekey or feedkey,
            "like": "false" if cancel else "true",
            "uin": self.uin,
            "hostuin": self.uin,
            "format": "json",
        }
        ok, payload, err = await self._req("POST", LIKE_URL, data=form)
        if not ok:
            return {"ok": False, "err": err}
        data = _loads(payload["text"])
        if isinstance(data, dict):
            code = data.get("code", data.get("ret", 0))
            if str(code) in ("0", "None") or data.get("data"):
                return {"ok": True, "data": data}
            return {"ok": False, "err": _code_text(code, data),
                    "raw": _snippet(payload["text"])}
        return {"ok": False, "err": "点赞返回无法解析", "raw": _snippet(payload["text"])}

    # ---------------- 评论 ----------------
    async def comment(self, tid: str, text: str, hostuin: str = "") -> dict:
        tid = str(tid or "").strip()
        text = (text or "").strip()
        if not tid or not text:
            return {"ok": False, "err": "缺 tid 或评论内容"}
        form = self._base_form()
        form.update({
            "topicId": tid,
            "content": text,
            "hostUin": hostuin or self.uin,
            "plat": "qzone",
            "code_version": "1",
        })
        ok, payload, err = await self._req("POST", REFEEDS_URL, data=form)
        if not ok:
            return {"ok": False, "err": err}
        data = _loads(payload["text"])
        if isinstance(data, dict):
            code = data.get("code", data.get("ret", 0))
            if str(code) in ("0", "None") or data.get("data"):
                # 评论别人的说说：接口回 ok 不一定真发出去（权限/风控）。
                # 回读一次评论列表确认，没看到就如实说没成功，别让她编"我回了"。
                if hostuin and str(hostuin) != str(self.uin):
                    check = await self.comments(tid, 20)
                    if check.get("ok"):
                        hit = any((it.get("text") or "").strip() == text
                                  for it in (check.get("items") or []))
                        if not hit:
                            return {"ok": False,
                                    "err": "空间没接收这条评论（大概率没有权限或触发验证）",
                                    "raw": _snippet(payload["text"])}
                return {"ok": True, "data": data}
            return {"ok": False, "err": _code_text(code, data),
                    "raw": _snippet(payload["text"])}
        return {"ok": False, "err": "评论返回无法解析", "raw": _snippet(payload["text"])}

    # ---------------- 读评论 ----------------
    async def comments(self, tid: str, num: int = 20) -> dict:
        """读某条说说下面的评论（别人在她说说下的留言）。"""
        tid = str(tid or "").strip()
        if not tid:
            return {"ok": False, "err": "缺 tid（先用 list 拿）"}
        params = {
            "uin": self.uin, "tid": tid, "format": "json",
            "code_version": "1", "need_private_comment": "1",
            "pos": "0", "num": str(max(1, min(50, num))),
        }
        last = ""
        for url in (DETAIL_URL, MSGLIST_URL):
            ok, payload, err = await self._req("GET", url, params=params)
            if not ok:
                last = err
                continue
            data = _loads(payload["text"])
            if not isinstance(data, dict):
                last = "返回不是 JSON"
                continue
            cl = data.get("commentlist")
            if cl is None:
                inner = data.get("data") or {}
                cl = inner.get("commentlist") or inner.get("comments")
            if cl is None and data.get("msglist"):
                for m in data["msglist"]:
                    if str(m.get("tid")) == tid:
                        cl = m.get("commentlist") or []
                        break
            if cl is not None:
                items = []
                for c in cl[:num]:
                    if not isinstance(c, dict):
                        continue
                    user = c.get("user") or {}
                    items.append({
                        "who": c.get("name") or c.get("nickname") or user.get("nickname") or "",
                        "uin": str(c.get("uin") or user.get("uin") or ""),
                        "text": _snippet(str(c.get("content") or ""), 120),
                        "time": _fmt_ts(c.get("create_time") or c.get("abstime")),
                    })
                return {"ok": True, "count": len(items), "items": items}
        return {"ok": False, "err": f"读评论失败：{last or '接口没给 commentlist'}"}


    # ---------------- 删除 ----------------
    async def delete(self, tid: str) -> dict:
        tid = str(tid or "").strip()
        if not tid:
            return {"ok": False, "err": "缺 tid"}
        form = self._base_form()
        form.pop("format", None)
        form.update({
            "tid": tid,
            "t1_source": "1",
            "code_version": "1",
            "format": "fs",
            "qzreferrer": f"https://user.qzone.qq.com/{self.uin}/311",
        })
        ok, payload, err = await self._req("POST", DEL_URL, data=form)
        if not ok:
            return {"ok": False, "err": err}
        data = _loads(payload["text"])
        if isinstance(data, dict) and str(data.get("code", data.get("ret", -1))) in ("0", "None"):
            return {"ok": True, "data": data}
        return {"ok": False, "err": _code_text(
            (data or {}).get("code", "?"), data if isinstance(data, dict) else {}),
            "raw": (data if isinstance(data, dict) else _snippet(payload["text"]))}


def _code_text(code, data: dict) -> str:
    try:
        hint = CODE_HINTS.get(int(code))
    except (TypeError, ValueError):
        hint = None
    msg = (data or {}).get("message") or (data or {}).get("msg") or ""
    return hint or f"空间返回 code={code} {msg}".strip()


def _fmt_ts(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return ""
