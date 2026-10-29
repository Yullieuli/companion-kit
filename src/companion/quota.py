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
        # 除"主动开口"之外还有别的额度：刷片、分享。它们和说话一样是每天限量的，
        # 但没有 45 分钟间隔这一说。放在同一本 quota.json 里，跨天一起归零。
        "buckets": {},
        # 已经记过账、还没真开口的那一次。"决策时记账"和"开口时记账"不能各记一次。
        "pending": {},
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
    reserved: bool = False,
) -> tuple[bool, str]:
    """能不能主动开口。第二项是理由（不能时说明为什么）。"""
    now = now or datetime.now()
    c = {**DEFAULTS, **(cfg or {})}
    st = _roll(state, now)
    # drive 已经为这张有效票做过完整的额度判定并预扣一次。
    # 不能因为它刚写入 last_at/count，又在真正开口时把同一次拒绝或再扣一遍。
    if reserved:
        return True, ""
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


# ── 命名桶 ────────────────────────────────────────────────────────────────
# 为什么并进这本账：刷片原来在 bili/state.json 里自己记一份，分享又记一份，
# 加上这里就是三本账。三本账的必然结果是"某一本忘了归零/忘了加"，而这类 bug
# 不会报错，只会表现为"她今天怎么刷了 7 条"。现在只有一处按天归零。

def _bucket(state: dict[str, Any], name: str) -> dict[str, Any]:
    b = (state.get("buckets") or {}).get(name) or {}
    return {"count": int(b.get("count") or 0), "last_at": float(b.get("last_at") or 0.0)}


def bucket_used(state: dict[str, Any], name: str, now: datetime | None = None) -> int:
    return _bucket(_roll(state, now or datetime.now()), name)["count"]


def bucket_record(state: dict[str, Any], name: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now()
    st = _roll(state, now)
    st.setdefault("buckets", {})
    b = _bucket(st, name)
    b["count"] += 1
    b["last_at"] = now.timestamp()
    st["buckets"][name] = b
    return st


def bucket_seed(state: dict[str, Any], name: str, count: int,
                now: datetime | None = None) -> dict[str, Any]:
    """把别处账本里今天的数字搬进来一次（迁移用）。已有计数就不动。"""
    now = now or datetime.now()
    st = _roll(state, now)
    if bucket_used(st, name, now) or int(count) <= 0:
        return st
    st.setdefault("buckets", {})[name] = {"count": int(count), "last_at": now.timestamp()}
    return st


def bucket_allow(state: dict[str, Any], name: str, cap: int, *,
                 now: datetime | None = None, gap_min: float = 0.0) -> tuple[bool, str]:
    now = now or datetime.now()
    b = _bucket(_roll(state, now), name)
    if b["count"] >= int(cap):
        return False, f"今天已经用掉 {b['count']}/{int(cap)}"
    if gap_min > 0 and b["last_at"]:
        left = gap_min * 60 - (now.timestamp() - b["last_at"])
        if left > 0:
            return False, f"还要等 {max(1, int(left // 60))} 分钟"
    return True, ""


PENDING_TTL_MIN = 20


def set_pending(state: dict[str, Any], action: str, now: datetime | None = None) -> dict[str, Any]:
    """决策时先记账，并留一张"这次已经付过钱了"的票。

    为什么需要票：drive 在决策那一刻就扣一次（否则同一分钟会反复叫醒），
    而宿主真开口时 `say` 还要过三道闸。没有票，同一次开口会被扣两次。
    """
    now = now or datetime.now()
    st = _roll(state, now)
    st["pending"] = {"action": str(action), "at": now.timestamp()}
    return st


def take_pending(state: dict[str, Any], now: datetime | None = None, *,
                 action: str | None = None,
                 ttl_min: float = PENDING_TTL_MIN) -> tuple[dict[str, Any], bool]:
    """取票。返回 (新状态, 是否用上了票)。

    过期就清掉：20 分钟前那次"想说"跟现在这句话没关系，不能拿旧票免单。
    """
    now = now or datetime.now()
    st = _roll(state, now)
    p = st.get("pending") or {}
    at = float(p.get("at") or 0)
    if not p:
        return st, False
    fresh = (now.timestamp() - at) <= float(ttl_min) * 60
    match = action is None or str(p.get("action") or "") == str(action)
    if fresh and match:
        st["pending"] = {}
        return st, True
    if not fresh:
        st["pending"] = {}
    return st, False


def summary(state: dict[str, Any], cfg: dict[str, Any] | None, now: datetime | None = None,
            owner_last_seen: float | None = None) -> str:
    now = now or datetime.now()
    st = _roll(state, now)
    cap = cap_for(cfg or {}, owner_last_seen, now)
    days = owner_silent_days(owner_last_seen, now)
    silent = f"，主人已 {int(days)} 天没回" if days >= 1 else ""
    return f"今天开口 {st['count']}/{cap}{silent}"
