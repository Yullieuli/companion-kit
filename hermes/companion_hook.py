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
      | COMPANION_HOME=/mnt/c/Users/Anran/.companion \
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
BUDGET = int(os.environ.get("COMPANION_INJECT_BUDGET", "1200") or 1200)
# 只在聊天平台注入，cron 会话拿的是自己的提示词，不需要再叠一层
CHAT_PLATFORMS = {"napcat", "qq", "onebot"}


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
    try:
        ctx = ctx_mod.build(
            home,
            budget=BUDGET,
            # hermes 的 system prompt 里已经有人格，这里只补她没有的状态与记忆
            include_soul=False,
            memory_text=memory.render(home, user_text),
            opinion_text=opinion.render(home, user_text),
            person_text=people.render(home, people.OWNER),
            life_text=life.render(home),
        )
        text = ctx.text.strip()
    except Exception as e:
        print(json.dumps({"warn": f"上下文构建失败：{e}"}, ensure_ascii=False))
        return 0

    if not text:
        return _empty("empty-context")
    _note(f"inject {len(text)}c session={session[:24]}")
    print(json.dumps({"context": text}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
