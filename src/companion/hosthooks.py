"""宿主 shell 钩子的批准表（consent allowlist）。

为什么需要这个模块：
hermes 对 shell 钩子有 consent 机制 —— config.yaml 里写了 `hooks:` 还不算，
每个 (事件, 命令) 对还必须在 `shell-hooks-allowlist.json` 里被批准过。
**没批准的话，hermes 会静默跳过这个钩子**：不报错、钩子就是不跑。

2026-10-29 实测踩过：按文档装完 companion_hook.sh + companion_post.sh，
config.yaml 里明明有，钩子却一个都不触发。查了两个小时才发现是 allowlist 没批准。
所以安装器必须顺手把钩子加进来，而且装完要自检 —— 本模块就是那个自检的依据。

放在 core 而不是 hermes/ 里，是因为"钩子到底生不生效"是跨宿主的诊断问题：
`companion doctor` 要能查 hermes，也要能查别的用同样机制的宿主。
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ALLOWLIST_NAME = "shell-hooks-allowlist.json"


# ── 基本读写 ────────────────────────────────────────────────
def allowlist_path(home: Path | str) -> Path:
    return Path(home).expanduser() / ALLOWLIST_NAME


def load(path: Path | str) -> dict[str, Any]:
    """读批准表。文件不存在/坏了都当空的 —— 不能因为表坏了就整体崩掉。"""
    p = Path(path)
    if not p.exists():
        return {"approvals": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"approvals": []}
    if not isinstance(data, dict):
        return {"approvals": []}
    approvals = data.get("approvals")
    data["approvals"] = approvals if isinstance(approvals, list) else []
    return data


def save(path: Path | str, data: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _script_mtime(command: str) -> str | None:
    """取脚本 mtime —— hermes 用它检测「批准之后脚本被换过」。"""
    path = command_path(command)
    if path is None:
        return None
    try:
        return (datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
                .isoformat().replace("+00:00", "Z"))
    except OSError:
        return None


def command_path(command: str) -> Path | None:
    """从命令行里抠出脚本路径（取最后一个存在且像路径的 token）。"""
    for token in reversed(command.split()):
        if "/" in token or "\\" in token:
            try:
                p = Path(token)
                if p.exists():
                    return p
            except OSError:
                continue
    return None


def approved_pairs(data: dict[str, Any]) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for e in data.get("approvals", []):
        if isinstance(e, dict) and e.get("event") and e.get("command"):
            out.add((str(e["event"]), str(e["command"])))
    return out


# ── add：批准 ──────────────────────────────────────────────
def approve(home: Path | str, event: str, command: str, *, backup: bool = True) -> dict[str, Any]:
    """把 (event, command) 加进批准表。已存在就只当成功，不重复写。"""
    path = allowlist_path(home)
    data = load(path)
    pair = (event, command)
    if pair in approved_pairs(data):
        return {"ok": True, "added": False, "reason": "already approved", "path": str(path)}
    if backup and path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak-companion"))
    entry = {"event": event, "command": command,
             "approved_at": _now_iso(), "script_mtime_at_approval": _script_mtime(command)}
    data["approvals"] = [e for e in data.get("approvals", [])
                         if not (isinstance(e, dict) and e.get("event") == event
                                 and e.get("command") == command)]
    data["approvals"].append(entry)
    save(path, data)
    return {"ok": True, "added": True, "event": event, "command": command, "path": str(path)}


# ── config 解析 ────────────────────────────────────────────
def parse_hooks_block(block: Any) -> list[tuple[str, str]]:
    """从 config.yaml 的 hooks 段解析出 (事件, 命令) 对（已有 yaml 对象时用）。"""
    out: list[tuple[str, str]] = []
    if not isinstance(block, dict):
        return out
    for event, entries in block.items():
        if not isinstance(entries, list):
            continue
        for item in entries:
            if isinstance(item, dict) and item.get("command"):
                out.append((str(event), str(item["command"])))
    return out


def parse_hooks_text(text: str) -> list[tuple[str, str]]:
    """没有 pyyaml 时的兜底解析。

    为什么不直接依赖 pyyaml：这个检查恰恰是「静默失败」的最后一道防线，
    不能因为缺一个可选依赖就跳过 —— 那等于把最后一道防线也弄没了。
    """
    out: list[tuple[str, str]] = []
    in_hooks = False
    section = ""
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if indent == 0:
            in_hooks = stripped == "hooks:"
            section = ""
            continue
        if not in_hooks:
            continue
        if indent == 2 and stripped.endswith(":"):
            section = stripped[:-1]
            continue
        if indent == 4 and stripped.startswith("- command:"):
            cmd = stripped.split(":", 1)[1].strip().strip('"').strip("'")
            if cmd:
                out.append((section, cmd))
        elif indent == 6 and stripped.startswith("command:"):
            cmd = stripped.split(":", 1)[1].strip().strip('"').strip("'")
            if cmd:
                out.append((section, cmd))
    return out


def configured_hooks(home: Path | str) -> list[tuple[str, str]]:
    """读宿主 config.yaml 里配置了哪些 shell 钩子。"""
    cfg = Path(home).expanduser() / "config.yaml"
    if not cfg.exists():
        return []
    text = cfg.read_text(encoding="utf-8")
    try:
        import yaml
        data = yaml.safe_load(text)
        return parse_hooks_block((data or {}).get("hooks"))
    except ImportError:
        return parse_hooks_text(text)
    except Exception:
        return parse_hooks_text(text)


# ── check：配了但没批准的钩子 ───────────────────────────────
def check(home: Path | str) -> dict[str, Any]:
    """对照 config.yaml 与批准表，报出「配了但没批准」的钩子。

    这些钩子**不会执行** —— 宿主静默跳过。这是最容易踩的坑，
    所以必须能独立查出来，而不是靠翻日志。
    """
    pairs = configured_hooks(home)
    allowed = approved_pairs(load(allowlist_path(home)))
    missing = [(e, c) for e, c in pairs if (e, c) not in allowed]
    return {"ok": not missing, "total": len(pairs), "missing": missing,
            "allowlist": str(allowlist_path(home)), "home": str(home)}
