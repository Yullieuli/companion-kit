"""group：群聊里该不该接话。

三档，不是一个概率：
    reply  直接接
    lurk   在，但不说（潜水）
    ignore 当作没看见（连潜水状态都不进）

为什么必须三档：只有"回/不回"两档时，她要么很吵要么很冷。真人在群里
大部分时间是「看到了、没说」，偶尔才插一句。`lurk` 就是这个中间态——
它也会消耗注意力，所以连续潜水太多次之后才开始真的忽略。

判据全是可解释的：被点名必回、主人的话提高概率、冷却未到就潜水、
同一小时说太多就闭嘴。没有一条依赖模型判断。
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any

from .store import Home

STATE = "group/state.json"
DEFAULT_CFG: dict[str, Any] = {
    "reply_chance": 0.22,       # 没被叫到时的基础概率
    "address_words": [],        # 她被叫的名字（@昵称 之外的口头称呼）
    "cooldown_min": 8,          # 两次接话的最小间隔
    "max_per_hour": 4,          # 一小时最多接几次
    "lurk_streak": 3,           # 连续潜水几次后转 ignore
    "owner_boost": 0.35,        # 主人说话时加多少概率
    "min_chars": 2,             # 太短的消息不值得接（"hhh" "。"）
}


def config(home: Home) -> dict[str, Any]:
    out = dict(DEFAULT_CFG)
    out.update(home._json(STATE, {}).get("config") or {})
    return out


def _state(home: Home) -> dict[str, Any]:
    d = home._json(STATE, {})
    d.setdefault("config", {})
    d.setdefault("last_at", 0.0)
    d.setdefault("lurk_streak", 0)
    d.setdefault("hour_key", "")
    d.setdefault("hour_count", 0)
    return d


def _save(home: Home, st: dict[str, Any]) -> None:
    home.save_json(STATE, st)


def addressed(text: str, cfg: dict[str, Any] | None = None) -> bool:
    cfg = cfg or DEFAULT_CFG
    t = text or ""
    if "@" in t or "[CQ:at," in t:
        return True
    return any(w and w in t for w in (cfg.get("address_words") or []))


def judge(
    home: Home,
    text: str,
    *,
    addressed_to_me: bool | None = None,
    from_owner: bool = False,
    now: datetime | None = None,
    rnd: Any | None = None,
) -> dict[str, Any]:
    """返回 {mode, reason, score}。mode ∈ reply/lurk/ignore。"""
    cfg = config(home)
    st = _state(home)
    now = now or datetime.now()
    r = rnd or random

    t = (text or "").strip()
    if len(t) < int(cfg.get("min_chars", 2)):
        return {"mode": "ignore", "reason": "太短，不值得接", "score": 0.0}

    hit = addressed(t, cfg) if addressed_to_me is None else addressed_to_me
    if hit:
        return {"mode": "reply", "reason": "被点名了", "score": 1.0}

    elapsed = now.timestamp() - float(st.get("last_at") or 0)
    if elapsed < float(cfg.get("cooldown_min", 8)) * 60:
        left = int(float(cfg["cooldown_min"]) * 60 - elapsed) // 60
        return {"mode": "lurk", "reason": f"刚说过话，再等 {left} 分钟", "score": 0.1}

    hour_key = now.strftime("%Y-%m-%d %H")
    count = int(st.get("hour_count") or 0) if st.get("hour_key") == hour_key else 0
    if count >= int(cfg.get("max_per_hour", 4)):
        return {"mode": "lurk", "reason": f"这一小时已经说了 {count} 次", "score": 0.1}

    if int(st.get("lurk_streak") or 0) >= int(cfg.get("lurk_streak", 3)):
        return {"mode": "ignore", "reason": "连着看了好几轮都没接，这会儿不在状态", "score": 0.0}

    p = float(cfg.get("reply_chance", 0.22))
    if from_owner:
        p += float(cfg.get("owner_boost", 0.35))
    roll = r.random()
    if roll < p:
        return {"mode": "reply", "reason": "这话接得上，想说一句", "score": round(roll, 3)}
    st["lurk_streak"] = int(st.get("lurk_streak") or 0) + 1
    _save(home, st)
    return {"mode": "lurk", "reason": "看到了，但没什么想说的", "score": round(roll, 3)}


def note_reply(home: Home, now: datetime | None = None) -> None:
    """真接了话才调。重置潜水计数、记冷却与小时配额。"""
    now = now or datetime.now()
    st = _state(home)
    hour_key = now.strftime("%Y-%m-%d %H")
    if st.get("hour_key") == hour_key:
        st["hour_count"] = int(st.get("hour_count") or 0) + 1
    else:
        st["hour_key"] = hour_key
        st["hour_count"] = 1
    st["last_at"] = now.timestamp()
    st["lurk_streak"] = 0
    home.append_jsonl("logs/group.jsonl", {"at": now.isoformat(timespec="seconds"), "event": "reply"})
    _save(home, st)


def note_lurk(home: Home, now: datetime | None = None) -> None:
    st = _state(home)
    st["lurk_streak"] = int(st.get("lurk_streak") or 0) + 1
    _save(home, st)


def status(home: Home, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now()
    st = _state(home)
    cfg = config(home)
    last = float(st.get("last_at") or 0)
    return {
        "config": cfg,
        "lurk_streak": int(st.get("lurk_streak") or 0),
        "hour_count": int(st.get("hour_count") or 0) if st.get("hour_key") == now.strftime("%Y-%m-%d %H") else 0,
        "minutes_since_last": round((now.timestamp() - last) / 60, 1) if last else None,
        "cooldown_left": max(0.0, round(float(cfg["cooldown_min"]) - (now.timestamp() - last) / 60, 1)) if last else 0.0,
    }
