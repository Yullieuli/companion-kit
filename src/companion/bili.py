"""bili：她自己刷 B 站。

这一层的边界（和 bilinet 的分工）：
  `bilinet` 负责联网、节流、熔断；`bili` 只负责**判断**——看哪条、要不要深读、
  值不值得给他、怎么记进生活流水。所有网络调用都从 `net` 参数进来，
  所以这个文件可以完全脱网单测。

三件事按顺序发生，每件都能单独失败：
  1. **挑**：五个入口（热门/排行/关注的动态/上一条的相关/她的观点关键词搜索）汇成候选池，
     去掉 72 小时内看过的、24 小时内给过的，再按"她自己写的偏好和观点"打分。
  2. **看**：只对选中的那条深读（字幕 + 弹幕热区 + 热评 + 封面，可选抽帧）。
     这一步是唯一花 token 的地方，所以一次只看一条。
  3. **记**：先落一条 life（`shared=False`），这样它自动推高"想跟他说话"的分。
     她要是给了自己的感受，就 `commit` 回填到同一条上——不新开一行，避免生活流水分裂。

为什么"看过的要去重"：她 10-26 重复推过同一期人工耳蜗。不是因为笨，
是因为新旧两套账本都记着，而挑片时只读了一套。现在只读 life 一处。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import events, fence, life, mood_rules, opinion, quota
from .store import Home

STATE = "bili/state.json"
CONFIG = "bili/config.json"
CACHE = "bili/search_cache.json"

BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")

# 给模型看的来源名，写英文她读起来要多想一步
SRC_LABEL = {
    "popular": "热门", "ranking": "排行", "related": "上一条的相关",
    "dynamic": "关注的人", "search": "按你的口味搜的", "": "推荐",
}

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "max_per_day": 4,
    "min_gap_min": 90,
    "min_score": 2,
    "duration": {"min_min": 3, "max_min": 45},
    "feeds": ["popular", "ranking", "related", "dynamic"],
    "deep_read_chars": 1200,
    "frames": "auto",          # auto = 只有深读且时长 ≥8 分钟才抽帧（贵、慢）
    "search_cache_min": 10,    # 同一关键词 10 分钟内复用（读额度只有 3000/天）
    "share": {"max_per_day": 2, "min_score": 13, "same_days": 7, "via": "qq"},
    # 迁移期用：旧 bili-whale 插件的账本还活着，只读它、不写它。
    # 不读就会重演"同一期视频推两次"；写了就成了双写。等旧插件下线，删掉这个键即可。
    "legacy_life": [],
    "taste": {
        "like": ["极客湾", "影之刃", "独立游戏", "纪录片", "手工", "科普", "拆解", "评测"],
        "dislike": ["营销号", "标题党", "擦边", "搬运", "切片", "合集"],
    },
}

DEFAULT_STATE: dict[str, Any] = {
    "day": "",
    "watched_today": 0,
    "last_at": 0.0,
    "last_bvid": "",
    "served": {},     # bvid -> {at, title}，24h 内不重复给
    "shares": {},     # bvid -> "YYYY-MM-DD"，7 天内不重复分享
    "share_day": "",
    "share_today": 0,
}


# ── 状态与配置 ────────────────────────────────────────────────────────────────

def load(home: Home) -> dict[str, Any]:
    st = home._json(STATE, DEFAULT_STATE)  # noqa: SLF001 — store 的唯一出口，这里复用它的坏 JSON 回落
    out = {**DEFAULT_STATE, **st}
    today = date.today().isoformat()
    if out.get("day") != today:
        out["day"] = today
        out["watched_today"] = 0
    if out.get("share_day") != today:
        out["share_day"] = today
        out["share_today"] = 0
    return out


def save(home: Home, st: dict[str, Any]) -> None:
    home.save_json(STATE, st)


def daily_used(home: Home, name: str, *, st: dict[str, Any] | None = None,
               now: datetime | None = None) -> int:
    """今天的刷片/分享次数，唯一来源是 quota.json。

    迁移：老状态文件里今天已经记了数，第一次读时搬进 quota 一次，免得当天重置成 0
    让她多刷一轮。
    """
    now = now or datetime.now()
    q = home.quota()
    st = load(home) if st is None else st
    today = now.date().isoformat()
    if name == "bili":
        seed, day = int(st.get("watched_today") or 0), str(st.get("day") or "")
    else:
        seed, day = int(st.get("share_today") or 0), str(st.get("share_day") or "")
    if seed > 0 and day == today:
        q2 = quota.bucket_seed(q, name, seed, now)
        if q2 != q:
            home.save_json("quota.json", q2)
            q = q2
    return quota.bucket_used(q, name, now)


def record_used(home: Home, name: str, now: datetime | None = None) -> int:
    now = now or datetime.now()
    st = quota.bucket_record(home.quota(), name, now)
    home.save_json("quota.json", st)
    return quota.bucket_used(st, name, now)


def config(home: Home) -> dict[str, Any]:
    cfg = {**DEFAULT_CONFIG, **home._json(CONFIG, DEFAULT_CONFIG)}  # noqa: SLF001
    # taste / duration / share 是嵌套字典，缺字段要补默认，不能整体替换
    for key in ("duration", "share", "taste"):
        cfg[key] = {**DEFAULT_CONFIG[key], **(cfg.get(key) or {})}
    return cfg


def save_config(home: Home, cfg: dict[str, Any]) -> None:
    home.save_json(CONFIG, cfg)


# ── 归一化 ────────────────────────────────────────────────────────────────────

def _seconds(value: Any) -> int:
    """时长可能是 123（秒）、"12:34"、"1:02:03"。"""
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value or "").strip()
    if not s:
        return 0
    if s.isdigit():
        return int(s)
    parts = [p for p in s.split(":") if p != ""]
    try:
        sec = 0
        for p in parts:
            sec = sec * 60 + int(float(p))
        return sec
    except Exception:
        return 0


def normalize(item: dict[str, Any], src: str = "") -> dict[str, Any]:
    """把五个入口的不同结构拍成一种。缺 bvid 的直接返回 {}。"""
    bvid = str(item.get("bvid") or item.get("bv_id") or "")
    if not bvid.startswith("BV"):
        return {}
    owner = item.get("owner")
    up = (owner.get("name") if isinstance(owner, dict) else owner) or item.get("author") or ""
    stat = item.get("stat") or {}
    dur = _seconds(item.get("duration") or item.get("duration_text") or item.get("length"))
    return {
        "bvid": bvid,
        "title": re.sub(r"\s+", " ", str(item.get("title") or "")).strip(),
        "up": str(up or "").strip(),
        "duration": dur,
        "view": int(stat.get("view") or stat.get("play") or item.get("play") or 0),
        "danmaku": int(stat.get("danmaku") or item.get("video_review") or 0),
        "pubdate": int(item.get("pubdate") or item.get("senddate") or 0),
        "desc": re.sub(r"\s+", " ", str(item.get("desc") or "")).strip()[:300],
        "src": src or str(item.get("_src") or ""),
    }


def _core(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]", "", str(text or ""))


def grams(text: str, n: int = 3) -> set[str]:
    core = _core(text)
    return {core[i:i + n] for i in range(len(core) - n + 1)} if len(core) >= n else set()


# ── 看过什么（唯一账本：life）─────────────────────────────────────────────────

def _legacy_watched(paths: list[str], *, hours: float,
                    now: datetime) -> tuple[set[str], set[str], set[str]]:
    """只读旧账本（bili-whale 的 life.jsonl / own_life.json）。

    这是迁移期的桥，不是设计。它解决一个很具体的问题：
    旧址记着「今天 14:39 看过小米电视」，新址不知道，于是又推一遍同一期。
    读两份、写一份 —— 写永远只有 life 一处。
    """
    import json

    cut = now.timestamp() - hours * 3600
    bvids: set[str] = set()
    frags: set[str] = set()
    cores: set[str] = set()
    for raw in paths or []:
        p = Path(str(raw)).expanduser()
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        rows: list[dict[str, Any]] = []
        if p.suffix == ".jsonl":
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
        else:
            try:
                rows = list((json.loads(text) or {}).get("history") or [])
            except Exception:
                rows = []
        for r in rows:
            ts = str(r.get("ts") or r.get("at") or "")
            ep = float(r.get("epoch") or 0)
            if not ep and ts:
                for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
                    try:
                        ep = datetime.strptime(ts[:19], fmt).timestamp()
                        break
                    except Exception:
                        continue
            if ep < cut:
                continue
            blob = f"{r.get('title') or ''} {r.get('text') or ''} {r.get('detail') or ''}"
            m = BV_RE.search(blob)
            if m:
                bvids.add(m.group(0))
            body = _core(blob.replace(m.group(0), "") if m else blob)
            frags |= grams(body)
            if body:
                cores.add(body)
    return bvids, frags, cores


def watched(home: Home, *, hours: float = 72, now: datetime | None = None,
            cfg: dict[str, Any] | None = None) -> tuple[set[str], set[str], set[str]]:
    """返回 (看过的 bvid, 看过的标题 3-gram)。

    主账本是 life 一处。迁移期再并上配置里列的旧账本（只读）。
    """
    now = now or datetime.now()
    cut = now.timestamp() - hours * 3600
    bvids: set[str] = set()
    frags: set[str] = set()
    cores: set[str] = set()
    for r in home.read_jsonl("life/life.jsonl", limit=100000):
        if float(r.get("epoch") or 0) < cut:
            continue
        blob = f"{r.get('title') or ''} {r.get('detail') or ''}"
        bv = str(r.get("bvid") or "")
        if not bv.startswith("BV"):
            m = BV_RE.search(blob)
            bv = m.group(0) if m else ""
        if bv.startswith("BV"):
            bvids.add(bv)
        body = _core(blob.replace(bv, ""))
        frags |= grams(body)
        if body:
            cores.add(body)
    legacy = (cfg or {}).get("legacy_life") or []
    if legacy:
        lb, lf, lc = _legacy_watched(legacy, hours=hours, now=now)
        bvids |= lb
        frags |= lf
        cores |= lc
    return bvids, frags, cores


def looks_watched(title: str, frags: set[str], need: int = 3,
                  cores: set[str] | None = None) -> bool:
    """标题像不像"已经看过的那一条"。

    阈值定 3 是权衡过的：
      2 太松 —— "影之刃零 新预告"会因为旧记录里有"影之刃零评测"而被挡，
                等于把她最喜欢的方向封三天（实测踩到过）
      4 太紧 —— 旧记录只有一句描述，长标题凑不满 4 个共同 3-gram
    真正的同一期视频有大量重合，3 足够；同题不同片只重合 2。
    """
    core = _core(title)
    # 短标题（4–6 字，比如「双人抬棺」）凑不出 3 个共同片段，
    # 但只要整串被看过的那句描述包含，就是同一条。长标题不走这条，免得误伤。
    if cores and 4 <= len(core) <= 12 and any(core in c or c in core for c in cores):
        return True
    if not frags:
        return False
    return len(grams(title) & frags) >= need


def pending_share(home: Home, *, days: float = 3, now: datetime | None = None) -> int:
    """看完还没给他的条数。这是"分享"这个动作唯一诚实的燃料。"""
    now = now or datetime.now()
    cut = now.timestamp() - days * 86400
    return sum(
        1 for r in home.read_jsonl("life/life.jsonl", limit=100000)
        if r.get("bvid") and not r.get("shared") and float(r.get("epoch") or 0) >= cut
    )


# ── 打分 ──────────────────────────────────────────────────────────────────────

def opinion_bonus(home: Home, blob: str) -> int:
    """她自己写过的方向，命中就加分。

    只认 tags，不做主张全文匹配：主张里全是"节目""时候"这类虚词，2-gram 会乱命中。
    """
    bonus = 0
    for op in opinion.theses(home):
        if op.confidence < 0.25:
            continue
        if not any(str(t).lower() and str(t).lower() in blob for t in op.tags):
            continue
        bonus += 3 if op.confidence >= 0.70 else 2
    return min(bonus, 6)


def hype_penalty(title: str) -> int:
    """标题党信号：emoji 堆砌、连串感叹号、全大写吼叫。

    为什么需要它：关键词打分只看"讲了什么"，不看"怎么讲的"。
    实测踩到：一条"炮轰AI科普乱象！😅🤏🤪"因为含"科普"被选中，
    点进去是三十秒情绪输出。这种标题有稳定特征，用便宜的规则挡掉。
    """
    t = str(title or "")
    emoji = sum(1 for ch in t if ord(ch) > 0x1F000 or 0x2600 <= ord(ch) <= 0x27BF)
    n = 0
    if emoji >= 2:
        n -= 2
    if "！！" in t or "!!" in t or "??" in t or "？？" in t:
        n -= 1
    letters = [c for c in t if c.isascii() and c.isalpha()]
    if len(letters) >= 8 and all(c.isupper() for c in letters):
        n -= 1
    return n


def score(item: dict[str, Any], cfg: dict[str, Any], home: Home) -> int:
    blob = f"{item.get('title', '')} {item.get('up', '')}".lower()
    n = 0
    for w in cfg["taste"].get("like") or []:
        if str(w).lower() and str(w).lower() in blob:
            n += 3
    for w in cfg["taste"].get("dislike") or []:
        if str(w).lower() and str(w).lower() in blob:
            n -= 5
    dur = int(item.get("duration") or 0)
    lo = float(cfg["duration"].get("min_min") or 0) * 60
    hi = float(cfg["duration"].get("max_min") or 0) * 60
    if lo <= dur <= hi:
        n += 2
    elif dur and dur < lo:
        n -= 1
    n += opinion_bonus(home, blob)
    n += hype_penalty(str(item.get("title") or ""))
    return n


def her_views(home: Home, limit: int = 3) -> str:
    """挑片前把她自己的看法摆出来 —— 让她自己判断，而不是靠字符串算分。"""
    ops = [o for o in opinion.theses(home) if o.confidence >= 0.25]
    ops.sort(key=lambda o: o.confidence, reverse=True)
    lines = []
    for o in ops[:limit]:
        lead = "你一直这么想" if o.confidence >= 0.70 else "你隐约觉得"
        lines.append(f"- {lead}：{o.claim}")
    return "\n".join(lines)


# ── 候选池 ────────────────────────────────────────────────────────────────────

def cached_search(home: Home, net: Any, keyword: str, *, cfg: dict[str, Any],
                  now: datetime | None = None) -> tuple[list[dict[str, Any]], bool]:
    """搜过的关键词 10 分钟内复用。返回 (结果, 是否命中)。

    为什么值得做：读额度只有 3000/天，而"按观点搜索"是唯一会连搜的入口。
    她一天问十几次"该刷什么"，同一句话不该打十几次接口。
    """
    cfg = cfg or config(home)
    now = now or datetime.now()
    ttl = float(cfg.get("search_cache_min") or 10) * 60
    d = home._json(CACHE, {})  # noqa: SLF001
    ent = d.get(keyword) or {}
    at = float(ent.get("at") or 0)
    if at and (now.timestamp() - at) < ttl and ent.get("items"):
        return list(ent["items"]), True
    items = net.search(keyword)
    d[keyword] = {"at": now.timestamp(), "items": list(items)[:30]}
    # 只留最近 20 个关键词、一天内的：这不是档案，是缓存
    d = {k: v for k, v in sorted(d.items(), key=lambda kv: float((kv[1] or {}).get("at") or 0))
         if now.timestamp() - float((v or {}).get("at") or 0) < 86400}
    home.save_json(CACHE, dict(list(d.items())[-20:]))
    return items, False


def gather(net: Any, cfg: dict[str, Any], *, last_bvid: str = "",
           keywords: list[str] | None = None, home: Home | None = None,
           now: datetime | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """按配置拉几个入口。单个入口失败不影响其它 —— 只记一句 why。"""
    out: list[dict[str, Any]] = []
    notes: list[str] = []
    feeds = cfg.get("feeds") or []
    if "popular" in feeds:
        try:
            out += [normalize(x, "popular") for x in net.popular()]
        except Exception as e:
            notes.append(f"热门失败:{type(e).__name__}")
    if "ranking" in feeds:
        try:
            out += [normalize(x, "ranking") for x in net.ranking()]
        except Exception as e:
            notes.append(f"排行失败:{type(e).__name__}")
    if "related" in feeds and last_bvid:
        try:
            out += [normalize(x, "related") for x in net.related(last_bvid)]
        except Exception as e:
            notes.append(f"相关失败:{type(e).__name__}")
    if "dynamic" in feeds:
        try:
            out += [normalize(x, "dynamic") for x in net.dynamics()]
        except Exception as e:
            notes.append(f"动态失败:{type(e).__name__}")
    for kw in (keywords or [])[:1]:  # 搜索一次就够，多了会烧读额度
        try:
            if home is None:
                hits = net.search(kw)
            else:
                hits, cached = cached_search(home, net, kw, cfg=cfg, now=now)
                if cached:
                    notes.append("搜索命中缓存")
            out += [normalize(x, "search") for x in hits]
        except Exception as e:
            notes.append(f"搜索失败:{type(e).__name__}")
    return [x for x in out if x], notes


def pick(items: list[dict[str, Any]], home: Home, st: dict[str, Any], cfg: dict[str, Any], *,
         now: datetime | None = None) -> dict[str, Any]:
    """去重 → 打分 → 选一条。选不出来就诚实地说选不出来。"""
    now = now or datetime.now()
    seen_bv, seen_frag, seen_cores = watched(home, hours=72, now=now, cfg=cfg)
    served_cut = now.timestamp() - 24 * 3600
    fresh: list[dict[str, Any]] = []
    for it in items:
        if it["bvid"] in seen_bv or it["bvid"] in st.get("served", {}):
            continue
        if float((st.get("served", {}).get(it["bvid"]) or {}).get("at") or 0) > served_cut:
            continue
        if looks_watched(it["title"], seen_frag, cores=seen_cores):
            continue
        it = {**it, "score": score(it, cfg, home)}
        fresh.append(it)
    if not fresh:
        return {"ok": False, "why": "池子里全是最近看过的"}

    # 分高者优先；同分选短的（便宜，且她其实更容易看完）
    fresh.sort(key=lambda x: (x["score"], -int(x.get("duration") or 0)), reverse=True)
    best = fresh[0]
    if best["score"] < int(cfg.get("min_score") or 0):
        return {"ok": False, "why": f"没有一条够想看的（最高 {best['score']} 分：{best['title'][:30]}）",
                "top": fresh[:3]}
    return {"ok": True, "item": best, "top": fresh[:5]}


# ── 深读 ──────────────────────────────────────────────────────────────────────

def _mask(text: str, names: list[str]) -> str:
    for n in names:
        if n and len(str(n)) >= 2:
            text = text.replace(str(n), "主人")
    return text


def _wrap_cjk(text: str, width: int = 90) -> str:
    """AI 字幕没有标点，一整段 1200 字读起来是墙。按标点断，没标点就硬折。"""
    text = str(text or "").strip()
    if not text:
        return ""
    parts = re.split(r"(?<=[。！？!?；;])", text)
    lines: list[str] = []
    buf = ""
    for p in parts:
        while len(p) > width:
            if buf:
                lines.append(buf)
                buf = ""
            lines.append(p[:width])
            p = p[width:]
        if len(buf) + len(p) > width:
            lines.append(buf)
            buf = p
        else:
            buf += p
    if buf:
        lines.append(buf)
    return "\n".join(lines)


def _external(text: str, names: list[str]) -> tuple[str, int]:
    """外部文本两道处理：去零宽 + 认劫持句式（fence），再把主人的昵称打码。"""
    clean, hits = fence.sanitize(text)
    return _mask(clean, names), len(hits)


def debrief(home: Home, bvid: str, *, net: Any, cfg: dict[str, Any] | None = None,
            frames: bool | None = None, names: list[str] | None = None) -> dict[str, Any]:
    """把一条视频读成一段文字。返回结构给模型看，不是给日志看。"""
    cfg = cfg or config(home)
    names = [n for n in (names or []) if n]
    v = net.view(bvid)
    aid = int(v.get("aid") or 0)
    pages = v.get("pages") or [{}]
    cid = int(pages[0].get("cid") or v.get("cid") or 0)
    duration = _seconds(v.get("duration"))
    title = _mask(re.sub(r"\s+", " ", str(v.get("title") or "")), names)
    up = str((v.get("owner") or {}).get("name") or "")
    stat = v.get("stat") or {}
    hits = 0

    plain, sub_src, _items = ("", "none", [])
    if cid:
        try:
            plain, sub_src, _items = net.subtitles(bvid, cid)
        except Exception as e:
            sub_src = f"error:{type(e).__name__}"
    dm = net.danmaku(cid) if cid else []
    hot = net.comments(aid, 8) if aid else []

    want_frames = frames if frames is not None else (
        cfg.get("frames") == "always" or (cfg.get("frames") == "auto" and duration >= 480)
    )
    frame_path = ""
    if want_frames and cid:
        try:
            frame_path = net.frames(bvid, cid, duration, 3)
        except Exception as e:
            frame_path = f"error:{e}"

    desc, h = _external(str(v.get("desc") or "")[:300], names)
    hits += h
    excerpt, h = _external(plain[: int(cfg.get("deep_read_chars") or 1200)], names)
    hits += h

    lines = [
        fence.BANNER.format(source="B 站"),
        "",
        f"《{title}》 · UP {up} · {duration // 60}分{duration % 60}秒 · "
        f"{SRC_LABEL.get(str(cfg.get('_src') or ''), '推荐')}",
        f"播放 {stat.get('view', 0)} 赞 {stat.get('like', 0)} 收藏 {stat.get('favorite', 0)} "
        f"弹幕 {stat.get('danmaku', 0)} 评论 {stat.get('reply', 0)}",
    ]
    if desc:
        lines.append(f"简介:{desc}")
    if excerpt:
        lines += ["", "【字幕节选】", _wrap_cjk(excerpt)]
    else:
        lines.append("（没字幕，只能靠画面和弹幕）")
    if dm:
        lines += ["", "【观众最炸的几段】"]
        for b in dm:
            sample, h = _external(" / ".join(b["samples"]), names)
            hits += h
            lines.append(f"  {b['at']} {b['count']}条：{sample}")
    if hot:
        lines += ["", "【热评】"]
        for c in hot:
            text, h = _external(c["text"], names)
            hits += h
            lines.append(f"  [{c['like']}赞] {c['author']}：{text}")
    views = her_views(home)
    if views:
        lines += ["", "【你自己以前说过的】", views]
    if frame_path:
        lines += ["", f"【抽帧】{frame_path}（用 vision 看这张九宫格，九格按时间顺序）"]

    return {
        "bvid": bvid, "aid": aid, "cid": cid, "title": title, "up": up,
        "duration": duration, "desc": desc,
        "stats": {k: int(stat.get(k) or 0) for k in ("view", "like", "favorite", "coin", "danmaku", "reply")},
        "subtitle_source": sub_src, "subtitle_chars": len(plain),
        "subtitle_excerpt": excerpt, "danmaku_hot": dm, "hot_comments": hot,
        "cover": net.cover(bvid, str(v.get("pic") or "")),
        "frame_grid": frame_path, "fenced": hits,
        "text": "\n".join(lines),
    }


# ── 记一笔 ────────────────────────────────────────────────────────────────────

def _life_title(item: dict[str, Any]) -> str:
    dur = int(item.get("duration") or 0)
    tail = f"，{dur // 60}分{dur % 60}秒" if dur else ""
    return f"刷B站，看了《{item['title']}》（UP：{item.get('up') or '未知'}{tail}）"


def mark_served(st: dict[str, Any], item: dict[str, Any], now: datetime) -> None:
    served = {k: v for k, v in (st.get("served") or {}).items()
              if now.timestamp() - float((v or {}).get("at") or 0) < 3 * 86400}
    served[item["bvid"]] = {"at": now.timestamp(), "title": item["title"][:80]}
    st["served"] = served


def commit(home: Home, bvid: str, *, summary: str = "", rating: int | None = None,
           tags: list[str] | None = None, now: datetime | None = None) -> dict[str, Any]:
    """她看完之后回填自己的感受。改的是同一条 life，不新开一行。"""
    now = now or datetime.now()
    rows = [r for r in home.read_jsonl("life/life.jsonl", limit=100000)
            if str(r.get("bvid") or "") == bvid]
    if not rows:
        return {"ok": False, "why": "没找到这条观看记录（先 tick）"}
    row = rows[-1]
    patch: dict[str, Any] = {}
    if summary.strip():
        patch["detail"] = summary.strip()[:300]
    if rating is not None:
        patch["rating"] = max(0, min(10, int(rating)))
    if tags:
        patch["tags"] = sorted({*(row.get("tags") or []), *tags})
    updated = life.update(home, str(row.get("id")), **patch) if patch else row
    return {"ok": True, "life": updated, "patched": sorted(patch)}


# ── 分享 ──────────────────────────────────────────────────────────────────────

def share_gate(home: Home, bvid: str, *, score_v: int, reason: str, rank: int = 1,
               cfg: dict[str, Any] | None = None, now: datetime | None = None) -> dict[str, Any]:
    """分享前过闸。三条：够好（评分）、说得出为什么、今天还没超。

    为什么要"理由"：她分享的是**自己的判断**，不是链接搬运。写不出理由就说明
    她自己也没想清楚，那就先别发。
    """
    cfg = cfg or config(home)
    s = cfg["share"]
    st = load(home)
    now = now or datetime.now()
    if int(score_v) < int(s.get("min_score") or 13):
        return {"ok": False, "why": f"评分 {score_v} 不够（要 ≥{s['min_score']}）"}
    if len(str(reason).strip()) < 4:
        return {"ok": False, "why": "说不出为什么好，先别发"}
    if int(rank) != 1:
        return {"ok": False, "why": "只分享排第一的那条"}
    used = daily_used(home, "share", st=st, now=now)
    if used >= int(s.get("max_per_day") or 2):
        return {"ok": False, "why": f"今天已经分享 {used} 条了"}
    last = st.get("shares", {}).get(bvid)
    if last:
        try:
            days = (now.date() - date.fromisoformat(str(last))).days
            if days < int(s.get("same_days") or 7):
                return {"ok": False, "why": f"这条 {days} 天前发过了"}
        except Exception:
            pass
    return {"ok": True, "score": int(score_v), "reason": str(reason).strip()}


def share(home: Home, bvid: str, text: str, *, score_v: int, reason: str, rank: int = 1,
          via: str = "", to_mid: int | None = None, net: Any = None,
          cfg: dict[str, Any] | None = None, now: datetime | None = None) -> dict[str, Any]:
    """分享给主人。via='dm' 走 B 站私信（唯一不进影子审核的通道），其余交给宿主通道。"""
    cfg = cfg or config(home)
    now = now or datetime.now()
    gate = share_gate(home, bvid, score_v=score_v, reason=reason, rank=rank, cfg=cfg, now=now)
    if not gate["ok"]:
        return {"ok": False, "why": gate["why"], "stage": "gate"}
    via = via or str(cfg["share"].get("via") or "qq")
    sent: dict[str, Any] = {}
    if via == "dm":
        if net is None:
            return {"ok": False, "why": "没给 net，发不了私信", "stage": "net"}
        if not to_mid:
            return {"ok": False, "why": "没给 to_mid（主人的 B 站 mid）", "stage": "target"}
        try:
            sent = net.send_dm(int(to_mid), text)
        except Exception as e:
            return {"ok": False, "why": f"{type(e).__name__}: {e}", "stage": "send"}

    st = load(home)
    st["shares"] = {**(st.get("shares") or {}), bvid: now.date().isoformat()}
    st["shares"] = {k: v for k, v in st["shares"].items()
                    if _within_days(v, now, 60)}
    st["share_today"] = record_used(home, "share", now)
    save(home, st)
    life.mark_shared(home, [str(r.get("id")) for r in home.read_jsonl("life/life.jsonl", limit=100000)
                            if str(r.get("bvid") or "") == bvid])
    events.emit(home, "bili_shared", now, bvid=bvid, via=via, chars=len(text))
    try:
        mood_rules.on_shared(home, now=now)
    except Exception:
        pass
    return {"ok": True, "via": via, "sent": sent, "score": int(score_v), "reason": reason}


def _within_days(day: str, now: datetime, days: int) -> bool:
    try:
        return (now.date() - date.fromisoformat(str(day))).days <= days
    except Exception:
        return False


# ── 一轮 ──────────────────────────────────────────────────────────────────────

def due(home: Home, *, cfg: dict[str, Any] | None = None, now: datetime | None = None) -> dict[str, Any]:
    """零成本判断"现在该不该刷"。宿主每小时都能问，不花钱。"""
    cfg = cfg or config(home)
    now = now or datetime.now()
    st = load(home)
    if not cfg.get("enabled", True):
        return {"due": False, "why": "开关关着"}
    used = daily_used(home, "bili", st=st, now=now)
    if used >= int(cfg.get("max_per_day") or 4):
        return {"due": False, "why": f"今天已经刷了 {used} 条"}
    gap = now.timestamp() - float(st.get("last_at") or 0)
    if gap < float(cfg.get("min_gap_min") or 90) * 60:
        left = int((float(cfg["min_gap_min"]) * 60 - gap) / 60)
        return {"due": False, "why": f"刚看过，还要等 {max(1, left)} 分钟"}
    return {"due": True, "why": ""}


def tick(home: Home, *, net: Any = None, cfg: dict[str, Any] | None = None,
         now: datetime | None = None, frames: bool | None = None,
         names: list[str] | None = None, force: bool = False) -> dict[str, Any]:
    """刷一轮：挑 → 读 → 记。返回给宿主的结构，不是给人看的日志。"""
    home.init()
    cfg = cfg or config(home)
    now = now or datetime.now()
    if not force:
        d = due(home, cfg=cfg, now=now)
        if not d["due"]:
            return {"ok": False, "stage": "gate", "why": d["why"]}

    st = load(home)
    keywords = [t for o in opinion.theses(home) if o.confidence >= 0.6 for t in o.tags][:1]
    if net is None:
        from .bilinet import Net
        net = Net(home.root)

    try:
        items, notes = gather(net, cfg, last_bvid=str(st.get("last_bvid") or ""),
                              keywords=keywords, home=home, now=now)
    except Exception as e:
        return {"ok": False, "stage": "gather", "why": f"{type(e).__name__}: {e}"}
    if not items:
        return {"ok": False, "stage": "gather", "why": "一个入口都没拿到", "notes": notes}

    chosen = pick(items, home, st, cfg, now=now)
    if not chosen["ok"]:
        return {"ok": False, "stage": "pick", "why": chosen["why"],
                "top": chosen.get("top", []), "notes": notes}

    item = chosen["item"]
    item["_src"] = item.get("src", "")
    cfg = {**cfg, "_src": item.get("src", "")}
    try:
        mat = debrief(home, item["bvid"], net=net, cfg=cfg, frames=frames, names=names)
    except Exception as e:
        return {"ok": False, "stage": "debrief", "why": f"{type(e).__name__}: {e}",
                "item": item, "notes": notes}

    row = life.record(
        home, _life_title(item), kind="watch",
        detail="", tags=["b站"],
        at=now, extra={"bvid": item["bvid"], "score": item["score"], "src": item.get("src", "")},
    )
    mark_served(st, item, now)
    # 真正的计数在 quota.json；state 里这两个字段只是给 status 看的镜像，
    # 镜像丢了不影响限额判断（反过来才会）。
    st["watched_today"] = record_used(home, "bili", now)
    st["last_at"] = now.timestamp()
    st["last_bvid"] = item["bvid"]
    save(home, st)
    events.emit(home, "bili_watch", now, bvid=item["bvid"], title=item["title"][:60],
                score=item["score"], minutes=int(item.get("duration") or 0) // 60)
    try:
        mood_rules.on_watched(home, int(item["score"]), now=now)
    except Exception:
        pass

    return {
        "ok": True,
        "bvid": item["bvid"],
        "title": mat["title"],
        "up": mat["up"],
        "duration": mat["duration"],
        "score": item["score"],
        "why": f"{SRC_LABEL.get(str(item.get('src') or ''), '推荐')}里挑的，偏好分 {item['score']}",
        "life_id": row.get("id"),
        "url": f"https://www.bilibili.com/video/{item['bvid']}",
        "materials": mat["text"],
        "cover": mat["cover"],
        "frame_grid": mat["frame_grid"],
        "fenced": mat["fenced"],
        "watched_today": st["watched_today"],
        "notes": notes,
        "hint": "读完了用 bili commit 回填一句自己的感受；觉得他会喜欢就用 bili share。",
    }


def status(home: Home, *, now: datetime | None = None) -> dict[str, Any]:
    cfg = config(home)
    st = load(home)
    now = now or datetime.now()
    d = due(home, cfg=cfg, now=now)
    shares = st.get("shares") or {}
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "due": d["due"],
        "why": d["why"],
        "watched_today": daily_used(home, "bili", st=st, now=now),
        "max_per_day": int(cfg.get("max_per_day") or 4),
        "last_at": datetime.fromtimestamp(float(st.get("last_at") or 0)).isoformat(timespec="minutes")
        if st.get("last_at") else "",
        "last_bvid": st.get("last_bvid") or "",
        "share_today": daily_used(home, "share", st=st, now=now),
        "shared_recent": sorted(shares.items(), key=lambda kv: kv[1], reverse=True)[:5],
        "taste": cfg.get("taste"),
    }
