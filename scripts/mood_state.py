"""她的"小状态"：好感、心情、最近说过的话。

文本聊天（AstrBot 插件）和通话（call_loop）共用同一份 ``data/mood.json``，
这样两边看到的是同一个她——不会出现"电话里刚哄好、QQ 上又冷着脸"。

设计原则：
- 状态只影响**语气和态度**，不控制行为（不能因为心情差就不干活）
- 更新规则保守：宁可少变，别把好感刷成 0 或 100
- 写入用临时文件 + 原子替换，两边同时写也不会写出半个 JSON
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time

DATA_DIR = os.environ.get("OHWHALE_HOME", r"C:\OhWhale")
PATH = os.path.join(DATA_DIR, "data", "mood.json")

DEFAULT = {
    "affection": 50,   # 0~100
    "mood": 0,         # -5~5
    "last": [],        # 最近他说过的几句（最多 5）
    "updated": 0,
}

PRAISE = ("可爱", "好看", "厉害", "聪明", "喜欢你", "爱你", "厉害", "牛", "牛逼", "真棒", "甜")
HURT = ("笨", "蠢", "傻", "滚", "闭嘴", "烦", "讨厌", "没用", "恶心")
SAD = ("累", "难受", "崩了", "烦死", "压力", "失眠", "哭", "不想活", "emo")
TEASE = ("哈哈", "笑死", "离谱", "救命", "绷不住", "草", "乐")
SORRY = ("对不起", "抱歉", "我错了", "别生气", "原谅")
OTHER_GIRL = ("她", "别的女生", "同学", "学姐", "前女友")


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def load(path: str | None = None) -> dict:
    p = path or PATH
    try:
        with open(p, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001
        data = dict(DEFAULT)
    for key, value in DEFAULT.items():
        data.setdefault(key, value)
    if not isinstance(data.get("last"), list):
        data["last"] = []
    return data


def save(data: dict, path: str | None = None) -> None:
    p = path or PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except Exception:  # noqa: BLE001
        try:
            os.unlink(tmp)
        except OSError:
            pass


def update_from_user(text: str, path: str | None = None) -> dict:
    """按他刚说的话微调好感/心情，并记下这句。"""
    text = (text or "").strip()
    if not text:
        return load(path)
    data = load(path)
    aff = float(data["affection"])
    mood = float(data["mood"])

    if any(w in text for w in PRAISE):
        aff += 2
        mood += 1
    if any(w in text for w in HURT):
        aff -= 4
        mood -= 2
    if any(w in text for w in SAD):
        # 他难受时，好感不会掉（她该心疼），但她心情也会被带低
        mood -= 1
        aff += 1
    if any(w in text for w in TEASE):
        mood += 1
    if any(w in text for w in SORRY):
        aff += 2
        mood += 1
    if any(w in text for w in OTHER_GIRL):
        aff -= 2
        mood -= 1

    # 自然回落：心情慢慢回 0，好感是慢变量
    data["affection"] = round(_clamp(aff, 0, 100), 1)
    data["mood"] = round(_clamp(mood, -5, 5), 1)
    data["last"] = (data["last"] + [text[:60]])[-5:]
    data["updated"] = int(time.time())
    save(data, path)
    return data


def summary(path: str | None = None) -> str:
    """给模型看的一行状态（别写太长，会被塞进 system prompt）。"""
    data = load(path)
    aff = data["affection"]
    mood = data["mood"]
    if aff >= 75:
        att = "很喜欢他，听到他声音就想笑"
    elif aff >= 55:
        att = "跟他熟了，会撒娇也会损他"
    elif aff >= 35:
        att = "还在观察他，有点好奇"
    else:
        att = "对他有点冷淡，说话收着点"
    if mood >= 3:
        m = "心情很好，话多、爱撩"
    elif mood >= 1:
        m = "心情不错"
    elif mood > -1:
        m = "心情一般"
    elif mood > -3:
        m = "有点烦，说话短"
    else:
        m = "很不爽，会顶嘴但不会真生气"
    recent = "；".join(x for x in data["last"][-2:] if x)
    line = f"[状态] 好感{aff:.0f}/100（{att}）；{m}。"
    if recent:
        line += f" 他刚说过：{recent}"
    return line


if __name__ == "__main__":
    print(summary())
