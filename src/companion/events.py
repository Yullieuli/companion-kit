"""events：给宿主用的事件流。

为什么用 append-only + cursor 而不是内存队列：
  · 宿主可能是另一个进程（dsh 的 Node、hermes 的 Python），它重启了我们不该丢事件
  · 用文件行号当 cursor，最简单、可重放、git 也能看
  · 同一事件只投递一次：cursor 是"读到第几行"，读过的不会再给

事件类型（目前三种）：
  · wake_due      —— 该叫醒她了（带 action / reason / 预计花费）
  · mood_shift    —— 心情跨了标签
  · spend_guard   —— 预算或风控拦下了一次醒来
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

EVENTS = "logs/events.jsonl"
CURSOR = "logs/.cursor"


def emit(home, kind: str, now: datetime | None = None, **payload: Any) -> dict[str, Any]:
    now = now or datetime.now()
    evt = {
        "id": f"{int(now.timestamp() * 1000)}-{kind}",
        "at": now.isoformat(timespec="seconds"),
        "kind": kind,
        **payload,
    }
    home.append_jsonl(EVENTS, evt)
    return evt


def read_all(home) -> list[dict[str, Any]]:
    return home.read_jsonl(EVENTS, limit=100000)


def cursor(home) -> int:
    try:
        return int((home.root / CURSOR).read_text(encoding="utf-8").strip() or 0)
    except Exception:
        return 0


def save_cursor(home, value: int) -> None:
    p = home.root / CURSOR
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(int(value)), encoding="utf-8")


def poll(home, since: int | None = None, advance: bool = True) -> tuple[list[dict[str, Any]], int]:
    """返回 (新事件, 新 cursor)。since 不传就沿用文件里存的。"""
    start = cursor(home) if since is None else max(0, int(since))
    rows = read_all(home)
    fresh = rows[start:]
    new_cursor = len(rows)
    if advance:
        save_cursor(home, new_cursor)
    return fresh, new_cursor


def wait(home, timeout: float = 10.0, interval: float = 0.5, since: int | None = None):
    """长轮询：等到有新事件或超时。给 HTTP /events 用。"""
    start = cursor(home) if since is None else max(0, int(since))
    deadline = time.time() + max(0.0, timeout)
    while True:
        rows = read_all(home)
        if len(rows) > start:
            save_cursor(home, len(rows))
            return rows[start:], len(rows)
        if time.time() >= deadline:
            return [], start
        time.sleep(interval)
