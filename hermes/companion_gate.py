"""hermes 适配层 ③：发送前闸。

为什么需要它：hermes 的 `pre_llm_call` 钩子发生在**生成之前**，
拦不住已经生成的消息 —— 她说"以后叫我小璃"、把同一句��两遍，
提示词管不住，得有确定性判定。而 hermes 的钩子协议里
`pre_llm_call` 只能注入上下文、`pre_verify` 只能要求"继续"，
**唯一真正的拦截点是渠道的发送函数**（napcat 的 `send()`）。
这个脚本就是给那个位置用的。

两种模式，区别很重要：

  reply  （默认）只过「身份锁」+「同话题重复」。
          适合**回复主人**：回复不占主动开口额度，也不能因为
          "距上次开口还差 40 分钟"被拦 —— 那不是主动开口。
  active 完整三道闸（身份锁 + 额度/间隔 + 同话题），用于**主动开口**。

协议：stdin 收 JSON，stdout 回 JSON。

    echo '{"text": "以后叫我小璃吧", "mode": "reply"}' | companion_gate.py
    → {"ok": false, "blocked": "identity", "why": "名字在 SOUL.md 里是「柚璃」，不能在对话里改"}

    echo '{"text": "在吗"}' | companion_gate.py
    → {"ok": true, "blocked": "", "why": ""}

退出码：0 放行；1 拦下（拦下时 stdout 仍有 JSON，方便调用方读原因）。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime

DEFAULT_HOME = os.path.expanduser("~/.companion")
LOG = "/tmp/companion_gate.log"


def _note(line: str) -> None:
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line.rstrip() + "\n")
    except Exception:
        pass


def _text_of(raw) -> str:
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, dict):
        inner = raw.get("content")
        if inner is not None:
            return _text_of(inner)
        return str(raw.get("text") or "").strip()
    if isinstance(raw, list):
        return " ".join(x for x in (_text_of(i) for i in raw) if x)
    return str(raw or "").strip()


def evaluate(home, text: str, mode: str = "reply") -> dict:
    """判定一句话能不能发出去。不记账、不改状态。"""
    from companion import guard

    text = (text or "").strip()
    if not text:
        return {"ok": False, "blocked": "empty", "why": "没有内容"}

    lock = guard.name_lock(home, text)
    if lock["blocked"]:
        return {"ok": False, "blocked": "identity", "why": lock["why"],
                "match": lock.get("match") or ""}

    topic = guard.same_topic(home, text, now=datetime.now())
    if topic["dup"]:
        return {"ok": False, "blocked": "repeat",
                "why": f"10 分钟内已经说过很像的话（重合 {topic['common']} 字）",
                "prev": topic.get("prev") or ""}

    if mode == "active":
        # 主动开口才看额度与间隔：回复主人不占这个额度。
        from companion import quota as quota_mod

        allowed, why = quota_mod.allow(home.quota(), None, datetime.now())
        if not allowed:
            return {"ok": False, "blocked": "quota", "why": why}

    return {"ok": True, "blocked": "", "why": ""}


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    text = _text_of(payload.get("text") or payload.get("content") or payload.get("message"))
    mode = str(payload.get("mode") or "reply").strip().lower()
    if mode not in ("reply", "active"):
        mode = "reply"

    try:
        from companion.store import Home
    except Exception as e:
        print(json.dumps({"ok": True, "blocked": "", "why": "",
                          "warn": f"companion 不可用：{e}"}, ensure_ascii=False))
        return 0

    try:
        home = Home.at(os.environ.get("COMPANION_HOME") or DEFAULT_HOME)
        out = evaluate(home, text, mode)
    except Exception as e:
        # 闸坏了不能把她的对话搞挂：放行，但留下痕迹
        _note(f"gate-fail {type(e).__name__}: {e}")
        print(json.dumps({"ok": True, "blocked": "", "why": "",
                          "warn": f"闸异常：{e}"}, ensure_ascii=False))
        return 0

    _note(f"mode={mode} ok={out.get('ok')} blocked={out.get('blocked')} text={text[:40]}")
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
