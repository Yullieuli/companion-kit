"""life：她自己的生活流水。

和 memory 的分工：
  · `life` 是**发生了什么**（今天看了什么、玩了什么、心情为什么变）—— 有时间、会过期
  · `memory` 是**沉淀下来的结论**（"他讨厌开放式结局"）—— 无时间、长期有效

一条 life 有三个字段决定它怎么被用：
  `shared`  已经跟他讲过了吗（没讲过的会推高"想说话"的分）
  `kind`    watch/play/read/make/think —— 决定渲染时的动词
  `tags`    用来和观点、记忆页对上号
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from .store import Home

FILE = "life/life.jsonl"
KIND_VERB = {
    "watch": "看了",
    "play": "玩了",
    "read": "读了",
    "make": "做了",
    "think": "想了",
    "meet": "遇到",
    "other": "经历了",
}


def _id(at: datetime, title: str) -> str:
    return hashlib.sha1(f"{at.isoformat()}|{title}".encode("utf-8")).hexdigest()[:12]


def record(
    home: Home,
    title: str,
    *,
    kind: str = "watch",
    detail: str = "",
    tags: list[str] | None = None,
    shared: bool = False,
    at: datetime | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    at = at or datetime.now()
    title = (title or "").strip()
    if not title:
        raise ValueError("title 不能为空")
    row = {
        "id": _id(at, title),
        "at": at.strftime("%Y-%m-%d %H:%M"),
        "epoch": at.timestamp(),
        "kind": kind if kind in KIND_VERB else "other",
        "title": title,
        "detail": (detail or "").strip(),
        "tags": sorted({*(tags or [])}),
        "shared": bool(shared),
    }
    if extra:
        # extra 只允许加字段，不许覆盖身份与语义字段。
        # 踩过：外部把 bvid 写成 id，整条记录的主键被顶掉，之后按 id 回填全部失效。
        for k, v in extra.items():
            if k not in ("id", "at", "epoch", "kind", "title", "shared"):
                row[k] = v
    # 同一分钟同一标题视为重复，不写
    for old in home.read_jsonl(FILE, limit=20):
        if old.get("id") == row["id"]:
            return old
    home.append_jsonl(FILE, row)
    return row


def recent(home: Home, *, days: float = 3, limit: int = 8) -> list[dict[str, Any]]:
    cut = (datetime.now() - timedelta(days=days)).timestamp()
    rows = [r for r in home.read_jsonl(FILE, limit=500) if float(r.get("epoch") or 0) >= cut]
    rows.sort(key=lambda r: float(r.get("epoch") or 0), reverse=True)
    return rows[:limit]


def unshared(home: Home, *, days: float = 2) -> list[dict[str, Any]]:
    return [r for r in recent(home, days=days, limit=30) if not r.get("shared")]


def update(home: Home, row_id: str, **patch: Any) -> dict[str, Any]:
    """改一条已有的记录。

    为什么需要它：她刷完 B 站先落一条（标题级的"我看了"），
    读完才补上自己的感受。要是再 append 一行，生活流水就会变成两条，
    而 `unshared` 会把同一件事算两遍，于是她又会重复讲。
    """
    rows = home.read_jsonl(FILE, limit=100000)
    target: dict[str, Any] | None = None
    for r in rows:
        if str(r.get("id")) == str(row_id):
            target = r
            break
    if target is None:
        return {}
    for k, v in patch.items():
        if k in ("id", "at", "epoch"):
            continue
        target[k] = v
    p = home.root / FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return target


def mark_shared(home: Home, ids: list[str]) -> int:
    """把若干条标成"已经跟他讲过了"。整文件重写 —— 量在千行内，简单比高效重要。"""
    if not ids:
        return 0
    want = set(ids)
    rows = home.read_jsonl(FILE, limit=100000)
    n = 0
    for r in rows:
        if r.get("id") in want and not r.get("shared"):
            r["shared"] = True
            n += 1
    if n:
        p = home.root / FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return n


def _lcs(a: str, b: str) -> int:
    """最长公共子串长度。O(len(a)*len(b))，两边都在几十字量级，够用。"""
    a, b = str(a or ""), str(b or "")
    if not a or not b:
        return 0
    best = 0
    prev = [0] * (len(b) + 1)
    for ch in a:
        cur = [0] * (len(b) + 1)
        for j, c2 in enumerate(b, 1):
            if ch == c2:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def match_shared(home: Home, said: str, *, days: float = 2, min_common: int = 3) -> list[str]:
    """她说了一句话，判断这句话讲的是哪几件「还没跟他说」的事。

    判据是**最长公共子串 ≥ 3 字**，不是词频重叠：
    标题往往是一长串（"刷B站热门，下载了'双人抬棺'…抽帧九宫格…"），
    按比例算阈值会高到永远匹配不上；而「双人抬棺」「极客湾」这种专名正好 3–4 字（定 4 会漏掉「极客湾」这类三字名）。
    一件都匹配不上就返回空——宁可下次再讲，也不要把没讲过的标成讲过了。
    """
    said = str(said or "")
    if len(said) < 4:
        return []
    hits: list[str] = []
    for r in unshared(home, days=days):
        title = str(r.get("title", ""))
        if _lcs(title, said) >= min_common:
            hits.append(str(r.get("id")))
    return hits


def ago(epoch: float, now: datetime | None = None) -> str:
    now = now or datetime.now()
    d = max(0.0, now.timestamp() - float(epoch or 0))
    if d < 3600:
        return f"{int(d // 60)} 分钟前"
    if d < 86400:
        return f"{int(d // 3600)} 小时前"
    return f"{int(d // 86400)} 天前"


def render(home: Home, *, days: float = 2, limit: int = 6, max_chars: int = 700, now: datetime | None = None) -> str:
    """给 context 的一段：带相对时间，因为她最容易在这里把"昨晚"说成"刚才"。"""
    rows = recent(home, days=days, limit=limit)
    if not rows:
        return ""
    now = now or datetime.now()
    lines = ["【你最近在做的事】"]
    used = 0
    for r in rows:
        verb = KIND_VERB.get(r.get("kind", "other"), "经历了")
        mark = "" if r.get("shared") else "（还没跟他说）"
        line = f"- {r['at'][11:16]}（{ago(r.get('epoch', 0), now)}）{verb}「{r['title']}」{mark}"
        if r.get("detail"):
            line += f"：{r['detail'][:60]}"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines) if len(lines) > 1 else ""
