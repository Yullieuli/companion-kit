"""quota：主动开口的闸。

三条都写死在代码里，不靠提示词：
  · 每天上限（默认 8 次）
  · 两次之间最小间隔（默认 45 分钟）
  · 沉默衰减：主人 3 天没回应 → 上限降到 1；7 天 → 0（但日记照写）

"沉默衰减"不是惩罚，是让她自己察觉到"最近没人接话"，从而少说一点。
"""

from __future__ import annotations

import copy
from datetime import date, datetime
from typing import Any

DEFAULTS = {
    "daily": 8,
    "gap_min": 45,
    "silence_soft_days": 3,
    "silence_hard_days": 7,
    "silent_cap": 1,
}


def default_state(today: str | None = None) -> dict[str, Any]:
    return {
        "date": today or date.today().isoformat(),
        "count": 0,
        "last_at": 0.0,
        "last_reason": "",
    }


def _roll(state: dict[str, Any], now: datetime) -> dict[str, Any]:
    """跨天重置。返回新对象，不改入参。"""
    out = copy.deepcopy(state) if state else default_state()
    today = now.date().isoformat()
    if out.get("date") != today:
        out.update(default_state(today))
    return out


def owner_silent_days(owner_last_seen: float | None, now: datetime) -> float:
    if not owner_last_seen:
        return 0.0
    return max(0.0, (now.timestamp() - float(owner_last_seen)) / 86400.0)


def cap_for(cfg: dict[str, Any], owner_last_seen: float | None, now: datetime) -> int:
    c = {**DEFAULTS, **(cfg or {})}
    days = owner_silent_days(owner_last_seen, now)
    if days >= float(c["silence_hard_days"]):
        return 0
    if days >= float(c["silence_soft_days"]):
        return min(int(c["daily"]), int(c["silent_cap"]))
    return int(c["daily"])


def allow(
    state: dict[str, Any],
    cfg: dict[str, Any] | None,
    now: datetime | None = None,
    owner_last_seen: float | None = None,
) -> tuple[bool, str]:
    """能不能主动开口。第二项是理由（不能时说明为什么）。"""
    now = now or datetime.now()
    c = {**DEFAULTS, **(cfg or {})}
    st = _roll(state, now)
    cap = cap_for(c, owner_last_seen, now)
    if cap <= 0:
        return False, "主人已经很久没回应了，今天不主动（日记照写）"
    if int(st["count"]) >= cap:
        return False, f"今天已经开口 {st['count']} 次（上限 {cap}）"
    gap = float(c["gap_min"]) * 60 - (now.timestamp() - float(st.get("last_at") or 0))
    if gap > 0 and st.get("last_at"):
        return False, f"距上次开口还差 {int(gap // 60)} 分钟"
    return True, ""


def record(
    state: dict[str, Any],
    now: datetime | None = None,
    reason: str = "",
) -> dict[str, Any]:
    now = now or datetime.now()
    st = _roll(state, now)
    st["count"] = int(st.get("count", 0)) + 1
    st["last_at"] = now.timestamp()
    st["last_reason"] = reason[:80]
    return st


def summary(state: dict[str, Any], cfg: dict[str, Any] | None, now: datetime | None = None,
            owner_last_seen: float | None = None) -> str:
    now = now or datetime.now()
    st = _roll(state, now)
    cap = cap_for(cfg or {}, owner_last_seen, now)
    days = owner_silent_days(owner_last_seen, now)
    silent = f"，主人已 {int(days)} 天没回" if days >= 1 else ""
    return f"今天开口 {st['count']}/{cap}{silent}"
