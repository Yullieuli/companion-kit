"""store：文件协议。人改得动，agent 读得懂，git 存得下。

设计约束：
  · init() 幂等 —— 已存在的文件绝不覆盖（人格是用户的，不是代码的）
  · 半残缺的文件不抛异常 —— 解析失败回落到默认值，并留一条 warning
  · 所有路径都从 Home.root 派生，测试里换个目录就能隔离
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_THREAD_LOCKS: dict[tuple[str, str], threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()
_LOCK_STATE = threading.local()

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
    # 总开关：false 时 companion 完全不介入（不注入、不决策），用来一键退回旧行为
    "enabled": True,
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
    "logs/bili.jsonl": "",
    "life/life.jsonl": "",
    "bili/state.json": "{}",
    "bili/config.json": "{}",
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
        for d in ("memory/pages", "people", "life", "logs", "mood", "voice", "shots", "stickers", "group", "bili"):
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
    @contextmanager
    def lock(self, name: str = "store", timeout: float = 30.0):
        """跨线程/跨进程互斥，且允许同一进程内重入。

        宿主各自起进程，单靠原子 replace 仍会丢「读—改—写」更新。锁文件只用于
        同步，不存业务数据；Windows 用 msvcrt，POSIX 用 flock，无第三方依赖。
        """
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)[:80] or "store"
        key = (str(self.root.expanduser().resolve()), safe)
        with _THREAD_LOCKS_GUARD:
            lock = _THREAD_LOCKS.setdefault(key, threading.RLock())
        lock.acquire()
        held = getattr(_LOCK_STATE, "held", None)
        if held is None:
            held = {}
            _LOCK_STATE.held = held
        # held[key] 形如 (depth, fh)：depth 是本线程的重入层数
        depth = held.get(key, (0, None))[0]
        fh = None
        acquired = False
        try:
            if depth == 0:
                path = self.root / "logs" / ".locks" / f"{safe}.lock"
                path.parent.mkdir(parents=True, exist_ok=True)
                # Windows 上：别的进程正持有这个文件的排他锁时，
                # 连 open 都会 PermissionError（不是等到 locking 才失败）。
                # 所以这里要重试打开，而不是让第二个进程直接崩。
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        fh = path.open("a+b")
                        break
                    except PermissionError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError(f"等待 store 锁超时：{safe}")
                        time.sleep(0.05)
                if os.name == "nt":
                    import msvcrt

                    if path.stat().st_size == 0:
                        fh.write(b"\0")
                        fh.flush()
                    deadline = time.monotonic() + timeout
                    while True:
                        try:
                            fh.seek(0)
                            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                            break
                        except OSError:
                            if time.monotonic() >= deadline:
                                raise TimeoutError(f"等待 store 锁超时：{safe}")
                            time.sleep(0.05)
                else:
                    import fcntl

                    deadline = time.monotonic() + timeout
                    while True:
                        try:
                            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except OSError:
                            if time.monotonic() >= deadline:
                                raise TimeoutError(f"等待 store 锁超时：{safe}")
                            time.sleep(0.05)
                acquired = True
                held[key] = (1, fh)
            else:
                held[key] = (depth + 1, held[key][1])
            yield
        finally:
            try:
                if depth == 0:
                    if acquired and fh is not None:
                        if os.name == "nt":
                            import msvcrt

                            fh.seek(0)
                            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl

                            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                    if fh is not None:
                        fh.close()
                    if acquired:
                        held.pop(key, None)
                elif key in held:
                    held[key] = (depth, held[key][1])
            finally:
                lock.release()

    def save_json(self, rel: str, data: dict[str, Any]) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        # 每个写者用不同临时文件，避免两个进程互删同一个 .tmp。
        tmp = p.with_name(f".{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(p)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        return p

    def read_json(self, rel: str, default: Any = None) -> Any:
        """读一个 json。文件不存在或坏了 → 返回 default（不抛）。

        坏格式回落而不是抛：store 是"她生活过的地方"，
        某个文件被手滑改坏不该让她整个启动不了。
        """
        p = self.root / rel
        if not p.exists():
            return default
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return default

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

    def save_jsonl(self, rel: str, rows: list[dict[str, Any]]) -> None:
        """整份重写一个 jsonl（原子替换）。

        append-only 的账本用这个不合适——那是账，丢了就真丢了。
        但"待写回清单"这种**状态表**需要能改状态，用它才合适。
        """
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        tmp = p.with_name(f".{p.name}.{os.getpid()}.{threading.get_ident():x}.tmp")
        try:
            tmp.write_text(body, encoding="utf-8")
            os.replace(tmp, p)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
