"""mcp：把 core 暴露成 MCP 服务，给 codex / openclaw / 任何支持 MCP 的宿主用。

为什么是 MCP 而不是各家 SDK：这是唯一一个"写一次、所有宿主都能挂"的接口。
DSH 有自己的 cordis、hermes 有自己的 hook，但两边都能当 MCP 客户端。

协议：JSON-RPC 2.0，stdio，**按行分隔**（不是 Content-Length 分帧）。
规矩：stdout 只输出协议消息，任何调试信息写 stderr —— 否则客户端会解析失败。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from typing import Any, Callable

from . import context as context_mod
from . import bili, cost, device, drive, events, fence, group, life, memory, mood as mood_mod, opinion, people, react as react_mod, sticker, voice
from .store import Home

PROTOCOL = "2024-11-05"
SERVER = {"name": "companion-kit", "version": "0.1.0"}

Tool = dict[str, Any]


def _schema(name: str, desc: str, props: dict[str, Any] | None = None, required: list[str] | None = None) -> Tool:
    return {
        "name": name,
        "description": desc,
        "inputSchema": {
            "type": "object",
            "properties": props or {},
            "required": required or [],
        },
    }


TOOLS: list[Tool] = [
    _schema("companion_context", "拿到这一轮该注入的上下文（人格/状态/记忆/观点/人物卡/生活）。宿主每轮开头调一次。",
            {"budget": {"type": "integer", "description": "字符预算，默认 2000", "default": 2000},
             "query": {"type": "string", "description": "按这句话挑相关记忆与观点；不给就取最近的"}}),
    _schema("companion_react", "把这一轮的感受写回去：心情变化、关于他的事实、记忆、观点、生活事件。一轮结束时调一次。",
            {"mood_delta": {"type": "object", "description": "{satisfaction, closeness, energy}，单轴最多 ±12"},
             "why": {"type": "string", "description": "一句话说明为什么，会进 history"},
             "person": {"type": "string", "description": "owner 或名字"},
             "facts": {"type": "array", "items": {"type": "string"}, "description": "关于这个人的新事实"},
             "memory": {"type": "object", "description": "{title, summary, body, tags}；summary 必填"},
             "opinion": {"type": "object", "description": "{claim, tags, confidence}；她自己的判断"},
             "life": {"type": "object", "description": "{title, kind, detail, tags, shared}；她生活里发生的事"},
             "said": {"type": "string", "description": "这一轮她说出口的话，用于回看与去重"}}),
    _schema("companion_opinion", "她的判断清单：看、加一条、被验证了、被反驳了。",
            {"action": {"type": "string", "enum": ["list", "add", "reinforce", "weaken", "stats"], "default": "list"},
             "claim": {"type": "string"}, "id": {"type": "string"},
             "tags": {"type": "array", "items": {"type": "string"}},
             "confidence": {"type": "number", "description": "0–1，默认 0.45"}}),
    _schema("companion_mood", "读心情，或推一下（跨频道共享一份）。",
            {"action": {"type": "string", "enum": ["get", "nudge"], "default": "get"},
             "delta": {"type": "object", "description": "{satisfaction, closeness, energy}"},
             "why": {"type": "string"}}),
    _schema("companion_fence", "给外部文本（群消息/弹幕/评论/网页）打注围栏：返回消毒后的文本与命中项。进 context 前必须过。",
            {"text": {"type": "string"}, "source": {"type": "string", "default": "外部"}}, ["text"]),
    _schema("companion_status", "只看状态：心情、额度、是否在作息窗口、上次决策。不推进决策、不扣额度。"),
    _schema("companion_tick", "问一次「现在想干什么」。零 token：纯本地计算。返回 action/why/cost。",
            {"emit_silent": {"type": "boolean", "description": "沉默也写一条事件（调试用）", "default": False}}),
    _schema("companion_events", "读事件流。宿主用它决定要不要起一轮会话。",
            {"since": {"type": "integer", "description": "游标（上次返回的 cursor）"},
             "wait": {"type": "number", "description": "长轮询秒数，0 表示立刻返回", "default": 0}}),
    _schema("companion_say", "记下她想说的一句话：扣额度、写日志，返回文本。真正发送由宿主的通道完成。",
            {"text": {"type": "string"}, "channel": {"type": "string", "description": "宿主自己的通道名"}},
            ["text"]),
    _schema("companion_memory_write", "写一页记忆（一个主题一页）。summary 必填：它是索引里唯一被读到的字。",
            {"title": {"type": "string"}, "body": {"type": "string"},
             "tags": {"type": "array", "items": {"type": "string"}},
             "summary": {"type": "string", "description": "一句话，会进索引"}},
            ["title", "body", "summary"]),
    _schema("companion_memory_search", "按关键词找记忆页（标签/摘要/正文）。",
            {"query": {"type": "string"}, "limit": {"type": "integer", "default": 5}}, ["query"]),
    _schema("companion_life_record", "记一件她生活里发生的事（看了什么/玩了什么/想了什么）。会进上下文，带相对时间。",
            {"title": {"type": "string"}, "kind": {"type": "string", "enum": list(life.KIND_VERB)},
             "detail": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}},
             "shared": {"type": "boolean", "description": "是否已经跟他讲过了", "default": False}},
            ["title"]),
    _schema("companion_life_mark_shared", "把生活事件标成「已经跟他讲过了」。讲完必须调，否则她会重复讲同一件事。",
            {"ids": {"type": "array", "items": {"type": "string"}}, "all": {"type": "boolean",
             "description": "true = 把最近两天没讲过的全标掉", "default": False}},
            ["ids"]),
    _schema("companion_group_judge", "群聊里该不该接话。三档：reply 直接接 / lurk 看到了但不说 / ignore 假装没看见。",
            {"text": {"type": "string"}, "from_owner": {"type": "boolean", "default": False},
             "addressed_to_me": {"type": "boolean", "description": "是否 @ 了她或叫了名字"}},
            ["text"]),
    _schema("companion_group_note", "记下这次接没接话（决定冷却与潜水计数）。接了必须 note。",
            {"mode": {"type": "string", "enum": ["reply", "lurk"]}}, ["mode"]),
    _schema("companion_sticker_pick", "按情绪挑一张表情。没有合适的不凑合，返回空。",
            {"mood": {"type": "string", "enum": sticker.MOODS},
             "tags": {"type": "array", "items": {"type": "string"}}}),
    _schema("companion_sticker_add", "把一张图收进表情库。",
            {"path": {"type": "string"}, "mood": {"type": "string", "enum": sticker.MOODS},
             "tags": {"type": "array", "items": {"type": "string"}}}, ["path"]),
    _schema("companion_sticker_stats", "表情库统计（总数 / 各情绪张数）。"),
    _schema("companion_person", "读或写人物卡。给了 fact 就是学一件关于他的事。",
            {"who": {"type": "string", "description": "owner 或名字"},
             "fact": {"type": "string"}, "relation": {"type": "string"},
             "closeness": {"type": "integer"}, "notes": {"type": "string"}}),
    _schema("companion_cost", "今天花了多少 / 预算还剩多少。"),
    _schema("companion_voice", "合成一段语音（本地 TTS），返回文件路径。sing=true 是降级版「唱」。",
            {"text": {"type": "string"}, "sing": {"type": "boolean", "default": False}}, ["text"]),
    _schema("companion_bili",
            "刷 B 站：tick=挑一条并读完（返回素材，不返回长文案）/ debrief=只读某条 / "
            "commit=回填她自己的感受 / share=分享给主人 / status=今天刷了几条、还能不能刷。",
            {"action": {"type": "string",
                        "enum": ["tick", "debrief", "commit", "share", "status"], "default": "status"},
             "bvid": {"type": "string"}, "summary": {"type": "string"},
             "rating": {"type": "integer"}, "score": {"type": "integer"},
             "reason": {"type": "string"}, "to_mid": {"type": "integer"},
             "via": {"type": "string"}, "frames": {"type": "boolean"},
             "force": {"type": "boolean"}}, []),
    _schema("device_look", "看屏幕：截图并返回路径与前台窗口。target 选 phone/desktop/browser/game。",
            {"target": {"type": "string", "enum": ["phone", "desktop", "browser", "game"], "default": "desktop"}}),
    _schema("device_act", "在屏幕上做一个动作（点/滑/打字），返回执行后的截图。危险动作会被拒。",
            {"target": {"type": "string", "enum": ["phone", "desktop", "browser", "game"]},
             "action": {"type": "string", "enum": ["tap", "click", "swipe", "type", "key"]},
             "x": {"type": "integer"}, "y": {"type": "integer"},
             "x2": {"type": "integer"}, "y2": {"type": "integer"},
             "text": {"type": "string"}, "key": {"type": "string"}, "double": {"type": "boolean"}},
            ["target", "action"]),
    _schema("device_open", "打开一个 App / 网址 / 程序（走白名单）。",
            {"target": {"type": "string", "enum": ["phone", "desktop", "browser", "game"]},
             "what": {"type": "string"}}, ["target", "what"]),
]


# ── 工具实现 ────────────────────────────────────────────────
def _t_context(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    ctx = context_mod.build(
        home,
        budget=int(a.get("budget") or 2000),
        memory_text=memory.render(home, a.get("query") or ""),
        opinion_text=opinion.render(home, a.get("query") or ""),
        person_text=people.render(home, people.OWNER),
        life_text=life.render(home),
    )
    return ctx.as_dict()


def _t_react(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    return react_mod.react(
        home,
        mood_delta=a.get("mood_delta") or None,
        why=a.get("why") or "",
        person=a.get("person") or "",
        facts=a.get("facts") or None,
        memory_page=a.get("memory") or None,
        opinion_item=a.get("opinion") or None,
        life_event=a.get("life") or None,
        said=a.get("said") or "",
    )


def _t_opinion(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    action = a.get("action") or "list"
    if action == "add":
        o = opinion.add(home, a["claim"], tags=a.get("tags") or [],
                        confidence=float(a.get("confidence") or 0.45))
        return {"ok": True, "opinion": o.as_dict()}
    if action == "reinforce":
        return {"ok": True, "opinion": opinion.reinforce(home, a["id"]).as_dict()}
    if action == "weaken":
        return {"ok": True, "opinion": opinion.weaken(home, a["id"]).as_dict()}
    if action == "stats":
        return {"ok": True, **opinion.stats(home)}
    return {"ok": True, "items": [o.as_dict() for o in opinion.theses(home)]}


def _t_mood(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    if (a.get("action") or "get") == "nudge":
        st = mood_mod.apply_delta(home.mood(), a.get("delta") or {}, why=a.get("why") or "")
        home.save_json("mood/state.json", st)
        return {"ok": True, "mood": st, "summary": mood_mod.summary(st)}
    st = mood_mod.decay(home.mood())
    return {"ok": True, "mood": st, "label": st.get("label"), "summary": mood_mod.summary(st),
            "low": mood_mod.is_low(st)}


def _t_fence(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    hits = fence.scan(a["text"])
    safe, _ = fence.sanitize(a["text"])
    return {"ok": True, "hits": hits, "safe": not hits,
            "text": safe, "wrapped": fence.wrap(safe, a.get("source") or "外部")}


def _t_status(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    rows = home.read_jsonl("logs/initiatives.jsonl", limit=3)
    from . import clock

    return {
        "mood": home.mood().get("label"),
        "quota": home.quota(),
        "in_window": clock.in_window(home.rhythm()),
        "in_sleep": clock.in_sleep(home.rhythm()),
        "cost_today": round(cost.today_total(home), 4),
        "last_decision": rows[-1] if rows else None,
        "memory_pages": len(memory.pages(home)),
        "life_recent": len(life.recent(home, days=2)),
    }


def _t_tick(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    return drive.tick(home, host=str(a.get("host") or "mcp"), now=datetime.now(),
                      emit_silent=bool(a.get("emit_silent")))


def _t_events(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    wait = float(a.get("wait") or 0)
    since = a.get("since")
    if wait > 0:
        rows, cur = events.wait(home, timeout=wait, since=since)
    else:
        rows, cur = events.poll(home, since=since)
    return {"events": rows, "cursor": cur}


def _t_say(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    text = (a.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "没有内容"}
    now = datetime.now()
    from . import quota as quota_mod

    st = quota_mod.record(home.quota(), now, "speak")
    home.save_json("quota.json", st)
    home.append_jsonl("logs/said.jsonl", {
        "at": now.isoformat(timespec="seconds"), "text": text,
        "channel": a.get("channel") or "", "source": "mcp",
    })
    return {"ok": True, "text": text, "quota": st}


def _t_memory_write(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    p = memory.write(home, a["title"], a.get("body", ""), tags=a.get("tags") or [], summary=a["summary"])
    return {"ok": True, **p.as_dict()}


def _t_memory_search(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    hits = memory.search(home, a["query"], limit=int(a.get("limit") or 5))
    return {"ok": True, "hits": [{**p.as_dict(), "score": round(s, 2)} for s, p in hits]}


def _t_life(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    row = life.record(home, a["title"], kind=a.get("kind") or "watch", detail=a.get("detail", ""),
                      tags=a.get("tags") or [], shared=bool(a.get("shared")))
    return {"ok": True, **row}


def _t_life_shared(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    ids = [str(x) for x in (a.get("ids") or [])]
    if a.get("all"):
        ids = [r["id"] for r in life.unshared(home) if r.get("id")]
    if not ids:
        return {"ok": False, "error": "没给 ids，也没标 all"}
    n = life.mark_shared(home, ids)
    return {"ok": True, "marked": n, "unshared_left": len(life.unshared(home))}


def _t_group_judge(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    out = group.judge(home, a["text"], addressed_to_me=a.get("addressed_to_me"),
                      from_owner=bool(a.get("from_owner")))
    return {"ok": True, **out}


def _t_group_note(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    if a["mode"] == "reply":
        group.note_reply(home)
    else:
        group.note_lurk(home)
    return {"ok": True, **group.status(home)}


def _t_sticker_pick(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    hit = sticker.pick(home, a.get("mood") or "", a.get("tags") or [])
    if not hit:
        return {"ok": False, "error": "库里没有对得上的，这条就纯文字吧"}
    return {"ok": True, **hit}


def _t_sticker_add(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, **sticker.add(home, a["path"], mood=a.get("mood") or "开心", tags=a.get("tags") or [])}


def _t_sticker_stats(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, **sticker.stats(home)}


def _t_person(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    who = a.get("who") or people.OWNER
    if a.get("fact"):
        p = people.learn(home, who, a["fact"])
    elif any(k in a for k in ("relation", "closeness", "notes")):
        p = people.upsert(home, who, relation=a.get("relation"), closeness=a.get("closeness"), notes=a.get("notes"))
    else:
        p = people.resolve(home, who)
    if not p:
        return {"ok": False, "error": f"没有这个人的卡：{who}"}
    return {"ok": True, "person": p.as_dict(), "card": people.render(home, who)}


def _t_cost(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "today_cny": round(cost.today_total(home), 4), "summary": cost.summary(home)}


def _t_voice(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    text = a.get("text") or ""
    r = voice.sing(home, text) if a.get("sing") else voice.speak(home, text)
    return r


def _t_bili(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    action = a.get("action") or "status"
    if action == "status":
        return {"ok": True, **bili.status(home)}
    if action == "tick":
        return bili.tick(home, frames=a.get("frames"), force=bool(a.get("force")))
    if action == "debrief":
        from .bilinet import Net

        return {"ok": True, **bili.debrief(home, a["bvid"], net=Net(home.root), frames=a.get("frames"))}
    if action == "commit":
        return bili.commit(home, a["bvid"], summary=a.get("summary") or "",
                           rating=a.get("rating"))
    if action == "share":
        from .bilinet import Net

        via = a.get("via") or ""
        net = Net(home.root) if (via == "dm" or a.get("to_mid")) else None
        return bili.share(home, a["bvid"], a.get("summary") or "",
                          score_v=int(a.get("score") or 0), reason=a.get("reason") or "",
                          to_mid=a.get("to_mid"), via=via, net=net)
    return {"ok": False, "why": f"不认识的动作 {action}"}


def _t_look(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    return device.look(home, a.get("target") or "desktop")


def _t_act(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    args = {k: a[k] for k in ("x", "y", "x2", "y2", "text", "key", "double") if k in a}
    return device.act(home, a["target"], a["action"], args, is_owner=bool(a.get("is_owner", True)))


def _t_open(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    return device.open_thing(home, a["target"], a["what"], is_owner=bool(a.get("is_owner", True)))


HANDLERS: dict[str, Callable[[Home, dict[str, Any]], dict[str, Any]]] = {
    "companion_context": _t_context,
    "companion_status": _t_status,
    "companion_tick": _t_tick,
    "companion_react": _t_react,
    "companion_opinion": _t_opinion,
    "companion_mood": _t_mood,
    "companion_fence": _t_fence,
    "companion_events": _t_events,
    "companion_say": _t_say,
    "companion_memory_write": _t_memory_write,
    "companion_memory_search": _t_memory_search,
    "companion_life_record": _t_life,
    "companion_life_mark_shared": _t_life_shared,
    "companion_group_judge": _t_group_judge,
    "companion_group_note": _t_group_note,
    "companion_sticker_pick": _t_sticker_pick,
    "companion_sticker_add": _t_sticker_add,
    "companion_sticker_stats": _t_sticker_stats,
    "companion_person": _t_person,
    "companion_cost": _t_cost,
    "companion_voice": _t_voice,
    "companion_bili": _t_bili,
    "device_look": _t_look,
    "device_act": _t_act,
    "device_open": _t_open,
}


# ── JSON-RPC ───────────────────────────────────────────────
def _err(id_: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _ok(id_: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def handle(home: Home, req: dict[str, Any]) -> dict[str, Any] | None:
    """处理一条请求。通知（没有 id）返回 None，不回包。"""
    method = req.get("method") or ""
    id_ = req.get("id")
    params = req.get("params") or {}

    if id_ is None:
        return None  # notifications/initialized 之类

    if method == "initialize":
        return _ok(id_, {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER,
            "instructions": "伴侣运行时。每轮开头调 companion_context；想让她主动说话时轮询 companion_events。",
        })
    if method == "ping":
        return _ok(id_, {})
    if method == "tools/list":
        return _ok(id_, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name") or ""
        args = params.get("arguments") or {}
        fn = HANDLERS.get(name)
        if not fn:
            return _err(id_, -32601, f"没有这个工具：{name}")
        try:
            out = fn(home, args)
        except Exception as e:
            out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return _ok(id_, {
            "content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
            "isError": not out.get("ok", True),
        })
    if method in ("resources/list", "prompts/list"):
        return _ok(id_, {"resources": []} if method.startswith("resources") else {"prompts": []})
    return _err(id_, -32601, f"不支持的方法：{method}")


def serve(home: Home, *, stdin=None, stdout=None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    home.init()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            stdout.write(json.dumps(_err(None, -32700, "JSON 解析失败"), ensure_ascii=False) + "\n")
            stdout.flush()
            continue
        resp = handle(home, req)
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            stdout.flush()
    return 0
