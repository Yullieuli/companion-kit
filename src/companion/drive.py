"""drive：一次"醒来"的完整决策。

这是调度器和模型之间的那条线：
  宿主每 N 分钟问一次 `tick()`，它不调模型，只做三件事 ——
    1. 收集她现在的处境（作息、心情、额度、预算、没做完的事）
    2. 让 initiative 挑一个动作
    3. 该开口就写一条 wake_due 事件，并把额度记掉

**tick 本身零 token。** 这是整套东西能便宜的根本：绝大多数 tick 只会返回"继续睡"。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from . import clock, cost, events, initiative, mood as mood_mod, quota as quota_mod
from .store import Home

SPEAKING = ("speak", "voice", "sing", "share")


def _owner_last_seen(home: Home) -> float | None:
    """最近一次主人说话的时间。从 turns 日志里取，取不到就当未知。"""
    rows = home.read_jsonl("logs/turns.jsonl", limit=200)
    for r in reversed(rows):
        if r.get("role") == "user" and r.get("at"):
            try:
                return datetime.fromisoformat(str(r["at"])).timestamp()
            except Exception:
                continue
    return None


def _pending_life(home: Home) -> int:
    """没跟他讲过的生活事件条数。这是"想说话"最主要的燃料。"""
    from . import life as life_mod

    return len(life_mod.unshared(home))


def tick(
    home: Home,
    *,
    now: datetime | None = None,
    rnd: Any | None = None,
    quota_cfg: dict[str, Any] | None = None,
    budget: dict[str, Any] | None = None,
    pending_share: int = 0,
    days_since_sing: float = 99.0,
    days_since_voice: float = 99.0,
    owner_waiting: bool = False,
    emit_silent: bool = False,
) -> dict[str, Any]:
    now = now or datetime.now()
    home.init()

    rhythm = home.rhythm()
    m = mood_mod.decay(home.mood(), now)
    q = home.quota()
    spent = cost.today_total(home, now)
    b = {**cost.DEFAULT_BUDGET, **(budget or {})}
    remaining = max(0.0, float(b["daily_cny"]) - spent)

    situation = initiative.Situation(
        now=now,
        mood_state=m,
        quota_state=q,
        rhythm=rhythm,
        quota_cfg=quota_cfg,
        owner_last_seen=_owner_last_seen(home),
        pending_life=_pending_life(home),
        pending_share=pending_share,
        days_since_sing=days_since_sing,
        days_since_voice=days_since_voice,
        remaining_cny=remaining,
        owner_waiting=owner_waiting,
    )
    decision = initiative.decide(situation, rnd)

    event = None
    if decision["action"] in SPEAKING:
        # 先记账再发事件：宁可少说一句，也不要重复开口
        q2 = quota_mod.record(q, now, decision["why"])
        home.save_json("quota.json", q2)
        cap = quota_mod.cap_for(quota_cfg or {}, _owner_last_seen(home), now)
        event = events.emit(
            home, "wake_due", now,
            action=decision["action"],
            reason=decision["why"],
            est_cost=decision["cost"],
            mood=m.get("label"),
            quota=f"{int(q2.get('count', 0))}/{cap}",
            window="in" if clock.in_window(rhythm, now) else "out",
        )
    elif emit_silent:
        event = events.emit(
            home, "spend_guard" if decision.get("blocked") == "budget" else "idle", now,
            action=decision["action"],
            reason=decision["why"],
            blocked=decision.get("blocked") or "",
        )

    home.append_jsonl(
        "logs/initiatives.jsonl",
        {
            "at": now.isoformat(timespec="seconds"),
            "action": decision["action"],
            "why": decision["why"],
            "blocked": decision.get("blocked") or "",
            "cost_est": decision["cost"],
            "mood": m.get("label"),
            "spent_today": round(spent, 4),
            "owner_waiting": bool(owner_waiting),
        },
    )
    home.save_json("mood/state.json", m)

    return {
        **decision,
        "event": event,
        "spent_today": round(spent, 4),
        "remaining_cny": round(remaining, 4),
        "in_sleep": clock.in_sleep(rhythm, now),
        "in_window": clock.in_window(rhythm, now),
    }
