"""watchdog：她是不是还活着。

**问题**：hermes / dsh 这类宿主是长驻进程。它们可能：
  · 卡死（线程死锁、模型调用挂住）——进程还在，但什么都不做
  · 崩了（被 OOM 杀掉、机器重启）——进程没了
  · 僵在那（网络断了，重连没成功）

这两种"她不说话了"从外面看**完全一样**：没有消息。主人不会知道该重启还是该等。

**做法**：宿主每次醒来都写一个心跳（`logs/heartbeat.json`：pid、宿主名、时间）。
然后：
  · `watchdog.check()` —— 现在有多久没心跳了？超过阈值就是"卡住了"
  · `watchdog.stale_hosts()` —— 哪些宿主卡住了 / 死了（按 pid 探活）
  · 宿主自己可以据此决定：重连（网络问题）还是退出让 systemd 拉起（进程死了）

**关键设计**：core 不替宿主做决定，只给**事实**。
"卡住了"和"死了"要分清，因为处置完全不同：
  · 死了 → 重启（systemd / supervisor 管）
  · 卡住了 → 重连（多半是网络或上游模型）
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Any

from .store import Home

HEARTBEAT = "logs/heartbeat.json"
# 超过这个分钟数没心跳就算"卡住了"
STALE_MINUTES = 20.0
# 超过这个小时数没心跳，跨天了就别指望它自己恢复
DEAD_HOURS = 6.0


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now()


def beat(home: Home, host: str = "cli", *, now: datetime | None = None,
         extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """写一次心跳。宿主每次醒来都调它。"""
    now = _now(now)
    row: dict[str, Any] = {
        "host": host,
        "pid": os.getpid(),
        "at": now.isoformat(timespec="seconds"),
        "epoch": now.timestamp(),
    }
    if extra:
        row.update(extra)
    home.save_json(HEARTBEAT, row)
    return row


def last_beat(home: Home) -> dict[str, Any]:
    """最后一次心跳（没有就空 dict）。"""
    return home.read_json(HEARTBEAT) or {}


def age_minutes(home: Home, *, now: datetime | None = None) -> float:
    """距上次心跳多少分钟。没有心跳 → 999（当作很久没动）。"""
    row = last_beat(home)
    ts = float(row.get("epoch") or 0)
    if not ts:
        return 999.0
    return max(0.0, (_now(now).timestamp() - ts) / 60.0)


def alive(pid: int) -> bool:
    """这个 pid 还活着吗。跨平台：Windows 用 OpenProcess，POSIX 用 kill(0)。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return False
                return code.value == STILL_ACTIVE
            finally:
                k32.CloseHandle(h)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # 活着，只是我们没权限看
    except Exception:
        return False


def check(home: Home, *, now: datetime | None = None,
          stale_minutes: float = STALE_MINUTES) -> dict[str, Any]:
    """体检心跳。

    返回 {ok, age_minutes, host, pid, alive, state}。
    `state` ∈ alive / stale / dead / never。
    """
    now = _now(now)
    row = last_beat(home)
    if not row:
        return {"ok": True, "state": "never", "age_minutes": 999.0,
                "host": "", "pid": 0, "alive": False,
                "why": "还没有心跳记录"}
    age = age_minutes(home, now=now)
    pid = int(row.get("pid") or 0)
    host = str(row.get("host") or "")
    is_alive = alive(pid) if pid else False
    if not is_alive:
        state = "dead"
        why = f"{host} 的进程 {pid} 已经不在了"
    elif age >= stale_minutes:
        state = "stale"
        why = f"{host} 已经 {age:.0f} 分钟没动静了（阈值 {stale_minutes:.0f}）"
    else:
        state = "alive"
        why = f"{host} {age:.0f} 分钟前还活着"
    return {"ok": state == "alive", "state": state, "age_minutes": round(age, 1),
            "host": host, "pid": pid, "alive": is_alive, "why": why}


def stale_hosts(home: Home, *, now: datetime | None = None,
                stale_minutes: float = STALE_MINUTES) -> list[dict[str, Any]]:
    """哪些宿主卡住了或死了。

    心跳文件只记"最后是谁"。要记多宿主，得写 `logs/heartbeats.json`——
    这份是每个宿主各写一行，按 host 去重后判断。
    """
    now = _now(now)
    best: dict[str, dict[str, Any]] = {}
    for r in home.read_jsonl("logs/heartbeats.jsonl", limit=200):
        host = str(r.get("host") or "")
        if not host:
            continue
        prev = best.get(host)
        if prev is None or float(r.get("epoch") or 0) > float(prev.get("epoch") or 0):
            best[host] = r
    out: list[dict[str, Any]] = []
    for host, r in best.items():
        age = max(0.0, (now.timestamp() - float(r.get("epoch") or 0)) / 60.0)
        pid = int(r.get("pid") or 0)
        is_alive = alive(pid) if pid else False
        if not is_alive:
            state = "dead"
        elif age >= stale_minutes:
            state = "stale"
        else:
            continue   # 健康的不用报
        out.append({"host": host, "state": state, "age_minutes": round(age, 1),
                    "pid": pid, "alive": is_alive})
    out.sort(key=lambda x: -x["age_minutes"])
    return out
