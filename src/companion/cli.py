"""companion 命令行 —— 所有宿主共用的那一层。

设计原则：命令行的输出就是契约。
  · 默认人读（中文、短句）
  · `--json` 机器读（给宿主、给 MCP 包装）
  · 任何命令都不该在没初始化时报错崩掉，而是提示先 `companion init`

命令：
  init                          建 store（幂等，不覆盖已有文件）
  context [--budget N]          打印本轮该注入的上下文
  status                        只读状态（不推进决策、不扣额度）
  tick                          问一次"现在该干什么"（不调模型）
  events [--since N] [--wait S]
  cost                          今天花了多少
  say --text ...                记一句她想说的话（预览，不发）
  memory list|search|write|index|stale
  life add|recent|unshared
  person get|learn|list
  device look|act|open|log
  voice say|sing|list
  mcp                           MCP 服务（stdio，给 codex / openclaw）
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from . import context as context_mod
from . import cost, drive, events, life, memory, people
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
    ctx = context_mod.build(
        home,
        budget=args.budget,
        memory_text=memory.render(home, args.query or ""),
        person_text=people.render(home, people.OWNER),
        life_text=life.render(home),
    )
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
    from . import clock as clock_mod
    from . import quota as quota_mod

    rows = home.read_jsonl("logs/initiatives.jsonl", limit=5)
    payload = {
        "home": str(home.root),
        "mood": home.mood().get("label"),
        "quota": home.quota(),
        "in_window": clock_mod.in_window(home.rhythm()),
        "in_sleep": clock_mod.in_sleep(home.rhythm()),
        "last_decision": rows[-1] if rows else None,
        "cost_today": round(cost.today_total(home), 4),
        "memory_pages": len(memory.pages(home)),
        "life_recent": len(life.recent(home, days=2)),
        "people": len(people.all_people(home)),
    }
    if args.json:
        _dump(payload, True)
    else:
        print(f"心情：{payload['mood']}｜今天 ¥{payload['cost_today']:.3f}")
        print(f"额度：{quota_mod.summary(home.quota(), None)}")
        print(f"记忆 {payload['memory_pages']} 页｜生活 {payload['life_recent']} 条（两天内）｜人物卡 {payload['people']} 张")
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
        _dump({"today_cny": round(cost.today_total(home), 4), "turns": len(cost.load_rows(home))}, True)
    else:
        print(cost.summary(home))
    return 0


def cmd_say(home: Home, args) -> int:
    payload = {"text": args.text, "channel": args.channel or "default", "dry_run": True}
    _dump(payload, args.json)
    return 0


# ── 记忆 ────────────────────────────────────────────────────
def cmd_memory(home: Home, args) -> int:
    if args.action == "write":
        page = memory.write(home, args.title, args.body or "", tags=args.tags or [], summary=args.summary)
        _dump(page.as_dict() if args.json else f"写了 {page.slug}：{page.summary}", args.json)
    elif args.action == "search":
        hits = memory.search(home, args.query or "", limit=args.limit)
        if args.json:
            _dump([{**p.as_dict(), "score": round(s, 2)} for s, p in hits], True)
        else:
            for s, p in hits:
                print(f"{s:5.2f}  {p.slug}  {p.summary or p.title}")
            if not hits:
                print("（没有对得上的）")
    elif args.action == "index":
        text = memory.build_index(home)
        _dump(text if not args.json else {"index": text, "pages": len(memory.pages(home))}, args.json)
    elif args.action == "stale":
        _dump(memory.stale(home) or ["（没有可以退休的页）"], args.json)
    else:  # list
        pages = memory.pages(home)
        if args.json:
            _dump([p.as_dict() for p in pages], True)
        else:
            for p in pages:
                print(f"- {p.slug}  [{','.join(p.tags)}]  {p.summary}")
            print(f"共 {len(pages)} 页")
    return 0


def cmd_life(home: Home, args) -> int:
    if args.action == "add":
        row = life.record(home, args.title, kind=args.kind, detail=args.detail or "",
                          tags=args.tags or [], shared=args.shared)
        _dump(row if args.json else f"记下了：{row['at']} {row['title']}", args.json)
    elif args.action == "unshared":
        rows = life.unshared(home)
        _dump(rows if args.json else (life.render(home) or "（没有没讲过的）"), args.json)
    else:  # recent
        rows = life.recent(home, days=args.days, limit=args.limit)
        _dump(rows if args.json else (life.render(home, days=args.days, limit=args.limit) or "（这几天什么都没做）"), args.json)
    return 0


def cmd_person(home: Home, args) -> int:
    if args.action == "learn":
        p = people.learn(home, args.who or people.OWNER, args.fact)
        _dump(p.as_dict() if args.json else f"记住了关于 {p.name or p.key} 的一件事：{args.fact}", args.json)
    elif args.action == "list":
        _dump([p.as_dict() for p in people.all_people(home)] if args.json
              else "\n".join(people.render(home, p.key) for p in people.all_people(home)) or "（还没有人物卡）", args.json)
    else:  # get
        p = people.resolve(home, args.who or people.OWNER)
        if not p:
            _dump({"ok": False, "error": "没有这张卡"}, args.json)
            return 1
        _dump(p.as_dict() if args.json else people.render(home, p.key), args.json)
    return 0


# ── 手 ─────────────────────────────────────────────────────
def cmd_device(home: Home, args) -> int:
    from . import device

    if args.action == "look":
        out = device.look(home, args.target)
    elif args.action == "act":
        payload = {k: getattr(args, k) for k in ("x", "y", "x2", "y2", "text", "key", "double") if getattr(args, k, None) is not None}
        out = device.act(home, args.target, args.act, payload, is_owner=not args.not_owner)
    elif args.action == "open":
        out = device.open_thing(home, args.target, args.what, is_owner=not args.not_owner)
    else:  # log
        out = device.log_tail(home, args.limit)
    _dump(out if args.json else json.dumps(out, ensure_ascii=False, indent=2), args.json)
    return 0 if (not isinstance(out, dict) or out.get("ok", True)) else 1


# ── 声音 ────────────────────────────────────────────────────
def cmd_voice(home: Home, args) -> int:
    from . import voice

    if args.action == "list":
        _dump(voice.last(home, args.limit) if args.json else voice.available(), args.json)
        return 0
    fn = voice.sing if args.action == "sing" else voice.speak
    out = fn(home, args.text, public=args.public)
    _dump(out if args.json else (f"生成：{out['path']}（{out.get('seconds')}s / {out.get('backend')}）"
                                 if out.get("ok") else f"失败：{out.get('error')}"), args.json)
    return 0 if out.get("ok") else 1


def cmd_mcp(home: Home, args) -> int:
    from . import mcp

    if args.list:
        print(json.dumps({"tools": [{"name": t["name"], "description": t["description"]} for t in mcp.TOOLS]},
                         ensure_ascii=False, indent=2))
        return 0
    return mcp.serve(home)


# ── 参数 ───────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="companion", description="跨 agent 的伴侣运行时")
    p.add_argument("--home", help="store 目录（默认 $COMPANION_HOME 或 ~/.companion）")
    p.add_argument("--json", action="store_true", help="机器可读输出")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="初始化 store")

    c = sub.add_parser("context", help="打印本轮注入上下文")
    c.add_argument("--budget", type=int, default=2000)
    c.add_argument("--query", help="按话题挑记忆（不给就取最近的）")

    t = sub.add_parser("tick", help="问一次该干什么")
    t.add_argument("--emit-silent", action="store_true")

    e = sub.add_parser("events", help="读事件")
    e.add_argument("--since", type=int, default=None)
    e.add_argument("--wait", type=float, default=0)

    sub.add_parser("status", help="只读状态（不推进决策）")
    sub.add_parser("cost", help="今日花费")

    s = sub.add_parser("say", help="记一句她想说的话")
    s.add_argument("--text", required=True)
    s.add_argument("--channel")

    m = sub.add_parser("memory", help="记忆页")
    m.add_argument("action", choices=["list", "search", "write", "index", "stale"], nargs="?", default="list")
    m.add_argument("query", nargs="?")
    m.add_argument("--title", default="")
    m.add_argument("--body", default="")
    m.add_argument("--summary", default="")
    m.add_argument("--tags", nargs="*")
    m.add_argument("--limit", type=int, default=5)

    lf = sub.add_parser("life", help="生活流水")
    lf.add_argument("action", choices=["add", "recent", "unshared"], nargs="?", default="recent")
    lf.add_argument("title", nargs="?", default="")
    lf.add_argument("--kind", default="watch", choices=list(life.KIND_VERB))
    lf.add_argument("--detail", default="")
    lf.add_argument("--tags", nargs="*")
    lf.add_argument("--shared", action="store_true")
    lf.add_argument("--days", type=float, default=2)
    lf.add_argument("--limit", type=int, default=8)

    pe = sub.add_parser("person", help="人物卡")
    pe.add_argument("action", choices=["get", "learn", "list"], nargs="?", default="get")
    pe.add_argument("who", nargs="?", default=people.OWNER)
    pe.add_argument("--fact", default="")

    d = sub.add_parser("device", help="手：看屏幕 / 动作 / 打开")
    d.add_argument("action", choices=["look", "act", "open", "log"])
    d.add_argument("--target", default="desktop", choices=["phone", "desktop", "browser", "game"])
    d.add_argument("--act", default="tap", choices=["tap", "click", "swipe", "type", "key"])
    d.add_argument("--x", type=int)
    d.add_argument("--y", type=int)
    d.add_argument("--x2", type=int)
    d.add_argument("--y2", type=int)
    d.add_argument("--text")
    d.add_argument("--key")
    d.add_argument("--double", action="store_true")
    d.add_argument("--what")
    d.add_argument("--not-owner", action="store_true", help="模拟非主人会话（验闸用）")
    d.add_argument("--limit", type=int, default=20)

    v = sub.add_parser("voice", help="语音 / 唱")
    v.add_argument("action", choices=["say", "sing", "list"])
    v.add_argument("text", nargs="?", default="")
    v.add_argument("--public", action="store_true", help="模拟公开发布（会被拒）")
    v.add_argument("--limit", type=int, default=5)

    mc = sub.add_parser("mcp", help="MCP 服务（stdio）")
    mc.add_argument("--list", action="store_true", help="只列工具，不进服务循环")
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
        "memory": cmd_memory,
        "life": cmd_life,
        "person": cmd_person,
        "device": cmd_device,
        "voice": cmd_voice,
        "mcp": cmd_mcp,
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
