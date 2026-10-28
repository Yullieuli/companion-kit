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

from . import events, fence, life, memory, mood as mood_mod, opinion, people
from .store import Home


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
    context: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now()
    home.init()
    changed: list[str] = []
    out: dict[str, Any] = {"ok": True, "at": now.isoformat(timespec="seconds")}

    if mood_delta:
        out["mood"] = _mood(home, mood_delta, why, now)
        changed.append("mood")
        if out["mood"]["shifted"]:
            changed.append("mood_shift")

    if person and facts:
        for f in facts:
            if str(f).strip():
                people.learn(home, person, str(f).strip(), now=now)
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

    # 每轮都留一条 turn：注入了什么、她说了什么、花了多少
    home.append_jsonl("logs/turns.jsonl", {
        "at": now.isoformat(timespec="seconds"),
        "role": "assistant",
        "why": why[:80],
        "said": said[:400],
        "changed": changed,
        "cost": cost or {},
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
    return out
