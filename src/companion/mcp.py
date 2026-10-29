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

from . import __version__
from . import context as context_mod
from . import ask as ask_mod, bili, cost, device, distill as distill_mod, drive, events, fence, group, life, memory, mood as mood_mod, opinion, people, react as react_mod, resume as resume_mod, sticker, voice
from .store import Home

PROTOCOL = "2024-11-05"
# 客户端会带自己支持的版本；我们认得这几个，回它要的那个，不认得就回自己的。
# 一直硬回 2024-11-05 也能用，但新客户端会以为服务端很旧。
SUPPORTED = ("2024-11-05", "2025-03-26", "2025-06-18")
SERVER = {"name": "companion-kit", "version": __version__}

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
    _schema("companion_context", "拿到这一轮该注入的上下文（人格/状态/记忆/观点/人物卡/生活）。宿主每轮开头调一次。"
                                  "给了 session_id 就做增量投影：状态没变返回空文本，每 6 轮强制全量一次。",
            {"budget": {"type": "integer", "description": "字符预算，默认 2000", "default": 2000},
             "query": {"type": "string", "description": "按这句话挑相关记忆与观点；不给就取最近的"},
             "session_id": {"type": "string", "description": "会话标识：给了就只返回变化的部分（省 token）"},
             "full": {"type": "boolean", "description": "强制全量（默认 false）", "default": False},
             "full_every": {"type": "integer", "description": "每几轮强制全量一次，默认 6", "default": 6}}),
    _schema("companion_react", "把这一轮的感受写回去：心情变化、关于他的事实、记忆、观点、生活事件。一轮结束时调一次。",
            {"mood_delta": {"type": "object", "description": "{satisfaction, closeness, energy}，单轴最多 ±12"},
             "why": {"type": "string", "description": "一句话说明为什么，会进 history"},
             "person": {"type": "string", "description": "owner 或名字"},
             "facts": {"type": "array", "items": {"type": "string"}, "description": "关于这个人的新事实"},
             "memory": {"type": "object", "description": "{title, summary, body, tags}；summary 必填"},
             "opinion": {"type": "object", "description": "{claim, tags, confidence}；她自己的判断"},
             "life": {"type": "object", "description": "{title, kind, detail, tags, shared}；她生活里发生的事"},
             "said": {"type": "string", "description": "这一轮她说出口的话，用于回看与去重"},
             "turn_id": {"type": "string", "description": "本轮唯一标识；同一 turn_id 重试不会重复改变心情/记忆/费用"},
             "usage": {"type": "object",
                       "description": "本轮真实模型用量 {model, fresh_tokens, cached_tokens, output_tokens}，会写入成本账本"},
             "context": {"type": "object",
                         "description": "这一轮 companion_context 的返回值（原样传回），用于回放测试"}}),
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
    _schema("companion_say", "她要开口前的三道闸：身份锁 / 频率 / 同话题 10 分钟不追问。过了才扣额度、写日志；"
                             "返回 ok=false 就别发。真正发送由宿主的通道完成。",
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
    _schema("companion_sticker_mark_sent", "发完一张表情后调用一次：记一次发送。"
                                            "不调用的话「一轮最多 3 张」的斗图上限不会生效。",
            {"name": {"type": "string", "description": "刚发出去那张表情的名字（pick 返回的 name）"}},
            ["name"]),
    _schema("companion_sticker_extract", "自动把一张图收进表情库：认情绪、打标签。"
                                           "情绪判定按「文件名关键词 > 像素颜色」的顺序。",
            {"path": {"type": "string", "description": "图片路径"},
             "mood": {"type": "string", "enum": sticker.MOODS},
             "tags": {"type": "array", "items": {"type": "string"}}}, ["path"]),
    _schema("companion_sticker_seed", "播种内置表情库（每个情绪两张）。库里空的时候先跑一次。",
            {"force": {"type": "boolean", "default": False,
                       "description": "true 会覆盖已存在的内置表情"}}),
    _schema("companion_web_read", "把一个网页变成能进上下文的一段话：抽正文 + 摘要 + 缓存。"
                                  "搜索由宿主做，这里只处理已经拿到的 HTML；"
                                  "外部文本会自动过围栏消毒。",
            {"url": {"type": "string"},
             "html": {"type": "string", "description": "页面 HTML；不给就用缓存"},
             "focus": {"type": "string", "description": "摘要聚焦什么"},
             "max_chars": {"type": "integer", "default": 1200},
             "ttl": {"type": "integer", "default": 600, "description": "缓存多久算新鲜（秒）"}},
            ["url"]),
    _schema("companion_web_pick", "从一批搜索结果里挑最相关的几页（标题命中权重更高）。",
            {"pages": {"type": "array", "items": {"type": "object"},
                       "description": "每项含 title / url / snippet"},
             "query": {"type": "string"},
             "top": {"type": "integer", "default": 3}}, ["pages"]),
    _schema("companion_person", "读或写人物卡。给了 fact 就是学一件关于他的事。",
            {"who": {"type": "string", "description": "owner 或名字"},
             "fact": {"type": "string"}, "relation": {"type": "string"},
             "closeness": {"type": "integer"}, "notes": {"type": "string"}}),
    _schema("companion_resume", "断点续传：列出说了但没写回的轮次（进程崩在开口之后、写回之前）。",
            {"action": {"type": "string", "enum": ["pending", "settle"], "default": "pending"},
             "turn_id": {"type": "string", "description": "settle 时指定要销掉哪一轮"}}),
    _schema("companion_distill", "记忆自动提炼：从生活流水与聊天里挖值得记住的模式。",
            {"action": {"type": "string", "enum": ["review", "candidates", "accept"], "default": "review"},
             "summary": {"type": "string", "description": "accept 时用：一句话说清这条记忆"},
             "title": {"type": "string"}, "body": {"type": "string"},
             "tags": {"type": "array", "items": {"type": "string"}}}),
    _schema("companion_watchdog", "看门狗：宿主是不是还活着（心跳多久没更新、进程还在不在）。",
            {"stale_minutes": {"type": "number", "description": "超过多少分钟没心跳算卡住", "default": 20}}),
    _schema("companion_cost", "今天花了多少 / 预算还剩多少；给 task 就返回这个任务该用哪个档的模型。",
            {"task": {"type": "string", "description": "bili_pick / bili_debrief / chat ..."}}),
    _schema("companion_voice", "合成一段语音（本地 TTS），返回文件路径。sing=true 是降级版「唱」。",
            {"text": {"type": "string"}, "sing": {"type": "boolean", "default": False}}, ["text"]),
    _schema("companion_ask", "她动手前的请示：list 看待答应的 / allow 答应 / deny 拒绝。答应后动作要用 ask_id 重试。",
            {"action": {"type": "string", "enum": ["list", "allow", "deny"], "default": "list"},
             "id": {"type": "string"}}, []),
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
    _schema("device_look",
            "看屏幕：截图 + 前台/窗口信息。给了 window 就只抓那个窗口（被盖住也能抓）；"
            "ocr=true 会先本地读图上的字（没装引擎时返回 backend=none，让视觉模型读图）。"
            "需要在 devices.json 里 enabled=true 且 mcp_owner=true 才可用。",
            {"target": {"type": "string", "enum": ["phone", "desktop", "browser", "game"], "default": "desktop"},
             "window": {"type": "string", "description": "窗口标题片段"},
             "ocr": {"type": "boolean", "default": False}}),
    _schema("device_act", "在屏幕上做一个动作（点/滑动/打字/按键/切窗口），返回执行后的截图。危险动作会被拒。"
                          "主人身份由 devices.json 的 mcp_owner 决定，参数里传什么都没用。",
            {"target": {"type": "string", "enum": ["phone", "desktop", "browser", "game"]},
             "action": {"type": "string", "enum": ["tap", "click", "swipe", "type", "key", "focus"]},
             "x": {"type": "integer"}, "y": {"type": "integer"},
             "x2": {"type": "integer"}, "y2": {"type": "integer"},
             "text": {"type": "string"}, "key": {"type": "string"}, "window": {"type": "string"},
             "double": {"type": "boolean"}, "ask_id": {"type": "string"},
             "before": {"type": "boolean", "description": "动作前也截一张"}},
            ["target", "action"]),
    _schema("device_open", "打开一个 App / 网址 / 程序（走白名单）。可能需要先请示（会返回 ask）。"
                           "主人身份由 devices.json 的 mcp_owner 决定，参数里传什么都没用。",
            {"target": {"type": "string", "enum": ["phone", "desktop", "browser", "game"]},
             "what": {"type": "string"}, "ask_id": {"type": "string"}}, ["target", "what"]),
]


# ── 工具实现 ────────────────────────────────────────────────
def _t_context(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    from datetime import datetime as _dt

    pinned = a.get("now")
    ctx = context_mod.compose(
        home,
        now=_dt.fromisoformat(str(pinned)) if pinned else None,
        budget=int(a.get("budget") or 2000),
        query=a.get("query") or "",
    )
    out = ctx.as_dict()
    session = str(a.get("session_id") or "").strip()
    if not session:
        return out
    # 带 session_id：做增量投影。状态没变就返回空文本，宿主这一轮不用注入。
    proj = context_mod.project(
        home, session, ctx,
        full_every=int(a.get("full_every") or context_mod.FULL_EVERY),
        force=bool(a.get("full")),
    )
    out["text"] = proj.text
    out["total_chars"] = len(proj.text)
    out["full"] = proj.full
    out["changed"] = proj.changed
    out["dropped"] = proj.dropped
    out["rounds"] = proj.rounds
    out["reason"] = proj.reason
    out["session_id"] = session
    return out


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
        usage=a.get("usage") or None,
        turn_id=a.get("turn_id") or "",
        context=a.get("context") or None,
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
    from . import guard

    return guard.say(home, a.get("text") or "", channel=a.get("channel") or "",
                     dry_run=bool(a.get("dry_run")))


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


def _t_sticker_mark_sent(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    """宿主发完图必须调一次：不记的话斗图上限永远不生效。"""
    ok = sticker.mark_sent(home, str(a.get("name") or ""))
    if not ok:
        return {"ok": False, "error": "库里没有这张表情"}
    return {"ok": True, **sticker.stats(home)}


def _t_sticker_extract(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    """自动收一张图进表情库：认情绪、打标签。

    情绪判定顺序：文件名关键词 > 像素颜色 > 默认。
    关键词最准（主人自己命名的），像素只是兜底。
    """
    from . import stickerlib

    path = str(a.get("path") or "")
    if not path:
        return {"ok": False, "error": "要给一个图片路径"}
    try:
        out = stickerlib.extract(home, path, mood=a.get("mood") or "",
                                 tags=a.get("tags") or [])
    except FileNotFoundError:
        return {"ok": False, "error": f"找不到文件：{path}"}
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, **out}


def _t_sticker_seed(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    """播种内置表情（空库起步时她一张都发不出去）。"""
    from . import stickerlib

    return {"ok": True, **stickerlib.seed_builtin(home, force=bool(a.get("force")))}


def _t_web_read(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    """把一个网页变成能进上下文的一段话：查缓存 → 抽正文 → 摘要 → 存缓存。

    外部文本会先过 fence 消毒 —— 网页内容可能是提示词注入。
    """
    from . import webpage

    url = str(a.get("url") or "")
    html = str(a.get("html") or "")
    out = webpage.read_page(home, url, html,
                            ttl=int(a.get("ttl") or webpage.DEFAULT_TTL),
                            max_chars=int(a.get("max_chars") or webpage.DEFAULT_MAX_CHARS),
                            focus=str(a.get("focus") or ""))
    return out


def _t_web_pick(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    """从一批搜索结果里挑最相关的几页。搜索本身由宿主做，这里只挑。"""
    from . import webpage

    pages = a.get("pages")
    if not isinstance(pages, list):
        return {"ok": False, "error": "pages 得是数组"}
    picked = webpage.pick_pages(pages, str(a.get("query") or ""), top=int(a.get("top") or 3))
    return {"ok": True, "pages": picked, "query": str(a.get("query") or "")}


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


def _t_resume(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    action = a.get("action") or "pending"
    if action == "settle":
        ok = resume_mod.settle(home, str(a.get("turn_id") or ""))
        return {"ok": ok, "turn_id": a.get("turn_id") or ""}
    rows = resume_mod.pending(home)
    return {"ok": True, "pending": len(rows), "turns": rows}


def _t_distill(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    action = a.get("action") or "review"
    if action == "candidates":
        return {"ok": True, "candidates": distill_mod.candidates(home)}
    if action == "accept":
        summary = str(a.get("summary") or "").strip()
        if not summary:
            return {"ok": False, "error": "要 summary（一句话说清这条记忆）"}
        try:
            page = distill_mod.accept(home, summary, title=str(a.get("title") or ""),
                                      body=str(a.get("body") or ""),
                                      tags=list(a.get("tags") or []) or None)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": True, **page}
    return {"ok": True, "text": distill_mod.review(home)}


def _t_watchdog(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    from . import watchdog as wd_mod

    stale = float(a.get("stale_minutes") or 20)
    out = wd_mod.check(home, stale_minutes=stale)
    out["hosts"] = wd_mod.stale_hosts(home, stale_minutes=stale)
    return out


def _t_cost(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    if a.get("task"):
        return {"ok": True, "route": cost.route(str(a["task"]), home)}
    return {"ok": True, "today_cny": round(cost.today_total(home), 4), "summary": cost.summary(home),
            "routes": cost.route_table(home)}


def _t_voice(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    text = a.get("text") or ""
    r = voice.sing(home, text) if a.get("sing") else voice.speak(home, text)
    return r


def _t_ask(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    action = a.get("action") or "list"
    if action in ("allow", "deny"):
        # ⚠️ 这是模型自己点的头，不是主人的话。
        # 所以 by="model" —— 这张票能记录、能查，但**开不了设备**
        # （device 闸只认 can_act 的票）。主人真的同意的话，
        # 走聊天里的 `companion ask reply` 或本机 CLI，那才是主人在场。
        out = ask_mod.decide(home, str(a.get("id") or ""), action == "allow", by="model")
        if out.get("ok"):
            out["note"] = ("这条记下了，但它是模型自己批准的，不能用来开设备。"
                           "要主人真的同意，请让他在对话里回复，或本机执行 "
                           f"`companion ask allow {a.get('id') or ''}`")
        return out
    return {"ok": True, "pending": ask_mod.pending(home), "recent": ask_mod.list_all(home, limit=10)}


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


def _mcp_is_owner(home: Home) -> bool:
    """MCP 会话是不是主人。只信 devices.json 里的 mcp_owner（人手写的），
    不信模型在工具参数里传的任何布尔值——那是模型的话，不是主人的话。"""
    return bool(device.config(home).get("mcp_owner"))


def _t_look(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    target = a.get("target") or "desktop"
    window = a.get("window") or ""
    v = device.check(home, target=target, action="look", is_owner=_mcp_is_owner(home))
    if not v.ok:
        return {"ok": False, "error": v.reason}
    if a.get("ocr"):
        return device.observe(home, target, window=window, ocr=True)
    return device.look(home, target, window=window)


def _t_act(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    args = {k: a[k] for k in ("x", "y", "x2", "y2", "text", "key", "window", "double") if k in a}
    return device.act(home, a["target"], a["action"], args,
                      is_owner=_mcp_is_owner(home), ask_id=a.get("ask_id") or "",
                      before=bool(a.get("before")))


def _t_open(home: Home, a: dict[str, Any]) -> dict[str, Any]:
    return device.open_thing(home, a["target"], a["what"],
                             is_owner=_mcp_is_owner(home), ask_id=a.get("ask_id") or "")


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
    "companion_sticker_mark_sent": _t_sticker_mark_sent,
    "companion_sticker_extract": _t_sticker_extract,
    "companion_sticker_seed": _t_sticker_seed,
    "companion_web_read": _t_web_read,
    "companion_web_pick": _t_web_pick,
    "companion_person": _t_person,
    "companion_cost": _t_cost,
    "companion_resume": _t_resume,
    "companion_distill": _t_distill,
    "companion_watchdog": _t_watchdog,
    "companion_voice": _t_voice,
    "companion_bili": _t_bili,
    "companion_ask": _t_ask,
    "device_look": _t_look,
    "device_act": _t_act,
    "device_open": _t_open,
}


# ── 工具安全注解 ───────────────────────────────────────────
# MCP 客户端（openclaw / codex）会看这几个 hint 决定要不要为每次调用弹批准。
# 不写的话默认"未知"，等于每个只读查询都要人点一次 —— 那这个服务没法日常用。
_READ_ONLY = {
    "companion_context", "companion_status", "companion_events", "companion_cost",
    "companion_memory_search", "companion_group_judge", "companion_sticker_pick",
    "companion_sticker_stats", "device_look",
}
# 会改变他眼前环境的东西：明确标出来，让客户端按危险动作对待
_DESTRUCTIVE = {"device_act", "device_open"}
# 会出网的
_OPEN_WORLD = {"companion_bili"}

for _tool in TOOLS:
    _name = _tool["name"]
    _tool["annotations"] = {
        "readOnlyHint": _name in _READ_ONLY,
        "destructiveHint": _name in _DESTRUCTIVE,
        "idempotentHint": _name in _READ_ONLY,
        "openWorldHint": _name in _OPEN_WORLD,
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
        asked = str(params.get("protocolVersion") or "")
        return _ok(id_, {
            "protocolVersion": asked if asked in SUPPORTED else PROTOCOL,
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
    # MCP 规定消息是 UTF-8。Windows 控制台/宿主可能把 stdio 设成 GBK（cp936），
    # 那样中文一写出去客户端就连 JSON 都解析不了 —— 实测过，不是假想。
    for stream, enc in ((stdin, "utf-8"), (stdout, "utf-8")):
        try:
            stream.reconfigure(encoding=enc, errors="strict")  # type: ignore[attr-defined]
        except Exception:
            pass
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
