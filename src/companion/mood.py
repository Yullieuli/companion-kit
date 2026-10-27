"""mood：三轴心情状态机。

三条硬规则（都是踩过的坑）：
  1. 一次反应最多推动 ±12 分 —— 否则一句"我想你"能让她从低落到狂喜
  2. 24 小时线性回归 0，但**不跨过当前标签的区间** —— 否则"开心"会自己淡成"平静"，
     读起来像失忆
  3. 低潮期（satisfaction <= -30）30 分钟内不下降 —— 已经很难过了，别补刀
"""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

AXES = ("satisfaction", "closeness", "energy")
CAP_PER_CALL = 12.0
DECAY_HOURS = 24.0
LOW_THRESHOLD = -30.0
LOW_HOLD_MINUTES = 30

# (阈值的下界, 标签)。判定时从高到低找第一个满足的。
_LABEL_RULES = [
    (60.0, "雀跃", "satisfaction"),
    (20.0, "开心", "satisfaction"),
    (-20.0, "低落", "satisfaction"),
    (-50.0, "难过", "satisfaction"),
    (40.0, "兴奋", "energy"),
    (-40.0, "疲惫", "energy"),
]


def default_state() -> dict[str, Any]:
    return {
        "axes": {k: 0.0 for k in AXES},
        "label": "平静",
        "last_change": None,
        "history": [],
    }


def _clamp(v: float) -> float:
    return max(-100.0, min(100.0, float(v)))


def label_for(axes: dict[str, Any]) -> str:
    sat = float(axes.get("satisfaction", 0))
    energy = float(axes.get("energy", 0))
    if sat >= 60:
        return "雀跃"
    if sat >= 20:
        return "开心"
    if sat <= -50:
        return "难过"
    if sat <= -20:
        return "低落"
    if energy >= 40:
        return "兴奋"
    if energy <= -40:
        return "疲惫"
    return "平静"


def band_floor(label: str) -> float:
    """这个标签至少需要多少 |satisfaction| 才保得住；0 表示不受限。"""
    return {
        "雀跃": 60.0,
        "开心": 20.0,
        "低落": 20.0,
        "难过": 50.0,
    }.get(label, 0.0)


def is_low(state: dict[str, Any]) -> bool:
    return float(state["axes"].get("satisfaction", 0)) <= LOW_THRESHOLD


def apply_delta(
    state: dict[str, Any],
    delta: dict[str, float],
    why: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """一次反应。单轴上限 ±12；低潮期不许往下推。"""
    now = now or datetime.now()
    out = copy.deepcopy(state)
    axes = out.setdefault("axes", {k: 0.0 for k in AXES})
    low = is_low(out)
    moved: dict[str, float] = {}
    for axis in AXES:
        if axis not in delta:
            continue
        step = float(delta[axis])
        step = max(-CAP_PER_CALL, min(CAP_PER_CALL, step))
        if low and axis == "satisfaction" and step < 0:
            continue
        before = float(axes.get(axis, 0.0))
        after = _clamp(before + step)
        axes[axis] = after
        if abs(after - before) > 1e-9:
            moved[axis] = round(after - before, 2)
    out["label"] = label_for(axes)
    if moved:
        out["last_change"] = now.isoformat(timespec="seconds")
        out.setdefault("history", []).append(
            {"at": out["last_change"], "delta": moved, "why": why[:120], "label": out["label"]}
        )
        out["history"] = out["history"][-50:]
    return out


def decay(state: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """24 小时线性回归 0；停在标签区间边界上。"""
    now = now or datetime.now()
    out = copy.deepcopy(state)
    last = out.get("last_change")
    if not last:
        return out
    try:
        then = datetime.fromisoformat(last)
    except Exception:
        return out
    hours = max(0.0, (now - then).total_seconds() / 3600.0)
    if hours <= 0:
        return out
    factor = max(0.0, 1.0 - hours / DECAY_HOURS)
    axes = out.setdefault("axes", {k: 0.0 for k in AXES})
    floor = band_floor(out.get("label") or "平静")
    for axis in AXES:
        before = float(axes.get(axis, 0.0))
        after = before * factor
        if axis == "satisfaction" and floor > 0:
            # 不跨过当前标签的边界：开心不会自己淡成平静
            if before >= floor:
                after = max(after, floor)
            elif before <= -floor:
                after = min(after, -floor)
        axes[axis] = round(_clamp(after), 2)
    new_label = label_for(axes)
    if new_label != out.get("label"):
        out["label"] = new_label
        out["last_change"] = now.isoformat(timespec="seconds")
    return out


def summary(state: dict[str, Any]) -> str:
    axes = state.get("axes") or {}
    parts = "，".join(f"{k} {float(axes.get(k, 0)):+.0f}" for k in AXES)
    return f"{state.get('label', '平静')}（{parts}）"
