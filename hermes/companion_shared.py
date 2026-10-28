#!/usr/bin/env python3
"""hermes 适配层 ③：她说出口之后，把讲过的事标成「讲过了」。

为什么必须是钩子而不是提示词：
  「记得标记」这种要求，模型十次里会漏三次；而漏掉的后果是**同一件事被反复讲**——
  她的唤醒理由会一直是「有 4 件生活里的事还没跟他说」，一天说四遍同一句。

做法：挂在 post_tool_call 上，抓 napcat_send_message 的正文，
用二字词重叠去 match 她还没讲过的生活事件，命中才标记。

匹配不上就不标——**宁可下次再讲，也不要把没讲过的标成讲过了**。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

DEFAULT_HOME = str(Path.home() / ".companion")
LOG = "/tmp/companion_hook.log"


def _note(line: str) -> None:
    try:
        from datetime import datetime

        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now():%m-%d %H:%M:%S} {line}\n")
    except Exception:
        pass


def _texts(payload: dict) -> list[str]:
    """从两种钩子 payload 里挖出她发出去的正文。

    形态一（matcher=napcat_send_message）：args 就是参数字典
    形态二（matcher=tool_call）：args.calls[] = [{name, arguments}]
    """
    out: list[str] = []
    tool = str(payload.get("tool_name") or "")
    raw = payload.get("args") or payload.get("arguments") or ""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        return out

    def take(d: dict) -> None:
        msg = d.get("message")
        if isinstance(msg, str) and msg.strip():
            out.append(msg)

    if tool == "tool_call":
        for call in raw.get("calls") or []:
            fn = call.get("function") or call
            if str(fn.get("name")) != "napcat_send_message":
                continue
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args or "{}")
                except Exception:
                    args = {}
            if isinstance(args, dict):
                take(args)
    elif tool == "napcat_send_message":
        take(raw)
    return out


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0

    texts = _texts(payload)
    if not texts:
        return 0

    try:
        from companion import life
        from companion.store import Home
    except Exception as e:
        _note(f"shared-skip import {e}")
        return 0

    home = Home.at(os.environ.get("COMPANION_HOME") or DEFAULT_HOME)
    marked = 0
    for t in texts:
        ids = life.match_shared(home, t)
        if ids:
            marked += life.mark_shared(home, ids)
    if marked:
        _note(f"shared {marked} 条（剩 {len(life.unshared(home))} 条没讲）")
    else:
        _note("shared 0 条（没匹配上，保留到下次）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
