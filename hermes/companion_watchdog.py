#!/usr/bin/env python3
"""hermes 适配层 ③：看门狗——她是不是还活着。

**为什么需要这个**：hermes 网关可能
  · 崩了（OOM、机器重启）—— 进程没了
  · 卡死（线程死锁、模型调用挂住）—— 进程还在，但什么都不做

这两种"她不说话了"从外面看**完全一样**：没有消息。
主人不会知道该重启还是该等。

**这个脚本做什么**：读 store 里的心跳，判断状态，然后**按状态给建议**：
  · alive  —— 正常，什么都不做
  · stale  —— 进程还在但很久没动静 → **该重连**（多半是网络或上游模型）
  · dead  —— 进程没了 → **该重启**（让 systemd/supervisor 拉起）
  · never —— 从没心跳过 → 可能还没装好，先看别的

**用法**：
    # 检查（只读）
    python companion_watchdog.py

    # 有问题就重启 hermes 网关（需配 COMPANION_RESTART_CMD）
    python companion_watchdog.py --fix

    # 给 cron 用：有问题时退出码非 0，方便监控告警
    python companion_watchdog.py --exit-code

**设计原则**：core 只给事实，不替宿主做决定。
重启宿主是**破坏性操作**，所以必须由人显式开 `--fix` 才做。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_HOME = str(Path.home() / ".companion")
HOST = os.environ.get("COMPANION_HOST", "hermes")
# 多久没心跳算卡住
STALE_MINUTES = float(os.environ.get("COMPANION_STALE_MINUTES", "20") or 20)
# 重启命令（不配就只报不修）
RESTART_CMD = os.environ.get("COMPANION_RESTART_CMD", "")


def _load():
    try:
        from companion import watchdog
        from companion.store import Home
    except Exception as e:
        print(f"companion 不可用：{e}", file=sys.stderr)
        return None, None
    return watchdog, Home.at(os.environ.get("COMPANION_HOME") or DEFAULT_HOME)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true",
                    help="卡住或死了就按 COMPANION_RESTART_CMD 重启（破坏性操作）")
    ap.add_argument("--exit-code", action="store_true",
                    help="有问题时退出码非 0（给 cron/monitoring 用）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--stale-minutes", type=float, default=STALE_MINUTES)
    args = ap.parse_args()

    wd, home = _load()
    if wd is None:
        return 0

    try:
        state = wd.check(home, stale_minutes=args.stale_minutes)
        hosts = wd.stale_hosts(home, stale_minutes=args.stale_minutes)
    except Exception as e:
        print(f"看门狗检查失败：{type(e).__name__}: {e}", file=sys.stderr)
        return 0

    state["hosts"] = hosts
    state["host"] = state.get("host") or HOST
    state["checked_at"] = datetime.now().isoformat(timespec="seconds")

    # 顺带把"待写回的轮次"报出来——这也是一种"她没做完事"的信号
    try:
        from companion import resume as resume_mod

        state["pending_turns"] = resume_mod.summary(home).get("pending", 0)
    except Exception:
        state["pending_turns"] = 0

    fixed = False
    if args.fix and state.get("state") in ("stale", "dead"):
        if not RESTART_CMD:
            state["fix"] = "没配 COMPANION_RESTART_CMD，只报不改"
        else:
            try:
                subprocess.run(RESTART_CMD, shell=True, check=False, timeout=60)
                state["fix"] = f"已执行重启：{RESTART_CMD}"
                fixed = True
            except Exception as e:
                state["fix"] = f"重启失败：{type(e).__name__}: {e}"

    if args.json:
        print(json.dumps(state, ensure_ascii=False, indent=2))
    else:
        print(f"[{state['state']}] {state.get('why', '')}")
        for h in hosts:
            print(f"  · {h['host']}：{h['state']}（{h['age_minutes']:.0f} 分钟没动，pid={h['pid']}）")
        if state.get("pending_turns"):
            print(f"  · 有 {state['pending_turns']} 轮说了话还没写回（companion resume 看详情）")
        if state.get("fix"):
            print(f"  · {state['fix']}")

    if args.exit_code and state.get("state") in ("stale", "dead"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
