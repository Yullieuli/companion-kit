"""sticker：表情库。

不是"随机发个表情"，是**按情绪挑一张**：她这会儿无语，就发无语的那类。
所以库里每个表情挂两个维度：
  mood  情绪目录（开心/无语/生气/害羞/困/得意/疑惑/安慰）
  tags  内容标签（猫/狗/翻白眼/鼓掌…），供更细的匹配

三条取舍：
  · 只存路径不存二进制进索引 —— 索引要能一眼看完
  · 最近发过的降权 —— 同一个表情连发三次就不像人了
  · 没有合适的就返回空，**不凑合** —— 宁可这条不发
"""

from __future__ import annotations

import hashlib
import random
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from .store import Home

DIR = "stickers"
INDEX = "stickers/index.json"
MOODS = ["开心", "无语", "生气", "害羞", "困", "得意", "疑惑", "安慰"]
EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def _index(home: Home) -> dict[str, Any]:
    d = home._json(INDEX, {})
    d.setdefault("items", {})
    return d


def _save(home: Home, d: dict[str, Any]) -> None:
    home.save_json(INDEX, d)


def add(
    home: Home,
    src: str | Path,
    *,
    mood: str = "开心",
    tags: list[str] | None = None,
    name: str = "",
) -> dict[str, Any]:
    src = Path(src)
    if not src.exists():
        raise FileNotFoundError(str(src))
    if src.suffix.lower() not in EXTS:
        raise ValueError(f"不支持的格式：{src.suffix}")
    mood = mood if mood in MOODS else "开心"
    name = name or hashlib.sha1(src.read_bytes()).hexdigest()[:10]
    dest = home.root / DIR / mood / f"{name}{src.suffix.lower()}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        shutil.copy2(src, dest)
    d = _index(home)
    d["items"][name] = {
        "mood": mood,
        "tags": sorted({*(tags or [])}),
        "file": str(dest.relative_to(home.root)).replace("\\", "/"),
        "added": datetime.now().strftime("%Y-%m-%d"),
        "sent": int((d["items"].get(name) or {}).get("sent") or 0),
        "last_sent": (d["items"].get(name) or {}).get("last_sent") or "",
    }
    _save(home, d)
    return {"name": name, "mood": mood, "file": d["items"][name]["file"], "tags": d["items"][name]["tags"]}


def pick(
    home: Home,
    mood: str = "",
    tags: list[str] | None = None,
    *,
    avoid_recent: int = 5,
    rnd: Any | None = None,
) -> dict[str, Any] | None:
    """挑一张。没有对得上的就返回 None（不凑合）。"""
    d = _index(home)
    items = d.get("items") or {}
    if not items:
        return None
    r = rnd or random
    recent = sorted(
        (k for k, v in items.items() if v.get("last_sent")),
        key=lambda k: items[k]["last_sent"],
        reverse=True,
    )[:avoid_recent]
    want_tags = set(tags or [])

    scored: list[tuple[float, str]] = []
    for name, v in items.items():
        s = 0.0
        if mood and v.get("mood") == mood:
            s += 3.0
        hit = want_tags & set(v.get("tags") or [])
        s += 1.5 * len(hit)
        s -= 0.4 * min(3, int(v.get("sent") or 0))   # 用多了就没劲了
        if name in recent:
            s -= 2.5
        if s <= 0 and mood:
            continue
        scored.append((s + r.random() * 0.3, name))

    if not scored:
        return None
    scored.sort(reverse=True)
    best = scored[0][1]
    v = items[best]
    return {"name": best, "mood": v.get("mood"), "tags": v.get("tags") or [],
            "path": str(home.root / v["file"]), "score": round(scored[0][0], 2)}


def mark_sent(home: Home, name: str, now: datetime | None = None) -> bool:
    d = _index(home)
    it = (d.get("items") or {}).get(name)
    if not it:
        return False
    it["sent"] = int(it.get("sent") or 0) + 1
    it["last_sent"] = (now or datetime.now()).isoformat(timespec="seconds")
    _save(home, d)
    home.append_jsonl("logs/group.jsonl", {"at": it["last_sent"], "event": "sticker", "name": name})
    return True


def stats(home: Home) -> dict[str, Any]:
    items = (_index(home).get("items") or {})
    by_mood: dict[str, int] = {}
    for v in items.values():
        by_mood[v.get("mood") or "?"] = by_mood.get(v.get("mood") or "?", 0) + 1
    return {"total": len(items), "by_mood": by_mood, "moods": MOODS}


def remove(home: Home, name: str) -> bool:
    d = _index(home)
    it = (d.get("items") or {}).pop(name, None)
    if not it:
        return False
    f = home.root / it["file"]
    if f.exists():
        f.unlink()
    _save(home, d)
    return True
