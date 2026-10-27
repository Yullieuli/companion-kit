"""context：把 store 里的东西拼成一段能直接塞进 system prompt 的文本。

为什么单独一层：三种宿主注入方式不同（hermes 是 hook、dsh 是 systemPrompt.context、
codex 是 MCP 工具），但它们要的**内容必须逐字节一致**，否则"同一个人换个宿主就变性格"。

三条规则：
  · 顺序固定：人格 → 状态（心情/时间/额度）→ 记忆 → 人物卡 → 生活事件
  · 有预算：超了就从最低优先级的尾部截，绝不无声丢整块
  · 每块带 hash：宿主可以只注入变化的部分（省 token 的关键）
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import clock, mood, quota
from .store import Home

DEFAULT_BUDGET = 2000
SOUL_CAP = 600
ELLIPSIS = "…"


@dataclass
class Block:
    name: str
    text: str
    priority: int = 50

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:16]


@dataclass
class Context:
    text: str
    blocks: list[Block] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "blocks": [{"name": b.name, "chars": len(b.text), "hash": b.hash} for b in self.blocks],
            "total_chars": len(self.text),
        }


def _head(text: str, limit: int) -> str:
    text = (text or "").strip()
    if limit <= 0:
        return ""
    return text if len(text) <= limit else text[: max(0, limit - len(ELLIPSIS))] + ELLIPSIS


def _state_block(
    home: Home,
    now: datetime,
    last_chat: float | None,
    owner_last_seen: float | None,
    quota_cfg: dict[str, Any] | None,
) -> str:
    rhythm = home.rhythm()
    lines = [f"【现在】{clock.describe(now, last_chat)}"]
    if clock.in_sleep(rhythm, now):
        wake = clock.sleep_until(rhythm, now)
        lines.append(f"【状态】你在睡觉（{wake:%H:%M} 醒）。除非主人直接叫你，否则别主动说话。")
    elif clock.in_window(rhythm, now):
        lines.append("【状态】在作息窗口里。")
    else:
        lines.append("【状态】不在作息窗口，安静一点。")
    m = mood.decay(home.mood(), now)
    lines.append(f"【心情】{mood.summary(m)}")
    q = home.quota() or {}
    lines.append(f"【开口额度】{quota.summary(q, quota_cfg, now, owner_last_seen)}")
    kind = clock.greeting_kind(rhythm, now)
    if kind == "morning":
        lines.append("【问候】这是今天的第一次，可以先说句早。")
    elif kind == "night":
        lines.append("【问候】夜深了，说话轻一点。")
    return "\n".join(lines)


def build(
    home: Home,
    *,
    now: datetime | None = None,
    budget: int = DEFAULT_BUDGET,
    last_chat: float | None = None,
    owner_last_seen: float | None = None,
    quota_cfg: dict[str, Any] | None = None,
    include_soul: bool = True,
    memory_text: str = "",
    opinion_text: str = "",
    person_text: str = "",
    life_text: str = "",
) -> Context:
    now = now or datetime.now()
    home.init()

    blocks: list[Block] = []
    # 宿主自己已经有人格时（hermes 把它当系统提示），别再注入一份：
    # 重复会导致两套语气打架，而且要挤占预算
    soul = _head(home.soul(), min(SOUL_CAP, max(0, budget // 3))) if include_soul else ""
    if soul:
        blocks.append(Block("soul", soul, 10))
    state = _state_block(home, now, last_chat, owner_last_seen, quota_cfg)
    blocks.append(Block("state", state, 20))
    if memory_text.strip():
        blocks.append(Block("memory", memory_text.strip(), 40))
    if opinion_text.strip():
        blocks.append(Block("opinion", opinion_text.strip(), 50))
    if person_text.strip():
        blocks.append(Block("person", person_text.strip(), 60))
    if life_text.strip():
        blocks.append(Block("life", life_text.strip(), 80))

    # 按优先级依次放行，剩余预算不够就截断这一块，后面的直接放空
    used = 0
    kept: list[Block] = []
    for b in sorted(blocks, key=lambda x: x.priority):
        sep = 2 if kept else 0
        room = budget - used - sep
        if room <= 20:
            break
        text = _head(b.text, room)
        if not text:
            break
        kept.append(Block(b.name, text, b.priority))
        used += len(text) + sep
    # 输出仍按语义顺序，不按预算顺序
    order = {"soul": 0, "state": 1, "memory": 2, "opinion": 3, "person": 4, "life": 5}
    kept.sort(key=lambda x: order.get(x.name, 99))
    parts = [b.text for b in kept]
    return Context(text="\n\n".join(parts), blocks=kept)


@dataclass
class Cache:
    """按会话记住上次注入了什么，只返回变化的块名。"""

    seen: dict[str, dict[str, str]] = field(default_factory=dict)

    def changed(self, session: str, blocks: list[Block]) -> list[str]:
        prev = self.seen.get(session) or {}
        now = {b.name: b.hash for b in blocks}
        diff = [name for name, h in now.items() if prev.get(name) != h]
        gone = [name for name in prev if name not in now]
        self.seen[session] = now
        return diff + gone
