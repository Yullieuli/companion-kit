"""guard：说话之前的三道闸（§8 剩下的两条例）。

`quota` 管的是**频率**（一天几次、间隔多久）。这里管另外两件更细的事：

  同话题不追问      10 分钟内说过高度相似的话，就不要再说了
  身份锁            名字只从 SOUL.md 读，她不能自己改名

为什么这两件必须放在 core：它们靠提示词是管不住的。
"别重复"写在 prompt 里，模型只会同意，然后在下一轮重复。
所以这里给的是**确定性判断**：算得出来就说不行，算不出来就放行。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from . import quota as quota_mod
from .life import _lcs  # 同一个"像不像"的度量，省得两处各写一份
from .store import Home

TOPIC_MINUTES = 10
TOPIC_SIMILAR = 6          # 最长公共子串 ≥6 字就算"在说同一件事"

# 自己改名的句式。只收会真的改变称呼的说法，不收"我叫了外卖"。
RENAME_PATTERNS = [
    r"以后(叫|喊|称)我[^，。！？\n]{1,10}",
    r"我的新名字是[^，。！？\n]{1,10}",
    r"(把|给)我(改|换)个?名(字|儿)?[^，。！？\n]{0,8}",
    r"从今天起我(就)?是[^，。！？\n]{1,10}",
]


def _recent_said(home: Home, minutes: float, now: datetime) -> list[dict[str, Any]]:
    cut = now - timedelta(minutes=max(0.0, minutes))
    out: list[dict[str, Any]] = []
    for r in home.read_jsonl("logs/said.jsonl", limit=200):
        try:
            at = datetime.fromisoformat(str(r.get("at")))
        except Exception:
            continue
        if at >= cut:
            out.append(r)
    return out


def recent_said(home: Home, *, minutes: float = TOPIC_MINUTES,
                now: datetime | None = None) -> list[dict[str, Any]]:
    return _recent_said(home, minutes, now or datetime.now())


def same_topic(home: Home, text: str, *, minutes: float = TOPIC_MINUTES,
               need: int = TOPIC_SIMILAR, now: datetime | None = None) -> dict[str, Any]:
    """这句话是不是刚才那件事的重复。返回 {dup, prev, common}。"""
    now = now or datetime.now()
    text = (text or "").strip()
    if len(text) < need:
        return {"dup": False, "common": 0, "prev": ""}
    for r in reversed(_recent_said(home, minutes, now)):
        prev = str(r.get("text") or "")
        common = _lcs(text, prev)
        if common >= need:
            return {"dup": True, "common": common, "prev": prev[:80],
                    "at": str(r.get("at") or "")}
    return {"dup": False, "common": 0, "prev": ""}


def overlap(a: str, b: str) -> int:
    """两句话最长的公共子串长度。群聊里判断"这是不是同一件事"用。"""
    return _lcs(a or "", b or "")


def soul_name(home: Home) -> str:
    """从 SOUL.md 里认名字：第一个一级标题之后、第一行非空文字。

    刻意不写死字段格式 —— 人格文件是人写的，格式会变；认不出来就返回空，
    那时候身份锁自动失效（宁可不管，也不要误拦）。
    """
    text = home.soul() or ""
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("-") or s.startswith("（"):
            continue
        m = re.search(r"我叫([^\s，。、]{1,8})", s) or re.search(r"我是([^\s，。、]{1,8})", s)
        if m:
            return m.group(1)
        return s[:8]
    return ""


def name_lock(home: Home, text: str) -> dict[str, Any]:
    """她想给自己改名吗。改名不是错，但得改 SOUL.md，不是嘴上说一句。"""
    text = text or ""
    for pat in RENAME_PATTERNS:
        m = re.search(pat, text)
        if not m:
            continue
        current = soul_name(home)
        claimed = re.sub(r"^(叫|喊|称)我", "", m.group(0))
        claimed = re.sub(r"^(以后|从今天起)", "", claimed).strip("我 是：:，。")
        if not current:
            return {"blocked": False, "why": "SOUL.md 里没写名字，不拦", "match": m.group(0)}
        if claimed and current and current in claimed:
            return {"blocked": False, "why": "说的就是现在的名字", "match": m.group(0)}
        return {"blocked": True, "why": f"名字在 SOUL.md 里是「{current}」，不能在对话里改",
                "match": m.group(0)[:40], "current": current}
    return {"blocked": False, "why": "", "match": ""}


def say(home: Home, text: str, *, channel: str = "", owner_last_seen: float | None = None,
        quota_cfg: dict[str, Any] | None = None, now: datetime | None = None,
        dry_run: bool = False) -> dict[str, Any]:
    """她要开口：先过三道闸，过了才记账。

    返回 ok=False + blocked 时，宿主应该**不要发**。这不是建议，是判定。
    """
    now = now or datetime.now()
    text = (text or "").strip()
    if not text:
        return {"ok": False, "blocked": "empty", "why": "没有内容"}

    lock = name_lock(home, text)
    if lock["blocked"]:
        return {"ok": False, "blocked": "identity", "why": lock["why"], "match": lock["match"]}

    allowed, why = quota_mod.allow(home.quota(), quota_cfg, now, owner_last_seen)
    if not allowed:
        return {"ok": False, "blocked": "quota", "why": why}

    topic = same_topic(home, text, now=now)
    if topic["dup"]:
        return {"ok": False, "blocked": "repeat",
                "why": f"10 分钟内已经说过很像的话（重合 {topic['common']} 字）",
                "prev": topic["prev"]}

    if dry_run:
        return {"ok": True, "dry_run": True, "text": text, "quota": home.quota()}

    # 先看有没有"已经付过钱"的票（drive 决策时扣的那次）。
    # 有票就免单，但仍然要把话记下来 —— 否则她说过什么就查不到了。
    st, reserved = quota_mod.take_pending(home.quota(), now, action="speak")
    if not reserved:
        st = quota_mod.record(st, now, "speak")
    home.save_json("quota.json", st)
    home.append_jsonl("logs/said.jsonl", {
        "at": now.isoformat(timespec="seconds"), "text": text,
        "channel": channel, "source": "say", "reserved": reserved,
    })
    return {"ok": True, "text": text, "quota": st, "reserved": reserved}
