"""PNG 编码 + 截图落盘路径。

单独一个文件，是为了不让 zlib/struct 和 ctypes、socket 混在同一个模块里
（原因见 docs/AV.md）。一张截图只需要 IHDR+IDAT+IEND，不引 Pillow。
"""

from __future__ import annotations

import struct
import zlib
from datetime import datetime
from pathlib import Path

from .store import Home


# ── PNG 编码（不引 Pillow：一张截图只要 IHDR+IDAT+IEND）──────
def write_png(path: Path, width: int, height: int, rgb: bytes) -> Path:
    raw = b"".join(b"\x00" + rgb[y * width * 3 : (y + 1) * width * 3] for y in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")
    )
    return path


def _shot_path(home: Home, target: str) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    return home.root / "shots" / f"{ts}-{target}.png"

