"""把本地文件搬到 NapCat 能读到的地方，并给出可直接发送的 CQ 码。

为什么需要这一层：NapCat 跑在 Docker 里，只挂了三个宿主机目录
（qq / napcat-config / napcat-plugins）。而截图、表情、语音都落在
``~/.companion`` 下面，容器根本看不见 —— 所以
``[CQ:image,file=C:\\Users\\...\\x.png]`` 发出去是空的。

把文件复制进 napcat-config 之后，容器里的路径就是 ``/app/napcat/config/out/x.png``，
这条路 2026-11-05 实测可读。
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

# 宿主机上的共享目录（WSL 与 Windows 两套写法，按当前环境挑一个）
_ROOTS = (
    "/mnt/c/AI/astrbot/data/napcat-config",
    r"C:\AI\astrbot\data\napcat-config",
)
# 容器内看到的路径
_CONTAINER = "/app/napcat/config"
_SUB = "out"
_KEEP_HOURS = 24


def share_dir() -> Path | None:
    """当前环境能看到的共享目录；找不到就返回 None（此时发图会失败）。

    ``COMPANION_NAPCAT_SHARE`` 可以覆盖（测试用，也可以指到别的挂载点）。
    """
    override = os.environ.get("COMPANION_NAPCAT_SHARE")
    if override:
        d = Path(override)
        return d if d.name == _SUB else d / _SUB
    for p in _ROOTS:
        d = Path(p)
        if d.is_dir():
            return d / _SUB if d.name != _SUB else d
    return None


def _cleanup(dir_: Path) -> None:
    """清掉 24 小时前的转发文件，别让 out/ 无限涨。"""
    cut = time.time() - _KEEP_HOURS * 3600
    try:
        for f in dir_.iterdir():
            if f.is_file() and f.stat().st_mtime < cut:
                f.unlink(missing_ok=True)
    except OSError:
        pass


def relay(path: str | Path, *, kind: str = "image", name: str = "") -> dict[str, str]:
    """把 *path* 复制进 NapCat 共享目录。

    返回 ``{"path": 容器内路径, "cq": CQ码}``；失败返回 ``{"error": ...}``。
    kind 决定 CQ 码类型：image / record / video / file。
    """
    src = Path(str(path or ""))
    if not src.is_file():
        return {"error": f"文件不存在: {src}"}
    root = share_dir()
    if root is None:
        return {"error": "找不到 NapCat 共享目录（/mnt/c/AI/astrbot/data/napcat-config）"}
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"error": f"共享目录不可写: {e}"}
    _cleanup(root)

    stem = name or src.stem
    dst = root / f"{stem}{src.suffix}"
    n = 1
    while dst.exists():
        dst = root / f"{stem}-{n}{src.suffix}"
        n += 1
    try:
        shutil.copy2(src, dst)
    except OSError as e:
        return {"error": f"复制失败: {e}"}

    inner = f"{_CONTAINER}/{_SUB}/{dst.name}"
    return {"path": inner, "cq": f"[CQ:{kind},file={inner}]"}


def attach(result: dict, path_key: str = "path", *, kind: str = "image") -> dict:
    """给工具返回值补上 cq 字段。原地改并返回同一个 dict。

    找不到共享目录时只加一个 note，不破坏原有返回 —— 让她至少知道为什么发不出去。
    """
    if not isinstance(result, dict) or not result.get("ok"):
        return result
    p = result.get(path_key)
    if not p:
        return result
    out = relay(p, kind=kind)
    if "cq" in out:
        result["cq"] = out["cq"]
        result["send_path"] = out["path"]
    else:
        result["relay_error"] = out.get("error", "未知错误")
    return result
