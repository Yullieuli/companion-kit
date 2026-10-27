"""memory：一页一件事，索引是从页里投影出来的。

为什么不用向量库：伴侣的记忆量在几百页这个量级，关键词 + 标签 + 时间衰减
已经够用，而向量检索要多一个服务、多一份不能 git 的二进制。

三条约束（写在 MEMORY.md 里给她看的，这里是对应的实现）：
  · 只写「以后会用到」的，不写流水账 —— `write()` 要求 summary 非空
  · 每页要有出链 —— `links()` 抽 [[slug]]，`orphans()` 报告孤岛
  · 三个月没碰的页允许删 —— `stale()` 只报告，绝不自动删

frontmatter 是自己解析的：不引 yaml 依赖，格式写死成三行，坏格式回落而不抛。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .store import Home

PAGES = "memory/pages"
INDEX = "memory/index.md"
MAX_PAGES = 300
STALE_DAYS = 90

_SLUG_BAD = re.compile(r"[^\w\u4e00-\u9fff-]+")


def slugify(title: str, fallback: str = "note") -> str:
    s = _SLUG_BAD.sub("-", (title or "").strip()).strip("-").lower()
    if not s:
        s = fallback
    return s[:60]


@dataclass
class Page:
    slug: str
    title: str
    body: str = ""
    tags: list[str] = field(default_factory=list)
    summary: str = ""
    last_access: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "title": self.title,
            "tags": self.tags,
            "summary": self.summary,
            "last_access": self.last_access,
            "chars": len(self.body),
        }


# ── 解析 / 序列化 ────────────────────────────────────────────
def _parse_tags(raw: str) -> list[str]:
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return [t.strip().strip("'\"") for t in raw.split(",") if t.strip()]


def parse(text: str, slug: str = "") -> Page:
    """宽容解析：frontmatter 缺字段就当空，坏行跳过，绝不抛。"""
    meta: dict[str, str] = {}
    body = text or ""
    if body.startswith("---"):
        end = body.find("\n---", 3)
        if end > 0:
            head = body[3:end]
            body = body[end + 4 :].lstrip("\n")
            for line in head.splitlines():
                if ":" not in line:
                    continue
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
    title = meta.get("title") or (body.splitlines()[0].lstrip("# ").strip() if body else slug)
    return Page(
        slug=slug,
        title=title or slug,
        body=body,
        tags=_parse_tags(meta.get("tags", "")),
        summary=meta.get("summary", "").strip(),
        last_access=meta.get("last_access", "").strip(),
    )


def dump(page: Page) -> str:
    tags = ", ".join(page.tags)
    return (
        "---\n"
        f"tags: [{tags}]\n"
        f"summary: {page.summary}\n"
        f"last_access: {page.last_access}\n"
        "---\n\n"
        f"# {page.title}\n\n"
        f"{page.body.strip()}\n"
    )


# ── 读写 ────────────────────────────────────────────────────
def _dir(home: Home) -> Path:
    p = home.root / PAGES
    p.mkdir(parents=True, exist_ok=True)
    return p


def pages(home: Home) -> list[Page]:
    out: list[Page] = []
    for f in sorted(_dir(home).glob("*.md")):
        try:
            out.append(parse(f.read_text(encoding="utf-8"), f.stem))
        except Exception:
            continue
    return out


def get(home: Home, slug: str) -> Page | None:
    f = _dir(home) / f"{slugify(slug)}.md"
    if not f.exists():
        return None
    return parse(f.read_text(encoding="utf-8"), f.stem)


def write(
    home: Home,
    title: str,
    body: str,
    *,
    tags: list[str] | None = None,
    summary: str = "",
    slug: str = "",
    now: datetime | None = None,
) -> Page:
    """写一页。summary 必填——它是索引里唯一会被读到的字，逼自己一句话说清。"""
    now = now or datetime.now()
    if not (summary or "").strip():
        raise ValueError("summary 不能为空：一页记忆必须能一句话说清")
    slug = slugify(slug or title)
    old = get(home, slug)
    page = Page(
        slug=slug,
        title=(title or "").strip() or slug,
        body=body.strip(),
        tags=sorted({*(old.tags if old else []), *(tags or [])}),
        summary=summary.strip(),
        last_access=now.strftime("%Y-%m-%d"),
    )
    (_dir(home) / f"{slug}.md").write_text(dump(page), encoding="utf-8")
    build_index(home)
    return page


def append(home: Home, slug: str, line: str, now: datetime | None = None) -> Page:
    """往已有页补一句（不重新写整页）。"""
    now = now or datetime.now()
    p = get(home, slug)
    if p is None:
        raise KeyError(f"没有这一页：{slug}")
    p.body = (p.body.rstrip() + "\n" + line.strip()).strip()
    p.last_access = now.strftime("%Y-%m-%d")
    (_dir(home) / f"{p.slug}.md").write_text(dump(p), encoding="utf-8")
    build_index(home)
    return p


def touch(home: Home, slug: str, now: datetime | None = None) -> bool:
    p = get(home, slug)
    if p is None:
        return False
    p.last_access = (now or datetime.now()).strftime("%Y-%m-%d")
    (_dir(home) / f"{p.slug}.md").write_text(dump(p), encoding="utf-8")
    return True


# ── 检索 ────────────────────────────────────────────────────
def _days_ago(date_str: str) -> float:
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
    except Exception:
        return 999.0
    return max(0.0, (datetime.now() - d).days)


def search(home: Home, query: str, *, limit: int = 5) -> list[tuple[float, Page]]:
    """标签命中 3 分、summary 2 分、正文每次命中 0.6 分（最多 3 次），越近加分。"""
    q = (query or "").strip().lower()
    scored: list[tuple[float, Page]] = []
    for p in pages(home):
        score = 0.0
        if q:
            for t in p.tags:
                if q in t.lower() or t.lower() in q:
                    score += 3.0
            if q in p.summary.lower():
                score += 2.0
            body = p.body.lower()
            hits = min(3, body.count(q))
            score += 0.6 * hits
            if q in p.title.lower():
                score += 1.5
        else:
            score = 0.1
        if score <= 0:
            continue
        score += max(0.0, 1.0 - _days_ago(p.last_access) / 90.0)
        scored.append((score, p))
    scored.sort(key=lambda x: (-x[0], x[1].slug))
    return scored[:limit]


def links(page: Page) -> list[str]:
    return re.findall(r"\[\[([^\]]+)\]\]", page.body or "")


def orphans(home: Home) -> list[str]:
    """没有任何页链到它、它也不链出去的页。"""
    all_pages = pages(home)
    linked: set[str] = set()
    for p in all_pages:
        linked.update(slugify(x) for x in links(p))
    return [p.slug for p in all_pages if not links(p) and p.slug not in linked]


def stale(home: Home, days: int = STALE_DAYS) -> list[str]:
    return sorted(p.slug for p in pages(home) if _days_ago(p.last_access) >= days)


# ── 索引（投影，不手写）──────────────────────────────────────
def build_index(home: Home) -> str:
    """从各页 summary 重新生成 memory/index.md。幂等：同样的页得到同样的索引。"""
    all_pages = sorted(pages(home), key=lambda p: (-len(p.tags), p.slug))
    lines = ["# 记忆目录", "", "（由 `companion memory index --write` 从各页 summary 生成，别手写。）", ""]
    by_tag: dict[str, list[str]] = {}
    for p in all_pages:
        for t in p.tags:
            by_tag.setdefault(t, []).append(p.slug)
    if all_pages:
        lines.append(f"共 {len(all_pages)} 页。")
        lines.append("")
        for p in all_pages:
            tag = ("[" + "/".join(p.tags) + "] ") if p.tags else ""
            lines.append(f"- {tag}**{p.slug}** — {p.summary or p.title}")
        if by_tag:
            lines += ["", "## 按标签"]
            for t in sorted(by_tag):
                lines.append(f"- {t}：{', '.join(sorted(by_tag[t]))}")
    else:
        lines.append("（还没有任何一页。）")
    text = "\n".join(lines) + "\n"
    p = home.root / INDEX
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return text


def render(home: Home, query: str = "", *, limit: int = 6, max_chars: int = 900) -> str:
    """给 context 用的一段：相关页的 summary，不是全文。"""
    hits = search(home, query, limit=limit) if query else [(0.0, p) for p in pages(home)[:limit]]
    if not hits:
        return ""
    lines = ["【你想起来的事】"]
    used = 0
    for _, p in hits:
        tag = ("[" + "/".join(p.tags) + "] ") if p.tags else ""
        line = f"- {tag}{p.summary or p.title}（上次想到 {p.last_access or '?'}）"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines) if len(lines) > 1 else ""
