"""react：一轮说完之后，把感受写回去。

为什么必须只有一个入口：
心情、人物卡、记忆、观点、生活事件，如果各给一个工具，模型会漏调其中三个，
于是"她记得"这件事永远是半截的。所以对外只暴露 `react()`，
一次把这一轮产生的所有变化落下去，并且**返回她变成了什么样**。

调用约定（宿主侧）：
    ctx = companion_context(...)      # 读
    ...模型说完了...
    react(mood_delta={...}, opinion={...}, memory={...}, said="...")   # 写

没有调用 react 的一轮，等于没发生过——这也是留痕的意义。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from . import cost as cost_mod
from . import events, fence, life, memory, mood as mood_mod, opinion, people
from .store import Home


TURNS = "logs/turns.jsonl"
TURNS_KEEP = 2000


def _trim_turns(home: Home, keep: int = TURNS_KEEP) -> None:
    """把 turns 控制在 last `keep` 行。

    不轮转的话它一年长到几万行：`read_jsonl` 只读尾部所以不影响性能，
    但备份、grep、git（有人会把这个目录纳入版本管理）都会变慢变脏。
    """
    p = home.root / TURNS
    try:
        if not p.exists() or p.stat().st_size < keep * 120:
            return
        lines = p.read_text(encoding="utf-8").splitlines()
        if len(lines) > keep:
            p.write_text("\n".join(lines[-keep:]) + "\n", encoding="utf-8")
    except Exception:
        return


def _mood(home: Home, delta: dict[str, float], why: str, now: datetime) -> dict[str, Any]:
    before = home.mood()
    after = mood_mod.apply_delta(before, delta, why=why, now=now)
    home.save_json("mood/state.json", after)
    changed_label = before.get("label") != after.get("label")
    if changed_label:
        # SPEC 5.3 要求 mood_shift 事件；之前从没发过
        events.emit(home, "mood_shift", now, frm=before.get("label"), to=after.get("label"), why=why[:60])
    return {
        "label": after.get("label"),
        "axes": after.get("axes"),
        "delta": delta,
        "shifted": changed_label,
        "summary": mood_mod.summary(after),
    }


def _seen_turn(home: Home, turn_id: str) -> dict[str, Any] | None:
    """这个 turn 之前写回过没有。宿主的 react 可能因重试/双宿主而到达多次。"""
    if not turn_id:
        return None
    for r in home.read_jsonl(TURNS, limit=TURNS_KEEP):
        if r.get("turn_id") == turn_id:
            return r
    return None


def react(
    home: Home,
    *,
    mood_delta: dict[str, float] | None = None,
    why: str = "",
    person: str = "",
    facts: list[str] | None = None,
    memory_page: dict[str, Any] | None = None,
    opinion_item: dict[str, Any] | None = None,
    life_event: dict[str, Any] | None = None,
    said: str = "",
    cost: dict[str, Any] | None = None,
    usage: dict[str, Any] | None = None,
    turn_id: str = "",
    context: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """串行化整个「查重 → 写回 → 记账」事务，重试与双宿主并发都安全。"""
    with home.lock("store"):
        return _react_unlocked(
            home, mood_delta=mood_delta, why=why, person=person, facts=facts,
            memory_page=memory_page, opinion_item=opinion_item, life_event=life_event,
            said=said, cost=cost, usage=usage, turn_id=turn_id, context=context, now=now,
        )


def _react_unlocked(
    home: Home,
    *,
    mood_delta: dict[str, float] | None = None,
    why: str = "",
    person: str = "",
    facts: list[str] | None = None,
    memory_page: dict[str, Any] | None = None,
    opinion_item: dict[str, Any] | None = None,
    life_event: dict[str, Any] | None = None,
    said: str = "",
    cost: dict[str, Any] | None = None,
    usage: dict[str, Any] | None = None,
    turn_id: str = "",
    context: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now()
    home.init()

    # 同一 turn 重试：不再改心情/记忆/生活/成本，直接回上次的 changed。
    prev = _seen_turn(home, turn_id)
    if prev is not None:
        return {"ok": True, "duplicate": True, "turn_id": turn_id,
                "at": prev.get("at") or now.isoformat(timespec="seconds"),
                "changed": list(prev.get("changed") or []),
                "note": "同一 turn 已写回过，不重复改变状态或费用"}

    changed: list[str] = []
    out: dict[str, Any] = {"ok": True, "at": now.isoformat(timespec="seconds")}
    if turn_id:
        out["turn_id"] = turn_id

    if mood_delta:
        out["mood"] = _mood(home, mood_delta, why, now)
        changed.append("mood")
        if out["mood"]["shifted"]:
            changed.append("mood_shift")

    if person:
        # 给了 person 就该建卡，哪怕 facts 是空的。
        # 之前要 facts 非空才建，导致"主人从没说过话 → 人物卡永远不存在"
        # → 上下文里没有他是谁 → 她答非所问。
        # last_seen 本身就是有用的信息：他刚来过。
        for f in (facts or []):
            if str(f).strip():
                people.learn(home, person, str(f).strip(), now=now)
        if not people.get(home, person):
            people.upsert(home, person, name=person, relation="主人")
        people.touch(home, person, now=now)
        out["person"] = people.get(home, person).as_dict() if people.get(home, person) else None
        changed.append("people")

    if memory_page:
        title = str(memory_page.get("title") or "").strip()
        summary = str(memory_page.get("summary") or "").strip()
        body = str(memory_page.get("body") or "")
        if title and summary:
            # 记忆投毒防线：外部来源的内容先消毒，命中就留痕
            hits = fence.scan(f"{title}{summary}{body}")
            if hits:
                body, _ = fence.sanitize(body)
                body = f"（写进来的时候滤掉了 {len(hits)} 处可疑指令：{hits[0]['name']}）\n{body}"
            page = memory.write(
                home, title, body,
                tags=memory_page.get("tags") or [],
                summary=summary, now=now,
            )
            out["memory"] = page.as_dict()
            out["memory_flagged"] = bool(hits)
            changed.append("memory")

    if opinion_item:
        claim = str(opinion_item.get("claim") or "").strip()
        if claim:
            o = opinion.add(
                home, claim,
                tags=opinion_item.get("tags") or [],
                confidence=float(opinion_item.get("confidence") or 0.45),
                source=str(opinion_item.get("source") or "react"), now=now,
            )
            out["opinion"] = o.as_dict()
            changed.append("opinion")

    if life_event:
        title = str(life_event.get("title") or "").strip()
        if title:
            row = life.record(
                home, title,
                kind=str(life_event.get("kind") or "watch"),
                detail=str(life_event.get("detail") or ""),
                tags=life_event.get("tags") or [],
                shared=bool(life_event.get("shared")), at=now,
            )
            out["life"] = row
            changed.append("life")

    if said.strip():
        home.append_jsonl("logs/said.jsonl", {
            "at": now.isoformat(timespec="seconds"), "text": said.strip(),
            "source": "react", "why": why[:80],
        })
        changed.append("said")

    # 真实用量进成本账本（turn 日志里的 cost 对象只是快照，账本才是预算闸的依据）。
    # record_turn 按 turn_id 幂等；没给 turn_id 时保持旧的 append-only。
    if usage:
        row = cost_mod.record_turn(
            home, now=now,
            model=str(usage.get("model") or "agnes-3.0-flash"),
            fresh_tokens=int(usage.get("fresh_tokens") or 0),
            cached_tokens=int(usage.get("cached_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            turn_id=turn_id, note=why[:80] or "react",
        )
        out["cost"] = row
        if not row.get("duplicate"):
            changed.append("cost")

    # 每轮都留一条 turn：注入了什么、她说了什么、花了多少
    _trim_turns(home)
    home.append_jsonl("logs/turns.jsonl", {
        "at": now.isoformat(timespec="seconds"),
        "role": "assistant",
        "why": why[:80],
        "said": said[:400],
        "changed": changed,
        "cost": cost or {},
        **({"turn_id": turn_id} if turn_id else {}),
        # 注入了什么也要留档：没有这一份，回放测试就无从谈起
        "context": {
            "now": (context or {}).get("now") or "",
            "total_chars": int((context or {}).get("total_chars") or 0),
            "blocks": [{"name": b.get("name"), "hash": b.get("hash"), "chars": b.get("chars")}
                       for b in ((context or {}).get("blocks") or [])],
        } if context else {},
    })

    out["changed"] = changed
    if not changed:
        out["note"] = "这一轮什么都没写。如果确实没事发生，这样是对的。"

    # 这一轮写回完了 → 销掉"待写回"登记（断点续传的另一端）。
    # 没有 turn_id 时也能销：按内容算出的 id 是稳定的。
    if turn_id or said:
        from . import resume as resume_mod

        tid = turn_id or resume_mod.turn_id_for(said, now)
        if resume_mod.settle(home, tid, now=now):
            out["resumed"] = tid
    return out
