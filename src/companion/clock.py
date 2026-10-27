"""clock：时间感知与作息。

两件事必须分开：
  · 时间感知 —— "现在几点、周几、距上次聊天多久"，这是给她看的
  · 作息窗口 —— "现在该不该醒"，这是给调度器看的

历史上把这两件混在一起，结果她会在凌晨两点说"刚睡醒"。
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any

_WEEK = "一二三四五六日"


def parse_hhmm(value: str) -> int:
    """'20:00' → 1200；'24:00' → 1440；非法抛 ValueError。"""
    try:
        h, m = str(value).split(":")
        h, m = int(h), int(m)
    except Exception:
        raise ValueError(f"时间格式不对：{value}")
    if h == 24 and m == 0:
        return 1440
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"时间超出范围：{value}")
    return h * 60 + m


def ago_text(seconds: float) -> str:
    """秒 → 人话。负数（未来）当成刚刚。"""
    s = max(0.0, float(seconds))
    if s < 60:
        return "刚刚"
    if s < 3600:
        return f"{int(s // 60)} 分钟前"
    if s < 86400:
        return f"{int(s // 3600)} 小时前"
    days = int(s // 86400)
    return f"{days} 天前" if days < 30 else "很久以前"


def describe(now: datetime | None = None, last_chat: float | None = None) -> str:
    """'周二 21:07（距上次聊天 6 小时前）'。"""
    now = now or datetime.now()
    base = f"周{_WEEK[now.weekday()]} {now:%H:%M}"
    if last_chat is None:
        return base
    delta = now.timestamp() - float(last_chat)
    return f"{base}（距上次聊天 {ago_text(delta)}）"


def windows(rhythm: dict[str, Any]) -> list[dict[str, Any]]:
    """把配置摊平成可比较的分钟区间；支持跨午夜。"""
    raw = rhythm.get("windows")
    if not isinstance(raw, list) or not raw:
        raw = [{"start": "20:00", "end": "24:00", "wakes": 2}]
    out = []
    for w in raw:
        start = parse_hhmm(w.get("start", "20:00"))
        end = parse_hhmm(w.get("end", "24:00"))
        wrap = end <= start
        out.append(
            {
                "start": start,
                "end": end + 1440 if wrap else end,
                "wrap": wrap,
                "label": f"{w.get('start', '20:00')}–{w.get('end', '24:00')}",
                "wakes": max(0, min(12, int(w.get("wakes", 1) or 0))),
            }
        )
    return out


def minutes_of(now: datetime) -> int:
    return now.hour * 60 + now.minute


def in_window(rhythm: dict[str, Any], now: datetime | None = None) -> bool:
    now = now or datetime.now()
    m = minutes_of(now)
    for w in windows(rhythm):
        if w["wrap"]:
            if m >= w["start"] or m < w["end"] - 1440:
                return True
        elif w["start"] <= m < w["end"]:
            return True
    return False


def in_sleep(rhythm: dict[str, Any], now: datetime | None = None) -> bool:
    """睡眠段。配置缺失就当没睡。"""
    now = now or datetime.now()
    sl = rhythm.get("sleep") or {}
    if not sl.get("at") or not sl.get("wake"):
        return False
    try:
        at = parse_hhmm(sl["at"])
        wake = parse_hhmm(sl["wake"])
    except ValueError:
        return False
    m = minutes_of(now)
    if at > wake:  # 跨午夜，如 00:30 → 09:00
        return m >= at or m < wake
    return at <= m < wake


def random_moment(start_min: int, end_min: int, rnd: Any) -> int:
    """在 [start, end) 里随机取一分钟。rnd 注入，方便测试。"""
    span = max(1, end_min - start_min)
    return start_min + int(rnd.random() * span)


def next_window_after(rhythm: dict[str, Any], now: datetime | None = None) -> dict[str, Any] | None:
    """今天剩下/即将到来的窗口；跨午夜时返回明早那个。"""
    now = now or datetime.now()
    m = minutes_of(now)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cands = []
    for w in windows(rhythm):
        for day_offset in (0, 1):
            base = today + timedelta(days=day_offset)
            start = base + timedelta(minutes=w["start"])
            end = base + timedelta(minutes=w["end"])
            if end > now:
                cands.append({**w, "starts_at": start, "ends_at": end})
    cands.sort(key=lambda x: (x["ends_at"], x["starts_at"]))
    return cands[0] if cands else None


def sleep_until(rhythm: dict[str, Any], now: datetime | None = None) -> datetime | None:
    """如果现在在睡眠段，返回醒来时刻。"""
    now = now or datetime.now()
    if not in_sleep(rhythm, now):
        return None
    sl = rhythm["sleep"]
    wake = parse_hhmm(sl["wake"])
    at = parse_hhmm(sl["at"])
    base = now.replace(hour=0, minute=0, second=0, microsecond=0)
    target = base + timedelta(minutes=wake)
    if at > wake and minutes_of(now) >= at:
        target += timedelta(days=1)
    return target


def greeting_kind(rhythm: dict[str, Any], now: datetime | None = None) -> str | None:
    """早上/晚上问候。只在当天第一次开口时由调用方决定是否使用。"""
    now = now or datetime.now()
    g = rhythm.get("greeting") or {}
    m = minutes_of(now)
    if g.get("morning") and 5 * 60 <= m < 10 * 60:
        return "morning"
    if g.get("night") and 22 * 60 <= m or (g.get("night") and m < 2 * 60):
        return "night"
    return None
