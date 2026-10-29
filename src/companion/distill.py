"""distill：记忆自动提炼。

**问题**：她每天记生活流水（`life/life.jsonl`）、说话（`logs/said.jsonl`），
但记忆页（`memory/pages/*.md`）一直靠手动写。手动的东西一定会烂——
三个月后一页都不写，记忆就永远是空的。

**但也不能全自动写**：流水账不是记忆。
"看到一只橘猫"不是记忆，"他最近总在晚上十点后找我"才是。
所以这里做的是**提炼**，不是搬运：

  · 只提炼**重复出现**的事——一次是巧合，两次三次才是模式
  · 只提炼**他自己说的/跟他有关的**，她看视频看到什么都不算
  · 提炼出来是**候选**，不直接落盘——让人（主人或模型）过一眼

**流水线**：
  1. `candidates()` —— 从 life + said 里挖模式，按频次排序
  2. `review()` —— 把候选渲染成一段人话，给主人/模型看
  3. `accept()` —— 认可的写进记忆页（走 memory.write，有 summary 约束）

**为什么候选要人看一眼**：自动提炼一定会抽错（"他今天回了个哦"被当成模式）。
错记忆比没记忆更糟——它会被当成事实反复影响她的判断。
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from . import memory
from .store import Home

# 提炼的时间窗：只看最近这些天
WINDOW_DAYS = 14.0
# 至少出现几次才算"模式"
MIN_OCCURRENCES = 3
# 最多给几个候选（多了主人看不过来）
MAX_CANDIDATES = 5
# 单个候选最多带几条证据
MAX_EVIDENCE = 4

# 时间模式：几点几分
_TIME_RE = re.compile(r"([01]?\d|2[0-3])\s*[:：]\s*([0-5]\d)")
# 中文字符（单字，用于切分与邻接统计）
_CJK = re.compile(r"[\u4e00-\u9fff]")
# 候选片段：2-4 个连续汉字
_FRAG = re.compile(r"[\u4e00-\u9fff]{2,4}")


def _cutoff(now: datetime | None = None, days: float = WINDOW_DAYS) -> float:
    now = now or datetime.now()
    return (now - timedelta(days=days)).timestamp()


def _hour_of(at: Any) -> int | None:
    """从时间戳里取"归一化后的小时"（23 点和 0 点算同一个：深夜）。

    ISO 串形如 2026-10-29T22:00:00，不能全局搜"两位数字+冒号"——
    日期部分也会误匹配，要锚定在 T 后面。
    """
    m = re.search(r"T(\d{1,2}):(\d{2})", str(at or ""))
    if not m:
        return None
    hour = int(m.group(1))
    if hour >= 22:
        return 23
    if hour < 5:
        return 0
    return hour


def _hours(rows: list[dict[str, Any]]) -> Counter:
    """出现得最多的钟点（按归一化后的小时）。"""
    c: Counter = Counter()
    for r in rows:
        h = _hour_of(r.get("at"))
        if h is not None:
            c[h] += 1
    return c


def _labels(rows: list[dict[str, Any]], key: str = "kind") -> Counter:
    c: Counter = Counter()
    for r in rows:
        v = str(r.get(key) or "").strip()
        if v:
            c[v] += 1
    return c


# 无词典新词发现的两个阈值
MIN_PMI = 2.0        # 内部结合紧（左右两半粘得牢）
MIN_ENTROPY = 0.6   # 左右搭配够多样（不是固定搭配的片段）
MAX_WORD_LEN = 4
# 小样本时 PMI 估计噪声大（unigram 概率只由几条文本估出来），
# 所以样本少时把 PMI 门槛放宽，但要求频次占比更高来兜底。
SMALL_SAMPLE = 30          # 字符数低于此算小样本
SMALL_SAMPLE_PMI = 1.5     # 小样本的 PMI 门槛
SMALL_SAMPLE_COVERAGE = 0.08   # 小样本时要求片段占全部字符的比例
SOLID_COVERAGE = 0.15      # 通用兜底：片段占全部字符的比例够高就算词


def _entropy(items: list[str]) -> float:
    """邻接字符的熵。多样 → 高；老是跟同一个字搭 → 低。"""
    if not items:
        return 0.0
    c = Counter(items)
    total = len(items)
    h = 0.0
    import math

    for v in c.values():
        p = v / total
        h -= p * math.log(p)
    return h


def _find_words(rows: list[dict[str, Any]], min_count: int) -> list[tuple[str, int]]:
    """从中文文本里找出像词的片段（无词典，靠统计）。

    两条判据：
      · 互信息 —— "奶茶" 内部 seldom 分开，左"奶"右"茶"粘得牢
      · 左右邻接熵 —— "聊聊奶茶" 左边老是"聊聊"，是固定搭配，不像个词

    返回 [(词, 次数)]，按次数倒序。
    """
    texts = [str(r.get("text") or "") for r in rows]
    texts = [t for t in texts if t]
    if not texts:
        return []

    # 统计所有 2..4 字的候选片段及其左右邻字
    freq: Counter = Counter()
    lefts: dict[str, list[str]] = {}
    rights: dict[str, list[str]] = {}
    evidence: dict[str, list[str]] = {}
    for row in rows:
        text = str(row.get("text") or "")
        at = str(row.get("at") or "")
        chars = [c for c in text if _CJK.fullmatch(c)]
        for n in range(2, MAX_WORD_LEN + 1):
            for i in range(len(chars) - n + 1):
                frag = "".join(chars[i:i + n])
                freq[frag] += 1
                lefts.setdefault(frag, []).append(chars[i - 1] if i > 0 else "^")
                rights.setdefault(frag, []).append(chars[i + n] if i + n < len(chars) else "$")
                evidence.setdefault(frag, []).append(at)

    total_chars = sum(len(t) for t in texts) or 1
    unigram: Counter = Counter()
    for t in texts:
        for c in t:
            unigram[c] += 1

    import math

    small = total_chars < SMALL_SAMPLE
    pmi_min = SMALL_SAMPLE_PMI if small else MIN_PMI
    scored: list[tuple[float, str, int]] = []
    for frag, n in freq.items():
        if n < min_count:
            continue
        # 互信息：拆成两半，看这两半是不是比随机凑在一起更常一起出现
        best_pmi = 0.0
        for split in range(1, len(frag)):
            a, b = frag[:split], frag[split:]
            pa = unigram[a] / total_chars
            pb = unigram[b] / total_chars
            pab = freq[frag] / total_chars
            if pa <= 0 or pb <= 0 or pab <= 0:
                continue
            pmi = math.log(pab / (pa * pb))
            best_pmi = max(best_pmi, pmi)
        coverage = (n * len(frag)) / total_chars
        # PMI 在短语料上天然偏低（unigram 被"的了在"这些高频字主导），
        # 所以再加一条兜底：**够高频 + 够占篇幅 + 左右够多样** 也算词。
        # 三条一起过才算碎片——"聊聊""我们聊"这种固定搭配熵为 0，会被挡掉。
        solid = coverage >= SOLID_COVERAGE and n >= max(min_count, 3)
        if best_pmi < pmi_min and not solid:
            continue
        # 小样本再要求"覆盖率"——碎片在全文里占的字符比例
        if small and coverage < SMALL_SAMPLE_COVERAGE:
            continue
        # 左右邻接熵：太固定就不是词
        ent = min(_entropy(lefts.get(frag, [])), _entropy(rights.get(frag, [])))
        if ent < MIN_ENTROPY:
            continue
        scored.append((best_pmi * ent, frag, n))

    scored.sort(key=lambda x: (-x[2], -x[0]))
    # 去掉被更长候选包含的短片段
    out: list[tuple[str, int]] = []
    for _, frag, n in scored:
        if any(frag != o and frag in o for o, _ in out):
            continue
        out.append((frag, n))
    return out


def _evidence_for(rows: list[dict[str, Any]], word: str, limit: int = MAX_EVIDENCE) -> list[str]:
    """这个词出现过的时刻（给人看"她凭什么这么说"）。"""
    out: list[str] = []
    for r in rows:
        text = str(r.get("text") or "")
        if word in text:
            out.append(str(r.get("at") or ""))
            if len(out) >= limit:
                break
    return out


def candidates(home: Home, *, now: datetime | None = None,
               days: float = WINDOW_DAYS,
               min_count: int = MIN_OCCURRENCES) -> list[dict[str, Any]]:
    """从流水与说话记录里挖模式。返回按强度倒序的候选。"""
    cut = _cutoff(now, days)
    life_rows = [r for r in home.read_jsonl("life/life.jsonl", limit=800)
                 if float(r.get("epoch") or 0) >= cut]
    said_rows = [r for r in home.read_jsonl("logs/said.jsonl", limit=800)
                 if float(r.get("epoch") or 0) >= cut]
    out: list[dict[str, Any]] = []

    # ① 他总在某个钟点找我
    hours = _hours(said_rows)
    for hour, n in hours.most_common(3):
        if n < min_count:
            continue
        slot = ("深夜" if hour in (0, 23) else
                "早上" if hour < 9 else "上午" if hour < 12 else
                "中午" if hour < 14 else "下午" if hour < 18 else "晚上")
        ev = [str(r.get("at") or "") for r in said_rows
              if _hour_of(r.get("at")) == hour]
        out.append({
            "kind": "time_pattern",
            "summary": f"他经常在{slot}（{hour:02d} 点前后）找我说话",
            "count": n,
            "evidence": ev[:MAX_EVIDENCE],
            "tags": ["习惯", "时间"],
        })

    # ② 他常聊的话题（从"她说过的话"里挖高频词组）
    #
    # 中文没有词边界，又不想为了这个引入分词依赖（SPEC 的规矩：
    # 不引 yaml 依赖，同理不该为一个可选功能拖进 jieba）。
    # 所以用**无词典新词发现**那套：互信息（内部结合紧）+
    # 左右邻接熵（左右搭配多样 → 像个独立的词）。这两条合起来
    # 能把"奶茶"从"聊聊奶茶"里分出来。
    roots = _find_words(said_rows, min_count)
    known = "".join(p.summary + p.title for p in memory.pages(home))
    for w, n in roots:
        if w in known:
            continue
        out.append({
            "kind": "topic_pattern",
            "summary": f"我们最近常聊到「{w}」",
            "count": n,
            "evidence": _evidence_for(said_rows, w),
            "tags": ["话题"],
        })
        if len([c for c in out if c["kind"] == "topic_pattern"]) >= 2:
            break

    # ③ 她最近在反复做的事（生活流水里同类事件多）
    kinds = _labels(life_rows, "kind")
    for k, n in kinds.most_common(3):
        if n < min_count:
            continue
        ev = [str(r.get("title") or "") for r in life_rows if str(r.get("kind") or "") == k]
        out.append({
            "kind": "life_pattern",
            "summary": f"我最近{n}次在做「{k}」这类事",
            "count": n,
            "evidence": ev[:MAX_EVIDENCE],
            "tags": ["生活"],
        })

    out.sort(key=lambda c: -c["count"])
    return out[:MAX_CANDIDATES]


def review(home: Home, **kw: Any) -> str:
    """把候选渲染成一段人话，给主人或模型看。"""
    cands = candidates(home, **kw)
    if not cands:
        return "（最近没有发现值得记住的模式。）"
    lines = ["【可能值得记住的事】"]
    for c in cands:
        lines.append(f"- {c['summary']}（出现 {c['count']} 次）")
        for e in c.get("evidence") or []:
            if e:
                lines.append(f"    · {e}")
    return "\n".join(lines)


def accept(home: Home, summary: str, *, title: str = "", body: str = "",
           tags: list[str] | None = None, now: datetime | None = None) -> dict[str, Any]:
    """认可一个候选 → 写进记忆页。"""
    summary = (summary or "").strip()
    if not summary:
        raise ValueError("summary 不能为空")
    title = title or summary[:24]
    body = body or summary
    page = memory.write(home, title, body, tags=tags or ["自动提炼"],
                        summary=summary, now=now)
    return {"ok": True, **page.as_dict()}
