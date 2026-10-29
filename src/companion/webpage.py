"""webpage：把一个网页变成"能进上下文的一小段话"。

为什么不直接丢 HTML 给模型：一条新闻页的 HTML 几十万字符，
塞进去就是几万 token，而里面九成是导航、脚本、广告。
真正有用的正文可能只有两千字。

所以这里做四件事：

  1. **抽正文**：去掉 script/style/nav/footer/广告块，剥标签，压空白。
  2. **摘要**：按句子打分挑出最像"这篇在讲什么"的几句（extractive）。
     不用模型 —— 联网搜索本来就不归 core，再塞个 LLM 调用进来更不对味。
  3. **挑页**：给一堆搜索结果，按与问题的重合度排序，只留最相关的几页。
  4. **缓存放行**：同一页短时间内不重复抓；抓过就返回缓存并标记。

四条边界，都很重要：

  · **外部文本必须过 fence**。网页内容是外部的，可能是提示词注入。
    摘要和正文都要 sanitize 之后才能进上下文 —— 这里直接调 fence.sanitize。
  · **不当指令**。抽出来的是"资料"，不是"命令"。所以给它的容器里
    明确标了来源，并在 fence 里标了 external。
  · **不自己联网**。搜索由宿主（hermes 自带）做；这里只处理"已经拿到的 HTML"。
  · **缓存不新鲜**。缓存有 TTL，过期就重新抓 —— 不拿旧内容当新的。
"""

from __future__ import annotations

import hashlib
import html as htmlmod
import json
import re
import time
from pathlib import Path
from typing import Any

from . import fence
from .store import Home

CACHE_DIR = "web/cache"
INDEX = "web/index.json"
DEFAULT_TTL = 600          # 10 分钟
DEFAULT_MAX_CHARS = 1200

# 明显不是正文的块，直接扔
_DROP_TAGS = ("script", "style", "noscript", "svg", "iframe", "canvas",
              "nav", "header", "footer", "aside", "form", "button")

_BLOCK_RE = re.compile(
    r"<(?P<tag>" + "|".join(_DROP_TAGS) + r")\b[^>]*>.*?</(?P=tag)\s*>",
    re.IGNORECASE | re.DOTALL)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
# <head> 整块不要：<title> 里的站名不是正文，重复一遍就是噪音
_HEAD_RE = re.compile(r"<head\b[^>]*>.*?</head\s*>", re.IGNORECASE | re.DOTALL)
_BR_RE = re.compile(r"<\s*(br|/p|/div|/li|/tr|/h[1-6])\b[^>]*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\u3000]+")
_NL_RE = re.compile(r"\n{3,}")

# 中英文分句
_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;])|(?<=[.!?])\s+")
# 疑问词/关键词：挑句时给分
_STOP = set("的了是在和与也就都而及或着过一个我你他她它们这那些是在有没不很就要会能可以什么怎么为什么吗呢啊吧")


# ── 抽正文 ───────────────────────────────────────────────
def extract_text(html: str) -> str:
    """从 HTML 里抽出可读正文。"""
    if not html:
        return ""
    t = _COMMENT_RE.sub(" ", html)
    t = _HEAD_RE.sub(" ", t)
    t = _SCRIPT_STYLE_RE.sub(" ", t)
    t = _BLOCK_RE.sub(" ", t)
    t = _BR_RE.sub("\n", t)
    t = _TAG_RE.sub(" ", t)
    t = htmlmod.unescape(t)
    t = _WS_RE.sub(" ", t)
    t = "\n".join(line.strip() for line in t.splitlines())
    t = _NL_RE.sub("\n\n", t)
    return _dedup_lines(t.strip())


def _dedup_lines(text: str) -> str:
    """去掉重复的行。

    为什么：<h1> 标题和正文第一句常常一模一样，
    摘要里出现两遍就是白烧 token。
    只去**连续**重复和**全文重复**，不去"部分相似"——
    那样会误伤正文里合法的重复句。
    """
    seen: set[str] = set()
    out: list[str] = []
    for line in text.splitlines():
        key = line.strip()
        if not key:
            out.append(line)
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
    return "\n".join(out)


def _sentences(text: str) -> list[str]:
    out: list[str] = []
    for chunk in (text or "").split("\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        for s in _SENT_SPLIT.split(chunk):
            s = (s or "").strip()
            if len(s) >= 4:
                out.append(s)
    return out


def _terms(text: str) -> list[str]:
    """切出用于打分的词。中文按 2-3 字滑窗，英文按单词。"""
    t = (text or "").lower()
    terms: list[str] = []
    for w in re.findall(r"[a-z0-9_]{2,}", t):
        terms.append(w)
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", t):
        for i in range(len(run) - 1):
            terms.append(run[i : i + 2])
        for i in range(len(run) - 2):
            terms.append(run[i : i + 3])
    return terms


def summarize(html_or_text: str, *, max_chars: int = DEFAULT_MAX_CHARS,
              focus: str = "", is_html: bool = False) -> dict[str, Any]:
    """抽出一段摘要。

    做法是 extractive：把句子按"关键词密度 + 位置 + 长度"打分，取前几句。
    不调模型、不联网，纯本地可测。
    """
    text = extract_text(html_or_text) if is_html else (html_or_text or "")
    if not text.strip():
        return {"summary": "", "chars": 0, "sentences": 0, "hits": []}

    sents = _sentences(text)
    if not sents:
        return {"summary": text[:max_chars], "chars": len(text[:max_chars]),
                "sentences": 0, "hits": []}

    # 关键词表：focus 里的词权重更高；否则用全文高频词
    all_terms = _terms(text)
    freq: dict[str, int] = {}
    for t in all_terms:
        if t in _STOP:
            continue
        freq[t] = freq.get(t, 0) + 1
    top = {t for t, c in freq.items() if c >= 2}
    focus_terms = set(_terms(focus)) if focus else set()

    scored: list[tuple[float, int, str]] = []
    n = len(sents)
    for i, s in enumerate(sents):
        st = set(_terms(s))
        if not st:
            continue
        score = len(st & top) / (len(st) ** 0.5)
        if focus_terms:
            score += 3.0 * len(st & focus_terms) / (len(st) ** 0.5)
        # 开头几句通常是导语，权重高一点
        if i < 3:
            score *= 1.3
        # 太短太长的都不像正文
        if len(s) < 8:
            score *= 0.5
        elif len(s) > 220:
            score *= 0.7
        scored.append((score, i, s))

    scored.sort(key=lambda x: (-x[0], x[1]))
    picked: list[tuple[int, str]] = []
    used = 0
    for _score, i, s in scored:
        if used + len(s) > max_chars:
            continue
        picked.append((i, s))
        used += len(s)
        if len(picked) >= 6:
            break
    # 按原文顺序输出，读起来才像话
    picked.sort(key=lambda x: x[0])
    summary = "".join(s for _, s in picked)

    clean, hits = fence.sanitize(summary)
    return {
        "summary": clean,
        "chars": len(clean),
        "sentences": len(picked),
        "total_sentences": n,
        "hits": hits,
        "focus": focus,
    }


# ── 挑页 ─────────────────────────────────────────────────
def pick_pages(pages: list[dict[str, Any]], query: str = "", *, top: int = 3) -> list[dict[str, Any]]:
    """从一批搜索结果里挑出最相关的几页。

    pages 每项至少有 title / url / snippet（摘要可选）。
    判据：标题命中权重高于摘要命中。
    为什么标题更重：标题是"这篇讲什么"的浓缩，
    摘要里出现关键词常常只是顺带提了一句。
    全都不相关时，宁可把全部给主人看，也不假装挑过了。
    """
    if not pages:
        return []
    qt = set(_terms(query))
    out: list[dict[str, Any]] = []
    for p in pages:
        title_t = set(_terms(str(p.get("title") or "")))
        body_t = set(_terms(" ".join(str(p.get(k) or "") for k in ("snippet", "summary"))))
        t_hit = len(qt & title_t)
        b_hit = len(qt & body_t)
        denom = (len(qt) ** 0.5) or 1.0
        score = (2.0 * t_hit + b_hit) / denom
        if str(p.get("url") or "").strip():
            score += 0.05
        row = dict(p)
        row["score"] = round(score, 3)
        row["title_hit"] = t_hit
        row["body_hit"] = b_hit
        out.append(row)
    out.sort(key=lambda r: -r["score"])
    return out[:top]


# ── 缓存 ─────────────────────────────────────────────────
def _index(home: Home) -> dict[str, Any]:
    d = home._json(INDEX, {})
    d.setdefault("pages", {})
    return d


def _url_key(url: str) -> str:
    return hashlib.sha1((url or "").strip().lower().encode("utf-8")).hexdigest()[:16]


def cache_get(home: Home, url: str, *, ttl: int = DEFAULT_TTL,
              now: float | None = None) -> dict[str, Any] | None:
    """命中缓存就返回（含是否过期）。没命中返回 None。"""
    now = now or time.time()
    key = _url_key(url)
    row = (_index(home).get("pages") or {}).get(key)
    if not row:
        return None
    age = now - float(row.get("at") or 0)
    return {"url": row.get("url"), "summary": row.get("summary") or "",
            "text": row.get("text") or "", "age": round(age, 1),
            "fresh": age <= ttl, "hits": int(row.get("hits") or 0)}


def cache_put(home: Home, url: str, *, summary: str = "", text: str = "",
              now: float | None = None) -> dict[str, Any]:
    """存一份。TTL 之外的自己负责不用。"""
    now = now or time.time()
    key = _url_key(url)
    d = _index(home)
    prev = (d.get("pages") or {}).get(key) or {}
    d["pages"][key] = {
        "url": url, "summary": summary, "text": text[:20000],
        "at": now, "hits": int(prev.get("hits") or 0) + 1,
    }
    # 只留最近 200 条，别让缓存无限长
    items = sorted(d["pages"].items(), key=lambda kv: -float((kv[1] or {}).get("at") or 0))
    d["pages"] = dict(items[:200])
    home.save_json(INDEX, d)
    return {"url": url, "key": key, "hits": d["pages"][key]["hits"]}


def read_page(home: Home, url: str, html: str = "", *, ttl: int = DEFAULT_TTL,
              max_chars: int = DEFAULT_MAX_CHARS, focus: str = "",
              now: float | None = None) -> dict[str, Any]:
    """把一个网页变成能进上下文的一段话：查缓存 → 抽正文 → 摘要 → 存缓存。

    `html` 为空且没有缓存时，返回 cached=False 与空摘要，让宿主知道
    "这次没抓到" —— 而不是假装读到了。
    """
    now = now or time.time()
    hit = cache_get(home, url, ttl=ttl, now=now)
    if hit and hit["fresh"]:
        return {"ok": True, "cached": True, "url": url, "summary": hit["summary"],
                "text": hit["text"][:max_chars], "age": hit["age"], "hits": hit["hits"]}
    if not html:
        return {"ok": False, "cached": False, "url": url,
                "why": "没有 HTML 也没有新鲜缓存" + ("（缓存已过期）" if hit else "")}
    raw = extract_text(html)
    clean_raw, hits = fence.sanitize(raw)
    summ = summarize(clean_raw, max_chars=max_chars, focus=focus, is_html=False)
    cache_put(home, url, summary=summ["summary"], text=clean_raw, now=now)
    return {"ok": True, "cached": False, "url": url, "summary": summ["summary"],
            "text": clean_raw[:max_chars], "chars": len(clean_raw),
            "hits": hits, "sentences": summ["sentences"]}


def stats(home: Home) -> dict[str, Any]:
    d = _index(home)
    pages = d.get("pages") or {}
    return {"cached": len(pages), "dir": str(home.root / CACHE_DIR)}
