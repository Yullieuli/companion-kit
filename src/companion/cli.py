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
import os
import sys
from datetime import datetime
from pathlib import Path

from . import connect as connect_mod
from . import context as context_mod
from . import device, devdelegate
from . import (cost, distill, drive, events, fence, group, life, memory,
               mood as mood_mod, mood_rules, opinion, people,
               react as react_mod, resume as resume_mod, sticker, watchdog)
from .store import Home


def _dump(obj, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    else:
        print(obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, indent=2))


def cmd_webpage(home: Home, args) -> int:
    """把网页变成能进上下文的一段话：抽正文 → 摘要 → 挑页 → 缓存。"""
    from . import webpage

    if args.action == "extract":
        html = args.html
        if args.file:
            try:
                html = Path(args.file).read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                _dump({"ok": False, "error": str(e)}, args.json)
                return 1
        out = {"ok": True, "text": webpage.extract_text(html),
               "chars": len(webpage.extract_text(html))}
    elif args.action == "read":
        html = args.html
        if args.file:
            try:
                html = Path(args.file).read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                _dump({"ok": False, "error": str(e)}, args.json)
                return 1
        out = webpage.read_page(home, args.url, html, ttl=args.ttl,
                               max_chars=args.max_chars, focus=args.focus)
    elif args.action == "pick":
        pages = []
        if args.pages:
            try:
                pages = json.loads(args.pages)
            except json.JSONDecodeError as e:
                _dump({"ok": False, "error": f"--pages 不是合法 JSON：{e}"}, args.json)
                return 1
        out = {"ok": True, "pages": webpage.pick_pages(pages, args.query, top=3),
               "query": args.query}
    else:
        out = {"ok": True, **webpage.stats(home)}
    _dump(out if args.json else json.dumps(out, ensure_ascii=False, indent=2), args.json)
    return 0 if out.get("ok", True) else 1


def cmd_init(home: Home, args) -> int:
    created = home.init()
    # 顺手播种内置表情：空库起步的话她一张表情都发不出去。
    # 失败不影响 init —— 表情库是锦上添花，不是地基。
    seeded = {}
    try:
        from . import stickerlib

        seeded = stickerlib.seed_builtin(home)
    except Exception as e:
        seeded = {"error": f"{type(e).__name__}: {e}"}
    if args.json:
        _dump({"home": str(home.root), "created": created, "stickers": seeded}, True)
    else:
        print(f"store: {home.root}")
        print("新建：" + ("、".join(created) if created else "（已存在，没有覆盖任何文件）"))
        if isinstance(seeded, dict) and not seeded.get("error"):
            print(f"内置表情：{len(seeded.get('written') or [])} 张")
    return 0


def cmd_context(home: Home, args) -> int:
    ctx = context_mod.compose(
        home,
        now=datetime.fromisoformat(args.now) if getattr(args, "now", "") else None,
        budget=args.budget,
        query=args.query or "",
        include_soul=not args.no_soul,
    )
    session = str(getattr(args, "session", "") or "").strip()
    if not session:
        if args.json:
            _dump(ctx.as_dict(), True)
        else:
            print(ctx.text)
        return 0
    # 带 session：增量投影。状态没变 → 空输出，这一轮不用注入。
    proj = context_mod.project(home, session, ctx,
                               force=bool(getattr(args, "full", False)))
    if args.json:
        out = ctx.as_dict()
        out.update({"text": proj.text, "total_chars": len(proj.text),
                    "full": proj.full, "changed": proj.changed, "dropped": proj.dropped,
                    "rounds": proj.rounds, "reason": proj.reason, "session_id": session})
        _dump(out, True)
    else:
        if proj.text:
            print(proj.text)
    return 0


def cmd_tick(home: Home, args) -> int:
    result = drive.tick(home, host=args.host, now=datetime.now(), emit_silent=args.emit_silent)
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
    # 给了任务名就是问"这件事该用哪个模型、大概多少钱"
    if getattr(args, "task", ""):
        r = cost.route(args.task, home)
        if args.json:
            _dump(r, True)
        else:
            print(f"{r['task']} → {r['tier']} 档 {r['model']}（约 ¥{r['est_cny']:.4f}/次）")
            print(f"  为什么：{r['why']}")
            print(f"  规模：新输入 {r['tokens']['fresh_tokens']} · 缓存 {r['tokens']['cached_tokens']}"
                  f" · 输出 {r['tokens']['output_tokens']}")
        return 0
    if args.json:
        _dump({"today_cny": round(cost.today_total(home), 4), "turns": len(cost.load_rows(home)),
               "routes": cost.route_table(home)}, True)
    else:
        print(cost.summary(home))
    return 0


def cmd_turns(home: Home, args) -> int:
    """她最近说过什么。给"我刚才是不是已经说过这句"用，也可用来回看语气。"""
    rows = [r for r in home.read_jsonl("logs/turns.jsonl", limit=500) if r.get("role") == "assistant"]
    rows = rows[-args.limit:]
    if args.json:
        _dump(rows, True)
        return 0
    if not rows:
        print("（还没有记录）")
        return 0
    for r in rows:
        at = str(r.get("at") or "")[5:16].replace("T", " ")
        said = str(r.get("said") or "").replace("\n", " ")
        print(f"{at}  {said[:80]}")
    return 0


def cmd_say(home: Home, args) -> int:
    """她要开口：身份锁 → 频率 → 同话题，过了才记账。"""
    from . import guard

    out = guard.say(home, args.text, channel=args.channel or "", dry_run=bool(args.dry_run))
    _dump(out if args.json else (out.get("text") if out.get("ok") else f"没说：{out.get('why')}"),
          args.json)
    return 0 if out.get("ok") else 1


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
        extra: dict[str, str] = {}
        for kv in getattr(args, "extra", None) or []:
            k, _, v = str(kv).partition("=")
            if k.strip():
                extra[k.strip()] = v.strip()
        row = life.record(home, args.title, kind=args.kind, detail=args.detail or "",
                          tags=args.tags or [], shared=args.shared,
                          at=_parse_when(getattr(args, "at", None)), extra=extra or None)
        _dump(row if args.json else f"记下了：{row['at']} {row['title']}", args.json)
    elif args.action == "unshared":
        rows = life.unshared(home)
        _dump(rows if args.json else (life.render(home) or "（没有没讲过的）"), args.json)
    else:  # recent
        rows = life.recent(home, days=args.days, limit=args.limit)
        _dump(rows if args.json else (life.render(home, days=args.days, limit=args.limit) or "（这几天什么都没做）"), args.json)
    return 0


def _parse_when(raw: str | None) -> datetime | None:
    """接受 'YYYY-MM-DD HH:MM'、ISO、或空。迁移脚本传的是前者。"""
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(raw), fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        raise SystemExit(f"时间格式不认识：{raw}")


def cmd_person(home: Home, args) -> int:
    if args.action == "learn":
        # --fact 可重复，所以拿到的是列表；people.learn 一次收一条。
        facts = list(getattr(args, "fact", None) or [])
        if not facts:
            _dump({"ok": False, "error": "要学什么？用 --fact \"...\""}, args.json)
            return 1
        p = None
        for one in facts:
            p = people.learn(home, args.who or people.OWNER, one)
        assert p is not None
        _dump(p.as_dict() if args.json else f"记住了关于 {p.name or p.key} 的 {len(facts)} 件事", args.json)
    elif args.action == "pending":
        out = {"ok": True, "pending": people.pending_distill(home)}
        _dump(out if args.json else (json.dumps(out["pending"], ensure_ascii=False, indent=2)
                                     if out["pending"] else "（没有该压缩的人物卡）"), args.json)
    elif args.action == "distill":
        key = args.who or people.OWNER
        if getattr(args, "fact", None):
            p = people.apply_distill(home, key, args.fact)
            _dump(p.as_dict() if args.json else f"压缩完，留了 {len(p.facts)} 条", args.json)
        else:
            _dump(people.distill_prompt(home, key) if args.json
                  else json.dumps(people.distill_prompt(home, key), ensure_ascii=False, indent=2),
                  args.json)
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
def cmd_react(home: Home, args) -> int:
    payload: dict = {}
    if args.source_file:
        try:
            payload = json.loads(Path(args.source_file).read_text(encoding="utf-8"))
        except Exception as e:
            _dump({"ok": False, "error": f"读不了 {args.source_file}: {e}"}, True)
            return 1
    delta = {}
    for kv in args.mood or []:
        if "=" not in kv:
            continue
        k, v = kv.split("=", 1)
        try:
            delta[k.strip()] = float(v)
        except ValueError:
            continue
    out = react_mod.react(
        home,
        mood_delta=delta or payload.get("mood_delta"),
        why=args.why or payload.get("why", ""),
        person=args.person or payload.get("person", ""),
        facts=args.fact or payload.get("facts"),
        memory_page=payload.get("memory") or (
            {"title": args.memory_title, "summary": args.memory_summary,
             "body": args.memory_body, "tags": args.memory_tags or []}
            if args.memory_title and args.memory_summary else None),
        opinion_item=payload.get("opinion") or (
            {"claim": args.opinion, "tags": args.opinion_tags or []} if args.opinion else None),
        life_event=payload.get("life") or (
            {"title": args.life, "kind": args.life_kind, "detail": args.life_detail or ""} if args.life else None),
        said=args.said or payload.get("said", ""),
        context=payload.get("context") or None,
        usage=payload.get("usage") or (json.loads(args.usage) if args.usage else None),
        turn_id=args.turn_id or payload.get("turn_id", ""),
    )
    _dump(out if args.json else json.dumps(out, ensure_ascii=False, indent=2), args.json)
    return 0


def cmd_opinion(home: Home, args) -> int:
    if args.action == "add":
        o = opinion.add(home, args.claim, tags=args.tags or [], confidence=args.confidence)
        _dump(o.as_dict() if args.json else f"记下了（{o.stage}）：{o.claim}", args.json)
    elif args.action == "reinforce":
        _dump(opinion.reinforce(home, args.id).as_dict(), True) if args.json else print("强化了")
    elif args.action == "weaken":
        _dump(opinion.weaken(home, args.id).as_dict(), True) if args.json else print("削弱了")
    elif args.action == "stats":
        st = opinion.stats(home)
        _dump(st if args.json else json.dumps(st, ensure_ascii=False, indent=2), args.json)
    else:
        items = [o.as_dict() for o in opinion.theses(home)]
        if args.json:
            _dump(items, True)
        else:
            for o in opinion.theses(home):
                mark = "信念" if o.stage == "belief" else "猜测"
                print(f"[{mark} {o.confidence:.2f}] {o.claim}")
            if not items:
                print("（还没有任何判断）")
    return 0


def cmd_mood(home: Home, args) -> int:
    if args.action == "nudge":
        delta = {}
        for kv in args.delta or []:
            if "=" in kv:
                k, v = kv.split("=", 1)
                try:
                    delta[k.strip()] = float(v)
                except ValueError:
                    pass
        st = mood_mod.apply_delta(home.mood(), delta, why=args.why or "")
        home.save_json("mood/state.json", st)
    else:
        st = mood_mod.decay(home.mood())
    if args.json:
        _dump({"mood": st, "summary": mood_mod.summary(st),
               "today": mood_rules.snapshot(home)}, True)
        return 0
    print(mood_mod.summary(st))
    snap = mood_rules.snapshot(home).get("left_today") or {}
    if snap:
        print("今天还能推动：" + "、".join(f"{k} {v:+.1f}" for k, v in snap.items()))
    return 0


def cmd_fence(home: Home, args) -> int:
    if args.show_wrapped:
        _dump(fence.wrap(args.text, args.source), args.json)
        return 0
    hits = fence.scan(args.text)
    safe, _ = fence.sanitize(args.text)
    out = {"safe": not hits, "hits": hits, "text": safe}
    _dump(out if args.json else (f"命中 {len(hits)} 处" + ("" if not hits else "：" + "、".join(h["name"] for h in hits))
                                 + f"\n消毒后：{safe}"), args.json)
    return 0


def cmd_group(home: Home, args) -> int:
    if args.action == "note":
        if args.mode == "reply":
            group.note_reply(home)
        else:
            group.note_lurk(home)
        _dump(group.status(home), True) if args.json else print(json.dumps(group.status(home), ensure_ascii=False, indent=2))
        return 0
    if args.action == "status":
        st = group.status(home)
        _dump(st if args.json else json.dumps(st, ensure_ascii=False, indent=2), args.json)
        return 0
    out = group.judge(home, args.text, from_owner=args.from_owner, addressed_to_me=None if args.addressed is None else args.addressed)
    _dump(out if args.json else f"{out['mode']}（{out['reason']}）", args.json)
    return 0


def cmd_sticker(home: Home, args) -> int:
    if args.action == "add":
        out = sticker.add(home, args.path, mood=args.mood, tags=args.tags or [])
        _dump(out if args.json else f"收进表情库：{out['name']}（{out['mood']}）", args.json)
    elif args.action == "seed":
        # 播种内置表情。空库起步的话她一张都发不出去。
        from . import stickerlib

        out = stickerlib.seed_builtin(home, force=bool(getattr(args, "force", False)))
        _dump(out if args.json else
              f"内置表情：写了 {len(out['written'])} 张，跳过 {len(out['skipped'])} 张",
              args.json)
    elif args.action == "extract":
        # 自动提取：单张或整目录，自动认情绪
        from . import stickerlib

        if not args.path:
            _dump({"ok": False, "error": "要给一个文件或目录"}, args.json)
            return 1
        p = Path(args.path)
        if p.is_dir():
            out = stickerlib.extract_dir(home, p, limit=int(getattr(args, "limit", 50) or 50))
        else:
            out = stickerlib.extract(home, p, mood=args.mood, tags=args.tags or [])
        _dump(out if args.json else json.dumps(out, ensure_ascii=False, indent=2), args.json)
    elif args.action == "pick":
        hit = sticker.pick(home, args.mood, args.tags or [])
        if not hit:
            _dump({"ok": False, "error": "没有对得上的"} if args.json else "没有对得上的", args.json)
            return 1
        _dump(hit if args.json else f"{hit['name']}（{hit['mood']}）→ {hit['path']}", args.json)
    elif args.action == "send":
        ok = sticker.mark_sent(home, args.name)
        _dump({"ok": ok} if args.json else ("记下了" if ok else "没有这个表情"), args.json)
        return 0 if ok else 1
    else:
        st = sticker.stats(home)
        _dump(st if args.json else json.dumps(st, ensure_ascii=False, indent=2), args.json)
    return 0


def cmd_device(home: Home, args) -> int:
    from . import device

    if args.action == "look":
        out = device.look(home, args.target, window=args.window or "")
    elif args.action == "observe":
        out = device.observe(home, args.target, window=args.window or "",
                             ocr=not getattr(args, "no_ocr", False))
    elif args.action == "act":
        payload = {k: getattr(args, k) for k in ("x", "y", "x2", "y2", "text", "key", "window", "double")
                   if getattr(args, k, None) is not None}
        out = device.act(home, args.target, args.act, payload, is_owner=not args.not_owner,
                         ask_id=args.ask_id, before=bool(args.before))
    elif args.action == "open":
        out = device.open_thing(home, args.target, args.what, is_owner=not args.not_owner,
                                ask_id=args.ask_id)
    elif args.action == "list":
        out = device.windows_list(home, args.limit)
    elif args.action == "focus":
        out = device.focus_window_by(home, args.what or "", is_owner=not args.not_owner,
                                     ask_id=args.ask_id)
    elif args.action == "replay":
        out = device.replay(home, args.last, execute=bool(args.yes),
                            with_text=bool(args.with_text), is_owner=not args.not_owner)
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


def cmd_bili(home: Home, args) -> int:
    """刷 B 站。注意：`tick` 会真的发请求、写生活流水 —— 不是 dry-run。"""
    from . import bili

    frames = None
    if getattr(args, "frames", False):
        frames = True
    if getattr(args, "no_frames", False):
        frames = False

    if args.action == "status":
        out = {**bili.status(home), "config": bili.config(home)}
    elif args.action == "config":
        out = bili.config(home)
    elif args.action == "me":
        from .bilinet import Net

        try:
            net = Net(home.root)
            out = {**net.me(), "cred": net.cred().get("path", ""),
                   "traffic": net.traffic(), "paused_sec": net.paused_for()}
        except Exception as e:
            out = {"ok": False, "why": f"{type(e).__name__}: {e}"}
    elif args.action == "tick":
        out = bili.tick(home, frames=frames, force=args.force)
    elif args.action == "debrief":
        from .bilinet import Net

        out = bili.debrief(home, args.bvid, net=Net(home.root), frames=frames)
    elif args.action == "commit":
        out = bili.commit(home, args.bvid, summary=args.summary, rating=args.rating)
    elif args.action == "share":
        from .bilinet import Net

        net = Net(home.root) if (args.via == "dm" or args.to_mid) else None
        out = bili.share(home, args.bvid, args.summary, score_v=args.score, reason=args.reason,
                         to_mid=args.to_mid or None, via=args.via, net=net)
    else:  # pragma: no cover - argparse 已经限定了
        out = {"ok": False, "why": f"不认识的动作 {args.action}"}

    _dump(out if args.json else json.dumps(out, ensure_ascii=False, indent=2), args.json)
    return 0 if (not isinstance(out, dict) or out.get("ok", True) is not False) else 1


def cmd_ask(home: Home, args) -> int:
    """她要动手之前先问的那一声：看列表 / 答应 / 拒绝 / 按主人原话回填。"""
    from . import ask

    if args.action == "list":
        out = {"ok": True, "pending": ask.pending(home), "recent": ask.list_all(home, limit=10)}
    elif args.action == "allow":
        out = ask.decide(home, args.id, True)
    elif args.action == "deny":
        out = ask.decide(home, args.id, False)
    elif args.action == "reply":
        # 主人在对话里说了话，让它落到待办上。
        # by="owner"：这是主人在场说的话，能开设备。
        out = ask.decide_by_reply(home, args.text)
    else:  # pragma: no cover
        out = {"ok": False, "why": f"不认识的动作 {args.action}"}
    _dump(out if args.json else json.dumps(out, ensure_ascii=False, indent=2), args.json)
    return 0 if out.get("ok") else 1


def cmd_serve(home: Home, args) -> int:
    from . import serve as serve_mod

    return serve_mod.serve(home, host=args.host, port=args.port)


# ── 断点续传 / 记忆提炼 / 心跳 ───────────────────────────────
def cmd_resume(home: Home, args) -> int:
    """断点续传：找出说了但没写回的轮次。"""
    if args.action == "settle":
        ok = resume_mod.settle(home, args.turn_id or "")
        _dump({"ok": ok, "turn_id": args.turn_id}, args.json)
        return 0 if ok else 1
    rows = resume_mod.pending(home)
    if args.json:
        _dump({"ok": True, "pending": len(rows), "turns": rows}, True)
    else:
        if not rows:
            print("（没有待写回的轮次。）")
        else:
            for r in rows:
                print(f"{r.get('turn_id')}  {r.get('at')}  {str(r.get('text'))[:40]}")
    return 0


def cmd_distill(home: Home, args) -> int:
    """记忆自动提炼：挖模式 → 给人看 → 认可后落盘。"""
    if args.action == "candidates":
        cands = distill.candidates(home)
        if args.json:
            _dump({"ok": True, "candidates": cands}, True)
        else:
            if not cands:
                print("（没有发现值得记住的模式。）")
            for c in cands:
                print(f"- {c['summary']}（{c['count']} 次）")
        return 0
    if args.action == "accept":
        if not args.summary:
            _dump({"ok": False, "error": "要 summary（一句话说清这条记忆）"}, args.json)
            return 1
        try:
            page = distill.accept(home, args.summary, title=args.title or "",
                                  body=args.body or "", tags=args.tags or None)
        except Exception as e:
            _dump({"ok": False, "error": f"{type(e).__name__}: {e}"}, args.json)
            return 1
        _dump(page if args.json else f"记住了：{page['summary']}", args.json)
        return 0
    text = distill.review(home)
    _dump(text if args.json else text, args.json)
    return 0


def cmd_heartbeat(home: Home, args) -> int:
    """心跳：宿主每次醒来写一次。看门狗据此判断她是不是还活着。"""
    row = watchdog.beat(home, host=args.host or "cli")
    # 同时往多宿主表里追加一条，方便看门狗分清"谁卡住了"
    home.append_jsonl("logs/heartbeats.jsonl", row)
    if args.json:
        _dump({"ok": True, **row}, True)
    else:
        print(f"心跳已记录：{row['host']} pid={row['pid']} at={row['at']}")
    return 0


def _host_hook_report() -> list[dict[str, Any]]:
    """宿主 shell 钩子有没有真的被批准。

    hermes 这类宿主对 shell 钩子有 consent：config.yaml 里写了不算，
    每个 (事件, 命令) 对还得在 allowlist 里被批准过。没批准就**静默不跑** ——
    不报错、不进日志、钩子就是不执行。这种坑必须能在体检里查出来。
    """
    from . import hosthooks

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for env_name, default in (("HERMES_HOME", "~/.hermes"),):
        home = Path(os.environ.get(env_name, default)).expanduser()
        key = str(home)
        if key in seen or not home.exists():
            continue
        seen.add(key)
        try:
            r = hosthooks.check(home)
        except Exception as e:                       # 体检不该因为宿主目录怪就崩
            out.append({"host": home.name, "ok": False, "detail": f"{type(e).__name__}: {e}",
                        "missing": []})
            continue
        out.append({"host": home.name, "ok": r["ok"], "total": r["total"],
                    "missing": [{"event": e, "command": c} for e, c in r["missing"]],
                    "detail": "" if r["ok"] else "配了但没批准 —— 这些钩子不会执行",
                    "allowlist": r["allowlist"]})
    return out


def cmd_connect(home: Home, args) -> int:
    """把 companion 接到本机已装的宿主上。

    这是"一行安装"的另一半：装完 core 之后，用户不該再手抄四份配置。
    """
    as_json = bool(getattr(args, "as_json", False) or args.json)

    if args.rollback:
        done = connect_mod.rollback(home, args.hosts or None)
        if as_json:
            print(json.dumps({"ok": True, "rolled_back": done}, ensure_ascii=False, indent=2))
        else:
            if not done:
                print("没有可回滚的记录（先跑一次 companion connect）")
            for line in done:
                print("  " + line)
        return 0

    if not home.root.is_dir():
        msg = f"store 还没初始化：{home.root}。先跑 companion init"
        if as_json:
            print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
        else:
            print(msg, file=sys.stderr)
        return 2

    opts = {
        "hermes_home": args.hermes_home,
        "dsh_home": args.dsh_home,
        "openclaw_home": args.openclaw_home,
        "codex_home": args.codex_home,
        "dsh_spec": args.dsh_spec,
    }
    try:
        results = connect_mod.run(
            home,
            args.hosts or None,
            dry_run=args.dry_run,
            status_only=args.status,
            force=args.force,
            opts=opts,
        )
    except connect_mod.ConnectError as e:
        if as_json:
            print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        else:
            print(f"出错：{e}", file=sys.stderr)
        return 1

    if as_json:
        print(json.dumps(
            {
                "ok": not any(r.status == "failed" for r in results),
                "dry_run": bool(args.dry_run),
                "results": [
                    {"host": r.host, "status": r.status, "detail": r.detail,
                     "files": r.files, "warnings": r.warnings}
                    for r in results
                ],
            },
            ensure_ascii=False,
            indent=2,
        ))
        return 1 if any(r.status == "failed" for r in results) else 0

    marks = {
        "connected": "✓",
        "latest": "=",
        "detected": "·",
        "skipped": "·",
        "planned": "?",
        "failed": "✗",
    }
    print(f"store: {home.root}")
    for r in results:
        mark = marks.get(r.status, "?")
        print(f"  {mark} {r.host:<9} {r.detail}")
        for w in r.warnings:
            print(f"    {' ' * 9} ⚠ {w}")
    if args.dry_run:
        print("\n（dry-run：没有改任何文件）")
    failed = [r for r in results if r.status == "failed"]
    if failed:
        print("\n有宿主没接上。修掉上面的问题后单独重试，例如：")
        print(f"  companion connect {failed[0].host}")
        return 1
    if not any(r.status in ("connected", "latest", "planned", "detected") for r in results):
        print("\n没探测到可接的宿主。装好宿主后重跑：companion connect")
        return 0
    if any(r.host == "hermes" and r.status == "connected" for r in results):
        print("\nhermes 接完要重启网关才生效：hermes gateway restart")
    return 0


def cmd_doctor(home: Home, args) -> int:
    """体检：版本、路径、模块能不能导入、外部工具在不在、以及给杀软的白名单建议。"""
    import importlib
    import platform
    import shutil as sh

    from . import __version__

    mods = [
        ("device", "手（闸 + 分发）"), ("devwin", "桌面截图/输入（ctypes）"),
        ("devcdp", "浏览器 CDP（socket）"), ("imgpng", "PNG 编码"),
        ("voice", "语音"), ("mcp", "MCP 服务"), ("ask", "动手前的请示"),
        ("bilinet", "B 站传输（WBI/节流/熔断）"), ("bili", "刷 B 站（挑/读/分享）"),
    ]
    # 导入成功 ≠ 这台机器上能用。桌面那块是 Win32 API，别的系统导得进来、
    # 一调用就抛错；体检如果只打 ✓，等于告诉用户"好的"，然后在第一次用时炸。
    import sys as _sys

    platform_notes: dict[str, str] = {}
    if _sys.platform != "win32":
        platform_notes["devwin"] = "仅 Windows；本机调用会报 DesktopError"
        platform_notes["device"] = "桌面截图/输入仅 Windows；adb 与浏览器 CDP 可用"

    checks: list[dict[str, Any]] = []
    for name, desc in mods:
        try:
            importlib.import_module(f"companion.{name}")
            checks.append({"item": name, "desc": desc, "ok": True, "detail": "",
                           "note": platform_notes.get(name, "")})
        except Exception as e:
            checks.append({"item": name, "desc": desc, "ok": False,
                           "detail": f"{type(e).__name__}: {e}",
                           "note": platform_notes.get(name, "")})

    tools = {
        "adb": sh.which("adb") or sh.which("adb.exe") or "",
        "ffmpeg": sh.which("ffmpeg") or "",
        "edge-tts": sh.which("edge-tts") or "",
    }
    pyc = list((Path(__file__).parent / "__pycache__").glob("*.pyc")) if (Path(__file__).parent / "__pycache__").exists() else []

    payload = {
        "ok": all(c["ok"] for c in checks),
        "version": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "home": str(home.root),
        "package_dir": str(Path(__file__).parent),
        "modules": checks,
        "limited": [c["item"] for c in checks if c.get("note")],
        "external": tools,
        "pyc_count": len(pyc),
        "host_hooks": _host_hook_report(),
        "av_hint": {
            "why": "device 相关模块会调用 ctypes / 裸 socket / subprocess，杀软容易按「加载器」特征误报",
            "what_was_flagged": "companion/device.pyc 曾被判为 Trojan/Python.ShellLoader（误报）",
            "fix": [
                f"把这个目录加进杀软信任区：{Path(__file__).parent}",
                "重装时加 --no-compile，不生成 .pyc：pip install --no-compile <whl>",
                "或临时设 PYTHONDONTWRITEBYTECODE=1 再运行",
            ],
        },
    }
    if args.json:
        _dump(payload, True)
        return 0 if payload["ok"] else 1

    print(f"companion {payload['version']} ｜ Python {payload['python']}")
    print(f"store   {payload['home']}")
    print(f"包目录  {payload['package_dir']}")
    print()
    for c in checks:
        mark = "✓" if c["ok"] else "✗"
        tail = "" if c["ok"] else f"  → {c['detail']}"
        if c.get("note"):
            tail += f"\n               · {c['note']}"
        print(f"  {mark} {c['item']:10} {c['desc']}{tail}")
    print()
    for k, v in tools.items():
        print(f"  {'✓' if v else '·'} {k:10} {v or '（没装，相关功能会自己降级）'}")
    print()
    for h in payload["host_hooks"]:
        mark = "✓" if h["ok"] else "✗"
        print(f"  {mark} 钩子批准   {h['host']}：{h['total']} 个钩子")
        if not h["ok"]:
            print(f"               · {h['detail']}")
            for m in h["missing"]:
                print(f"                 {m['event']} → {m['command']}")
    print()
    print(f"  __pycache__ 里 {payload['pyc_count']} 个 .pyc")
    if payload["pyc_count"]:
        print("  如果杀软反复报毒，把上面那个包目录加进信任区，并用 --no-compile 重装。")
    return 0 if payload["ok"] else 1


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
    c.add_argument("--no-soul", action="store_true", help="不注入人格块（宿主自己有人格时用）")
    c.add_argument("--now", help="钉住时间（回放/契约测试用，ISO 格式）")
    c.add_argument("--session", help="会话标识：给了就做增量投影（只返回变化的部分）")
    c.add_argument("--full", action="store_true", help="配合 --session：强制全量")

    t = sub.add_parser("tick", help="问一次该干什么")
    t.add_argument("--emit-silent", action="store_true")
    t.add_argument("--host", default="cli", help="谁在掌管时钟（多宿主共用一个 store 时用）")

    e = sub.add_parser("events", help="读事件")
    e.add_argument("--since", type=int, default=None)
    e.add_argument("--wait", type=float, default=0)

    sub.add_parser("status", help="只读状态（不推进决策）")
    co = sub.add_parser("cost", help="今日花费 / 某任务的模型档位")
    co.add_argument("task", nargs="?", default="", help="任务名（如 bili_pick / bili_debrief / chat）")

    tn = sub.add_parser("turns", help="她最近说过什么")
    tn.add_argument("--limit", type=int, default=10)

    s = sub.add_parser("say", help="记一句她想说的话")
    s.add_argument("--text", required=True)
    s.add_argument("--channel", default="")
    s.add_argument("--dry-run", action="store_true", help="只判能不能说，不记账")

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
    lf.add_argument("--at", help="记录时间（迁移/回填用，'YYYY-MM-DD HH:MM' 或 ISO）")
    lf.add_argument("--extra", action="append", default=[], metavar="K=V",
                    help="附加字段，可重复：--extra progress=08:30/25:42 --extra bvid=BV1xx")

    pe = sub.add_parser("person", help="人物卡")
    pe.add_argument("action", choices=["get", "learn", "list", "pending", "distill"],
                    nargs="?", default="get")
    pe.add_argument("who", nargs="?", default=people.OWNER)
    pe.add_argument("--fact", action="append", default=[], help="可重复（distill 时是压缩结果）")

    rc = sub.add_parser("react", help="把这一轮的感受写回去（心情/人物卡/记忆/观点/生活）")
    rc.add_argument("--from", dest="source_file", help="从一个 JSON 文件读整包")
    rc.add_argument("--why", default="")
    rc.add_argument("--person", default="")
    rc.add_argument("--mood", action="append", metavar="轴=值", help="可重复，如 closeness=3")
    rc.add_argument("--fact", action="append", help="关于他的新事实，可重复")
    rc.add_argument("--opinion", default="")
    rc.add_argument("--opinion-tags", nargs="*")
    rc.add_argument("--memory-title", default="")
    rc.add_argument("--memory-summary", default="")
    rc.add_argument("--memory-body", default="")
    rc.add_argument("--memory-tags", nargs="*")
    rc.add_argument("--life", default="")
    rc.add_argument("--life-kind", default="watch", choices=list(life.KIND_VERB))
    rc.add_argument("--life-detail", default="")
    rc.add_argument("--said", default="")
    rc.add_argument("--turn-id", dest="turn_id", default="",
                    help="本轮唯一标识：同一个 turn 重试不会重复改变状态或费用")
    rc.add_argument("--usage", default="",
                    help="本轮真实用量 JSON：{model, fresh_tokens, cached_tokens, output_tokens}")

    op = sub.add_parser("opinion", help="她的判断")
    op.add_argument("action", choices=["list", "add", "reinforce", "weaken", "stats"], nargs="?", default="list")
    op.add_argument("claim", nargs="?", default="")
    op.add_argument("--id", default="")
    op.add_argument("--tags", nargs="*")
    op.add_argument("--confidence", type=float, default=0.45)

    md = sub.add_parser("mood", help="心情")
    md.add_argument("action", choices=["get", "nudge"], nargs="?", default="get")
    md.add_argument("--delta", action="append", metavar="轴=值")
    md.add_argument("--why", default="")

    fc = sub.add_parser("fence", help="给外部文本打注围栏 / 消毒")
    fc.add_argument("text")
    fc.add_argument("--source", default="外部")
    fc.add_argument("--show-wrapped", action="store_true")

    g = sub.add_parser("group", help="群聊该不该接话")
    g.add_argument("action", choices=["judge", "note", "status"], nargs="?", default="judge")
    g.add_argument("text", nargs="?", default="")
    g.add_argument("--from-owner", action="store_true")
    g.add_argument("--addressed", dest="addressed", action="store_true", default=None)
    g.add_argument("--mode", choices=["reply", "lurk"], default="reply")

    sk = sub.add_parser("sticker", help="表情库")
    sk.add_argument("action", choices=["stats", "add", "pick", "send", "seed", "extract"],
                    nargs="?", default="stats")
    sk.add_argument("path", nargs="?", default="")
    sk.add_argument("--mood", default="", choices=[""] + sticker.MOODS)
    sk.add_argument("--tags", nargs="*")
    sk.add_argument("--name", default="")
    sk.add_argument("--force", action="store_true", help="seed：覆盖已存在的内置表情")
    sk.add_argument("--limit", type=int, default=50, help="extract：整目录时最多取几张")

    d = sub.add_parser("device", help="手：看屏幕 / 动作 / 打开")
    d.add_argument("action", choices=["look", "observe", "act", "open", "list", "focus", "log", "replay"])
    d.add_argument("--target", default="desktop", choices=["phone", "desktop", "browser", "game"])
    d.add_argument("--act", default="tap", choices=["tap", "click", "swipe", "type", "key", "focus"])
    d.add_argument("--x", type=int)
    d.add_argument("--y", type=int)
    d.add_argument("--x2", type=int)
    d.add_argument("--y2", type=int)
    d.add_argument("--text")
    d.add_argument("--key")
    d.add_argument("--window", help="窗口标题（focus 用）")
    d.add_argument("--ask-id", dest="ask_id", default="", help="主人答应后拿到的那张许可")
    d.add_argument("--no-ocr", action="store_true", help="observe 时不做 OCR")
    d.add_argument("--before", action="store_true", help="动作前也截一张（拿来对比）")
    d.add_argument("--last", type=int, default=5, help="replay 回放最近几个动作")
    d.add_argument("--yes", action="store_true", help="replay 时真跑（默认只看）")
    d.add_argument("--with-text", dest="with_text", action="store_true",
                   help="replay 时连输入一起重放（默认跳过含文本的动作）")
    d.add_argument("--double", action="store_true")
    d.add_argument("--what")
    d.add_argument("--not-owner", action="store_true", help="模拟非主人会话（验闸用）")
    d.add_argument("--limit", type=int, default=20)

    v = sub.add_parser("voice", help="语音 / 唱")
    v.add_argument("action", choices=["say", "sing", "list"])
    v.add_argument("text", nargs="?", default="")
    v.add_argument("--public", action="store_true", help="模拟公开发布（会被拒）")
    v.add_argument("--limit", type=int, default=5)

    bl = sub.add_parser("bili", help="刷 B 站：挑片 / 深读 / 分享")
    bl.add_argument("action", choices=["status", "config", "me", "tick", "debrief", "commit", "share"])
    bl.add_argument("bvid", nargs="?", default="")
    bl.add_argument("--summary", default="", help="她的感受（commit）/ 分享的话（share）")
    bl.add_argument("--rating", type=int, help="0-10，她自己给的分")
    bl.add_argument("--score", type=int, default=0, help="分享闸要的评分")
    bl.add_argument("--reason", default="", help="分享闸要的理由")
    bl.add_argument("--to-mid", dest="to_mid", type=int, default=0, help="主人的 B 站 mid（私信）")
    bl.add_argument("--via", default="", help="qq / dm；空 = 用配置")
    bl.add_argument("--frames", action="store_true", help="强制抽九宫格")
    bl.add_argument("--no-frames", action="store_true", help="不抽帧")
    bl.add_argument("--force", action="store_true", help="绕过每日额度与间隔")
    bl.add_argument("--json", action="store_true")

    a = sub.add_parser("ask", help="她动手前的请示：列表/答应/拒绝/回填")
    a.add_argument("action", choices=["list", "allow", "deny", "reply"])
    a.add_argument("--id", default="", help="请求 id（allow/deny 要）")
    a.add_argument("--text", default="", help="主人原话（reply 要）：'可以' / '别' / '算了'")
    a.add_argument("--json", action="store_true")

    sv = sub.add_parser("serve", help="把事件流开成 HTTP（只读，默认只绑本机）")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=7788)

    sub.add_parser("doctor", help="体检：模块/外部工具/杀毒误报位置")

    cn = sub.add_parser("connect", help="把 companion 接到本机已装的宿主（hermes/dsh/openclaw/codex）")
    cn.add_argument("hosts", nargs="*", default=[], help="要接的宿主；不写=全部已装")
    cn.add_argument("--dry-run", action="store_true", help="只打印计划，不改文件")
    cn.add_argument("--status", action="store_true", help="只探测，不写")
    cn.add_argument("--rollback", action="store_true", help="还原到最近一次备份")
    cn.add_argument("--force", action="store_true", help="已存在的配置也覆盖（谨慎）")
    cn.add_argument("--json", action="store_true", dest="as_json", help="机器可读输出")
    cn.add_argument("--hermes-home", default="", help="hermes 目录（默认 $HERMES_HOME 或 ~/.hermes）")
    cn.add_argument("--dsh-home", default="", help="dsh 目录（默认 $DSH_HOME 或 ~/.dsh）")
    cn.add_argument("--openclaw-home", default="", help="openclaw 目录（默认 ~/.openclaw）")
    cn.add_argument("--codex-home", default="", help="codex 目录（默认 $CODEX_HOME 或 ~/.codex）")
    cn.add_argument("--dsh-spec", default="", help="dsh 插件来源（默认 git+https://github.com/Yullieuli/companion-kit.git）")

    rs = sub.add_parser("resume", help="断点续传：找出说了但没写回的轮次")
    rs.add_argument("action", choices=["pending", "settle"], nargs="?", default="pending")
    rs.add_argument("--turn-id", default="")

    ds = sub.add_parser("distill", help="记忆自动提炼：从流水里挖值得记住的模式")
    ds.add_argument("action", choices=["review", "candidates", "accept"], nargs="?", default="review")
    ds.add_argument("--summary", default="")
    ds.add_argument("--title", default="")
    ds.add_argument("--body", default="")
    ds.add_argument("--tags", nargs="*")

    hb = sub.add_parser("heartbeat", help="心跳：宿主每次醒来写一次（看门狗用）")
    hb.add_argument("--host", default="cli")

    mc = sub.add_parser("mcp", help="MCP 服务（stdio）")
    mc.add_argument("--list", action="store_true", help="只列工具，不进服务循环")

    wbp = sub.add_parser("webpage", help="把网页变成能进上下文的一段话（摘要/挑页/缓存）")
    wbp.add_argument("action", choices=["read", "pick", "stats", "extract"], nargs="?", default="stats")
    wbp.add_argument("--url", default="", help="read：页面地址")
    wbp.add_argument("--html", default="", help="read：HTML（不给就用缓存）")
    wbp.add_argument("--file", default="", help="read：从文件读 HTML")
    wbp.add_argument("--query", default="", help="pick：按什么挑")
    wbp.add_argument("--pages", default="", help="pick：JSON 数组（title/url/snippet）")
    wbp.add_argument("--focus", default="", help="read：摘要聚焦什么")
    wbp.add_argument("--max-chars", dest="max_chars", type=int, default=1200)
    wbp.add_argument("--ttl", type=int, default=600, help="缓存多久算新鲜（秒）")
    return p


def main(argv: list[str] | None = None) -> int:
    raw = list(argv) if argv is not None else sys.argv[1:]
    args = build_parser().parse_args(raw)
    home = Home.at(args.home)
    # WSL 里没有 Win32：把 device 整个交给 Windows 侧同一份代码跑
    if args.cmd == "device" and devdelegate.wanted(home):
        fwd, skip = [], False
        for tok in raw:
            if skip:
                skip = False
                continue
            if tok == "--home":
                skip = True
                continue
            if tok.startswith("--home="):
                continue
            fwd.append(tok)
        if args.action == "observe":
            # 截图要 Windows 做，认字要 WSL 做（tesseract 装在这边），所以拆成两步
            remote = ["look" if t == "observe" else t for t in fwd if t != "--no-ocr"]
            rc, out, err = devdelegate.run(remote, home)
            if rc != 0 or not out.strip().startswith("{"):
                sys.stdout.write(out)
                if err.strip():
                    sys.stderr.write(err)
                return rc
            data = json.loads(out)
            if data.get("ok") and not getattr(args, "no_ocr", False):
                data["ocr"] = device.ocr_image(home, data.get("shot") or "")
                data["describe_hint"] = device.DESCRIBE_HINT
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return rc
        return devdelegate.call(fwd, home)
    table = {
        "init": cmd_init,
        "context": cmd_context,
        "tick": cmd_tick,
        "events": cmd_events,
        "cost": cmd_cost,
        "status": cmd_status,
        "turns": cmd_turns,
        "say": cmd_say,
        "memory": cmd_memory,
        "life": cmd_life,
        "person": cmd_person,
        "device": cmd_device,
        "group": cmd_group,
        "react": cmd_react,
        "opinion": cmd_opinion,
        "mood": cmd_mood,
        "fence": cmd_fence,
        "sticker": cmd_sticker,
        "voice": cmd_voice,
        "bili": cmd_bili,
        "ask": cmd_ask,
        "serve": cmd_serve,
        "mcp": cmd_mcp,
        "doctor": cmd_doctor,
        "connect": cmd_connect,
        "resume": cmd_resume,
        "distill": cmd_distill,
        "heartbeat": cmd_heartbeat,
        "webpage": cmd_webpage,
    }
    try:
        if args.cmd in {
            "init", "tick", "say", "memory", "life", "person", "device", "group",
            "react", "opinion", "mood", "sticker", "voice", "bili", "ask",
            "resume", "distill", "heartbeat", "webpage",
        }:
            with home.lock("store"):
                return table[args.cmd](home, args)
        return table[args.cmd](home, args)
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # 给宿主一个稳定的错误形状
        if args.json:
            print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
        else:
            print(f"出错：{type(e).__name__}: {e}", file=sys.stderr)
        return 1
