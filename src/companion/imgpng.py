"""PNG 编码 + 解码 + 截图落盘路径。

单独一个文件，是为了不让 zlib/struct 和 ctypes、socket 混在同一个模块里
（原因见 docs/AV.md）。一张截图只需要 IHDR+IDAT+IEND，不引 Pillow。

也提供最小 PNG 解码（8 位、灰度/RGB/RGBA、非隔行）——
表情包自动提取要读图的像素来判断颜色，不引 Pillow 就得自己解。
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


def _unfilter(raw: bytes, width: int, height: int, channels: int) -> bytearray:
    """还原 PNG 的逐行滤波（5 种）。"""
    bpp = channels
    stride = width * bpp
    out = bytearray(stride * height)
    pos = 0
    prev = bytearray(stride)
    for y in range(height):
        if pos >= len(raw):
            break
        ft = raw[pos]
        pos += 1
        line = bytearray(raw[pos : pos + stride])
        if len(line) < stride:
            line.extend(b"\x00" * (stride - len(line)))
        pos += stride
        if ft == 1:  # Sub
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ft == 2:  # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ft == 3:  # Average
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ft == 4:  # Paeth
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        out[y * stride : (y + 1) * stride] = line
        prev = line
    return out


def read_png(path: Path) -> tuple[int, int, bytes]:
    """读一张 PNG，返回 (宽, 高, RGB 字节)。不支持就抛 ValueError。

    只支持 8 位、灰度/RGB/RGBA、非隔行 —— 够用，且不引 Pillow。
    """
    data = Path(path).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("不是 PNG")
    pos = 8
    width = height = 0
    depth = color = interlace = 0
    idat = bytearray()
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        tag = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if tag == b"IHDR":
            width, height, depth, color, _comp, _filt, interlace = struct.unpack(">IIBBBBB", body)
        elif tag == b"IDAT":
            idat.extend(body)
        elif tag == b"IEND":
            break
    if depth != 8:
        raise ValueError(f"只支持 8 位深度（这张是 {depth}）")
    if interlace:
        raise ValueError("不支持隔行 PNG")
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color)
    if not channels:
        raise ValueError(f"不支持的颜色类型 {color}")
    raw = zlib.decompress(bytes(idat))
    flat = _unfilter(raw, width, height, channels)
    if channels == 3:
        return width, height, bytes(flat)
    # 灰度 / 灰度+Alpha / RGBA → 统一成 RGB
    rgb = bytearray(width * height * 3)
    for i in range(width * height):
        if channels == 1:
            g = flat[i]
            rgb[i * 3 : i * 3 + 3] = bytes((g, g, g))
        elif channels == 2:
            g = flat[i * 2]
            rgb[i * 3 : i * 3 + 3] = bytes((g, g, g))
        else:
            rgb[i * 3 : i * 3 + 3] = flat[i * 4 : i * 4 + 3]
    return width, height, bytes(rgb)


def _shot_path(home: Home, target: str) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    return home.root / "shots" / f"{ts}-{target}.png"

