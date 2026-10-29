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


def normalize_payload(payload: dict) -> dict:
    """把 hermes shell hook 的 payload 规范化成"顶层可读"。

    hermes 给钩子的 stdin 不是把调用参数原样平铺 —— 它有自己的字段表
    （agent/shell_hooks.py::_payload_fields）：
      · tool_name          → 顶层 tool_name
      · args（dict）       → 顶层 **tool_input**（换了名字！）
      · session_id / cwd / profile → 顶层
      · **其余全部塞进 extra**：user_message、assistant_response、turn_id、
        platform、conversation_history、group_id……全都在 extra 里。

    2026-10-29 实测踩过：三个钩子各自读顶层字段，全读不到 ——
    pre 拿不到 user_message（相关检索一直空查询）、post 拿不到
    assistant_response（静默退出，账本永远空）、shared 拿不到 args
    （生活事件从未标记过"讲过了"）。一个 payload 结构坑掉三个钩子。

    这里统一做：extra 里的字段提升到顶层；args 同时补 tool_input 与 args 两个名字。
    顶层已有的字段优先 —— extra 只补缺，不覆盖。
    """
    if not isinstance(payload, dict):
        return payload
    out = dict(payload)
    extra = out.pop("extra", None)
    if isinstance(extra, dict):
        for k, v in extra.items():
            if out.get(k) in (None, ""):
                out[k] = v
    # args 的两个名字都要有：hermes 把 kwargs["args"] 映射成 tool_input，
    # 但有些钩子按 args 读。两个名字指向同一份数据，谁读都能拿到。
    if isinstance(out.get("tool_input"), dict) and not isinstance(out.get("args"), dict):
        out["args"] = out["tool_input"]
    elif isinstance(out.get("args"), dict) and not isinstance(out.get("tool_input"), dict):
        out["tool_input"] = out["args"]
    return out


def _texts(payload: dict) -> list[str]:
    """从两种钩子 payload 里挖出她发出去的正文。

    形态一（matcher=napcat_send_message）：args 就是参数字典
    形态二（matcher=tool_call）：args.calls[] = [{name, arguments}]
    """
    out: list[str] = []
    tool = str(payload.get("tool_name") or "")
    raw = payload.get("args") or payload.get("arguments") or payload.get("tool_input") or ""
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

    texts = _texts(normalize_payload(payload))
    if not texts:
        return 0

    try:
        from companion import life
        from companion.store import Home
    except Exception as e:
        _note(f"shared-skip import {e}")
        return 0

    home = Home.at(os.environ.get("COMPANION_HOME") or DEFAULT_HOME)
    # store 不存在就什么都没得标 —— 但**要说一声**。
    # 2026-10-29 实测：COMPANION_HOME 没设时钩子会悄悄操作一个空 store，
    # 日志写"剩 0 条没讲"，看起来像"都标完了"，其实是"根本没读到"。
    if not (home.root / "life" / "life.jsonl").exists():
        _note(f"shared-skip 空 store（{home.root} 没有 life.jsonl）")
        return 0
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
