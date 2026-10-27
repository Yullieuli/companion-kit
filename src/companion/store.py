"""store：文件协议。人改得动，agent 读得懂，git 存得下。

设计约束：
  · init() 幂等 —— 已存在的文件绝不覆盖（人格是用户的，不是代码的）
  · 半残缺的文件不抛异常 —— 解析失败回落到默认值，并留一条 warning
  · 所有路径都从 Home.root 派生，测试里换个目录就能隔离
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SOUL_TEMPLATE = """# 我是谁

（名字、来历、一句话的自我认知。写完这几行她才知道自己是谁。）

## 说话

- 句子长短、标点习惯、口头禅
- 自称 / 怎么称呼对方
- 哪些词不用

## 我在意什么

（判断标准、偏好、怕什么）

## 我绝不会

（底线。比如不冒充真人、不泄露地址、不替主人做决定）
"""

MEMORY_PROTOCOL = """# 记忆协议

每页一个文件，frontmatter 三个字段：

```yaml
---
tags: [游戏, 设计]
summary: 一句话，会进索引
last_access: 2026-10-27
---
```

规则：
1. 只写「以后会用到」的东西，不写流水账
2. 每页有 1–3 条指向别的页的链接，否则它是一座孤岛
3. 三个月没用到的页，允许删
"""

INDEX_TEMPLATE = """# 记忆目录

（这里由 `companion memory index --write` 从各页 summary 投影生成，不要手写。）
"""

DEFAULT_RHYTHM: dict[str, Any] = {
    "timezone": "Asia/Shanghai",
    "windows": [{"start": "20:00", "end": "24:00", "wakes": 2}],
    "sleep": {"at": "00:30", "wake": "09:00"},
    "greeting": {"morning": True, "night": True, "active_days": 3},
}

DEFAULT_MOOD: dict[str, Any] = {
    "axes": {"satisfaction": 0, "closeness": 0, "energy": 0},
    "label": "平静",
    "last_change": None,
    "history": [],
}

_REQUIRED = {
    "SOUL.md": SOUL_TEMPLATE,
    "MEMORY.md": MEMORY_PROTOCOL,
    "memory/index.md": INDEX_TEMPLATE,
    "rhythm.json": json.dumps(DEFAULT_RHYTHM, ensure_ascii=False, indent=2),
    "mood/state.json": json.dumps(DEFAULT_MOOD, ensure_ascii=False, indent=2),
    "opinions.jsonl": "",
    "quota.json": "{}",
    "logs/turns.jsonl": "",
    "logs/cost.jsonl": "",
    "logs/initiatives.jsonl": "",
    "logs/events.jsonl": "",
    "logs/said.jsonl": "",
    "logs/voice.jsonl": "",
    "logs/device.jsonl": "",
    "life/life.jsonl": "",
}


@dataclass
class Home:
    root: Path

    @classmethod
    def at(cls, path: str | Path | None = None) -> "Home":
        import os

        base = path or os.environ.get("COMPANION_HOME") or (Path.home() / ".companion")
        return cls(Path(base).expanduser())

    # ── 初始化 ────────────────────────────────────────────────
    def init(self) -> list[str]:
        """建目录与缺的模板。返回这次真正创建的文件（相对路径）。"""
        created: list[str] = []
        for d in ("memory/pages", "people", "life", "logs", "mood", "voice", "shots"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        for rel, body in _REQUIRED.items():
            p = self.root / rel
            if p.exists():
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
            created.append(rel)
        return created

    # ── 读 ────────────────────────────────────────────────────
    def soul(self) -> str:
        return self._text("SOUL.md")

    def memory_index(self) -> str:
        return self._text("memory/index.md")

    def rhythm(self) -> dict[str, Any]:
        return self._json("rhythm.json", DEFAULT_RHYTHM)

    def mood(self) -> dict[str, Any]:
        return self._json("mood/state.json", DEFAULT_MOOD)

    def quota(self) -> dict[str, Any]:
        return self._json("quota.json", {})

    def _text(self, rel: str) -> str:
        p = self.root / rel
        try:
            return p.read_text(encoding="utf-8")
        except Exception:
            return ""

    def _json(self, rel: str, default: dict[str, Any]) -> dict[str, Any]:
        p = self.root / rel
        try:
            data = json.loads(p.read_text(encoding="utf-8") or "{}")
            return data if isinstance(data, dict) else dict(default)
        except Exception:
            return json.loads(json.dumps(default))  # 深拷贝，别让调用方改到常量

    # ── 写 ────────────────────────────────────────────────────
    def save_json(self, rel: str, data: dict[str, Any]) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
        return p

    def append_jsonl(self, rel: str, row: dict[str, Any]) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def read_jsonl(self, rel: str, limit: int = 200) -> list[dict[str, Any]]:
        p = self.root / rel
        if not p.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out[-limit:]
