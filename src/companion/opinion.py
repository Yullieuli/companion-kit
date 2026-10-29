"""opinion：她自己的判断。

和 memory 的区别是**主语**：
  memory  记录「世界是什么样」（这部片子讲了什么）
  opinion 记录「我怎么看」（这片子该死，因为它不敢让玩家失败）

为什么单独一层：性格不是一个形容词列表，是**一串可以被反驳的判断**。
她说话像不像一个人，取决于她有没有稳定的立场，而不是语气词多不多。

生命周期（和人在真实世界里的想法一致）：
  hunch(<0.70)  —— 刚冒出来，随口一说
  belief(>=0.70) —— 反复被验证，会主动拿它当理由
  retired(<0.25) —— 没人接、自己也不再提，慢慢就淡了

衰减很慢（×0.998/天，一年还剩一半），因为立场不该天天变；
但被反驳（`weaken`）掉得快，因为被说服是瞬时的事。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .store import Home

FILE = "opinions.jsonl"
MAX_ITEMS = 120
REINFORCE = 0.12
WEAKEN = 0.20
RETIRE_BELOW = 0.25
BELIEF_AT = 0.70
# hunch 里"认真写过"的下限。add() 的默认值就在这条线上。
# 低于它的是随口一猜（0.3），高于的是她想过、觉得成立、只是还没验证。
HUNCH_SHOW_AT = 0.45
DECAY_PER_DAY = 0.998


def _norm(text: str) -> str:
    return re.sub(r"[\s，。！？、,.!?；;：:]+", "", (text or "").strip().lower())


def _id(claim: str) -> str:
    return hashlib.sha1(_norm(claim).encode("utf-8")).hexdigest()[:10]


@dataclass
class Opinion:
    id: str
    claim: str
    tags: list[str] = field(default_factory=list)
    confidence: float = 0.45
    hits: int = 0
    at: str = ""
    last_hit: str = ""
    source: str = ""

    @property
    def stage(self) -> str:
        if self.confidence < RETIRE_BELOW:
            return "retired"
        return "belief" if self.confidence >= BELIEF_AT else "hunch"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "claim": self.claim, "tags": self.tags,
            "confidence": round(self.confidence, 3), "stage": self.stage,
            "hits": self.hits, "at": self.at, "last_hit": self.last_hit,
        }


def _load(home: Home) -> list[Opinion]:
    out: list[Opinion] = []
    for r in home.read_jsonl(FILE, limit=100000):
        try:
            out.append(Opinion(
                id=str(r.get("id") or _id(r.get("claim", ""))),
                claim=str(r.get("claim") or ""),
                tags=list(r.get("tags") or []),
                confidence=float(r.get("confidence", 0.45)),
                hits=int(r.get("hits") or 0),
                at=str(r.get("at") or ""),
                last_hit=str(r.get("last_hit") or ""),
                source=str(r.get("source") or ""),
            ))
        except Exception:
            continue
    return out


def _save(home: Home, items: list[Opinion]) -> None:
    p = home.root / FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for o in items:
        d = o.as_dict()
        d.pop("stage", None)
        d["source"] = o.source
        rows.append(json.dumps(d, ensure_ascii=False))
    p.write_text("".join(r + "\n" for r in rows), encoding="utf-8")


def theses(home: Home, *, include_retired: bool = False) -> list[Opinion]:
    """她的立场清单，按置信度排。默认不含已淡出的。"""
    items = [o for o in _load(home) if include_retired or o.stage != "retired"]
    items.sort(key=lambda o: (-o.confidence, -o.hits))
    return items


def add(
    home: Home,
    claim: str,
    *,
    tags: list[str] | None = None,
    confidence: float = 0.45,
    source: str = "",
    now: datetime | None = None,
) -> Opinion:
    """冒出一个判断。同一句话再说一遍 = 强化，不新增。"""
    claim = (claim or "").strip()
    if not claim:
        raise ValueError("观点不能为空")
    if len(claim) > 200:
        claim = claim[:200]
    now = now or datetime.now()
    items = _load(home)
    key = _norm(claim)
    for o in items:
        if _norm(o.claim) == key:
            return reinforce(home, o.id, now=now)
    o = Opinion(
        id=_id(claim), claim=claim, tags=sorted({*(tags or [])}),
        confidence=max(0.05, min(1.0, float(confidence))),
        at=now.isoformat(timespec="seconds"), last_hit=now.isoformat(timespec="seconds"),
        source=source,
    )
    items.append(o)
    if len(items) > MAX_ITEMS:
        items.sort(key=lambda x: (x.confidence * (1 + min(9, x.hits)), x.last_hit))
        items = items[-MAX_ITEMS:]
    _save(home, items)
    return o


def reinforce(home: Home, oid: str, *, delta: float = REINFORCE, now: datetime | None = None) -> Opinion:
    """又验证了一次。置信度上升，命中计数 +1。"""
    now = now or datetime.now()
    items = _load(home)
    for o in items:
        if o.id == oid:
            o.confidence = max(0.05, min(1.0, o.confidence + delta))
            o.hits += 1
            o.last_hit = now.isoformat(timespec="seconds")
            _save(home, items)
            return o
    raise KeyError(f"没有这条观点：{oid}")


def weaken(home: Home, oid: str, *, delta: float = WEAKEN, now: datetime | None = None) -> Opinion:
    """被反驳了 / 自己改了主意。掉得比长得快。"""
    return reinforce(home, oid, delta=-abs(delta), now=now)


def decay(home: Home, now: datetime | None = None) -> int:
    """惰性衰减。返回这次真正降了置信度的条数。"""
    now = now or datetime.now()
    items = _load(home)
    changed = 0
    for o in items:
        if not o.last_hit:
            continue
        try:
            days = max(0.0, (now - datetime.fromisoformat(o.last_hit)).total_seconds() / 86400)
        except Exception:
            continue
        if days < 1:
            continue
        new = o.confidence * (DECAY_PER_DAY ** days)
        if abs(new - o.confidence) > 1e-4:
            o.confidence = max(0.05, new)
            changed += 1
    if changed:
        _save(home, items)
    return changed


def relevant(home: Home, query: str = "", *, limit: int = 4) -> list[Opinion]:
    """挑和这句话有关的观点。没沾边的直接跳过——硬塞会让回答跑题。"""
    items = [o for o in _load(home) if o.stage != "retired"]
    if not items:
        return []
    q = _norm(query)
    if not q:
        return sorted(items, key=lambda o: -o.confidence)[:limit]
    bigrams = {q[i:i + 2] for i in range(max(0, len(q) - 1))} or {q}
    scored: list[tuple[float, Opinion]] = []
    for o in items:
        s = 0.0
        for t in o.tags:
            tn = _norm(t)
            if tn and (tn in q or q in tn):
                s += 2.0
        claim = _norm(o.claim)
        s += 0.3 * len([b for b in bigrams if b in claim])
        if s <= 0:
            continue
        scored.append((s + o.confidence, o))
    scored.sort(key=lambda x: -x[0])
    return [o for _, o in scored[:limit]]


def render(home: Home, query: str = "", *, limit: int = 4, max_chars: int = 600,
           settled_only: bool = False, recent_days: float = 0) -> str:
    """给 context 的一段：她自己的判断。

    `settled_only=True` 时只要**认真写下的**（belief，或 confidence ≥ 0.45 的 hunch），
    随口猜的（0.3 那种）不进。

    为什么不是"只要 belief"：BELIEF_AT=0.70，而 add() 默认 0.45 ——
    她写完的观点必须被反复 reinforce 才够得着 0.70，而 reinforce 要她先说出来，
    说出来又要观点先在上下文里。**闭环锁死了**：2026-10-29 真机上她 4 条观点
    全是 0.5，一条都没进过上下文，所以主人问她"做这个有啥用"，
    她只能答"我今天刷了六个显卡评测"——她手里只有流水，没有判断。
    放行 0.45 以上的 hunch，并标注「还没想定」，比滤光真实。

    `recent_days > 0` 时，query 匹配不满 limit 就用**最近写的**补上：
    她刚看完东西写下的判断，就是"此刻正在想的事"，哪怕和这句话字面无关。
    """
    picks = relevant(home, query, limit=limit)
    if settled_only:
        # 要多取一些再筛，否则 limit=4 可能一条都不剩
        picks = relevant(home, query, limit=limit * 4)
        picks = [
            o for o in picks
            if o.stage == "belief" or (o.stage == "hunch" and o.confidence >= HUNCH_SHOW_AT)
        ][:limit]
    if recent_days and len(picks) < limit:
        have = {o.id for o in picks}
        fresh = [
            o for o in _load(home)
            if o.stage != "retired" and o.id not in have and o.confidence >= HUNCH_SHOW_AT
        ]
        fresh.sort(key=lambda o: str(o.at or ""), reverse=True)
        picks = picks + fresh[: limit - len(picks)]
    if not picks:
        return ""
    lines = ["【你自己的看法】"]
    used = 0
    for o in picks:
        tag = ("[" + "/".join(o.tags) + "] ") if o.tags else ""
        mark = "" if o.stage == "belief" else "（还没想定）"
        line = f"- {tag}{o.claim}{mark}"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines) if len(lines) > 1 else ""


def stats(home: Home) -> dict[str, Any]:
    items = _load(home)
    return {
        "total": len(items),
        "belief": sum(1 for o in items if o.stage == "belief"),
        "hunch": sum(1 for o in items if o.stage == "hunch"),
        "retired": sum(1 for o in items if o.stage == "retired"),
        "avg_confidence": round(sum(o.confidence for o in items) / len(items), 3) if items else 0.0,
    }
