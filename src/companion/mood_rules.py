"""mood_rules：心情被生活推着走。

三个方案我都试想过，最后选第三个：

  1. 每轮按消息长度/感叹号现算 —— 不可解释，也不会留下痕迹，
     本质是把心情变成"他打了多少字"的函数
  2. 等宿主每轮判一次 —— 宿主从来不判。结果就是 `mood/state.json` 永远是 0
  3. **只认可观察的行为**，每条都能指着事件说"因为这件事"

所以这里只有四种推力，全部来自已经发生、已经记账的事实：

    她开口了          closeness +0.6 / energy -0.2   聊得多会近，也会累
    他隔了很久才回来    satisfaction +3 / closeness +1   被想起是有分量的
    她刷到一条喜欢的    satisfaction +1.5
    她分享出去了      satisfaction +2

每天有上限（`CAP_PER_DAY`）。不设上限的话，一天聊 50 句就是 closeness +30——
那不是"关系变近"，那是计数器。上限本身也是设计：真实的亲近会饱和。
"""

from __future__ import annotations

import copy
import json
from datetime import date, datetime
from typing import Any

from . import events, mood as mood_mod
from .store import Home

DAILY = "mood/daily.json"

# 每天最多能推动多少（绝对值）。energy 是负数，所以用负值表示"最多掉这么多"。
CAP_PER_DAY = {"closeness": 20.0, "satisfaction": 30.0, "energy": -6.0}

# 事件 → 推力。加事件就加一行，调用点只写事件名。
RULES: dict[str, dict[str, float]] = {
    "turn": {"closeness": 1.5, "satisfaction": 1.0, "energy": -0.2},
    "return_after_silence": {"satisfaction": 8.0, "closeness": 3.0},
    "watched": {"satisfaction": 5.0},
    "shared": {"satisfaction": 4.0},
}

SILENCE_HOURS = 12.0
WHY = {
    "turn": "跟他说了句话",
    "return_after_silence": "隔了很久，他又回来了",
    "watched": "刷到一条自己真想看的",
    "shared": "把喜欢的东西分享出去了",
}


def _load(home: Home) -> dict[str, Any]:
    try:
        d = json.loads((home.root / DAILY).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(home: Home, data: dict[str, Any]) -> None:
    p = home.root / DAILY
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def used_today(home: Home, now: datetime | None = None) -> dict[str, float]:
    now = now or datetime.now()
    d = _load(home)
    if str(d.get("day")) != now.date().isoformat():
        return {}
    return {k: float(v) for k, v in (d.get("used") or {}).items()}


def _clip(delta: dict[str, float], used: dict[str, float]) -> dict[str, float]:
    """按当天剩余额度裁剪。正负方向各自算额度。"""
    out: dict[str, float] = {}
    for axis, step in delta.items():
        cap = float(CAP_PER_DAY.get(axis, 0.0))
        already = float(used.get(axis, 0.0))
        if cap == 0:
            continue
        if cap > 0:
            room = cap - already
            step = min(step, room)
        else:
            room = cap - already           # cap 是负数，already 也是负数
            step = max(step, room)
        if abs(step) > 1e-9:
            out[axis] = round(step, 2)
    return out


def nudge(home: Home, event: str, *, gap_hours: float = 0.0,
          now: datetime | None = None) -> dict[str, Any]:
    """给心情一个推力。事件不认识就什么都不做（不猜）。"""
    now = now or datetime.now()
    base = RULES.get(event or "")
    if not base:
        return {"ok": False, "why": f"不认识的事件 {event}"}
    delta = dict(base)
    if event == "turn" and gap_hours >= SILENCE_HOURS:
        # 同一次开口，既是"说了句话"也是"他回来了"：两笔都算，但要一起去重额度
        delta = {**delta}
        for axis, step in RULES["return_after_silence"].items():
            delta[axis] = delta.get(axis, 0.0) + step
        event = "turn+return"

    used = used_today(home, now)
    clipped = _clip(delta, used)
    if not clipped:
        return {"ok": True, "applied": {}, "why": "今天的额度已经用完了", "event": event}

    before = mood_mod.decay(home.mood(), now)
    after = mood_mod.apply_delta(before, clipped, why=WHY.get(event.split("+")[0], event), now=now)
    home.save_json("mood/state.json", after)
    for axis, step in clipped.items():
        used[axis] = round(float(used.get(axis, 0.0)) + step, 2)
    _save(home, {"day": now.date().isoformat(), "used": used})
    if before.get("label") != after.get("label"):
        events.emit(home, "mood_shift", now, frm=before.get("label"), to=after.get("label"),
                    why=WHY.get(event.split("+")[0], event))
    return {"ok": True, "applied": clipped, "event": event,
            "label": after.get("label"), "summary": mood_mod.summary(after)}


def on_turn(home: Home, *, gap_hours: float = 0.0, now: datetime | None = None) -> dict[str, Any]:
    return nudge(home, "turn", gap_hours=gap_hours, now=now)


def on_watched(home: Home, score: int = 0, *, now: datetime | None = None) -> dict[str, Any]:
    """只有她真觉得好看才推（评分 ≥6）。什么都推等于什么都不是。"""
    if int(score or 0) < 6:
        return {"ok": False, "why": f"评分 {score} 不够喜欢"}
    return nudge(home, "watched", now=now)


def on_shared(home: Home, *, now: datetime | None = None) -> dict[str, Any]:
    return nudge(home, "shared", now=now)


def snapshot(home: Home, now: datetime | None = None) -> dict[str, Any]:
    """给 status 用：今天还剩多少额度、心情是什么。"""
    now = now or datetime.now()
    used = used_today(home, now)
    left = {axis: round(cap - float(used.get(axis, 0.0)), 2)
            for axis, cap in CAP_PER_DAY.items()}
    st = mood_mod.decay(home.mood(), now)
    return {"label": st.get("label"), "axes": st.get("axes"), "left_today": left,
            "rules": sorted(RULES)}

# ── 配置层覆盖（settings.json，缺省回落上面的常量）──────────────
from . import settings as _settings
CAP_PER_DAY = _settings.section("mood_caps", CAP_PER_DAY)

# RULES 是嵌套结构（事件 → 轴 → 数值），settings.section 只处理扁平 dict，这里单独合并。
_over = _settings.raw().get("mood_rules")
if isinstance(_over, dict):
    for _ev, _axes in _over.items():
        if _ev in RULES and isinstance(_axes, dict):
            for _ax, _val in _axes.items():
                if _ax in RULES[_ev]:
                    try:
                        RULES[_ev][_ax] = float(_val)
                    except (TypeError, ValueError):
                        continue

# ── 环境事件：没有具体触发者，时间本身带的情绪 ──────────────────
# 为什么单独一套：RULES 是「有人做了某事」的推力，走过 nudge 的每日额度；
# 这些是「什么都没发生」的结果，不该占用说话/刷片的额度，也不该被 _clip 拦。
IGNORE_HOURS = 4.0       # 她说完话多久没人接，算被冷落
ABSENCE_DAYS = 3.0       # 多久没见，算「很久」
NIGHT_START = 23         # 深夜起点
NIGHT_END = 5            # 深夜终点

AMBIENT = {
    "ignored": {"satisfaction": -3.0},
    "long_absence": {"closeness": -1.0},
    "late_night": {"energy": -2.0},
}

AMBIENT_WHY = {
    "ignored": "说了话，半天没人应",
    "long_absence": "好几天没见到人",
    "late_night": "这么晚还醒着",
}


def _apply_ambient(home: Home, event: str, now: datetime) -> dict[str, Any]:
    before = mood_mod.decay(home.mood(), now)
    after = mood_mod.apply_delta(before, AMBIENT[event], why=AMBIENT_WHY[event], now=now)
    home.save_json("mood/state.json", after)
    return after


def ambient(home: Home, now: datetime | None = None) -> dict[str, Any]:
    """每轮 tick 检查一次：被冷落 / 很久没见 / 深夜。每种每天最多一次。"""
    now = now or datetime.now()
    day = now.date().isoformat()
    book = home.read_json("mood/ambient.json", {}) or {}
    if book.get("day") != day:
        book = {"day": day, "done": {}}
    done = book.setdefault("done", {})
    hits: list[str] = []

    owner = home.read_json("people/owner.json", {}) or {}
    last_seen = str(owner.get("last_seen") or "")

    # 被冷落：她最后一句说出去超过 IGNORE_HOURS，且这之后他没来过
    if "ignored" not in done:
        rows = home.read_jsonl("logs/said.jsonl", limit=1)
        if rows:
            try:
                at = datetime.fromisoformat(str(rows[-1].get("at")))
                resp = datetime.strptime(last_seen, "%Y-%m-%d %H:%M") if last_seen else None
                if (now - at).total_seconds() / 3600.0 >= IGNORE_HOURS and (resp is None or resp <= at):
                    _apply_ambient(home, "ignored", now)
                    done["ignored"] = True
                    hits.append("ignored")
            except Exception:
                pass

    # 很久没见：不是「他没回她」，是「他压根没出现」
    if "long_absence" not in done and last_seen:
        try:
            resp = datetime.strptime(last_seen, "%Y-%m-%d %H:%M")
            if (now - resp).days >= ABSENCE_DAYS:
                _apply_ambient(home, "long_absence", now)
                done["long_absence"] = True
                hits.append("long_absence")
        except Exception:
            pass

    # 深夜还醒着：精力往下掉
    if "late_night" not in done and (now.hour >= NIGHT_START or now.hour < NIGHT_END):
        _apply_ambient(home, "late_night", now)
        done["late_night"] = True
        hits.append("late_night")

    if hits:
        home.save_json("mood/ambient.json", book)
    return {"ok": True, "applied": hits, "day": day}

IGNORE_HOURS = _settings.number("mood_rules", "ignore_hours", IGNORE_HOURS)
ABSENCE_DAYS = _settings.number("mood_rules", "absence_days", ABSENCE_DAYS)

_amb = _settings.raw().get("ambient")
if isinstance(_amb, dict):
    for _ev, _axes in _amb.items():
        if _ev in AMBIENT and isinstance(_axes, dict):
            for _ax, _val in _axes.items():
                if _ax in AMBIENT[_ev]:
                    try:
                        AMBIENT[_ev][_ax] = float(_val)
                    except (TypeError, ValueError):
                        continue
