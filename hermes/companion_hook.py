#!/usr/bin/env python3
"""hermes 适配层 ①：把 companion 的上下文塞进 hermes 的 pre_llm_call 钩子。

hermes 的 shell hook 协议：stdin 收 JSON，stdout 回 JSON（`{"context": "..."}`），
返回的 context 会被并进这一轮的用户消息之前。

和现有的 said.py 不是替代关系——第一版是**并存**：
  · said.py   记住"她说过什么、干过什么"（历史）
  · 这个脚本   提供人格、心情、额度、作息、观点（状态）
两者内容不重叠，所以同时挂不会重复。

装法（不激活前先手动测）：
    echo '{"session_id":"t","platform":"napcat","user_message":"在吗"}' \
      | COMPANION_HOME=/mnt/c/Users/you/.companion \
        ~/.companion-venv/bin/python ~/.hermes/scripts/companion_hook.py

激活 = 在 ~/.hermes/config.yaml 的 hooks.pre_llm_call 里加一条 command，
指向上面那个命令（带 COMPANION_HOME）。**改完要重启 gateway。**
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

DEFAULT_HOME = str(Path.home() / ".companion")
# 1600 而不是 1200：常驻块（人格 + 人物卡 + 生活）本身就 ~1000 字，
# 1200 会把 life 挤到截断边缘；等记忆和看法攒起来更不够。
# hermes 的 system prompt 已经 8k token，多这几百字不算什么。
BUDGET = int(os.environ.get("COMPANION_INJECT_BUDGET", "1600") or 1600)
# 只在聊天平台注入，cron 会话拿的是自己的提示词，不需要再叠一层
CHAT_PLATFORMS = {"napcat", "qq", "onebot"}

# 部署时和 companion_shared.py 同目录（hermes/scripts/）——
# payload 规范化是三个钩子共用的逻辑，别各抄一份然后改出三个样。
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from companion_shared import normalize_payload as _normalize
except ImportError:  # 单文件跑（没部署全）就本地实现，行为一致
    def _normalize(payload: dict) -> dict:
        if not isinstance(payload, dict):
            return payload
        out = dict(payload)
        extra = out.pop("extra", None)
        if isinstance(extra, dict):
            for k, v in extra.items():
                if out.get(k) in (None, ""):
                    out[k] = v
        if isinstance(out.get("tool_input"), dict) and not isinstance(out.get("args"), dict):
            out["args"] = out["tool_input"]
        elif isinstance(out.get("args"), dict) and not isinstance(out.get("tool_input"), dict):
            out["tool_input"] = out["args"]
        return out


def _text_of(raw) -> str:
    """hermes 的 user_message 可能是字符串、{content} 或 [{type:text,text}]。"""
    if isinstance(raw, dict):
        return str(raw.get("content") or "")
    if isinstance(raw, list):
        parts = []
        for part in raw:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text") or ""))
            elif isinstance(part, str):
                parts.append(part)
        return " ".join(parts)
    return str(raw or "")


LOG = "/tmp/companion_hook.log"


def _note(line: str) -> None:
    """埋点：hermes 的钩子输出不进 journal，只能自己记一行，否则没法验证有没有跑。"""
    try:
        from datetime import datetime

        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now():%m-%d %H:%M:%S} {line}\n")
    except Exception:
        pass


SAID_STATE = "logs/.hook_said.json"
CTX_STATE = "logs/.hook_ctx.json"     # 旧路径：投影状态已迁到 core 的 context/projection.json
# 每 N 轮强制整段重发一次。历史被压缩/裁剪时，之前注入的那份可能已经不在了，
# 只发"变化块"会让她丢掉状态；这个数字就是"最多陈旧几轮"。
FULL_EVERY = int(os.environ.get("COMPANION_FULL_EVERY", "6") or 6)


def _render_delta(home, session: str, ctx) -> str:
    """只发变化的块。状态没变就不发 —— 上一轮那份还在会话历史里。

    省的是**每一轮重复注入同一段前缀**的钱；不这么做的话，
    700 字的状态每轮都重发一次，10 轮就是 7000 字。

    投影逻辑已经下沉到 core（companion.context.project）：hermes、dsh、MCP
    必须用同一套指纹与全量周期，否则同一个 store 会出现三种"去重口径"。
    """
    from companion import context as context_mod

    proj = context_mod.project(home, session, ctx, full_every=FULL_EVERY)
    return proj.text


def _msg_text(raw) -> str:
    """从 history 里抠出正文。hermes 的消息体可能是 str，也可能是 parts 列表。"""
    if isinstance(raw, dict):
        inner = raw.get("content")
        return _msg_text(inner) if inner is not None else str(raw.get("text") or "")
    if isinstance(raw, list):
        parts = []
        for part in raw:
            if isinstance(part, dict):
                if part.get("type") in (None, "text"):
                    parts.append(str(part.get("text") or part.get("content") or ""))
            elif isinstance(part, str):
                parts.append(part)
        return " ".join(x for x in parts if x)
    return str(raw or "")


def _last_assistant(history) -> str:
    """这一轮之前她说的最后一句。pre_llm_call 拿得到完整历史，所以不用等 post 钩子。"""
    if not isinstance(history, list):
        return ""
    for msg in reversed(history):
        role = str((msg or {}).get("role") or "").lower() if isinstance(msg, dict) else ""
        if role in ("assistant", "model"):
            text = _msg_text(msg).strip()
            if text:
                return text
    return ""


def _said_state(home) -> list[str]:
    import json as _json

    try:
        d = _json.loads((home.root / SAID_STATE).read_text(encoding="utf-8"))
        return [str(x) for x in d if isinstance(x, str)][-40:]
    except Exception:
        return []


def _record_turn(home, said: str, ctx: dict, turn: str, platform: str) -> bool:
    """【已停用】pre 钩子里的写回兜底。

    2026-10-29 起写回统一走 post_llm_call（companion_post.py）。
    这个函数的去重 key 是 (turn_id + 正文)，而它拿到的是「上一轮正文 + 本轮 turn_id」，
    与 post 的「本轮正文 + 本轮 turn_id」永远撞不上——只要调用就必然重复记录。
    保留函数体只为不破坏可能的外部引用，正常流程不再调用。
    """
    import hashlib
    import json as _json
    import subprocess

    if not said or said.startswith("[SILENT]"):
        return False
    digest = hashlib.sha1(f"{turn}|{said}".encode("utf-8")).hexdigest()[:16]
    seen = _said_state(home)
    if digest in seen:
        return
    payload = {"said": said[:1500], "why": "hermes 钩子自动记录", "context": ctx}
    tmp = home.root / "logs" / ".react_payload.json"
    try:
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(_json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        done = subprocess.run(
            [sys.executable, "-m", "companion", "--json", "react", "--from", str(tmp)],
            capture_output=True, text=True, timeout=5,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        if done.returncode != 0:
            _note(f"react-fail {done.returncode}: {(done.stderr or '')[:120]}")
            return False
        seen.append(digest)
        (home.root / SAID_STATE).write_text(_json.dumps(seen[-40:]), encoding="utf-8")
        _note(f"react-said {len(said)}c platform={platform}")
        return True
    except Exception as e:
        _note(f"react-error {type(e).__name__}: {e}")
        return False


def _empty(reason: str = "") -> int:
    """没有可注入的东西时必须打印 {} —— 与 said.py 保持同一协议。"""
    _note(f"skip {reason}")
    print("{}")
    return 0


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        payload = {}
    # hermes 把 user_message / turn_id / platform 等塞在 extra 里 ——
    # 不规范化就读不到：相关检索一直是空查询、ask 回填拿不到主人的原话
    #（2026-10-29 实测踩过：pre 注入能跑，但 user_text 永远是空）。
    payload = _normalize(payload)

    session = str(payload.get("session_id") or "")
    platform = str(payload.get("platform") or "")
    user_text = _text_of(payload.get("user_message"))

    # cron / 自由时间会话不注入：它们有自己的上下文，叠上去只会拉高 token
    if session.startswith("local-"):
        return _empty("cron")
    if platform and platform not in CHAT_PLATFORMS:
        return _empty(f"platform={platform}")

    try:
        from companion import context as ctx_mod
        from companion import life, memory, opinion, people
        from companion.store import Home
    except Exception as e:  # 装少了不该让她的对话失败
        _note(f"import-fail {type(e).__name__}: {e}")
        print(json.dumps({"warn": f"companion 不可用：{e}"}, ensure_ascii=False))
        return 0

    home = Home.at(os.environ.get("COMPANION_HOME") or DEFAULT_HOME)

    # 心跳：hermes 每次注入前都写一次。看门狗据此判断她是"卡住"还是"死了"。
    # 写失败不影响注入——心跳只是体检用的。
    try:
        from companion import watchdog as wd_mod

        wd_mod.beat(home, host=os.environ.get("COMPANION_HOST", "hermes"))
    except Exception as e:
        _note(f"beat-skip {type(e).__name__}: {e}")

    # 记一笔"主人最后说话时间"：沉默衰减（3 天降额、7 天归零）靠它。
    # 不记的话 companion 永远以为他刚说过话，衰减机制等于没开。
    # 按 (turn_id + 正文) 去重：一次用户消息会触发多次 LLM 调用（工具循环），
    # 不去重的话 turns 会被同一条消息刷满，`_owner_last_seen` 也会被推到现在。
    gap_hours = 0.0
    if user_text.strip():
        try:
            import hashlib
            from datetime import datetime

            udigest = hashlib.sha1(
                f"u|{payload.get('turn_id') or ''}|{user_text}".encode("utf-8")).hexdigest()[:16]
            marker = home.root / "logs" / ".hook_user"
            last = ""
            try:
                last = marker.read_text(encoding="utf-8").strip()
            except Exception:
                pass
            if udigest != last:
                # 上次他说话是多久以前：用来判断"他隔了很久才回来"
                try:
                    prev = [r for r in home.read_jsonl("logs/turns.jsonl", limit=200)
                            if r.get("role") == "user" and r.get("at")]
                    if prev:
                        then = datetime.fromisoformat(str(prev[-1]["at"]))
                        gap_hours = max(0.0, (datetime.now() - then).total_seconds() / 3600.0)
                except Exception:
                    gap_hours = 0.0
                home.append_jsonl("logs/turns.jsonl", {
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "role": "user", "platform": platform, "chars": len(user_text),
                    "session": session[:32],
                })
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(udigest, encoding="utf-8")
        except Exception:
            pass

    # 主人在聊天里回了话 → 自动落��对应的待办上。
    # 不做这一步，主人说"可以"之后还得有人手敲 `companion ask allow <id>`，
    # 一道没人记得的手工步骤，流程就等于没有。
    # 身份：只有私聊会话才算主人。群里别人说"可以"不能算数。
    if user_text.strip() and not str(payload.get("group_id") or payload.get("is_group") or ""):
        try:
            from companion import ask as ask_mod

            r = ask_mod.decide_by_reply(home, user_text, by="owner")
            if r.get("ok"):
                _note(f"ask 自动回填：{r.get('intent')} → {r['ask'].get('id')}")
                # 她问过、主人答了 —— 这条请示确实说出口了
                try:
                    ask_mod.mark_spoken(home, str(r["ask"].get("id")))
                except Exception:
                    pass
        except Exception as e:
            _note(f"ask-reply-skip {type(e).__name__}: {e}")

    try:
        ctx = ctx_mod.build(
            home,
            budget=BUDGET,
            # hermes 的 system prompt 里已经有人格，这里只补她没有的状态与记忆
            include_soul=False,
            memory_text=memory.render(home, user_text),
            opinion_text=opinion.render(home, user_text, limit=2, settled_only=True, recent_days=3),
            person_text=people.render(home, people.OWNER),
            life_text=life.render(home),
        )
        text = ctx.text.strip()
    except Exception as e:
        print(json.dumps({"warn": f"上下文构建失败：{e}"}, ensure_ascii=False))
        return 0

    # 写回不在这里做了。
    #
    # 这里曾经有一段"pre 兜底"：从 conversation_history 取最后一条 assistant 写回，
    # 理由是"hermes 没有 post_response 钩子"。后来 post_llm_call 钩子（companion_post.py）
    # 补上了，而且完整覆盖（react 一条命令落心情 / 人物卡 / 记忆 / 成本）。
    #
    # 但那段兜底没删，结果是每轮 assistant 记录写两遍：pre 用的是
    # (本轮 turn_id + 上一轮正文)，post 用的是 (本轮 turn_id + 本轮正文)，
    # 两个哈希永远撞不上，去重形同虚设；连 mood_rules.on_turn 也跟着多推一次。
    # 2026-10-29 实测：turns.jsonl 里两类记录 7:7 完全对半。
    #
    # 现在写回只有一条路：post_llm_call → companion_post.py。

    # 把"你刚说过什么"摆到她面前：靠提示词管不住重复，但给事实比给规则有用
    try:
        from datetime import datetime as _dt

        from companion import guard

        said_recent = guard.recent_said(home, minutes=guard.TOPIC_MINUTES, now=_dt.now())
        if said_recent:
            # 只摆最近那一句。三条一起摆，她会以为"这些都不许说"，反而哑掉。
            # 措辞也别说"换个说法或不接"——正常延续话题时那句话会挡路，
            # 该挡的只是"原样再发一遍"。
            last = str(said_recent[-1].get("text") or "").replace("\n", " ")[:40]
            if last:
                hint = f"【别复读】你刚说过：{last}……原样再说一遍，等于没听见他。"
                from companion.context import Block

                ctx.blocks.append(Block("repeat", hint, 90))
                ctx.text = ctx.text + "\n\n" + hint
                text = ctx.text
    except Exception as e:
        _note(f"hint-skip {type(e).__name__}: {e}")

    # 把"她在等主人点头的事"摆出来：请示单没人呈现就等于没问过。
    try:
        from companion import ask as ask_mod

        waiting = ask_mod.pending(home)
        if waiting:
            items = "；".join(str(r.get("say") or r.get("action") or "")[:60] for r in waiting[:3])
            block = f"【等你点头】{items}（主人答应后你才能动手）"
            from companion.context import Block

            ctx.blocks.append(Block("ask", block, 95))
            ctx.text = ctx.text + "\n\n" + block
            text = ctx.text
    except Exception as e:
        _note(f"ask-skip {type(e).__name__}: {e}")

    if not text:
        return _empty("empty-context")
    try:
        payload_text = _render_delta(home, session, ctx)
    except Exception as e:
        _note(f"dedupe-fail {type(e).__name__}: {e}")
        payload_text = text
    if not payload_text.strip():
        _note(f"inject-delta 0c session={session[:24]}（状态没变，省一轮）")
        print("{}")
        return 0
    _note(f"inject {len(payload_text)}c session={session[:24]}")
    print(json.dumps({"context": payload_text}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
