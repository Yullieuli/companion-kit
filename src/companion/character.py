"""性格层：从经历里沉淀出来的倾向。

三层的关系：

- ``mood``      几小时就衰减回去的情绪（今天开心 ≠ 明天也开心）
- ``opinion``   "我认为 X 是 Y" 的判断，天级累积
- ``character`` "我偏向 X" 的倾向，由大量经历聚合，周级演化

这一层的重点是**可追溯**：每条倾向都带着证据（哪几条 life / 哪几条 opinion
推出来的），随时能回答"她为什么变成这样"。这和大肥鱼那种"一次伤害永久改写
性格"是相反的设计——这里没有不可逆，只有缓慢累积和缓慢衰减。
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from .store import Home

STATE = "character.json"

# 出现次数够多才配叫"倾向"。低于这个数只是零星经历，不该塑形。
MIN_HITS = 3
# 低于这个强度不往 prompt 里写：写进去就是噪音。
RENDER_AT = 0.45
# 衰减半衰期（天）。45 天没碰，强度掉一半——兴趣会淡，但不消失。
HALF_LIFE_DAYS = 45.0

# 这些 tag 太泛，任何经历都会带上，靠它们聚不出性格。
NOISE_TAGS = {
    "b站", "bilibili", "聊天", "对话", "日常", "其他", "other", "misc",
    "生活", "随便", "闲聊",
}

_KIND_WEIGHT = {
    # 自己动手做出来的事，比"看到了"更能塑形
    "diy": 1.6,
    "code": 1.6,
    "watch": 1.0,
    "read": 1.0,
    "chat": 0.8,
    "other": 0.8,
}


def _path(home: Home) -> str:
    return STATE


def _now(now: datetime | None) -> datetime:
    return now or datetime.now()


def _parse(ts: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts))
    except Exception:
        return None


def load(home: Home) -> dict[str, Any]:
    try:
        data = home.read_json(_path(home)) or {}
    except Exception:
        data = {}
    if not isinstance(data.get("traits"), dict):
        data = {"traits": {}, "updated_at": ""}
    return data


def _save(home: Home, data: dict[str, Any]) -> None:
    home.save_json(_path(home), data)


def _clean_tag(tag: Any) -> str:
    t = str(tag or "").strip()
    if not t or len(t) > 12:
        return ""
    if t.lower() in NOISE_TAGS:
        return ""
    if not re.search(r"[\u4e00-\u9fa5A-Za-z]", t):
        return ""
    return t


def _freshness(days_ago: float) -> float:
    """指数衰减：越久没被强化，权重越低。"""
    if days_ago <= 0:
        return 1.0
    return math.pow(0.5, days_ago / HALF_LIFE_DAYS)


def rebuild(home: Home, *, days: float = 120.0, now: datetime | None = None) -> dict[str, Any]:
    """从 life + opinion 重新聚合性格。

    只读不写别人的数据；结果整体替换 ``character.json``。
    用"权重和"而不是"次数"：一次动手做的事（diy）比一次随手刷片更重。
    """
    from . import life as life_mod
    from . import opinion as opinion_mod

    now = _now(now)
    cutoff = now - timedelta(days=days)

    scores: dict[str, float] = defaultdict(float)
    evidence: dict[str, list[dict[str, str]]] = defaultdict(list)
    first_seen: dict[str, datetime] = {}
    last_seen: dict[str, datetime] = {}

    def bump(tag: str, weight: float, when: datetime | None, label: str) -> None:
        if not tag:
            return
        w = weight * _freshness(((now - when).total_seconds() / 86400.0) if when else 0.0)
        if w <= 0:
            return
        scores[tag] += w
        if len(evidence[tag]) < 5:
            evidence[tag].append({"what": label, "at": when.isoformat(timespec="minutes") if when else ""})
        if when:
            if tag not in first_seen or when < first_seen[tag]:
                first_seen[tag] = when
            if tag not in last_seen or when > last_seen[tag]:
                last_seen[tag] = when

    # ① 生活流水：看过什么、做过什么
    try:
        rows = life_mod.recent(home, days=days, limit=400, now=now)
    except Exception:
        rows = []
    for r in rows:
        at = _parse(r.get("at"))
        if at is None or at < cutoff:
            continue
        kind = str(r.get("kind") or "other").lower()
        weight = _KIND_WEIGHT.get(kind, 1.0)
        title = str(r.get("title") or "")[:40]
        for tag in (r.get("tags") or []):
            bump(_clean_tag(tag), weight, at, title)

    # ② 观点：成形了的判断，本身就该算进性格（她信什么，就是她是什么人）
    try:
        theses = opinion_mod.theses(home)
    except Exception:
        theses = []
    for o in theses:
        at = _parse(o.last_hit or o.at)
        # 置信度加权：0.5 的观点算半票，0.8 的算满票
        weight = 1.2 + float(o.confidence or 0.0) * 1.8
        for tag in (o.tags or []):
            bump(_clean_tag(tag), weight, at, str(o.claim or "")[:40])

    # ③ 和主人的聊天——人跟人交往是塑形最狠的部分，权重 1.4：
    #    比"看了一支片"重，比"自己动手做了一个东西"轻。
    #
    #    这里只做**匹配已知方向**，不做新词发现：从自由文本里抽话题要么上分词
    #    要么上模型，前者噪音大、后者花钱，而「聊到的恰好是她已经在意的东西」
    #    这个信号本身就足够强——反复聊到，说明这个方向在她生活里真的重要。
    known = {t for t, s in scores.items() if s > 0}
    if known:
        try:
            turns = home.read_jsonl("logs/turns.jsonl", limit=1000)
        except Exception:
            turns = []
        for tr in turns:
            if str(tr.get("role") or "") != "user":
                continue  # 只看主人说的话：她自己说了什么，life/opinion 已经记了
            said = str(tr.get("said") or "")
            at = _parse(tr.get("at"))
            if not said or at is None or at < cutoff:
                continue
            for tag in known:
                if tag and tag in said:
                    bump(tag, 1.4, at, "聊天：" + said[:32])

    traits: dict[str, Any] = {}
    for tag, score in scores.items():
        # 次数不够的，再高也不叫倾向
        if len(evidence[tag]) < MIN_HITS and score < MIN_HITS * 1.2:
            continue
        # 归一到 0~1：4 次普通经历 ≈ 0.55，8 次 ≈ 0.75
        strength = 1.0 - math.exp(-score / 3.2)
        if strength < 0.2:
            continue
        fs = first_seen.get(tag)
        ls = last_seen.get(tag)
        traits[tag] = {
            "strength": round(min(0.99, strength), 3),
            # 最近的排前面：evidence 是给人查"她最近为什么这样"用的，
            # 最早那条反而次要。
            "evidence": sorted(evidence[tag], key=lambda x: x.get("at") or "", reverse=True)[:5],
            "first_seen": fs.isoformat(timespec="minutes") if fs else "",
            "last_reinforced": ls.isoformat(timespec="minutes") if ls else "",
        }

    data = {"traits": traits, "updated_at": now.isoformat(timespec="minutes")}
    _save(home, data)
    return data


def decay(home: Home, *, now: datetime | None = None) -> int:
    """只做时间衰减，不重新聚合。返回被削弱的条数。

    定期跑一次即可：没有新证据的倾向会慢慢变淡，但不会被删——
    删掉就再也追不回"她曾经喜欢过这个"。
    """
    now = _now(now)
    data = load(home)
    traits = data.get("traits") or {}
    changed = 0
    for tag, t in list(traits.items()):
        last = _parse(t.get("last_reinforced"))
        if last is None:
            continue
        days = (now - last).total_seconds() / 86400.0
        if days < 7:  # 一周内不衰减，避免短期波动
            continue
        keep = math.pow(0.5, days / HALF_LIFE_DAYS)
        new = float(t.get("strength") or 0.0) * keep
        if abs(new - float(t.get("strength") or 0.0)) > 0.01:
            t["strength"] = round(new, 3)
            changed += 1
    if changed:
        data["updated_at"] = now.isoformat(timespec="minutes")
        _save(home, data)
    return changed


def maybe_rebuild(home: Home, *, hours: float = 6.0, now: datetime | None = None) -> bool:
    """数据太旧才重算，返回是否真的重算过。

    为什么不做成"每次对话前都 rebuild"：一次聚合要扫几十条 life + 全部 opinion，
    每轮都跑是浪费。为什么不做成 cron：她没有为性格单独开定时任务的必要，
    挂在对话入口上、按"距上次超过 6 小时"触发就够了——她一天聊几次，
    每次都会顺手把性格刷新一遍。
    """
    now = _now(now)
    data = load(home)
    last = _parse(data.get("updated_at"))
    if last is not None and (now - last).total_seconds() < hours * 3600:
        return False
    rebuild(home, now=now)
    return True


def traits(home: Home, *, min_strength: float = RENDER_AT) -> list[tuple[str, float, list[dict[str, str]]]]:
    """当前成形的倾向，强到弱。"""
    data = load(home)
    out: list[tuple[str, float, list[dict[str, str]]]] = []
    for tag, t in (data.get("traits") or {}).items():
        s = float(t.get("strength") or 0.0)
        if s >= min_strength:
            out.append((tag, s, list(t.get("evidence") or [])))
    out.sort(key=lambda x: -x[1])
    return out


def render(home: Home, *, min_strength: float = RENDER_AT, limit: int = 5,
           max_chars: int = 240) -> str:
    """注入 prompt 的一段话。

    只写"倾向"不写强度数字——数字是给人查的，不是给她念的。
    也不写来源，否则模型会把"你看过三支评测"当成台词说出来。

    两类倾向分开说：``taste.like`` 里的词是**具体对象**（某个 UP、某个系列），
    「常看谁」；其余是**方向**，「在意什么」。混在一起说会变成
    「你越来越在意极客湾」——像在念广告词。
    """
    rows = traits(home, min_strength=min_strength)[:limit]
    if not rows:
        return ""
    try:
        from . import bili as bili_mod

        likes = {str(w) for w in ((bili_mod.config(home).get("taste") or {}).get("like") or []) if w}
    except Exception:
        likes = set()

    fields = [n for n, _s, _e in rows if n not in likes]
    subjects = [n for n, _s, _e in rows if n in likes]

    parts: list[str] = []
    if fields:
        parts.append("你在意的方向：" + "、".join(fields[:4]))
    if subjects:
        parts.append("你常看的是：" + "、".join(subjects[:2]))
    return ("；".join(parts) + "。")[:max_chars]


def explain(home: Home, tag: str) -> dict[str, Any]:
    """某条倾向的完整来路。给人查的，不给模型看。"""
    data = load(home)
    t = (data.get("traits") or {}).get(tag)
    if not t:
        return {"tag": tag, "found": False}
    return {
        "tag": tag,
        "found": True,
        "strength": t.get("strength"),
        "first_seen": t.get("first_seen"),
        "last_reinforced": t.get("last_reinforced"),
        "evidence": t.get("evidence") or [],
    }


def stats(home: Home) -> dict[str, Any]:
    data = load(home)
    ts = data.get("traits") or {}
    strong = [k for k, v in ts.items() if float(v.get("strength") or 0) >= RENDER_AT]
    return {
        "total": len(ts),
        "rendered": len(strong),
        "top": sorted(((k, float(v.get("strength") or 0)) for k, v in ts.items()),
                      key=lambda x: -x[1])[:5],
        "updated_at": data.get("updated_at", ""),
    }

# ── 配置层覆盖（settings.json，缺省回落上面的常量）──────────────
from . import settings as _settings
HALF_LIFE_DAYS = _settings.number("character", "half_life_days", HALF_LIFE_DAYS)
