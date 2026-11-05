"""settings：把写死在代码里的几个旋钮挪到 settings.json。

为什么需要这一层：每日开口额度、预算、心情上限、自由时间次数原本都是模块常量，
改一个数字要改代码。看板能读不能写，等于「看得见够不着」。

做法：在模块 import 时把 ``~/.companion/settings.json`` 的覆盖套到常量上。
companion 每次 tick 都是新进程（``python -m companion``），模块重新加载，
所以改完配置**下一个 tick 就生效**，不需要重启任何服务。

失败策略：文件缺失、JSON 坏、类型不对、符号反了，一律退回默认值——
配置层坏了是小事，她的生活循环不能因为一个逗号停摆。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

FILE = "settings.json"

CANDIDATES = [
    os.environ.get("COMPANION_HOME"),
    "/mnt/c/Users/Anran/.companion",
    str(Path.home() / ".companion"),
]


def _path() -> Path | None:
    for c in CANDIDATES:
        if c and Path(c).is_dir():
            return Path(c) / FILE
    return None


def raw() -> dict[str, Any]:
    p = _path()
    if p is None:
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _align(value: Any, exemplar: Any) -> Any:
    """把 json 里的值转成跟默认值同类型。

    数值还要过一道符号闸：默认是正数的项不许写成负数（预算 -5 会让所有动作卡死），
    默认是负数的项（比如 mood_caps.energy = -6，表示每天最多降 6）才允许负值。
    """
    if isinstance(exemplar, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)

    if isinstance(exemplar, (int, float)):
        got = float(value)
        if (exemplar >= 0 and got < 0) or (exemplar < 0 and got > 0):
            raise ValueError("sign mismatch")
        return int(got) if isinstance(exemplar, int) else got

    if isinstance(exemplar, str):
        return str(value)
    return value


def section(name: str, base: dict[str, Any]) -> dict[str, Any]:
    """把 settings.json 里 ``name`` 段的同名键覆盖到 base 上。

    只覆盖 base 里已有的键——不认识的键忽略，避免打错字悄悄生效。
    单个键出错只跳过那一个，其余照常应用。
    """
    ov = raw().get(name)
    if not isinstance(ov, dict):
        return dict(base)
    out = dict(base)
    for key, val in ov.items():
        if key not in base:
            continue
        try:
            out[key] = _align(val, base[key])
        except Exception:
            continue
    return out


def number(name: str, key: str, default: float | int) -> Any:
    """取单个数：``{"mood": {"cap_per_call": 12}}`` 里的 cap_per_call。"""
    ov = raw().get(name)
    if not isinstance(ov, dict) or key not in ov:
        return default
    try:
        return _align(ov[key], default)
    except Exception:
        return default


def int_or(name: str, key: str, default: int) -> int:
    try:
        return int(number(name, key, default))
    except Exception:
        return default
