"""resume：断点续传——她说过但没写回的那些话，找回来。

**问题**：一轮对话要分两半——
  1. 她开口（`say`）：额度扣了、话说出去了、写进 `said.jsonl`
  2. 她写回（`react`）：心情变了、记忆写了、账记了

如果第 2 步之前进程崩了（断网、宿主被杀、模型超时），这一轮就"丢了"：
她说了话，但心情没动、记忆没写、账也没记。
更糟的是**重试**会让她重复说、重复扣额度。

**做法**：`say` 成功时顺手登记一条"待写回"（`logs/pending_turns.jsonl`），
写回成功（`react` 带同一个 `turn_id`）时销掉。
于是：
  · 崩了 → `resume.pending()` 列出未写回的轮次，宿主可以用 `turn_id` 补 `react`
  · 重试 → 补的时候 `turn_id` 幂等，不会重复改心情/重复计费

这跟 `react` 的 `turn_id` 幂等是同一套机制的两端：
  · `react` 保证"补一次不重复"
  · `resume` 保证"漏了能发现"
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

from .store import Home

PENDING = "logs/pending_turns.jsonl"
# 超过这个时间就算"陈旧"：再补也不一定有意义（比如一周前的对话）
STALE_HOURS = 72.0


def turn_id_for(text: str, at: datetime | None = None) -> str:
    """给一句话算一个稳定的 turn_id（同一句话同一分钟 → 同一个 id）。

    这样宿主崩溃后重试，系统能认出"这是同一轮"而不是新一轮。
    """
    at = at or datetime.now()
    key = f"{at.strftime('%Y-%m-%d %H:%M')}|{(text or '').strip()}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def register(
    home: Home,
    text: str,
    *,
    turn_id: str = "",
    channel: str = "",
    now: datetime | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """登记一个"待写回"的轮次。`say` 出口调用它。"""
    now = now or datetime.now()
    text = (text or "").strip()
    tid = turn_id or turn_id_for(text, now)
    row = {
        "turn_id": tid,
        "at": now.isoformat(timespec="seconds"),
        "epoch": now.timestamp(),
        "text": text[:400],
        "channel": channel,
        "status": "pending",
    }
    if extra:
        row["extra"] = extra
    home.append_jsonl(PENDING, row)
    return row


def settle(home: Home, turn_id: str, now: datetime | None = None) -> bool:
    """写回成功：把这条标成 done。"""
    if not turn_id:
        return False
    now = now or datetime.now()
    rows = home.read_jsonl(PENDING, limit=500)
    # 按 turn_id 去重，只留最后一次状态（同 id 多行说明重试过）
    latest: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    hit = False
    for r in rows:
        tid = str(r.get("turn_id") or "")
        if not tid:
            continue
        if tid == str(turn_id):
            if r.get("status") != "done":
                r = {**r, "status": "done",
                     "settled_at": now.isoformat(timespec="seconds")}
                hit = True
            else:
                hit = True   # 已经 done 了：算成功，但不用再改
        if tid not in latest:
            order.append(tid)
        latest[tid] = r
    if not hit:
        return False
    home.save_jsonl(PENDING, [latest[t] for t in order])
    return True


def pending(home: Home, *, max_age_hours: float = STALE_HOURS,
            now: datetime | None = None) -> list[dict[str, Any]]:
    """还没写回的轮次（按时间倒序）。"""
    now = now or datetime.now()
    cut = (now - timedelta(hours=max_age_hours)).timestamp()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in home.read_jsonl(PENDING, limit=500):
        tid = str(r.get("turn_id") or "")
        if not tid or tid in seen:
            continue
        seen.add(tid)
        if r.get("status") == "done":
            continue
        if float(r.get("epoch") or 0) < cut:
            continue
        out.append(r)
    out.sort(key=lambda r: float(r.get("epoch") or 0), reverse=True)
    return out


def summary(home: Home, **kw: Any) -> dict[str, Any]:
    """待写回轮次的概况（给 status / doctor 用）。"""
    rows = pending(home, **kw)
    return {
        "pending": len(rows),
        "turns": [r.get("turn_id") for r in rows[:10]],
        "oldest": rows[-1].get("at") if rows else "",
    }
