"""表情包引擎：从一句话里读出情绪，挑一张合适的表情发出去。

三层来源，按优先级：
1. 本地图库 `stickers/<情绪>/*.png|jpg|gif|webp`——真人表情包，四季往里丢就行
2. 图库整个为空时，用 Pillow 画一张「白字黑边」的文字表情（不需要任何素材）
3. 都没有就返回 None，插件安静跳过

去重：同一个会话最近用过的图不再重复，避免连续两次发同一张。
"""

from __future__ import annotations

import hashlib
import os
import random
import time

# 情绪表：顺序即优先级（分数相同时排前面的赢）
# 「讨厌」这种两头沾的词放在害羞里，因为鲸鲸说出来大概率是娇嗔而不是真生气
EMOTIONS: dict[str, tuple[str, ...]] = {
    "laugh": ("哈哈", "笑死", "嘿嘿", "233", "绷不住", "忍不住", "笑", "乐"),
    "happy": ("开心", "高兴", "好耶", "太好了", "真好", "舒服", "愉快", "棒"),
    "shy": ("害羞", "脸红", "别说了", "讨厌", "唔", "捂脸", "羞", "不好意思"),
    "love": ("喜欢你", "想你", "抱抱", "亲亲", "贴贴", "爱你", "想见你"),
    "sad": ("难过", "失落", "想哭", "委屈", "不开心", "唉", "空落落"),
    "cry": ("呜呜", "哭", "眼泪", "难受", "崩了", "忍不住了"),
    "angry": ("生气", "好烦", "烦死", "哼", "过分", "不理你", "气死"),
    "sleepy": ("困", "睡觉", "累", "晚安", "打哈欠", "没力气", "好累"),
    "confuse": ("疑惑", "不懂", "奇怪", "为啥", "什么鬼", "啊？", "嗯？"),
    "proud": ("得意", "厉害吧", "哼哼", "我赢了", "当然", "小看我"),
}

# 情绪 → 文字表情兜底文案（图库为空时用）
FALLBACK_TEXT = {
    "laugh": "哈哈",
    "happy": "开心",
    "shy": "害羞",
    "love": "抱抱",
    "sad": "难过",
    "cry": "呜呜",
    "angry": "哼",
    "sleepy": "困了",
    "confuse": "嗯？",
    "proud": "哼哼",
}

EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")

# 太短的话不值得配表情：「嗯」「哦」这种本身就是半个表情
MIN_TEXT_LEN = 4

FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyhbd.ttc",   # 微软雅黑 粗体
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
)


def score_emotion(text: str) -> tuple[str | None, int]:
    """返回 (情绪, 命中强度)。命中强度 = 关键词命中的总次数。"""
    if not text or len(text.strip()) < MIN_TEXT_LEN:
        return None, 0
    best, best_score = None, 0
    for emo, words in EMOTIONS.items():
        score = sum(text.count(w) for w in words)
        if score > best_score:
            best, best_score = emo, score
    return best, best_score


def sticker_probability(score: int) -> float:
    """命中越强越容易发。1 次命中 0.30，2 次 0.45，3 次以上封顶 0.60。"""
    if score <= 0:
        return 0.0
    return min(0.60, 0.15 + 0.15 * score)


class StickerLibrary:
    def __init__(self, root: str, text_cache_dir: str | None = None):
        self.root = root
        self.text_cache = text_cache_dir or os.path.join(root, "_text")
        self._index: dict[str, list[str]] = {}
        self._recent: dict[str, list[str]] = {}   # umo -> 最近用过的图
        self._scanned_at = 0.0
        self._font_path = self._pick_font()

    # ---------------- 图库 ----------------
    def scan(self, force: bool = False) -> dict[str, list[str]]:
        """扫一遍图库。60 秒内复用上次结果，四季中途加图也能自动发现。"""
        now = time.time()
        if not force and self._index and now - self._scanned_at < 60:
            return self._index
        index: dict[str, list[str]] = {}
        for emo in EMOTIONS:
            d = os.path.join(self.root, emo)
            files: list[str] = []
            if os.path.isdir(d):
                for name in os.listdir(d):
                    if name.lower().endswith(EXTS):
                        p = os.path.join(d, name)
                        if os.path.getsize(p) > 0:
                            files.append(p)
            if files:
                index[emo] = sorted(files)
        self._index = index
        self._scanned_at = now
        return index

    def total(self) -> int:
        return sum(len(v) for v in self.scan().values())

    def pick(self, emotion: str, umo: str = "", fallback_any: bool = True) -> str | None:
        """按情绪挑一张本地图。没有对应情绪时，fallback_any 允许从别的情绪里挑。"""
        index = self.scan()
        if not index:
            return None
        pool = list(index.get(emotion, []))
        if not pool and fallback_any:
            pool = [p for files in index.values() for p in files]
        if not pool:
            return None

        recent = self._recent.setdefault(umo, [])
        fresh = [p for p in pool if p not in recent]
        choice = random.choice(fresh or pool)

        recent.append(choice)
        if len(recent) > 6:
            del recent[:-6]
        return choice

    # ---------------- 文字表情兜底 ----------------
    def _pick_font(self) -> str | None:
        for p in FONT_CANDIDATES:
            if os.path.exists(p):
                return p
        return None

    def text_sticker(self, emotion: str) -> str | None:
        """图库为空时，现画一张白字黑边表情。返回文件路径。"""
        text = FALLBACK_TEXT.get(emotion)
        if not text or not self._font_path:
            return None
        os.makedirs(self.text_cache, exist_ok=True)
        key = hashlib.md5(f"{emotion}|{text}|v1".encode()).hexdigest()[:12]
        out = os.path.join(self.text_cache, f"{emotion}_{key}.png")
        if os.path.exists(out):
            return out
        try:
            from PIL import Image, ImageDraw, ImageFont
        except Exception:
            return None

        size = 240
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        font_size = 120 if len(text) <= 2 else 88
        try:
            font = ImageFont.truetype(self._font_path, font_size)
        except Exception:
            return None
        try:
            box = draw.textbbox((0, 0), text, font=font, stroke_width=6)
        except Exception:
            return None
        w, h = box[2] - box[0], box[3] - box[1]
        xy = ((size - w) / 2 - box[0], (size - h) / 2 - box[1])
        draw.text(xy, text, font=font, fill=(255, 255, 255, 255),
                  stroke_width=6, stroke_fill=(20, 20, 20, 255))
        img.save(out, "PNG")
        return out

    # ---------------- 统一入口 ----------------
    def get(self, emotion: str, umo: str = "") -> str | None:
        p = self.pick(emotion, umo)
        if p:
            return p
        return self.text_sticker(emotion)
