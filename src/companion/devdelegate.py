"""devdelegate：在 WSL 里把 `device` 交给 Windows 侧执行。

为什么需要：她跑在 WSL，而截屏、SendInput、窗口枚举全是 Win32 调用。
WSL 里直接调只会在第一个 API 处抛错，等于"手"是断的。

做法不是再写一份 PowerShell 实现（两份实现必然走样），而是
**让 Windows 的 python 跑同一份代码**：`python.exe -m companion device ...`。
所以这一层只做三件事：找到 Windows 解释器、把参数原样递过去、把回来的
Windows 路径翻成 WSL 能打开的路径。

配置（`devices.json`，因为路径是这台机器的事实，不是代码的事）：
    "windows": {"python": "C:\\\\...\\\\python.exe", "src": "C:\\\\...\\\\companion-kit\\\\src"}

没有配置就返回 None，调用方按"本机不支持"处理 —— 不猜、不自动装。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def in_wsl() -> bool:
    if sys.platform == "win32":
        return False
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8", errors="ignore").lower()
    except Exception:
        return False


def win_to_wsl(path: str) -> str:
    """C:////Users////... 换成 /mnt/c/Users/...（用来在 WSL 里判断文件在不在）"""
    p = str(path or "").replace("\\", "/")
    if len(p) > 2 and p[1] == ":":
        return "/mnt/" + p[0].lower() + "/" + p[3:].lstrip("/")
    return p


def config_of(home) -> dict[str, str]:
    cfg = home._json("devices.json", {})  # noqa: SLF001 — store 的唯一出口
    win = cfg.get("windows") or {}
    return {"python": str(win.get("python") or ""), "src": str(win.get("src") or "")}


def wanted(home) -> bool:
    """该不该转发。三个条件：在 WSL、不是 Windows、配置齐了。"""
    if sys.platform == "win32" or not in_wsl():
        return False
    c = config_of(home)
    return bool(c["python"] and Path(win_to_wsl(c["python"])).exists())


def _env(home, src: str) -> dict[str, str]:
    """WSL → Win32 只传 WSLENV 里列出来的变量。

    踩过：直接设 PYTHONPATH 不会传过去，Windows 那边的 python 导入不到 companion，
    报出来是"没有这个模块"，看起来像装错了。`/p` 让 WSL 把路径翻成 C:////...。
    """
    env = {**os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = win_to_wsl(src)
    env["COMPANION_HOME"] = str(home.root)
    env["WSLENV"] = "PYTHONPATH/p:COMPANION_HOME/p:PYTHONIOENCODING"
    return env


def wsl_to_win(path: str, home_root: str = "") -> str:
    """把 WSL 路径翻成 Windows 风格（子进程要用它比对自己在哪个盘）。

    /mnt/c/Users/... 变成 C: 开头加反斜杠分隔
    """
    bs = chr(92)
    p = str(path or home_root or "")
    if p.startswith("/mnt/") and len(p) > 6:
        drive = p[5].upper()
        rest = p[6:].lstrip("/")
        return drive + ":" + bs + rest.replace("/", bs)
    return p


def _translate(obj: Any, pairs: list[tuple[str, str]]) -> Any:
    """返回值里的 Windows 路径换成 WSL 能打开的写法。

    踩过：只换前缀不够 —— `C:////a////b.png` 换完是 `/mnt/c/a\\b.png`，
    Linux 打不开。命中前缀之后要把余下的反斜杠也换掉。
    """
    if isinstance(obj, str):
        for win, wsl in pairs:
            if win and win in obj:
                obj = obj.replace(win, wsl).replace("\\", "/")
                break
        return obj
    if isinstance(obj, list):
        return [_translate(x, pairs) for x in obj]
    if isinstance(obj, dict):
        return {k: _translate(v, pairs) for k, v in obj.items()}
    return obj


def run(argv: list[str], home, *, timeout: int = 180) -> tuple[int, str, str]:
    """原样转发，返回值已把 Windows 路径翻好。"""
    c = config_of(home)
    if not wanted(home):
        msg = "这台机器上没法用设备控制：需要在 devices.json 里配 windows.python / windows.src"
        return 1, json.dumps({"ok": False, "error": msg}, ensure_ascii=False), ""
    # 启动要用 /mnt/c/... 这条路（Linux 认的是它）；配置里写 C: 开头是给人看的
    py, src = win_to_wsl(c["python"]), c["src"]
    win_root = wsl_to_win(str(home.root), str(home.root))
    cmd = [py, "-m", "companion", *argv]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, env=_env(home, src), timeout=timeout)
    except Exception as e:
        return 1, json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False), ""
    out = done.stdout or ""
    if out.strip().startswith("{"):
        try:
            data = json.loads(out)
            bs = chr(92)
            data = _translate(data, [(win_root, str(home.root)),
                                     (win_root.replace(bs, "/"), str(home.root))])
            out = json.dumps(data, ensure_ascii=False, indent=2) + chr(10)
        except Exception:
            pass
    return done.returncode, out, done.stderr or ""


def call(argv: list[str], home, *, timeout: int = 180) -> int:
    """转发并回显。返回退出码，交给 cli.main 当自己的退出码。"""
    rc, out, err = run(argv, home, timeout=timeout)
    sys.stdout.write(out)
    if err.strip():
        sys.stderr.write(err)
    return rc
