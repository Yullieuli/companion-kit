"""drive：一次"醒来"的完整决策。

这是调度器和模型之间的那条线：
  宿主每 N 分钟问一次 `tick()`，它不调模型，只做三件事 ——
    1. 收集她现在的处境（作息、心情、额度、预算、没做完的事）
    2. 让 initiative 挑一个动作
    3. 该开口就写一条 wake_due 事件，并把额度记掉

**tick 本身零 token。** 这是整套东西能便宜的根本：绝大多数 tick 只会返回"继续睡"。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

from . import bili, clock, cost, events, initiative, mood as mood_mod, quota as quota_mod
from .store import Home

SPEAKING = ("speak", "voice", "sing", "share")
# 不占"说话额度"的动作：刷 B 站是过日子，不是找他说话。
# 但它照样花预算（一次短会话），所以预算闸对它一样生效。
DOING = ("bili",)

# 时钟只能有一个主人：DSH 每 5 分钟 tick、hermes 每 15 分钟 tick，
# 都从同一本额度里扣，结果是谁先 tick 谁把额度吃掉。
# 锁 30 分钟内有效：主人正常心跳（≤15 分钟）不会过期，换了宿主也能接管。
OWNER = "logs/tick_owner.json"
OWNER_TTL = 1800


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


def _pending_life_hint(home: Home) -> str:
    """最近一件还没跟他讲过的事，一句话。

    为什么要单独给一句：`pending_life` 那个**数字**以前会原样写进唤醒理由
    ——「有 6 件生活里的事还没跟他说」。模型把理由当台词念，
    开口就是「今天一口气刷了六条硬件评测」，还顺手把标题里的词缝成结论。
    数字是打分用的，不该给模型看；给她一件具体的，她才讲得出内容。
    """
    from . import life as life_mod

    try:
        rows = life_mod.unshared(home)
    except Exception:  # pragma: no cover - 生活流坏了不该拦住开场
        return ""
    for r in rows:
        core = life_mod.core_title(r.get("title", ""))
        if core:
            return f"{life_mod._verb_for(r)}「{core}」"
    return ""


def _bili_state(home: Home, now: datetime) -> tuple[bool, str, int]:
    """B 站那边现在什么情况：(还有额度吗, 为什么没有, 有几条读完没分享)。

    读不到就当"不能刷"——适配层坏掉时她确实什么都做不了，
    但不该连累说话、唱歌这些只在本地的事。
    """
    from . import bili as bili_mod

    try:
        d = bili_mod.due(home, now=now)
        return bool(d.get("due")), str(d.get("why") or ""), bili_mod.pending_share(home)
    except Exception as e:  # pragma: no cover - 配置坏了才会走到
        return False, f"B站不可用：{type(e).__name__}", 0


def _claim(home: Home, host: str, now: datetime) -> tuple[bool, str]:
    """尝试成为时钟主人。返回 (是否拿到, 当前主人)。"""
    try:
        cur = json.loads((home.root / OWNER).read_text(encoding="utf-8"))
    except Exception:
        cur = {}
    owner = str(cur.get("host") or "")
    age = now.timestamp() - float(cur.get("at") or 0)
    if owner and owner != host and age < OWNER_TTL:
        return False, owner
    home.save_json(OWNER, {"host": host, "at": now.timestamp()})
    return True, host


def _tick_unlocked(
    home: Home,
    *,
    host: str = "cli",
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

    # 心跳先写：**不管这次能不能拿到时钟**，这个宿主都还活着。
    # 写在这里（裁决之前）而不是后面，是为了让"被别的宿主占了时钟"的那一方
    # 也能被看门狗看见——它活着，只是不当家而已。
    from . import watchdog as wd_mod

    wd_mod.beat(home, host=host, now=now)
    home.append_jsonl("logs/heartbeats.jsonl",
                       {"host": host, "pid": os.getpid(),
                        "at": now.isoformat(timespec="seconds"),
                        "epoch": now.timestamp()})

    ok, owner = _claim(home, host, now)
    if not ok:
        return {
            "action": "silent", "score": 0.0,
            "why": f"时钟由 {owner} 掌管，这边只看不插手",
            "cost": 0.0, "blocked": "host", "candidates": [],
            "event": None, "spent_today": round(cost.today_total(home, now), 4),
            "remaining_cny": 0.0, "in_sleep": False, "in_window": False, "host": host,
        }

    rhythm = home.rhythm()
    if not bool(rhythm.get("enabled", True)):
        return {
            "action": "silent", "score": 0.0,
            "why": "灰度关闭（rhythm.enabled=false）",
            "cost": 0.0, "blocked": "gray", "candidates": [],
            "event": None, "spent_today": round(cost.today_total(home, now), 4),
            "remaining_cny": 0.0, "in_sleep": False, "in_window": False, "host": host,
        }
    fb = initiative.settle(home, now=now)
    m = mood_mod.decay(home.mood(), now)
    q = home.quota()
    spent = cost.today_total(home, now)
    b = {**cost.DEFAULT_BUDGET, **(budget or {})}
    remaining = max(0.0, float(b["daily_cny"]) - spent)
    bili_ok, bili_why, bili_pending = _bili_state(home, now)
    if bili_ok and not bili.has_credential(home):
        bili_ok, bili_why = False, "还没配 B 站凭证"
    # 刷片只在作息窗口内（SPEC 5.8）。窗口外她该休息，不是换个方式熬夜。
    if bili_ok and not clock.in_window(rhythm, now):
        bili_ok, bili_why = False, "不在作息窗口"

    situation = initiative.Situation(
        now=now,
        mood_state=m,
        quota_state=q,
        rhythm=rhythm,
        quota_cfg=quota_cfg,
        owner_last_seen=_owner_last_seen(home),
        pending_life=_pending_life(home),
        pending_share=pending_share or bili_pending,
        days_since_sing=days_since_sing,
        days_since_voice=days_since_voice,
        remaining_cny=remaining,
        owner_waiting=owner_waiting,
        extra={"feedback": fb, "pending_life_hint": _pending_life_hint(home)},
        bili_due=bili_ok,
        bili_why=bili_why,
    )
    decision = initiative.decide(situation, rnd)

    event = None
    if decision["action"] in SPEAKING:
        # 先记账再发事件：宁可少说一句，也不要重复开口
        q2 = quota_mod.record(q, now, decision["why"])
        # 留票：宿主真开口时 say 用它免掉第二次扣费（见 quota.take_pending）
        q2 = quota_mod.set_pending(q2, decision["action"], now)
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
    elif decision["action"] in DOING:
        # 「去刷会儿」——不扣说话额度，但要让人知道她现在想干什么
        event = events.emit(
            home, "bili_due", now,
            action=decision["action"],
            reason=decision["why"],
            est_cost=decision["cost"],
            mood=m.get("label"),
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

    # 她"打字"要多久：主动开口一般比回话长（她在组织语言）。
    # 通道照这个数等一下再发，不要秒回。
    from . import typing as typing_mod

    typing_ms = typing_mod.delay_ms("主动开口", busy=True, now=now)

    return {
        **decision,
        "event": event,
        "spent_today": round(spent, 4),
        "remaining_cny": round(remaining, 4),
        "in_sleep": clock.in_sleep(rhythm, now),
        "in_window": clock.in_window(rhythm, now),
        "host": host,
        "typing_ms": typing_ms,
        "typing_seconds": round(typing_ms / 1000.0, 2),
    }


def tick(home: Home, **kwargs: Any) -> dict[str, Any]:
    """串行化时钟与额度的整个读—改—写事务。"""
    with home.lock("store"):
        return _tick_unlocked(home, **kwargs)
