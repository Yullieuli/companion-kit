#!/usr/bin/env python3
"""hermes 适配层 ②：用 companion 的时钟决定"该不该叫醒她"。

用法：把它设成 hermes cron 的 `monitor_script`。
hermes 的判定是"输出变了就起会话"，而空输出现在被当作"没叫我"（不再触发）。
于是这个脚本只需要：该说就打印一行 `WAKE|...`，不该说就什么都不打印。

这样一来：
  · pulse.py 的骰子、自由时间的三个固定时刻都不再需要，全交给 companion
  · 一天 96 次 tick 里绝大多数是空输出，**零 token**
  · 配额、作息、心情、预算都在 companion 的 store 里，只有一本账

两种"不叫醒但做事"的情况：
  · `diary` —— 她自己写点什么，不用模型
  · `bili`  —— 她自己刷一条，写进生活流水；下一轮有话说时会自然提起，
    所以这里也不打印 WAKE。**不为一段视频起一次会话**，这是成本约束不是省事。

测法（不激活）：
    COMPANION_HOME=/mnt/c/Users/you/.companion \
      ~/.companion-venv/bin/python ~/.hermes/scripts/companion_wake.py
    # 有输出 = 这次会叫她；没输出 = 继续睡

注意：`--dry-run` 只读，不扣额度、不写事件，用来观察"现在会怎么判"。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_HOME = str(Path.home() / ".companion")
HOST = os.environ.get("COMPANION_HOST", "hermes")

SILENT = ("silent", "diary")


def _owner_talked_recently(home, minutes: float = 15.0) -> bool:
    """主人最近有没有在说话。

    主动开口最忌讳插进一场正在进行的对话：话题接不上，她的回复还会被挤到两条
    不相干的消息中间。tick 本身看不到宿主的聊天状态（drive.tick 的 owner_waiting
    默认 False，且没人计算它），所以在这一层挡一道。

    只看**最近一条 user 记录**：它比 now 老过阈值，就说明这场对话已经停了。
    不遍历全部历史——更早的记录只说明"今天说过话"，不是"正在说"。
    """
    try:
        rows = home.read_jsonl("logs/turns.jsonl")
    except Exception:
        return False  # 读不到就不拦：宁可少一次沉默，也不要因为她不说话而卡住
    now = datetime.now()
    for row in reversed(rows):
        if not isinstance(row, dict) or row.get("role") != "user":
            continue
        try:
            at = datetime.fromisoformat(str(row.get("at") or ""))
        except Exception:
            return False
        return (now - at).total_seconds() < minutes * 60
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只读：不扣额度、不写事件")
    ap.add_argument("--json", action="store_true", help="调试：打印完整决策")
    args = ap.parse_args()

    home_path = os.environ.get("COMPANION_HOME") or DEFAULT_HOME
    try:
        from companion import drive
        from companion.store import Home
    except Exception as e:
        print(f"companion 不可用：{e}", file=sys.stderr)
        return 0  # 返回 0 且无输出 = 不叫醒，绝不因为适配层坏掉而乱发消息

    home = Home.at(home_path)
    try:
        if args.dry_run:
            from companion import initiative, mood, quota

            rhythm = home.rhythm()
            st = mood.decay(home.mood(), datetime.now())
            allowed, why = quota.allow(home.quota(), None, datetime.now())
            decision = initiative.decide(
                initiative.Situation(
                    now=datetime.now(), mood_state=st, quota_state=home.quota(), rhythm=rhythm
                )
            )
            result = {**decision, "quota_allowed": allowed, "quota_why": why}
        else:
            # 他正在说话就别插嘴：主动消息应该是"没人理她时她来找你"，
            # 不是"你说一句她插一句"。15 分钟是对话停止的判定窗口——
            # wake 每 15 分钟跑一次，正好对上。
            if _owner_talked_recently(home):
                return 0
            result = drive.tick(home, host=HOST)
    except Exception as e:
        print(f"tick 失败：{e}", file=sys.stderr)
        return 0

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    action = result.get("action") or "silent"
    if action in SILENT or result.get("blocked") in ("sleep", "dice", "budget", "quota"):
        return 0  # 空输出 = 不叫醒

    if action == "bili":
        # 刷片不需要模型：直接跑一轮，把「看了什么」写进生活流水。
        # 下一轮她自然会说（没讲过的生活事件会推高"想说话"的分），
        # 所以这里**不打印 WAKE** —— 不为一支视频起一次会话。
        try:
            from companion import bili

            out = bili.tick(home)
            if not out.get("ok"):
                print(f"bili 跳过：{out.get('why')}", file=sys.stderr)
        except Exception as e:
            print(f"bili 失败：{type(e).__name__}: {e}", file=sys.stderr)
        return 0

    reason = (result.get("why") or "").replace("|", "／")[:60]
    print(f"WAKE|{reason}|{action}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
