#!/usr/bin/env python3
"""把 hermes 已有的记忆搬进 companion 的 store。

只搬三样，都是**纯追加、可重复执行**的：
  life.jsonl      → life/life.jsonl      她干过什么
  opinions.jsonl  → opinions.jsonl       她自己的判断
  said.jsonl      → logs/said.jsonl      她说过的话

**不搬 SOUL.md**：hermes 的 SOUL 是 4KB 的人格 + 分流规则，本来就在她的系统提示里。
再往 companion 里放一份会被截断到 600 字，还会和原提示的语气打架。
companion 的 SOUL.md 留作模板，适配层用 `include_soul=False` 跳过它。

用法：
    COMPANION_HOME=/mnt/c/Users/Anran/.companion \\
      ~/.companion-venv/bin/python migrate.py --from ~/.hermes --dry-run
    （去掉 --dry-run 才真写；重复跑不会产生重复条目）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


_KINDS = {"watch", "play", "read", "make", "think", "meet", "other"}
_BRACKET = re.compile(r"^【([^】]{1,8})】(.+)$", re.S)
_ARTIFACT = re.compile(r"^(证据[:：]|.*\.(jpg|png|jpeg|webp)\s*$)", re.I)


def _life_fields(r: dict) -> tuple[str, str]:
    """她那边的 life 有两种写法，字段会错位：

    正常：{kind: "watch", text: "看了…"}
    错位：{kind: "【watch】描述…", text: "证据：f/grid.jpg", title: "双人抬棺"}
    统一成 (kind, 正文)。
    """
    kind_raw = str(r.get("kind") or "")
    m = _BRACKET.match(kind_raw.strip())
    if m:
        body = m.group(2).strip()
        if not body or _ARTIFACT.match(body):
            body = str(r.get("title") or "").strip()
        return _kind(m.group(1)), body
    body = str(r.get("text") or "").strip()
    if not body or _ARTIFACT.match(body):
        body = str(r.get("title") or "").strip()
    return _kind(kind_raw), body


def _kind(raw) -> str:
    """她那边的 kind 有的写成「类型」「【watch】」，统一成合法值。"""
    k = str(raw or "").strip().strip("[]【】").lower()
    return k if k in _KINDS else "other"


def _flat(text: str) -> str:
    return " ".join(str(text or "").split())


def _dt(raw: str) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(raw), fmt)
        except Exception:
            continue
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", default=str(Path.home() / ".hermes"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    src = Path(args.src).expanduser()
    try:
        from companion import life, opinion
        from companion.store import Home
    except Exception as e:
        print(f"companion 不可用：{e}", file=sys.stderr)
        return 1

    home = Home.at()
    home.init()

    report = {"life": 0, "opinion": 0, "said": 0, "skipped": 0}

    # ── life ──
    seen_life = {str(x.get("id") or "") for x in home.read_jsonl("life/life.jsonl", limit=100000)}
    for r in _rows(src / "proactive/life.jsonl"):
        kind, text = _life_fields(r)
        text = _flat(text)
        at = _dt(r.get("ts") or "") or (datetime.fromtimestamp(float(r["epoch"])) if r.get("epoch") else None)
        if not text or not at or life._id(at, text) in seen_life:
            report["skipped"] += 1
            continue
        seen_life.add(life._id(at, text))
        # 旧账本里这几个字段只在它那边有：id 是 BV 号，progress 是看到哪了。
        # 不带过来，她就"忘了自己看到哪"，pulse 也找不回没看完的东西。
        extra = {}
        for src_key, dst_key in (("id", "bvid"), ("progress", "progress"), ("mood", "mood")):
            if r.get(src_key):
                extra[dst_key] = str(r[src_key])
        tags = [t.strip() for t in str(r.get("tags") or "").replace("，", ",").split(",") if t.strip()]
        if args.dry_run:
            print(f"[life] {at:%Y-%m-%d %H:%M} {kind} {text[:40]} {extra or ''}")
        else:
            life.record(home, text, kind=kind, at=at, tags=tags, extra=extra or None)
        report["life"] += 1

    # ── opinions ──
    # 注意：不能用 opinion.add 判重 —— 它遇到同一句会 reinforce（置信度 +0.12），
    # 迁移脚本重跑就会把她的立场越搬越硬。这里自己先查一遍。
    from companion.opinion import _norm as _opnorm

    existing = {_opnorm(o.claim) for o in opinion.theses(home, include_retired=True)}
    for r in _rows(src / "state/opinions.jsonl"):
        claim = _flat(r.get("claim") or "")
        if not claim or _opnorm(claim) in existing:
            report["skipped"] += 1
            continue
        existing.add(_opnorm(claim))
        at = _dt(r.get("created") or "") or datetime.now()
        if args.dry_run:
            print(f"[opinion] {float(r.get('confidence', 0.45)):.2f} {claim[:50]}")
        else:
            opinion.add(home, claim, tags=list(r.get("tags") or []),
                        confidence=float(r.get("confidence", 0.45)), source="hermes", now=at)
        report["opinion"] += 1

    # ── said ──
    seen = {(str(x.get("at") or ""), str(x.get("text") or "")) for x in home.read_jsonl("logs/said.jsonl", limit=100000)}
    for r in _rows(src / "proactive/said.jsonl"):
        text = _flat(r.get("text") or "")
        ts = str(r.get("ts") or "")
        if not text or not ts:
            report["skipped"] += 1
            continue
        if (ts, text) in seen:
            report["skipped"] += 1
            continue
        if args.dry_run:
            print(f"[said] {ts} {text[:40]}")
        else:
            home.append_jsonl("logs/said.jsonl", {"at": ts, "text": text, "source": "hermes-migrate"})
        report["said"] += 1

    tag = "（dry-run，没写）" if args.dry_run else ""
    print(f"\n迁移完成{tag}：life {report['life']} 条、opinion {report['opinion']} 条、"
          f"said {report['said']} 条，跳过 {report['skipped']} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
