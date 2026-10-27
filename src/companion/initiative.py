"""initiative：她今天想干什么。

这一层存在的理由：**她不该等别人提**。所有能力（说话、发语音、唱歌、分享视频、
写日记）都要有一个"她自己的入口"，否则再多的功能也只是命令行。

做法：先过硬闸（在睡觉吗、额度还有吗、今天想不想动），再给候选行动打分。
打分只看**她自己的状态**——心情、精力、多久没做这件事、有没有没说完的事。
最后按"性价比"挑一个：收益差不多时选便宜的（这条是为了压成本）。

输出只有一个动作。不做"一次干三件"：那既不真实，也贵。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import clock, mood as mood_mod, quota as quota_mod

# 每个动作的典型花费（元），用于"同样想做时选便宜的"
COST_EST = {
    "speak": 0.020,
    "voice": 0.030,
    "sing": 0.050,
    "share": 0.035,
    "diary": 0.008,
    "silent": 0.0,
}


@dataclass
class Situation:
    now: datetime
    mood_state: dict[str, Any]
    quota_state: dict[str, Any]
    rhythm: dict[str, Any]
    quota_cfg: dict[str, Any] | None = None
    owner_last_seen: float | None = None
    pending_life: int = 0            # 有几天没回应的生活事件
    pending_share: int = 0           # 深读过但还没分享的视频
    days_since_sing: float = 99.0
    days_since_voice: float = 99.0
    remaining_cny: float = 99.0      # 今天还剩多少预算
    owner_waiting: bool = False      # 主人刚说完话，等她接

    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Candidate:
    action: str
    score: float
    why: str
    cost: float

    def as_dict(self) -> dict[str, Any]:
        return {"action": self.action, "score": round(self.score, 3), "why": self.why, "cost": self.cost}


def _candidates(s: Situation) -> list[Candidate]:
    m = s.mood_state
    axes = m.get("axes") or {}
    label = m.get("label") or "平静"
    low = mood_mod.is_low(m)
    energy = float(axes.get("energy", 0))
    closeness = float(axes.get("closeness", 0))
    satisfaction = float(axes.get("satisfaction", 0))

    out: list[Candidate] = []

    # 说话：有没说完的事最想做；心情低的时候不想开口
    score = 0.50
    why = []
    if s.pending_life:
        score += 0.25
        why.append(f"有 {s.pending_life} 件生活里的事还没跟他说")
    if s.owner_waiting:
        score += 0.20
        why.append("他刚说完话，接着聊最自然")
    if closeness >= 30:
        score += 0.10
        why.append("最近关系近")
    if low:
        score -= 0.45
        why.append("心情低，不太想开口")
    out.append(Candidate("speak", score, "、".join(why) or "就是想找他说句话", COST_EST["speak"]))

    # 语音：亲近 + 有精神时才想让他听见声音
    score = 0.42
    why = []
    if closeness >= 40:
        score += 0.22
        why.append("跟他说话用声音才够亲近")
    if energy >= 20:
        score += 0.15
        why.append("今天有精神")
    if s.days_since_voice < 1:
        score -= 0.25
        why.append("刚发过语音")
    if low:
        score -= 0.30
    out.append(Candidate("voice", score, "、".join(why) or "想让他听见声音", COST_EST["voice"]))

    # 唱歌：高精力 + 心情好 + 有些天没唱了。这是"她自己想唱"，不是被点歌
    score = 0.30
    why = []
    if energy >= 40:
        score += 0.30
        why.append("精力很足")
    if satisfaction >= 20:
        score += 0.22
        why.append("心情好，想哼两句")
    if s.days_since_sing >= 3:
        score += 0.22
        why.append(f"已经 {int(s.days_since_sing)} 天没唱了")
    if s.days_since_sing < 1:
        score -= 0.50
    if low:
        score -= 0.40
    out.append(Candidate("sing", score, "、".join(why) or "想唱两句", COST_EST["sing"]))

    # 分享：手里有读完还没给他的东西
    score = 0.25 + (0.35 if s.pending_share else 0)
    why = f"有 {s.pending_share} 条看完了觉得他会喜欢" if s.pending_share else ""
    out.append(Candidate("share", score, why or "没什么非分享不可的", COST_EST["share"]))

    # 写日记：不打扰任何人，任何时候都允许
    score = 0.28 + (0.10 if s.pending_life else 0)
    out.append(Candidate("diary", score, "有些事想自己记下来", COST_EST["diary"]))

    # 什么都不做：基线。真实的她大部分时间是安静的
    out.append(Candidate("silent", 0.35, "没什么想说的", 0.0))
    return out


def decide(s: Situation, rnd: Any | None = None) -> dict[str, Any]:
    """返回 {action, score, why, cost, blocked, candidates}。"""
    import random

    r = rnd or random
    now = s.now

    # ── 硬闸 ────────────────────────────────────────────────
    if clock.in_sleep(s.rhythm, now):
        return _single("silent", "在睡觉（除非主人直接叫，否则不主动）", "sleep")

    allowed, why_not = quota_mod.allow(
        s.quota_state, s.quota_cfg, now, s.owner_last_seen
    )
    if not allowed:
        # 不能对他说话，但可以自己记一笔 —— 这也是"过日子"
        return _single("diary", why_not, "quota")

    # 30% 概率今天就是不想动（真实的惰性，不是 bug）
    skip_chance = float((s.rhythm or {}).get("skip_chance", 0.30))
    if r.random() < skip_chance and not s.owner_waiting:
        return _single("silent", "今天不太想动", "dice")

    # ── 打分 + 挑一个 ───────────────────────────────────────
    all_cands = _candidates(s)
    cands = [c for c in all_cands if c.cost <= s.remaining_cny or c.cost == 0]
    if not any(c.action != "silent" for c in cands):
        return _single("silent", "今天的预算用完了，先不花", "budget")
    best = max(cands, key=lambda c: (c.score, -c.cost))
    # 收益差在 0.05 以内时，选便宜的
    close = [c for c in cands if best.score - c.score <= 0.05]
    if len(close) > 1:
        best = min(close, key=lambda c: c.cost)
    return {
        "action": best.action,
        "score": round(best.score, 3),
        "why": best.why,
        "cost": best.cost,
        "blocked": "",
        "candidates": [c.as_dict() for c in sorted(cands, key=lambda c: c.score, reverse=True)],
    }


def _single(action: str, why: str, blocked: str) -> dict[str, Any]:
    return {
        "action": action,
        "score": 0.0,
        "why": why,
        "cost": COST_EST.get(action, 0.0),
        "blocked": blocked,
        "candidates": [],
    }
