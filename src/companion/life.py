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
import re
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


def recent(home: Home, *, days: float = 3, limit: int = 8,
           now: datetime | None = None) -> list[dict[str, Any]]:
    """最近几天的事。

    `now` 必须能传进来：`render(now=X)` 名义上以 X 为基准，
    但这里原来写死 `datetime.now()`，于是「两天窗口」按真实时间算，
    和渲染用的 X 不是同一个时刻。测试里用固定日期就会在跨过窗口的那一刻
    突然变红（2026-10-29 20:05 实测：10-27 20:00 的记录刚好 48.1 小时，
    测试从绿变红，代码一行没动）。跨天边界少给一条、多给一条，也是这个来路。
    """
    base = now or datetime.now()
    cut = (base - timedelta(days=days)).timestamp()
    rows = [r for r in home.read_jsonl(FILE, limit=500) if float(r.get("epoch") or 0) >= cut]
    rows.sort(key=lambda r: float(r.get("epoch") or 0), reverse=True)
    return rows[:limit]


def unshared(home: Home, *, days: float = 2, now: datetime | None = None) -> list[dict[str, Any]]:
    return [r for r in recent(home, days=days, limit=30, now=now) if not r.get("shared")]


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
        rid = r.get("id")
        # 没 id 的记录标不上（mark_shared 按 id 回填）。历史上外部写进来的
        # 行可能缺 id —— 与其返回字符串 "None" 让 mark_shared 空转，不如跳过。
        if not rid:
            continue
        if _lcs(title, said) >= min_common:
            hits.append(str(rid))
    return hits


def ago(epoch: float, now: datetime | None = None) -> str:
    now = now or datetime.now()
    d = max(0.0, now.timestamp() - float(epoch or 0))
    if d < 3600:
        return f"{int(d // 60)} 分钟前"
    if d < 86400:
        return f"{int(d // 3600)} 小时前"
    return f"{int(d // 86400)} 天前"


_BILI_NOISE = re.compile(r"[（(]\s*UP[:：].*?[)）]")
_BV = re.compile(r"BV[0-9A-Za-z]{10}")
# 外部灌进来的前缀（bili 抓取写的，不是她会说的话）
_PREFIXES = ("刷B站热门，下载了", "刷B站，看了", "刷B站热门，下了", "刷B站热门,下载了")


def core_title(title: str) -> str:
    """标题只留「她看了什么」。

    她自己落 life 时写的是 `刷B站，看了《X》（UP：极客湾Geekerwan，12分37秒）`。
    原样喂回去，她就会把「UP：极客湾Geekerwan，12分37秒」当成内容讲出来 ——
    2026-10-29 她把六条标题里的词缝成一句「显卡跌了价还看什么50系」，
    就是因为她手里只有标题字符串，没有内容。
    """
    t = str(title or "").strip()
    m = re.search(r"《([^》]{2,})》", t)
    if m:
        t = m.group(1)
    t = _BILI_NOISE.sub("", t).strip()
    for pre in _PREFIXES:
        if t.startswith(pre):
            t = t[len(pre):]
            break
    return t.strip(" ，,。：:")[:80]


_ADVICE = re.compile(r"[，,；;]?\s*[^，,；;。]{0,8}(?:建议|推荐)(?:买|入手|入|选|等)[^，,；;。]*")


def core_detail(detail: str, *, max_chars: int = 40) -> str:
    """detail 是她自己抓回来的摘要，会夹带调试尾巴、别人的主张。

    真机上见过 `…厂商建议买30系｜grid=/tmp/xxxx`：
      · `｜` 后面是抓取脚本的中间产物，`/tmp/` 是路径；
      · `厂商建议买30系` 是**别人的营销主张**。
    她转述了后半句，又拿它和「5090 跌价」推了个因果：「显卡跌了价还看什么50系」。
    别人的建议一旦原样回灌，下一句就会变成她的判断。所以这里只留事实，主张切掉。
    """
    d = str(detail or "")
    if not d:
        return ""
    d = re.split(r"[｜|]", d)[0]
    d = re.sub(r"\b(?:grid|out|dir|path|tmp)=\S*", "", d)
    d = re.sub(r"/tmp/\S*", "", d)
    d = _ADVICE.sub("", d)
    d = re.sub(r"\s+", " ", d).strip(" ，,。；;")
    return d[:max_chars]


def _ts(s: str) -> float:
    t = str(s or "").strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(t[:19], fmt).timestamp()
        except ValueError:
            continue
    return 0.0


def stances(home: Home, rows: list[dict[str, Any]], *, limit: int = 2) -> list[tuple[str, bool]]:
    """把她**自己的立场**接回她的经历上。返回 [(claim, 还没想定)]。

    2026-10-29 最要紧的一处：她的观点库里明明有
    「评测的价值在解释为什么，不是给排名；排名三个月就过期」，
    她的生活里明明有六条显卡评测 —— 但这两样在注入时是两条平行线，
    从来不交叉。于是她只能数条数、拼标题，拼出「显卡跌了价还看什么50系」。
    记得事实又有判断，却不知道自己对这件事怎么看，说出来的话当然没逻辑。

    三个匹配信号，**必须有一个是强的**才接：
      · tag 交集 ≥ 2（一个「极客湾」太泛，能把电视评测接到显卡评测上）
      · 时间接近（distill 提炼出的观点，`at` 和对应那条 life 几乎同一分钟）
      · 标题与 claim 共享 6 字以上的片段

    宁可接不上，也不能乱接 —— 接错一条，她会拿"RGB Mini-LED 色域"去回显卡的话，
    那正是主人说的"牛头不对马嘴"。

    hunch 也要带上：她刚看完东西写下的判断就停在 hunch，
    而 opinion.render(settled_only=True) 会把它们全挡在上下文外 ——
    结果她是个"只记标题、没有判断"的人。带上并标「还没想定」，比滤掉真实。
    """
    try:
        from . import opinion as op
    except Exception:  # pragma: no cover - 观点模块缺失不该拖垮生活流
        return []
    try:
        items = op.theses(home)
    except Exception:  # pragma: no cover
        return []
    if not items:
        return []

    tags: set[str] = set()
    titles: list[str] = []
    epochs: list[float] = []
    for r in rows:
        tags.update(str(t) for t in (r.get("tags") or []) if str(t).strip())
        titles.append(str(r.get("title") or ""))
        ep = float(r.get("epoch") or 0) or _ts(str(r.get("at") or ""))
        if ep:
            epochs.append(ep)

    scored: list[tuple[float, str, bool]] = []
    for o in items:
        ot = [str(t) for t in (o.tags or []) if str(t).strip()]
        score = 0.0
        shared_tags = set(ot) & tags
        if len(shared_tags) >= 2:
            score += 3.0
        at = _ts(o.at)
        if at and epochs and min(abs(at - e) for e in epochs) <= 90 * 60:
            score += 3.0
        joined = "".join(titles)
        if joined and any(_lcs(t, str(o.claim or "")) >= 6 for t in titles):
            score += 2.0
        if score <= 0:
            continue
        score += float(o.confidence)
        claim = str(o.claim or "").strip()
        if claim:
            scored.append((score, claim, o.stage != "belief"))
    if not scored:
        return []
    scored.sort(key=lambda x: -x[0])
    out: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for _, claim, hunch in scored:
        key = claim[:24]
        if key in seen:
            continue
        seen.add(key)
        out.append((claim[:70], hunch))
        if len(out) >= limit:
            break
    return out


def _verb_for(row: dict[str, Any]) -> str:
    """分清「看了个视频」和「刷到条消息」。

    10:45 那条「显卡价格大面积回调：11连涨被终结」是个资讯条目，没有 BV 号、
    没有书名号，原来一律按 kind=watch 渲染成「看了」。于是她把这**一条新闻**
    算进了「今天刷了六条硬件评测」，还从里面拎出「50系全线离高点」当结论。
    动词分开，「六条评测」这个说法就不成立了。
    """
    t = str(row.get("title") or "")
    if "《" in t or row.get("bvid") or _BV.search(t):
        return KIND_VERB.get(row.get("kind", "watch"), "看了")
    kind = row.get("kind", "other")
    if kind == "watch":
        return "刷到"
    return KIND_VERB.get(kind, "经历了")


def _day_key(epoch: float, now: datetime) -> int:
    """距今天数：0=今天，1=昨天。无法判断时返回 9（调用方按"很久以前"过滤）。

    必须显式挡住异常值，不能依赖 ``fromtimestamp`` 的行为：
    ``datetime.fromtimestamp(-1)`` 在 Windows 上抛 ``OSError``，
    在 Linux/macOS 上返回 1969-12-31 —— 同一个输入两个平台给出不同答案，
    于是「外部写坏了 epoch 也不能把整段生活流搞崩」这条只在一个平台成立。
    """
    try:
        ts = float(epoch or 0)
    except (TypeError, ValueError):
        return 9
    if ts <= 0:  # 缺失（0）或负数：不是有效时间戳
        return 9
    try:
        d = datetime.fromtimestamp(ts).date()
    except (ValueError, OSError, OverflowError):
        return 9
    days = (now.date() - d).days
    return 9 if days < 0 else days


def _line(r: dict[str, Any], now: datetime, *, chatty: bool) -> str:
    verb = _verb_for(r)
    core = core_title(r.get("title", ""))
    # 标题本身就以动词开头（"看了柴西自驾拉斯维加斯"）时别叠成"看了「看了…」"
    if verb and core.startswith(verb):
        core = core[len(verb):].strip(" ，,。：:")
    at = str(r.get("at") or "")
    clock = at[11:16] if len(at) >= 16 else ""
    head = f"  {clock}（{ago(r.get('epoch', 0), now)}）{verb}「{core}」"
    det = core_detail(r.get("detail", ""))
    if det:
        head += f" —— {det}"
    if r.get("shared"):
        return head + "（已跟他说过）"
    # 只有前两条未讲过的才标「可以聊」。六条全标，等于给她一张六项待办清单，
    # 她的最优解就是逐条播报 —— 这正是「一口气刷了六条」的来路。
    return head + ("（还没跟他说，可以聊）" if chatty else "（还没跟他说）")


def render(home: Home, *, days: float = 2, limit: int = 6, max_chars: int = 780,
           now: datetime | None = None, unshared_first: bool = True,
           per_day: int = 3) -> str:
    """给 context 的一段：按天分组，带相对时间。

    她做过的事**全部**都要记得 —— "我今天看了三个显卡评测"是她自己的生活，
    不知道就等于没有生活。`shared` 标记只决定标注，不决定去留：

      · shared=False → "（还没跟他说）"：**可以聊的素材**，不是隐私。
        她主动提起来之后，宿主会把这条标成 shared（companion_shared.py）。
      · shared=True  → "（已跟他说过）"：别翻来覆去讲同一件事。

    `unshared_first=True` 时没讲过的排前面 —— 唤醒逻辑挑"有什么想跟他说的"
    也是这个优先级，两边保持一致，她才不会对着讲过的旧事起劲。

    2026-10-29 的反复：这函数曾改成 `shared_only=True`（只给讲过的），
    结果主人发现她根本不知道自己刷过显卡评测 —— 她的生活被过滤掉了。
    把"防重复讲"错做成了"不许知道"，方向就反了。

    同一天的第二处修正：**数据全对，读出来是错的**。
    那天她确实有 6 条记录，其中 5 条显卡、1 条价格资讯，原样平铺成 6 行、
    每行都标「可以聊」，她张口就是「今天一口气刷了六条硬件评测，显卡跌了价
    还看什么50系」。六个并列的同类项 + 六句"可以聊"就是一张待办清单，
    模型面对清单只会做一件事：汇总播报。
    于是：按天分组（跨天就不能叫"一口气"）、组内超过 per_day 条折叠成一行、
    「可以聊」只标前两条、结尾明说不许数条数。
    """
    now = now or datetime.now()
    rows = recent(home, days=days, limit=limit, now=now)
    if not rows:
        return ""
    if unshared_first:
        rows = sorted(rows, key=lambda r: bool(r.get("shared")))

    buckets: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        k = _day_key(r.get("epoch", 0), now)
        if k > 2:  # 两天以前的不进这段（recent 的 days 已经挡过一层，这里兜外部写入）
            continue
        buckets.setdefault(k, []).append(r)
    if not buckets:
        return ""

    lines = ["【你最近在做的事】"]
    # 禁令放**开头**，不放末尾：`context.build` 预算不够时 `_head` 是从尾部砍的
    #（life 的 priority 是 80，最后一个放行），禁令在末尾就第一个被切掉 ——
    # 刚修好的"别数条数"会被一次预算紧张悄悄吃掉。
    lines.append("（这是你的底子，不是待办清单：别数「今天看了几条」，"
                 "别把两个标题里的词拼成一句结论，也别把摘要里谁建议买什么当成你的判断。）")
    used = 0
    chatty_left = 2
    detailed = 0
    for k in sorted(buckets):
        label = "今天" if k == 0 else ("昨天" if k == 1 else f"{k} 天前")
        lines.append(f"{label}")
        day = buckets[k]
        shown, folded = day[:per_day], day[per_day:]
        for r in shown:
            chatty = chatty_left > 0 and not r.get("shared")
            if chatty:
                chatty_left -= 1
            line = _line(r, now, chatty=chatty)
            if used + len(line) > max_chars:
                # 一条明细都放不下就整段不给 —— 只剩「【你最近在做的事】今天」
                # 这种半截比没有更糟，模型会拿它当"我什么都没做"的依据。
                return "\n".join(lines) if detailed else ""
            lines.append(line)
            used += len(line)
            detailed += 1
        if folded:
            rest = "、".join(
                f"{str(r.get('at') or '')[11:16]} {core_title(r.get('title', ''))}" for r in folded
            )
            lines.append(f"  （还有 {len(folded)} 件没列：{rest}）")
            used += len(rest)
        if used > max_chars:
            break
    if not detailed:
        return ""
    mine = stances(home, rows)
    if mine:
        lines.append("（你自己琢磨出来的，不是摘要里谁说的：")
        for claim, hunch in mine:
            lines.append(f"  · {claim}{'（还没想定）' if hunch else ''}")
        lines.append("）")
    return "\n".join(lines)
