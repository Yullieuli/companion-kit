"""表情包的两件 previously 没做的事：**内置库** 与 **自动提取**。

为什么要有内置库：空库起步意味着她一张表情都发不出去——
`sticker.pick` 永远返回空，"按情绪挑表情"这个能力等于没上线。
所以给一份**程序生成**的最小表情库：每个情绪目录至少两张，
画的是简笔脸，不需要下载任何素材，也不用引 Pillow。

为什么要有自动提取：主人随手发来一张图/表情，该能收进库里。
但不能靠模型去"看图猜情绪"——那要花钱还不稳。
这里的做法是**从像素算**：读 PNG，算主色调、明暗、饱和度，
映射到情绪目录；再从文件名/目录名里认关键词（"笑"、"困"、"怒"）。
认不出就归到"开心"并标记 `auto:low`，让人来改。

三条取舍：
  · 内置库是**种子**，不是仓库 —— 主人自己的库可以随便删
  · 自动提取**不覆盖**已有的同名文件
  · 提取时留痕（`sticker.auto`），改主意了查得到是谁收的
"""

from __future__ import annotations

import colorsys
import hashlib
import shutil
from pathlib import Path
from typing import Any

from . import imgpng
from .store import Home

# 情绪 → 关键词（文件名/目录名里认）
MOOD_KEYWORDS: dict[str, tuple[str, ...]] = {
    "开心": ("笑", "开心", "高兴", "乐", "happy", "smile", "laugh", "haha", "233"),
    "无语": ("无语", "无语.jpg", "翻白眼", "白眼", "无奈", "meh", "dead", "speakless", "呵呵"),
    "生气": ("怒", "生气", "气", "骂", "angry", "rage", "furious", "tmd"),
    "害羞": ("害羞", "脸红", "羞", "shy", "blush", "uwu"),
    "困": ("困", "睡", "zzz", "sleep", "sleepy", "tired"),
    "得意": ("得意", "炫", "骄傲", " smug", "proud", "smug", "得意.jpg"),
    "疑惑": ("疑", "问号", "疑惑", "why", "confused", "puzzled", " huh"),
    "安慰": ("抱", "安慰", "安慰抱抱", "hug", "comfort", "pat"),
}

# 关键词 → 情绪（反向查，省得每次遍历）
_KEYWORD_INDEX: list[tuple[str, str]] = [
    (kw, mood) for mood, kws in MOOD_KEYWORDS.items() for kw in kws
]

# 内置库：每个情绪两张，够挑又不至于喧宾夺主
BUILTIN: tuple[tuple[str, str], ...] = (
    ("开心", "smile"), ("开心", "happy"),
    ("无语", "speechless"), ("无语", "flat"),
    ("生气", "angry"), ("生气", "mad"),
    ("害羞", "shy"), ("害羞", "blush"),
    ("困", "sleepy"), ("困", "zzz"),
    ("得意", "smug"), ("得意", "proud"),
    ("疑惑", "confused"), ("疑惑", "huh"),
    ("安慰", "hug"), ("安慰", "pat"),
)


# ── 画图（纯 RGB 字节，不引 Pillow）──────────────────────
def _blank(w: int, h: int, rgb: tuple[int, int, int] = (255, 255, 255)) -> bytearray:
    return bytearray(bytes(rgb) * (w * h))


def _put(buf: bytearray, w: int, h: int, x: int, y: int, c: tuple[int, int, int]) -> None:
    if 0 <= x < w and 0 <= y < h:
        i = (y * w + x) * 3
        buf[i : i + 3] = bytes(c)


def _circle(buf: bytearray, w: int, h: int, cx: float, cy: float, r: float,
            c: tuple[int, int, int]) -> None:
    r2 = r * r
    for y in range(int(cy - r), int(cy + r) + 1):
        for x in range(int(cx - r), int(cx + r) + 1):
            if (x - cx) ** 2 + (y - cy) ** 2 <= r2:
                _put(buf, w, h, x, y, c)


def _line(buf: bytearray, w: int, h: int, x0: float, y0: float, x1: float, y1: float,
          c: tuple[int, int, int], thick: float = 2.0) -> None:
    steps = int(max(abs(x1 - x0), abs(y1 - y0)) * 2) + 1
    for i in range(steps + 1):
        t = i / steps
        _circle(buf, w, h, x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, thick / 2, c)


def _arc(buf: bytearray, w: int, h: int, cx: float, cy: float, r: float,
         a0: float, a1: float, c: tuple[int, int, int], thick: float = 2.0) -> None:
    import math

    # 步数按周长算，不然圆会画成虚线（96px 的脸只走 24 步就断得很明显）
    span = abs(a1 - a0)
    steps = max(8, int(r * span / 180.0 * 6) + 8)
    for i in range(steps + 1):
        a = math.radians(a0 + (a1 - a0) * i / steps)
        _circle(buf, w, h, cx + r * math.cos(a), cy + r * math.sin(a), thick / 2, c)


# 表情 → (底色, 线条色, 嘴形, 眼形)
_FACES: dict[str, tuple[tuple[int, int, int], str, str]] = {
    "smile":       ((255, 214, 165), "smile", "dot"),
    "happy":       ((255, 224, 165), "grin", "dot"),
    "speechless":  ((226, 226, 230), "flat", "line"),
    "flat":        ((214, 214, 218), "flat", "line"),
    "angry":       ((255, 186, 160), "frown", "angry"),
    "mad":         ((255, 160, 140), "frown", "angry"),
    "shy":         ((255, 200, 200), "smile", "shy"),
    "blush":       ((250, 190, 195), "smile", "shy"),
    "sleepy":      ((200, 210, 240), "sleep", "line"),
    "zzz":         ((205, 215, 245), "sleep", "line"),
    "smug":        ((255, 216, 170), "smirk", "half"),
    "proud":       ((255, 220, 175), "smile", "squint"),
    "confused":    ((230, 225, 210), "wavy", "dot"),
    "huh":         ((225, 220, 205), "wavy", "dot"),
    "hug":         ((255, 200, 195), "smile", "heart"),
    "pat":         ((250, 205, 195), "smile", "wink"),
}


def _draw(name: str, size: int = 96) -> bytes:
    """画一张 96×96 的简笔表情，返回 RGB 字节。"""
    w = h = size
    bg, mouth, eyes = _FACES.get(name, ((240, 240, 240), "flat", "dot"))
    buf = _blank(w, h, (252, 252, 252))
    cx = cy = size / 2
    r = size * 0.38
    # 脸
    _circle(buf, w, h, cx, cy, r, bg)
    _arc(buf, w, h, cx, cy, r, 0, 360, (120, 100, 90), 2.0)
    ink = (70, 55, 50)
    ex = size * 0.15
    ey = cy - size * 0.08
    # 眼
    if eyes == "dot":
        _circle(buf, w, h, cx - ex, ey, size * 0.035, ink)
        _circle(buf, w, h, cx + ex, ey, size * 0.035, ink)
    elif eyes == "line":
        _line(buf, w, h, cx - ex - 6, ey, cx - ex + 6, ey, ink, 2.5)
        _line(buf, w, h, cx + ex - 6, ey, cx + ex + 6, ey, ink, 2.5)
    elif eyes == "angry":
        _line(buf, w, h, cx - ex - 7, ey - 7, cx - ex + 5, ey + 2, ink, 2.5)
        _line(buf, w, h, cx + ex + 7, ey - 7, cx + ex - 5, ey + 2, ink, 2.5)
        _circle(buf, w, h, cx - ex, ey + 3, size * 0.03, ink)
        _circle(buf, w, h, cx + ex, ey + 3, size * 0.03, ink)
    elif eyes == "shy":
        _arc(buf, w, h, cx - ex, ey, size * 0.05, 180, 360, ink, 2.5)
        _arc(buf, w, h, cx + ex, ey, size * 0.05, 180, 360, ink, 2.5)
    elif eyes == "half":
        # 半睁眼：上眼睑压下来一点，显得"斜着眼看人"——得意的招牌
        _circle(buf, w, h, cx - ex, ey, size * 0.035, ink)
        _circle(buf, w, h, cx + ex, ey, size * 0.035, ink)
        _line(buf, w, h, cx - ex - size * 0.045, ey - size * 0.025,
              cx - ex + size * 0.045, ey - size * 0.03, ink, 2.2)
        _line(buf, w, h, cx + ex - size * 0.045, ey - size * 0.03,
              cx + ex + size * 0.045, ey - size * 0.025, ink, 2.2)
    elif eyes == "squint":
        # 眯眼：一条上翘的弧——骄傲的招牌
        _arc(buf, w, h, cx - ex, ey + size * 0.02, size * 0.06, 200, 340, ink, 2.5)
        _arc(buf, w, h, cx + ex, ey + size * 0.02, size * 0.06, 200, 340, ink, 2.5)
    elif eyes == "heart":
        # 心形眼：安慰的招牌
        for hx in (cx - ex, cx + ex):
            _circle(buf, w, h, hx - size * 0.022, ey - size * 0.012, size * 0.026, ink)
            _circle(buf, w, h, hx + size * 0.022, ey - size * 0.012, size * 0.026, ink)
            for k in range(-5, 6):
                yy = ey + size * 0.004 + k * size * 0.008
                half = size * 0.045 * (1 - abs(k) / 6.0)
                for xx in range(int(hx - half), int(hx + half) + 1):
                    _put(buf, w, h, xx, int(yy), ink)
    elif eyes == "wink":
        # 眨眼：一只圆一只闭——"摸摸头"的招牌
        _circle(buf, w, h, cx - ex, ey, size * 0.035, ink)
        _arc(buf, w, h, cx + ex, ey, size * 0.05, 180, 360, ink, 2.5)
    # 嘴
    my = cy + size * 0.16
    if mouth == "smile":
        _arc(buf, w, h, cx, my - size * 0.05, size * 0.13, 20, 160, ink, 2.5)
    elif mouth == "grin":
        _arc(buf, w, h, cx, my - size * 0.06, size * 0.14, 15, 165, ink, 3.0)
    elif mouth == "frown":
        _arc(buf, w, h, cx, my + size * 0.12, size * 0.13, 200, 340, ink, 2.5)
    elif mouth == "flat":
        _line(buf, w, h, cx - size * 0.09, my, cx + size * 0.09, my, ink, 2.5)
    elif mouth == "wavy":
        _line(buf, w, h, cx - size * 0.11, my, cx - size * 0.03, my - 3, ink, 2.5)
        _line(buf, w, h, cx - size * 0.03, my - 3, cx + size * 0.03, my + 3, ink, 2.5)
        _line(buf, w, h, cx + size * 0.03, my + 3, cx + size * 0.09, my, ink, 2.5)
    elif mouth == "smirk":
        _line(buf, w, h, cx - size * 0.08, my + 2, cx + size * 0.10, my - 2, ink, 2.5)
    elif mouth == "sleep":
        _circle(buf, w, h, cx, my, size * 0.035, ink)
    return bytes(buf)


def builtin_png(mood: str, name: str) -> bytes:
    """内置表情的 PNG 字节。"""
    import io
    import struct
    import zlib

    rgb = _draw(name)
    size = 96
    raw = b"".join(b"\x00" + rgb[y * size * 3 : (y + 1) * size * 3] for y in range(size))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


# ── 播种内置库 ───────────────────────────────────────────
def seed_builtin(home: Home, *, force: bool = False) -> dict[str, Any]:
    """把内置表情写进库。已经有的不覆盖（主人改过的留着）。"""
    from . import sticker

    written: list[str] = []
    skipped: list[str] = []
    for mood, name in BUILTIN:
        dest = home.root / sticker.DIR / mood / f"builtin_{name}.png"
        if dest.exists() and not force:
            skipped.append(f"{mood}/{name}")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(builtin_png(mood, name))
        try:
            sticker.add(home, dest, mood=mood, tags=["内置"], name=f"builtin_{name}")
        except Exception:
            # 索引写失败不该让整批种子回滚 —— 图已经在库里了
            pass
        written.append(f"{mood}/{name}")
    return {"written": written, "skipped": skipped, "total": len(BUILTIN)}


# ── 自动提取 ─────────────────────────────────────────────
def _mood_from_text(text: str) -> tuple[str, str]:
    """从文件名/目录名认情绪。返回 (情绪, 命中的关键词)。"""
    t = (text or "").lower()
    best = ("开心", "")
    best_len = 0
    for kw, mood in _KEYWORD_INDEX:
        if kw in t and len(kw) > best_len:
            best, best_len = (mood, kw), len(kw)
    return best


def _mood_from_pixels(path: Path) -> tuple[str, str, float]:
    """从像素猜情绪：返回 (情绪, 依据, 置信度)。

    做法很土但够用：取中心区域平均色 → 算 HSV →
      · 饱和度很低（灰） → 无语
      · 色相偏红 → 生气
      · 明度高且暖 → 开心
      · 明度低且冷 → 困
    """
    try:
        w, h, rgb = imgpng.read_png(path)
    except Exception:
        return ("开心", "读不出图", 0.0)
    if not w or not h:
        return ("开心", "读不出图", 0.0)
    # 中心 60% 区域（边缘常常是背景/边框）
    x0, x1 = int(w * 0.2), int(w * 0.8)
    y0, y1 = int(h * 0.2), int(h * 0.8)
    n = 0
    r = g = b = 0
    for y in range(y0, y1, max(1, (y1 - y0) // 24)):
        for x in range(x0, x1, max(1, (x1 - x0) // 24)):
            i = (y * w + x) * 3
            r += rgb[i]
            g += rgb[i + 1]
            b += rgb[i + 2]
            n += 1
    if not n:
        return ("开心", "读不出图", 0.0)
    r, g, b = r / n / 255, g / n / 255, b / n / 255
    hh, ll, ss = colorsys.rgb_to_hls(r, g, b)
    if ss < 0.12:
        return ("无语", "整体偏灰", 0.45)
    if 0.95 <= hh <= 1.05 or hh <= 0.05:
        if ll < 0.45:
            return ("生气", "偏红且暗", 0.4)
        return ("开心", "偏红", 0.35)
    if hh <= 0.16:
        return ("得意", "偏橙", 0.35)
    if 0.16 < hh <= 0.45:
        return ("开心", "偏暖黄", 0.45)
    if 0.45 < hh <= 0.72:
        return ("疑惑", "偏绿", 0.3)
    if ll < 0.4:
        return ("困", "偏暗", 0.4)
    return ("安慰", "偏冷亮", 0.25)


def extract(
    home: Home,
    src: str | Path,
    *,
    mood: str = "",
    tags: list[str] | None = None,
    now_iso: str = "",
) -> dict[str, Any]:
    """把一张图收进表情库，自动判断情绪并打标签。

    判断顺序：**文件名关键词 > 像素颜色 > 默认**。
    关键词最准（主人自己命名的），像素只是兜底。
    """
    from . import sticker

    p = Path(src)
    if not p.exists():
        raise FileNotFoundError(str(p))
    if p.suffix.lower() not in sticker.EXTS:
        raise ValueError(f"不支持的格式：{p.suffix}")

    # 同图不入库两次
    digest = hashlib.sha1(p.read_bytes()).hexdigest()[:10]
    d = sticker._index(home)
    for name, it in (d.get("items") or {}).items():
        if hashlib.sha1((home.root / it["file"]).read_bytes()).hexdigest()[:10] == digest \
                if (home.root / it["file"]).exists() else False:
            return {"ok": True, "skipped": True, "name": name, "why": "这张已经在库里了"}

    guessed_mood, reason, conf = _mood_from_pixels(p)
    kw_mood, kw = _mood_from_text(p.name + " " + p.parent.name)
    final_mood = mood or (kw_mood if kw else guessed_mood)
    if final_mood not in sticker.MOODS:
        final_mood = "开心"
    auto_tags = list(tags or [])
    if kw:
        auto_tags.append(f"kw:{kw}")
    if not mood:
        auto_tags.append(f"auto:{reason}")
    auto_tags.append("auto")

    out = sticker.add(home, p, mood=final_mood, tags=auto_tags, name=digest)
    out["auto"] = {
        "mood_from": "given" if mood else ("keyword" if kw else "pixels"),
        "keyword": kw,
        "pixel_guess": guessed_mood,
        "pixel_reason": reason,
        "confidence": conf,
    }
    home.append_jsonl("logs/sticker.jsonl", {
        "at": now_iso, "event": "auto_extract", "name": out["name"],
        "mood": final_mood, "from": out["auto"]["mood_from"],
    })
    return out


def extract_dir(home: Home, directory: str | Path, *, limit: int = 50,
                now_iso: str = "") -> dict[str, Any]:
    """从一个目录里批量提取。"""
    d = Path(directory)
    if not d.is_dir():
        raise NotADirectoryError(str(d))
    files = [p for p in sorted(d.iterdir())
             if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp")]
    added, skipped, failed = [], [], []
    for p in files[:limit]:
        try:
            r = extract(home, p, now_iso=now_iso)
            (skipped if r.get("skipped") else added).append(p.name)
        except Exception as e:
            failed.append(f"{p.name}: {e}")
    return {"added": added, "skipped": skipped, "failed": failed,
            "scanned": len(files[:limit]), "total": len(files)}
