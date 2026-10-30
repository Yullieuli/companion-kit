"""connect：把 companion 接到本机已装的宿主上，不需要用户手写配置。

## 为什么要有这个模块

SPEC 的卖点是"换宿主行为不变"，但在此之前，接宿主的动作一直是手抄：
hermes 要改 ``config.yaml``、dsh 要改 profile 的 ``package.json``、
openclaw / codex 要自己往 JSON / TOML 里塞一段。四份格式、四次升级、四处出错。
首页写着"一行安装"，实际是"一行装 core，剩下四个宿主各自手工接线"。

``companion connect`` 把这件事压成一条命令：探测 → 计划 → 备份 → 写 → 自检。

## 四条硬规则

1. **不猜格式**。能用宿主自己的 CLI 就用 CLI；没有 CLI 才按本机实证格式写
   （codex 的 ``[mcp_servers.*]``、dsh 的 profile ``package.json``），
   写前先读、写后必验。
2. **先备份再改**。任何用户文件改动前复制成 ``.bak-companion-<时间戳>``；
   校验失败自动还原——宿主配置写坏了，用户的 agent 直接起不来。
3. **幂等**。已经配过就报"已是最新"，不重写、不堆备份。
4. **可回滚**。``companion connect --rollback`` 还原到最近一次备份。

## 边界

- 不装宿主、不升级宿主、不碰宿主的模型配置。
- 不写任何凭据：MCP 段里只有命令、参数和 store 路径。
- 探测不到就明说"没装"，不猜路径、不建空目录。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import hosthooks
from .store import Home

# 备份后缀。带时间戳，--rollback 取最新一份。
BAK = ".bak-companion-"
# 记一次 connect 动过哪些文件，供 --rollback 用（不重新探测）。
REGISTRY = "connect.json"


class ConnectError(RuntimeError):
    """连接失败。消息要能直接给人看：哪一步、哪个文件、怎么退回去。"""


# ── 文件工具 ─────────────────────────────────────────────────


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def read_text(path: Path) -> str:
    """按原样读（不转换换行）。

    保留 CRLF 很重要：用户文件是 CRLF，我们写成 LF 之后
    git diff 会显示整文件改动，看着像我们重写了配置。
    """
    with path.open("r", encoding="utf-8", errors="replace", newline="") as f:
        return f.read()


def write_text(path: Path, text: str, newline: str = "") -> None:
    """原子写。

    ``newline=""`` 不转换：``.sh`` 必须是 LF，``.cmd`` 必须是 CRLF。
    Windows 上默认转换会把 .sh 写成 CRLF，Linux 里 ``sh`` 会报
    ``word unexpected (expecting "in")``。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline=newline) as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def make_backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    dst = path.with_name(path.name + BAK + _stamp())
    shutil.copy2(path, dst)
    return dst


def latest_backup(path: Path) -> Path | None:
    cands = sorted(path.parent.glob(path.name + BAK + "*"))
    return cands[-1] if cands else None


# ── JSON / JSON5 ─────────────────────────────────────────────


def _top_level_close(text: str) -> int:
    """返回顶层对象最后一个 ``}`` 的下标；找不到返回 -1。

    不用 ``json.loads`` 是因为 ``openclaw.json`` 是 JSON5（键可以不带引号、
    允许尾逗号），标准解析器直接拒。这里只做一件事：
    跳过字符串与注释，数花括号。
    """
    depth = 0
    in_str = False
    quote = ""
    esc = False
    line_comment = False
    block_comment = False
    i = 0
    n = len(text)
    found = -1
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if line_comment:
            if c == "\n":
                line_comment = False
            i += 1
            continue
        if block_comment:
            if c == "*" and nxt == "/":
                block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == quote:
                in_str = False
            i += 1
            continue
        if c in "\"'":
            in_str = True
            quote = c
            i += 1
            continue
        if c == "/" and nxt == "/":
            line_comment = True
            i += 2
            continue
        if c == "/" and nxt == "*":
            block_comment = True
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            found = i
            if depth == 0:
                return i
        i += 1
    return found


def _has_top_key(text: str, key: str) -> bool:
    """顶层是否已有这个键（JSON5 也认：键可以带引号也可以不带）。"""
    close = _top_level_close(text)
    body = text[:close] if close > 0 else text
    pat = re.compile(rf'(?m)^[ \t]*["\']?{re.escape(key)}["\']?[ \t]*:')
    return bool(pat.search(body))


def _json_insert_top(text: str, key: str, value: Any) -> str:
    """在顶层对象末尾插入一个键，保留原有排版与注释。"""
    close = _top_level_close(text)
    if close < 0:
        raise ConnectError("找不到顶层对象的结束括号，不敢改")
    head = text[:close].rstrip()
    tail = text[close:]
    needs_comma = not head.endswith("{") and not head.endswith(",")
    inner = json.dumps({key: value}, ensure_ascii=False, indent=2)
    inner = inner.strip()[1:-1].strip()
    inner = "\n".join("  " + ln for ln in inner.splitlines())
    sep = "," if needs_comma else ""
    return f"{head}{sep}\n{inner}\n{tail}"


def json_merge_top(path: Path, updates: dict[str, Any], *, overwrite: bool = False) -> tuple[bool, str]:
    """合并顶层键。返回 (是否改动, 说明)。

    标准 JSON 走 dict 合并（可以覆盖）；JSON5 走文本插入（只新增，不覆盖）。
    覆盖用户的 JSON5 配置是危险的：注释、键顺序、未加引号的键都会丢。
    """
    text = read_text(path)
    try:
        data = json.loads(text)
    except Exception:
        data = None

    if isinstance(data, dict):
        merged = dict(data)
        changed = False
        for k, v in updates.items():
            if k in merged and merged[k] == v:
                continue
            if k in merged and not overwrite:
                raise ConnectError(f"{path.name} 里已有 {k}，且内容不同；加 --force 才覆盖")
            merged[k] = v
            changed = True
        if not changed:
            return False, "已是最新"
        write_text(path, json.dumps(merged, ensure_ascii=False, indent=2) + "\n")
        return True, "标准 JSON 已合并"

    # JSON5 / JSONC：只做新增，不重排原文
    try:
        json_parse_loose(text)
    except Exception as e:
        raise ConnectError(f"{path.name} 不是可解析的 JSON/JSON5（{e}）；不敢往里写") from e

    added = []
    for k, v in updates.items():
        if _has_top_key(text, k):
            if not overwrite:
                raise ConnectError(f"{path.name} 里已有 {k}（JSON5 不覆盖）；确需覆盖请手工改")
            raise ConnectError(f"{path.name} 是 JSON5 且已有 {k}；不自动覆盖，请手工合并")
        text = _json_insert_top(text, k, v)
        added.append(k)
    write_text(path, text)
    return True, "JSON5 已插入 " + "、".join(added)


def _json5_to_json(text: str) -> str:
    """把 JSON5 的常见写法转成标准 JSON，只用于**校验**，不用来写回。

    覆盖三种：裸键（``models:``）、单引号字符串、尾逗号。
    注释直接丢掉。字符串里的内容原样保留，所以不会把
    ``"a//b"`` 误当成注释。
    """
    out: list[str] = []
    i, n = 0, len(text)
    expect_key = False
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and nxt == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if c in "\"'":
            q = c
            j = i + 1
            buf: list[str] = []
            while j < n:
                ch = text[j]
                if ch == "\\":
                    buf.append(text[j:j + 2])
                    j += 2
                    continue
                if ch == q:
                    break
                buf.append(ch)
                j += 1
            raw = "".join(buf)
            if q == "'":
                raw = raw.replace('"', '\\"')
            out.append('"' + raw + '"')
            i = j + 1
            expect_key = False
            continue
        if c == "{":
            out.append(c)
            i += 1
            expect_key = True
            continue
        if c == "}":
            while out and not out[-1].strip():
                out.pop()
            if out and out[-1].rstrip().endswith(","):
                out[-1] = out[-1].rstrip()[:-1]
            out.append(c)
            i += 1
            expect_key = False
            continue
        if c == ",":
            out.append(c)
            i += 1
            expect_key = True
            continue
        if expect_key and (c.isalpha() or c in "_$"):
            j = i
            while j < n and (text[j].isalnum() or text[j] in "_$"):
                j += 1
            out.append('"' + text[i:j] + '"')
            i = j
            expect_key = False
            continue
        out.append(c)
        i += 1
        if not c.isspace():
            expect_key = False
    return "".join(out)


def json_parse_loose(text: str) -> dict[str, Any]:
    """先按标准 JSON 解析，失败再按 JSON5 宽松解析。都失败就抛。"""
    try:
        data = json.loads(text)
    except Exception:
        data = json.loads(_json5_to_json(text))
    if not isinstance(data, dict):
        raise ValueError("顶层不是对象")
    return data


def json_verify(path: Path, keys: list[str]) -> tuple[bool, str]:
    """写完的校验：必须能解析（标准 JSON 或 JSON5），且键在。"""
    text = read_text(path)
    try:
        data = json_parse_loose(text)
    except Exception as e:
        return False, f"解析失败：{e}"
    missing = [k for k in keys if k not in data]
    if missing:
        return False, "缺少键：" + "、".join(missing)
    return True, "JSON 解析通过"


# ── TOML ─────────────────────────────────────────────────────


def _toml_str(s: str) -> str:
    """TOML 字符串。路径含反斜杠，优先用字面量单引号（不转义）。"""
    if "'" not in s and "\n" not in s:
        return f"'{s}'"
    return json.dumps(s, ensure_ascii=False)


def toml_append_server(
    path: Path,
    table: str,
    command: str,
    args: list[str],
    env: dict[str, str],
    *,
    timeout_sec: int = 60,
) -> tuple[bool, str]:
    """追加一个 ``[mcp_servers.<name>]`` 段。已存在则不覆盖。"""
    text = read_text(path)
    head = f"[{table}]"
    if re.search(rf"(?m)^\s*\[\s*{re.escape(table)}\s*\]\s*$", text):
        return False, f"已有 {head}，不动"

    args_toml = ", ".join(_toml_str(a) for a in args)
    block = [
        "",
        head,
        'type = "stdio"',
        f"command = {_toml_str(command)}",
        f"args = [{args_toml}]",
        f"startup_timeout_sec = {timeout_sec}",
    ]
    if env:
        block.append("")
        block.append(f"[{table}.env]")
        for k, v in env.items():
            block.append(f"{k} = {_toml_str(v)}")
    block.append("")

    if text and not text.endswith("\n"):
        text += "\n"
    write_text(path, text + "\n".join(block))
    return True, f"已追加 {head}"


def toml_verify(path: Path, table: str) -> tuple[bool, str]:
    text = read_text(path)
    if not re.search(rf"(?m)^\s*\[\s*{re.escape(table)}\s*\]\s*$", text):
        return False, f"找不到 [{table}]"
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        return True, "段已存在（Python 3.10 无 tomllib，跳过语法校验）"
    try:
        tomllib.loads(text)
    except Exception as e:
        return False, f"TOML 语法错误：{e}"
    return True, "TOML 解析通过"


# ── YAML（只做 hooks 段的最小注入）────────────────────────────


def _yaml_hooks_bounds(lines: list[str]) -> tuple[int, int] | None:
    """返回 (``hooks:`` 行号, 段结束行号)。没有 hooks 段返回 None。"""
    return _yaml_section_bounds(lines, "hooks")


def _yaml_section_bounds(lines: list[str], name: str) -> tuple[int, int] | None:
    """返回 (顶层段 ``name:`` 的行号, 段结束行号)。找不到返回 None。"""
    start = None
    for i, ln in enumerate(lines):
        if ln.rstrip() == f"{name}:":
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for i in range(start + 1, len(lines)):
        ln = lines[i]
        if ln.strip() and not ln.startswith((" ", "\t")):
            end = i
            break
    return start, end


def yaml_inject_mcp(
    text: str,
    server: str,
    command: str,
    args: list[str],
    env: dict[str, str],
) -> tuple[str, bool]:
    """把 companion 挂成 ``mcp_servers:`` 下的一个 stdio server。返回 (新文本, 是否新增)。

    为什么非做不可：hooks 只覆盖「注入上下文」和「写回说话」两件事，其余能力
    ——记忆写入/检索、表情、群聊判断、观点加权、网页读取、请示单——全是 MCP 工具。
    模型调不到就等于不存在。2026-10-30 实测：只接 hooks 不接 MCP 时，
    ``memory/`` 0 页、``stickers/`` 0 张、``group/`` 0 次，34 个工具里 31 个闲置。
    """
    lines = text.splitlines(True)
    for ln in lines:
        if ln.rstrip() == f"  {server}:":
            return text, False  # 已经挂过了

    block = [f"  {server}:\n", f"    command: {_yaml_quote(command)}\n"]
    if args:
        block.append("    args:\n")
        for a in args:
            block.append(f"      - {_yaml_quote(a)}\n")
    else:
        block.append("    args: []\n")
    if env:
        block.append("    env:\n")
        for k, v in env.items():
            block.append(f"      {k}: {_yaml_quote(v)}\n")

    bounds = _yaml_section_bounds(lines, "mcp_servers")
    if bounds is None:
        if text and not text.endswith("\n"):
            text += "\n"
        text += "\nmcp_servers:\n"
        lines = text.splitlines(True)
        bounds = _yaml_section_bounds(lines, "mcp_servers")
        if bounds is None:
            raise ConnectError("写入 mcp_servers: 段失败")
    start, end = bounds
    lines[end:end] = block
    return "".join(lines), True


def _yaml_quote(s: str) -> str:
    """YAML 里写路径。

    必须用单引号：双引号里 ``\\U``、``\\A`` 会被当成转义序列，
    ``C:\\Users\\...`` 直接让 safe_load 抛错——Windows 上这个前缀太常见，
    这是必然会踩的。
    单引号内反斜杠不转义，只有单引号本身要写成两个。
    """
    if "'" in s:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return "'" + s + "'"


def yaml_inject_hooks(text: str, pairs: list[tuple[str, str]]) -> tuple[str, list[str]]:
    """把 (事件, 命令) 追加到 ``hooks:`` 下。返回 (新文本, 实际新增的事件)。

    为什么不用 pyyaml：core 的依赖是空的（``dependencies = []``），
    装宿主插件的人不该被迫多装一个 YAML 库；而且 pyyaml 回写会丢注释。
    这里只做"往固定结构里追加条目"，写完由调用方校验结构。
    """
    added: list[str] = []
    for event, command in pairs:
        lines = text.splitlines(True)
        if command in text:
            continue
        bounds = _yaml_hooks_bounds(lines)
        if bounds is None:
            if text and not text.endswith("\n"):
                text += "\n"
            text += "\nhooks:\n"
            lines = text.splitlines(True)
            bounds = _yaml_hooks_bounds(lines)
            if bounds is None:
                raise ConnectError("写入 hooks: 段失败")
        start, end = bounds
        quoted = _yaml_quote(command)
        # 找该事件段
        key_line = None
        for i in range(start + 1, end):
            if lines[i].strip() == event + ":":
                key_line = i
                break
        if key_line is None:
            # 整个事件段都没有：插到 hooks 段末尾
            block = [f"  {event}:\n", f"    - command: {quoted}\n", "      timeout: 10\n"]
            lines[end:end] = block
        else:
            # 已有事件段：插到该段最后一条之后
            j = key_line + 1
            while j < end and (lines[j].startswith("      ") or lines[j].startswith("    - ")):
                j += 1
            lines[j:j] = [f"    - command: {quoted}\n", "      timeout: 10\n"]
        text = "".join(lines)
        added.append(event)
    return text, added


def yaml_verify_hooks(text: str, pairs: list[tuple[str, str]]) -> tuple[bool, str]:
    """结构校验：每个命令都出现在对应事件段下，且缩进是 4 空格。"""
    for event, command in pairs:
        lines = text.splitlines()
        bounds = _yaml_hooks_bounds([ln + "\n" for ln in lines])
        if bounds is None:
            return False, "没有 hooks: 段"
        start, end = bounds
        key_line = None
        for i in range(start + 1, end):
            if lines[i].strip() == event + ":":
                key_line = i
                break
        if key_line is None:
            return False, f"缺少 {event}: 段"
        found = False
        for i in range(key_line + 1, end):
            ln = lines[i]
            if ln.strip() and not ln.startswith("      ") and not ln.startswith("    - "):
                break
            if command in ln:
                found = True
                break
        if not found:
            return False, f"{event} 下没有这条命令"
    return True, "hooks 结构检查通过"


# ── 子进程 ───────────────────────────────────────────────────


def run_cmd(cmd: list[str], cwd: Path | None, timeout: int = 300) -> tuple[int, str]:
    """跑一条外部命令，带超时与一次重试。

    C4：所有外部调用必须有超时、重试、降级。这里重试一次是因为
    ``pnpm install`` / ``openclaw mcp add`` 都是幂等的；失败后由调用方降级成
    "只改文件 + 打印手动命令"，不让整个 connect 失败。

    第一条命令必须用 ``shutil.which`` 解析成全路径：Windows 上 ``pnpm`` /
    ``openclaw`` 实际是 ``pnpm.CMD``，``CreateProcess`` 不认裸名字，
    直接 spawn 会报"找不到命令"——明明在 PATH 里。
    """
    argv = list(cmd)
    resolved = shutil.which(argv[0])
    if resolved:
        argv[0] = resolved
    last = ""
    for attempt in (1, 2):
        try:
            p = subprocess.run(
                argv,
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            out = (p.stdout or "") + (p.stderr or "")
            if p.returncode == 0:
                return 0, out.strip()
            last = out.strip() or f"退出码 {p.returncode}"
        except FileNotFoundError:
            return 127, f"找不到命令：{cmd[0]}"
        except subprocess.TimeoutExpired:
            last = f"超时（{timeout}s）"
        except Exception as e:  # noqa: BLE001 - 要把原始错误带回去
            last = f"{type(e).__name__}: {e}"
        if attempt == 1:
            time.sleep(1.0)
    return 1, last


def find_exe(name: str) -> str | None:
    return shutil.which(name)


# ── MCP 启动命令 ─────────────────────────────────────────────


def mcp_spec(home: Home) -> tuple[str, list[str], dict[str, str]]:
    """返回 (command, args, env)。

    用 ``sys.executable -m companion`` 而不是 ``companion``：
    GUI 宿主不继承登录后写入的 PATH（Windows 上尤其明显），
    绝对路径是唯一可靠的形式。store 路径也显式带上，避免宿主
    在别的用户/工作目录下跑时读到空 store。
    """
    return (
        sys.executable,
        ["-m", "companion", "mcp"],
        {"COMPANION_HOME": str(home.root)},
    )


def _bundled_hermes_dir() -> Path | None:
    """hermes 适配层脚本在哪。

    三种布局都要认：源码/editable（仓库根）、wheel 的 data-files、
    以及用户已经装过的 ~/.hermes/scripts（什么都不用做）。
    """
    repo = Path(__file__).resolve().parents[2] / "hermes"
    if (repo / "companion_hook.py").is_file():
        return repo
    data = Path(sysconfig.get_path("data") or "") / "share" / "companion" / "hermes"
    if (data / "companion_hook.py").is_file():
        return data
    return None


# ── 步骤与宿主基类 ───────────────────────────────────────────


@dataclass
class Step:
    kind: str          # write | edit | run
    note: str          # 人读：要做什么
    where: str         # 人读：动哪里
    path: Path | None = None
    fn: Callable[[], None] | None = None
    # optional 的步骤失败只警告，不回滚。用于"配置已经写对了，但依赖没装上"
    # 这种场景：pnpm install 失败不代表注册错了，不该把 package.json 退回去。
    optional: bool = False


@dataclass
class Result:
    host: str
    status: str        # connected | latest | skipped | failed
    detail: str
    files: list[str] = None  # type: ignore[assignment]
    warnings: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.files is None:
            self.files = []
        if self.warnings is None:
            self.warnings = []


class Host:
    """一个宿主的接入器。四段式：detect / plan / apply / verify。"""

    name = ""
    label = ""

    def __init__(self, home: Home, opts: dict[str, Any]) -> None:
        self.home = home
        self.opts = opts
        self.root: Path | None = None
        self.reason = ""
        self.backups: list[Path] = []
        self.touched: list[Path] = []
        # 新建的文件（原来不存在）。回滚时直接删，不是"还原"。
        self.created: list[Path] = []
        self.warnings: list[str] = []

    # 子类实现 ────────────────────────────────────────────
    def detect(self) -> bool:
        raise NotImplementedError

    def plan(self) -> list[Step]:
        raise NotImplementedError

    def verify(self) -> tuple[bool, str]:
        return True, ""

    # 公共执行 ────────────────────────────────────────────
    def _before_write(self, path: Path) -> None:
        if not path.exists():
            self.created.append(path)
            return
        b = make_backup(path)
        if b:
            self.backups.append(b)

    def apply(self, steps: list[Step], dry_run: bool) -> None:
        for st in steps:
            if dry_run:
                print(f"    [dry] {st.note}  →  {st.where}")
                continue
            if st.path is not None and st.kind in ("write", "edit"):
                self._before_write(st.path)
                self.touched.append(st.path)
            if st.fn is None:
                continue
            try:
                st.fn()
            except Exception as e:  # noqa: BLE001 - 统一转成可读错误
                if st.optional:
                    self.warnings.append(f"{st.note} 没成功：{e}")
                    continue
                raise ConnectError(f"{st.note} 失败：{e}") from e

    def rollback(self) -> list[str]:
        """失败时自动还原。

        有备份的还原；本次新建的直接删——否则 hermes 那边会留下 8 个
        没人引用的 .py，而"失败自动回滚"等于没做到。
        """
        done: list[str] = []
        for p in self.touched:
            b = latest_backup(p)
            if b:
                shutil.copy2(b, p)
                done.append(f"{p} ← {b.name}")
            elif p in self.created and p.exists():
                try:
                    p.unlink()
                    done.append(f"{p} 已删除（本次新建）")
                except OSError as e:
                    done.append(f"{p} 删不掉：{e}")
        return done


# ── hermes ───────────────────────────────────────────────────


HERMES_SCRIPTS = [
    "companion_hook.py",
    "companion_post.py",
    "companion_wake.py",
    "companion_shared.py",
    "companion_gate.py",
    "companion_watchdog.py",
    "patch_napcat_guard.py",
    "allowlist_hooks.py",
]


class HermesHost(Host):
    name = "hermes"
    label = "hermes（Python 插件，hooks 注入）"

    def detect(self) -> bool:
        root = Path(
            self.opts.get("hermes_home")
            or os.environ.get("HERMES_HOME")
            or str(Path.home() / ".hermes")
        ).expanduser()
        if not root.is_dir():
            self.reason = f"没找到 {root}（未安装 hermes）"
            return False
        if not (root / "config.yaml").is_file():
            self.reason = f"{root} 里没有 config.yaml"
            return False
        self.root = root
        return True

    def _scripts(self) -> Path:
        assert self.root is not None
        return self.root / "scripts"

    def _hook_path(self, script: Path) -> Path:
        """钩子入口的路径。POSIX 用 .sh，Windows 用 .cmd。"""
        return script.with_suffix(".cmd") if os.name == "nt" else script.with_suffix(".sh")

    def _hook_cmd(self, script: Path) -> str:
        """生成钩子入口并返回它的路径。

        直接给解释器 + 脚本，不让宿主去猜 shell：
        POSIX 用 ``.sh``，Windows 没有 sh 所以写 ``.cmd``。
        两者的作用一样——把 COMPANION_HOME 钉死，不依赖宿主环境变量。
        """
        if os.name == "nt":
            cmd_path = self._hook_path(script)
            body = (
                "@echo off\r\n"
                f'if not defined COMPANION_HOME set "COMPANION_HOME={self.home.root}"\r\n'
                f'"{sys.executable}" "{script}" %*\r\n'
            )
            write_text(cmd_path, body, newline="\r\n")
            return str(cmd_path)
        sh_path = self._hook_path(script)
        body = (
            "#!/bin/sh\n"
            f'export COMPANION_HOME="{self.home.root}"\n'
            f'exec "{sys.executable}" "{script}" "$@"\n'
        )
        write_text(sh_path, body, newline="\n")
        try:
            sh_path.chmod(0o755)
        except OSError:
            pass
        return str(sh_path)

    def _expected(self) -> list[tuple[str, str]]:
        """我们打算接的两条钩子。plan 与 verify 共用同一份，避免两边算不一样。"""
        scripts = self._scripts()
        return [
            ("pre_llm_call", str(self._hook_path(scripts / "companion_hook.py"))),
            ("post_llm_call", str(self._hook_path(scripts / "companion_post.py"))),
        ]

    def _up_to_date(self) -> bool:
        """适配层、钩子、批准表三样都到位才算"已是最新"。

        不做这个判断的话，第二次 connect 会重写一遍文件、堆一份备份，
        用户看到"又改了一次"会以为哪里出问题了。
        """
        src = _bundled_hermes_dir()
        if src is None:
            return False
        scripts = self._scripts()
        for fname in HERMES_SCRIPTS:
            s = src / fname
            if not s.is_file():
                continue
            d = scripts / fname
            if not d.is_file() or read_text(d) != read_text(s):
                return False
        configured = set(hosthooks.configured_hooks(self.root))
        allowed = hosthooks.approved_pairs(hosthooks.load(hosthooks.allowlist_path(self.root)))
        for pair in self._expected():
            if pair not in configured or pair not in allowed:
                return False
        # MCP 没挂也算没接好：否则第二次 connect 会直接跳过，
        # 「hooks 在、工具没有」这种状态就永远补不上。
        text = read_text(self.root / "config.yaml")
        if not any(ln.rstrip() == "  companion:" for ln in text.splitlines()):
            return False
        return True

    def plan(self) -> list[Step]:
        assert self.root is not None
        if self._up_to_date():
            return []
        steps: list[Step] = []
        src = _bundled_hermes_dir()
        scripts = self._scripts()

        # ① 适配层本体
        if src is None:
            raise ConnectError(
                "找不到 hermes 适配脚本（companion_hook.py 等）。"
                "wheel 安装的话请重装：pip install --force-reinstall companion-kit"
            )
        for fname in HERMES_SCRIPTS:
            s = src / fname
            if not s.is_file():
                continue
            dst = scripts / fname
            steps.append(
                Step(
                    "write",
                    f"复制适配层 {fname}",
                    str(dst),
                    path=dst,
                    fn=lambda s=s, dst=dst: write_text(dst, read_text(s), newline=""),
                )
            )

        # ② 包装脚本 + hooks 命令
        hook_py = scripts / "companion_hook.py"
        post_py = scripts / "companion_post.py"

        def make_hooks() -> None:
            self._hook_cmd(hook_py)
            self._hook_cmd(post_py)

        steps.append(
            Step("run", "生成 hooks 包装脚本（.sh / .cmd）", str(scripts), fn=make_hooks)
        )

        # ③ config.yaml
        cfg = self.root / "config.yaml"

        def patch_cfg() -> None:
            text = read_text(cfg)
            new, added = yaml_inject_hooks(text, self._expected())
            if added:
                write_text(cfg, new, newline="")

        steps.append(
            Step(
                "edit",
                "在 config.yaml 里接上 pre_llm_call / post_llm_call",
                str(cfg),
                path=cfg,
                fn=patch_cfg,
            )
        )

        # ④ allowlist（没批准 = 钩子静默不跑）
        def approve() -> None:
            for event, cmd in self._expected():
                hosthooks.approve(self.root, event, cmd)

        steps.append(
            Step("run", "批准 shell 钩子（不批准会静默跳过）", str(self.root / "shell-hooks-allowlist.json"), fn=approve)
        )

        # ⑤ MCP —— hooks 只管「注入上下文」和「写回说话」，其余能力都在 MCP 工具里。
        # 缺这一步的表现极具迷惑性：对话完全正常，但 memory/ 永远是空的、
        # 表情库一张图都没有、群聊从不接话。因为它不报错，只是那些工具没人调。
        cfg_mcp = self.root / "config.yaml"

        def patch_mcp() -> None:
            text = read_text(cfg_mcp)
            new, added = yaml_inject_mcp(
                text,
                "companion",
                sys.executable,
                ["-m", "companion", "mcp"],
                {"COMPANION_HOME": str(self.home.root)},
            )
            if added:
                write_text(cfg_mcp, new, newline="")

        steps.append(
            Step(
                "edit",
                "挂上 MCP server（34 个工具；只挂 hooks 的话其余 31 个没人调）",
                str(cfg_mcp),
                path=cfg_mcp,
                fn=patch_mcp,
            )
        )
        return steps

    def verify(self) -> tuple[bool, str]:
        assert self.root is not None
        configured = set(hosthooks.configured_hooks(self.root))
        allowed = hosthooks.approved_pairs(hosthooks.load(hosthooks.allowlist_path(self.root)))
        for event, cmd in self._expected():
            if (event, cmd) not in configured:
                return False, f"config.yaml 里没有 {event} 的 companion 钩子"
            if (event, cmd) not in allowed:
                return False, f"{event} 的钩子没批准（会静默不跑）"
        # MCP 也要验：只验 hooks 的话，工具没挂上也会报「已接上」
        text = read_text(self.root / "config.yaml")
        if not any(ln.rstrip() == "  companion:" for ln in text.splitlines()):
            return False, "config.yaml 的 mcp_servers 里没有 companion（其余 31 个工具不会生效）"
        return True, "钩子已接上且已批准；MCP 已挂"


# ── dsh ──────────────────────────────────────────────────────


class DshHost(Host):
    name = "dsh"
    label = "dsh（cordis 插件，profile 注册）"

    def detect(self) -> bool:
        base = Path(
            self.opts.get("dsh_home") or os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
        ).expanduser()
        if not base.is_dir():
            self.reason = f"没找到 {base}（未安装 dsh）"
            return False
        if not list((base / "profiles").glob("*/package.json")):
            self.reason = f"{base}/profiles 下没有 profile"
            return False
        self.root = base
        return True

    def _profiles(self) -> list[Path]:
        assert self.root is not None
        return sorted(p.parent for p in (self.root / "profiles").glob("*/package.json"))

    def _spec(self) -> str:
        spec = (
            self.opts.get("dsh_spec")
            or os.environ.get("COMPANION_DSH_SPEC")
            or "git+https://github.com/Yullieuli/companion-kit.git"
        ).strip()
        # package.json 的 value 只是"版本/来源"，键已经是包名。
        # 有人会照着 npm 的写法传 dsh-plugin-companion@0.3.0，pnpm 会拼成
        # dsh-plugin-companion@dsh-plugin-companion@0.3.0 然后报 resolver 错。
        prefix = "dsh-plugin-companion@"
        if spec.startswith(prefix):
            spec = spec[len(prefix):]
        return spec

    def _needs(self, prof: Path, spec: str) -> bool:
        """这个 profile 是否需要改。

        逐个判断而不是"有一个要改就全改"：desktop 已经配好时不该被重写——
        重写会改 mtime，dsh 可能因此判定插件有更新，白跑一次安装。
        """
        data = json.loads(read_text(prof / "package.json"))
        deps = data.get("dependencies") or {}
        if deps.get("dsh-plugin-companion") != spec:
            return True
        bundles = ((data.get("dsh") or {}).get("profile") or {}).get("bundles") or []
        return "dsh-plugin-companion" not in bundles

    def plan(self) -> list[Step]:
        spec = self._spec()
        steps: list[Step] = []
        assert self.root is not None
        for prof in self._profiles():
            if not self._needs(prof, spec):
                continue
            pkg = prof / "package.json"

            def patch(pkg: Path = pkg) -> None:
                data = json.loads(read_text(pkg))
                deps = data.setdefault("dependencies", {})
                deps["dsh-plugin-companion"] = spec
                dsh = data.setdefault("dsh", {})
                profile = dsh.setdefault("profile", {})
                bundles = profile.setdefault("bundles", [])
                if "dsh-plugin-companion" not in bundles:
                    bundles.append("dsh-plugin-companion")
                write_text(pkg, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

            steps.append(
                Step(
                    "edit",
                    "依赖 + bundles 里注册 dsh-plugin-companion",
                    str(pkg),
                    path=pkg,
                    fn=patch,
                )
            )

            pnpm = find_exe("pnpm")
            # 测试与离线环境用 no_install 跳过联网；CI 里不该为了一个安装步骤
            # 去拉公网（慢且不稳定，失败还会变成"看环境"的假红）。
            if pnpm and not self.opts.get("no_install"):
                def install(prof: Path = prof, pnpm: str = pnpm) -> None:
                    rc, out = run_cmd([pnpm, "install", "--prefer-offline"], prof, timeout=300)
                    if rc != 0:
                        raise ConnectError(
                            f"pnpm install 没成功（{out[:160]}）；"
                            f"手动补一次：cd {prof} && pnpm install"
                        )
                steps.append(
                    Step("run", "pnpm install（拉插件本体）", str(prof), fn=install, optional=True)
                )
            else:
                steps.append(
                    Step(
                        "run",
                        "跳过插件安装（no_install 或没装 pnpm）；需要时手动：pnpm install",
                        str(prof),
                        fn=lambda: None,
                        optional=True,
                    )
                )
        return steps

    def verify(self) -> tuple[bool, str]:
        for prof in self._profiles():
            data = json.loads(read_text(prof / "package.json"))
            deps = data.get("dependencies") or {}
            if "dsh-plugin-companion" not in deps:
                return False, f"{prof.name} 的 dependencies 里没有插件"
            bundles = ((data.get("dsh") or {}).get("profile") or {}).get("bundles") or []
            if "dsh-plugin-companion" not in bundles:
                return False, f"{prof.name} 的 bundles 里没有插件"
        return True, "profile 已注册"


# ── openclaw ─────────────────────────────────────────────────


class OpenclawHost(Host):
    name = "openclaw"
    label = "openclaw（MCP stdio）"

    def detect(self) -> bool:
        root = Path(
            self.opts.get("openclaw_home")
            or os.environ.get("OPENCLAW_HOME")
            or str(Path.home() / ".openclaw")
        ).expanduser()
        cfg = root / "openclaw.json"
        if not cfg.is_file():
            if find_exe("openclaw"):
                self.root = root
                return True
            self.reason = f"没找到 {cfg}（未安装 openclaw）"
            return False
        self.root = root
        return True

    def plan(self) -> list[Step]:
        assert self.root is not None
        cfg = self.root / "openclaw.json"
        exe, args, env = mcp_spec(self.home)
        steps: list[Step] = []

        # 已经配过就别再动（幂等）。
        if cfg.is_file() and "companion" in read_text(cfg):
            return []

        # 优先让宿主自己写：CLI 知道当前版本的 schema，比我们猜稳。
        # 失败也只是 optional —— 下面还有文件兜底。
        cli = find_exe("openclaw")
        if cli:
            cmd = [cli, "mcp", "add", "--transport", "stdio", "companion", exe, *args]
            steps.append(
                Step("run", "openclaw mcp add companion", " ".join(cmd),
                     fn=lambda: self._cli_add(cmd), optional=True)
            )

        def patch() -> None:
            text = read_text(cfg) if cfg.is_file() else ""
            # CLI 已经写成功了，不重复写。
            if "companion" in text:
                return
            # 已有 mcpServers（老式）就跟着它；否则用 mcp.servers（官方文档口径）。
            if _has_top_key(text, "mcpServers"):
                payload = {"mcpServers": {"companion": {"command": exe, "args": args, "env": env}}}
            elif _has_top_key(text, "mcp"):
                raise ConnectError(
                    f"{cfg.name} 里已有 mcp 段但没有 companion。"
                    "connect 不会覆盖你手写的段——请把 companion 手动加进去，"
                    "或先删掉 mcp 段再跑一次"
                )
            else:
                payload = {"mcp": {"servers": {"companion": {"command": exe, "args": args, "env": env}}}}
            json_merge_top(cfg, payload)

        steps.append(Step("edit", "写入 mcp.servers.companion", str(cfg), path=cfg, fn=patch))
        return steps

    def _cli_add(self, cmd: list[str]) -> None:
        rc, out = run_cmd(cmd, None, timeout=120)
        if rc != 0:
            raise ConnectError(f"openclaw mcp add 失败：{out[:200]}")

    def verify(self) -> tuple[bool, str]:
        assert self.root is not None
        cfg = self.root / "openclaw.json"
        if not cfg.is_file():
            return True, "由 openclaw CLI 写入，未做文件校验"
        text = read_text(cfg)
        if "companion" not in text:
            return False, "openclaw.json 里没有 companion"
        ok, note = json_verify(cfg, ["mcp"] if _has_top_key(text, "mcp") else ["mcpServers"])
        return ok, note


# ── codex ────────────────────────────────────────────────────


class CodexHost(Host):
    name = "codex"
    label = "codex（MCP stdio，TOML）"

    def detect(self) -> bool:
        root = Path(
            self.opts.get("codex_home")
            or os.environ.get("CODEX_HOME")
            or str(Path.home() / ".codex")
        ).expanduser()
        cfg = root / "config.toml"
        if not cfg.is_file():
            if find_exe("codex"):
                self.root = root
                root.mkdir(parents=True, exist_ok=True)
                write_text(cfg, "")
                return True
            self.reason = f"没找到 {cfg}（未安装 codex）"
            return False
        self.root = root
        return True

    def plan(self) -> list[Step]:
        assert self.root is not None
        cfg = self.root / "config.toml"
        if cfg.is_file() and re.search(r"(?m)^\s*\[\s*mcp_servers\.companion\s*\]\s*$", read_text(cfg)):
            return []
        exe, args, env = mcp_spec(self.home)
        steps = [
            Step(
                "edit",
                "追加 [mcp_servers.companion]",
                str(cfg),
                path=cfg,
                fn=lambda: toml_append_server(cfg, "mcp_servers.companion", exe, args, env),
            )
        ]
        return steps

    def verify(self) -> tuple[bool, str]:
        assert self.root is not None
        return toml_verify(self.root / "config.toml", "mcp_servers.companion")


# ── 注册表与总流程 ───────────────────────────────────────────


HOSTS: dict[str, type[Host]] = {
    "hermes": HermesHost,
    "dsh": DshHost,
    "openclaw": OpenclawHost,
    "codex": CodexHost,
}


def _registry_path(home: Home) -> Path:
    return home.root / REGISTRY


def _load_registry(home: Home) -> dict[str, Any]:
    p = _registry_path(home)
    if not p.is_file():
        return {}
    try:
        return json.loads(read_text(p))
    except Exception:
        return {}


def _save_registry(home: Home, data: dict[str, Any]) -> None:
    write_text(_registry_path(home), json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def build(name: str, home: Home, opts: dict[str, Any]) -> Host:
    cls = HOSTS.get(name)
    if cls is None:
        raise ConnectError(f"不认识的宿主：{name}（可选：{'、'.join(HOSTS)}）")
    return cls(home, opts)


def run(
    home: Home,
    names: list[str] | None = None,
    *,
    dry_run: bool = False,
    status_only: bool = False,
    force: bool = False,
    opts: dict[str, Any] | None = None,
) -> list[Result]:
    """探测并连接。返回每个宿主的结果（顺序稳定，便于测试）。"""
    opts = dict(opts or {})
    opts["force"] = force
    wanted = names or list(HOSTS)
    unknown = [n for n in wanted if n not in HOSTS]
    if unknown:
        raise ConnectError(f"不认识的宿主：{'、'.join(unknown)}（可选：{'、'.join(HOSTS)}）")

    results: list[Result] = []
    registry = _load_registry(home)

    for name in wanted:
        host = build(name, home, opts)
        if not host.detect():
            results.append(Result(name, "skipped", host.reason))
            continue
        if status_only:
            results.append(Result(name, "detected", f"已安装：{host.root}"))
            continue

        try:
            steps = host.plan()
        except ConnectError as e:
            results.append(Result(name, "failed", str(e)))
            continue

        if not steps:
            results.append(Result(name, "latest", "没有需要改的东西"))
            continue

        try:
            host.apply(steps, dry_run)
            if dry_run:
                results.append(Result(name, "planned", f"{len(steps)} 步（未执行）",
                                      [str(s.path) for s in steps if s.path]))
                continue
            ok, note = host.verify()
            if not ok:
                restored = host.rollback()
                detail = f"校验失败：{note}"
                if restored:
                    detail += f"；已还原 {len(restored)} 个文件"
                results.append(Result(name, "failed", detail))
                continue
            # key 里带 root：同一个宿主在 Windows 与 WSL 各有自己的配置目录，
            # 而 store 可能是共用的（WSL 的 COMPANION_HOME 指向 /mnt/c/...）。
            # 只按宿主名存的话，后一次 connect 会把前一次的记录覆盖掉，
            # --rollback 就会拿着 WSL 的路径去还原 Windows 的文件。
            registry[f"{name}@{host.root}"] = {
                "host": name,
                "root": str(host.root),
                "files": [str(p) for p in host.touched],
                "created": [str(p) for p in host.created],
                "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            results.append(Result(name, "connected", note, [str(p) for p in host.touched], list(host.warnings)))
        except ConnectError as e:
            restored = host.rollback()
            detail = str(e)
            if restored:
                detail += f"；已还原 {len(restored)} 个文件"
            results.append(Result(name, "failed", detail))

    if not dry_run and not status_only and registry:
        _save_registry(home, registry)
    return results


def rollback(home: Home, names: list[str] | None = None) -> list[str]:
    """按注册表还原。

    registry 的 key 是 ``宿主@配置目录``，所以 ``names`` 传宿主名即可，
    会匹配到该宿主在所有平台上的记录。
    """
    registry = _load_registry(home)
    done: list[str] = []
    for key, rec in registry.items():
        if not isinstance(rec, dict):
            continue
        host_name = rec.get("host") or key.split("@")[0]
        if names and host_name not in names:
            continue
        created = set(rec.get("created", []))
        for f in rec.get("files", []):
            p = Path(f)
            b = latest_backup(p)
            if b:
                shutil.copy2(b, p)
                done.append(f"{host_name}: {p} ← {b.name}")
            elif f in created and p.exists():
                try:
                    p.unlink()
                    done.append(f"{host_name}: {p} 已删除（connect 新建的）")
                except OSError as e:
                    done.append(f"{host_name}: {p} 删不掉：{e}")
            elif p.exists():
                done.append(f"{host_name}: {p.name} 找不到备份，跳过")
    if not done:
        done.append("没有可回滚的记录（先跑一次 companion connect）")
    return done
