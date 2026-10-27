"""people：人物卡。

她需要知道「对面是谁」，而不是每次从零推断。每个人一张 json：
名字、别名、关系、亲近/信任两个轴、确定知道的事实、上次说话时间。

两条设计取向：
  · 事实是**追加**的（learn），不覆盖 —— 人会变，旧事实留着看变化
  · 卡里只放「能改变她怎么说话」的东西，不放这个人说过什么（那属于记忆页）
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .store import Home

DIR = "people"
OWNER = "owner"
MAX_FACTS = 40


@dataclass
class Person:
    key: str
    name: str = ""
    aliases: list[str] = field(default_factory=list)
    relation: str = "朋友"
    closeness: int = 20
    trust: int = 50
    facts: list[str] = field(default_factory=list)
    notes: str = ""
    last_seen: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name or self.key,
            "aliases": self.aliases,
            "relation": self.relation,
            "closeness": self.closeness,
            "trust": self.trust,
            "facts": self.facts[-8:],
            "notes": self.notes,
            "last_seen": self.last_seen,
        }


def _path(home: Home, key: str):
    p = home.root / DIR
    p.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", key).strip("-") or "someone"
    return p / f"{safe}.json"


def get(home: Home, key: str) -> Person | None:
    p = _path(home, key)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8") or "{}")
    except Exception:
        return None
    return Person(
        key=p.stem,
        name=d.get("name", ""),
        aliases=list(d.get("aliases") or []),
        relation=d.get("relation", "朋友"),
        closeness=int(d.get("closeness", 20)),
        trust=int(d.get("trust", 50)),
        facts=list(d.get("facts") or []),
        notes=d.get("notes", ""),
        last_seen=d.get("last_seen", ""),
    )


def all_people(home: Home) -> list[Person]:
    out: list[Person] = []
    d = home.root / DIR
    if not d.exists():
        return out
    for f in sorted(d.glob("*.json")):
        p = get(home, f.stem)
        if p:
            out.append(p)
    return out


def resolve(home: Home, who: str) -> Person | None:
    """按 key / 名字 / 别名找人。"""
    w = (who or "").strip()
    if not w:
        return None
    p = get(home, w)
    if p:
        return p
    for person in all_people(home):
        if w == person.name or w in person.aliases:
            return person
    return None


def upsert(home: Home, key: str, **fields) -> Person:
    p = get(home, key) or Person(key=key, name=key)
    for k in ("name", "relation", "notes", "last_seen"):
        if k in fields and fields[k] is not None:
            setattr(p, k, str(fields[k]))
    for k in ("closeness", "trust"):
        if k in fields and fields[k] is not None:
            setattr(p, k, max(0, min(100, int(fields[k]))))
    if fields.get("aliases"):
        p.aliases = sorted({*p.aliases, *map(str, fields["aliases"])})
    if fields.get("facts"):
        for f in fields["facts"]:
            if f and f not in p.facts:
                p.facts.append(str(f))
        p.facts = p.facts[-MAX_FACTS:]
    _path(home, key).write_text(
        json.dumps(
            {
                "name": p.name,
                "aliases": p.aliases,
                "relation": p.relation,
                "closeness": p.closeness,
                "trust": p.trust,
                "facts": p.facts,
                "notes": p.notes,
                "last_seen": p.last_seen,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return p


def learn(home: Home, key: str, fact: str, now: datetime | None = None) -> Person:
    """学会一件关于他的事。重复的不加。"""
    fact = (fact or "").strip()
    if not fact:
        raise ValueError("事实不能为空")
    p = get(home, key) or Person(key=key, name=key)
    if fact not in p.facts:
        p.facts.append(fact)
    p.facts = p.facts[-MAX_FACTS:]
    p.last_seen = (now or datetime.now()).strftime("%Y-%m-%d %H:%M")
    return _save(home, p)


def _save(home: Home, p: Person) -> Person:
    _path(home, p.key).write_text(
        json.dumps(
            {
                "name": p.name,
                "aliases": p.aliases,
                "relation": p.relation,
                "closeness": p.closeness,
                "trust": p.trust,
                "facts": p.facts,
                "notes": p.notes,
                "last_seen": p.last_seen,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return p


def touch(home: Home, key: str, now: datetime | None = None) -> Person | None:
    p = get(home, key)
    if not p:
        return None
    p.last_seen = (now or datetime.now()).strftime("%Y-%m-%d %H:%M")
    return _save(home, p)


def render(home: Home, who: str = OWNER, *, max_facts: int = 6) -> str:
    """给 context 的一段。没有卡就返回空，不占预算。"""
    p = resolve(home, who)
    if not p:
        return ""
    head = f"【{p.name or p.key}】{p.relation}｜亲近 {p.closeness}｜信任 {p.trust}"
    if p.last_seen:
        head += f"｜上次说话 {p.last_seen}"
    lines = [head]
    for f in p.facts[-max_facts:]:
        lines.append(f"- {f}")
    if p.notes:
        lines.append(f"- 备注：{p.notes}")
    return "\n".join(lines)
