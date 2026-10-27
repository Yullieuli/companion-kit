"""companion 命令行 —— 所有宿主共用的那一层。

设计原则：命令行的输出就是契约。
  · 默认人读（中文、短句）
  · `--json` 机器读（给宿主、给 MCP 包装）
  · 任何命令都不该在没初始化时报错崩掉，而是提示先 `companion init`

命令：
  init                       建 store（幂等，不覆盖已有文件）
  context [--budget N]       打印本轮该注入的上下文
  tick                       问一次"现在该干什么"（不调模型）
  events [--since N] [--wait SECONDS]
  cost                       今天花了多少
  say --text ...             预览将要发出去的消息（不真发）
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from . import context as context_mod
from . import cost, drive, events
from .store import Home


def _dump(obj, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    else:
        print(obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, indent=2))


def cmd_init(home: Home, args) -> int:
    created = home.init()
    if args.json:
        _dump({"home": str(home.root), "created": created}, True)
    else:
        print(f"store: {home.root}")
        print("新建：" + ("、".join(created) if created else "（已存在，没有覆盖任何文件）"))
    return 0


def cmd_context(home: Home, args) -> int:
    ctx = context_mod.build(home, budget=args.budget)
    if args.json:
        _dump(ctx.as_dict(), True)
    else:
        print(ctx.text)
    return 0


def cmd_tick(home: Home, args) -> int:
    result = drive.tick(home, now=datetime.now(), emit_silent=args.emit_silent)
    if args.json:
        _dump(result, True)
    else:
        print(f"动作：{result['action']}（{result['why']}）")
        print(f"预计花费：¥{result['cost']:.3f}；今天已花 ¥{result['spent_today']:.3f}")
        if result.get("blocked"):
            print(f"被拦：{result['blocked']}")
    return 0


def cmd_status(home: Home, args) -> int:
    """只读：看当前状态，不推进任何决策（不扣额度、不写事件）。"""
    from . import quota as quota_mod

    rows = home.read_jsonl("logs/initiatives.jsonl", limit=5)
    payload = {
        "home": str(home.root),
        "mood": home.mood().get("label"),
        "quota": home.quota(),
        "in_window": None,
        "last_decision": rows[-1] if rows else None,
        "cost_today": cost.today_total(home),
    }
    try:
        from . import clock as clock_mod
        payload["in_window"] = clock_mod.in_window(home.rhythm())
        payload["in_sleep"] = clock_mod.in_sleep(home.rhythm())
    except Exception:
        pass
    if args.json:
        _dump(payload, True)
    else:
        print(f"心情：{payload['mood']}｜今天 ¥{payload['cost_today']:.3f}")
        print(f"额度：{quota_mod.summary(home.quota(), None)}")
        if payload["last_decision"]:
            d = payload["last_decision"]
            print(f"上次决策：{d.get('action')}（{d.get('why')}）@ {d.get('at')}")
    return 0


def cmd_events(home: Home, args) -> int:
    if args.wait > 0:
        rows, cur = events.wait(home, timeout=args.wait, since=args.since)
    else:
        rows, cur = events.poll(home, since=args.since)
    _dump({"events": rows, "cursor": cur} if args.json else (rows or "（没有新事件）"), args.json)
    return 0


def cmd_cost(home: Home, args) -> int:
    if args.json:
        _dump({"today_cny": cost.today_total(home), "turns": len(cost.load_rows(home))}, True)
    else:
        print(cost.summary(home))
    return 0


def cmd_say(home: Home, args) -> int:
    """预览：她要说的话 + 会走哪个通道。不真发。"""
    payload = {"text": args.text, "channel": args.channel or "default", "dry_run": True}
    _dump(payload, args.json)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="companion", description="跨 agent 的伴侣运行时")
    p.add_argument("--home", help="store 目录（默认 $COMPANION_HOME 或 ~/.companion）")
    p.add_argument("--json", action="store_true", help="机器可读输出")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="初始化 store")

    c = sub.add_parser("context", help="打印本轮注入上下文")
    c.add_argument("--budget", type=int, default=2000)

    t = sub.add_parser("tick", help="问一次该干什么")
    t.add_argument("--emit-silent", action="store_true")

    e = sub.add_parser("events", help="读事件")
    e.add_argument("--since", type=int, default=None)
    e.add_argument("--wait", type=float, default=0)

    sub.add_parser("status", help="只读状态（不推进决策）")

    sub.add_parser("cost", help="今日花费")

    s = sub.add_parser("say", help="预览要发的消息")
    s.add_argument("--text", required=True)
    s.add_argument("--channel")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    home = Home.at(args.home)
    table = {
        "init": cmd_init,
        "context": cmd_context,
        "tick": cmd_tick,
        "events": cmd_events,
        "cost": cmd_cost,
        "status": cmd_status,
        "say": cmd_say,
    }
    try:
        return table[args.cmd](home, args)
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # 给宿主一个稳定的错误形状
        if args.json:
            print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
        else:
            print(f"出错：{type(e).__name__}: {e}", file=sys.stderr)
        return 1
