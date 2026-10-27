"""Windows 桌面：GDI 截图 + SendInput 输入。

单独成文件的理由见 docs/AV.md：ctypes 调用和网络代码放在同一个模块里，
杀软会按"加载器"特征打分。这里只碰窗口与输入，不碰网络。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from .imgpng import _shot_path, write_png


# ── Windows 桌面（GDI 截图 + SendInput 输入）───────────────
class DesktopError(RuntimeError):
    pass


def _win():
    if sys.platform != "win32":
        raise DesktopError("桌面控制目前只实现了 Windows（这台机器是 %s）" % sys.platform)
    import ctypes
    from ctypes import wintypes

    u = ctypes.WinDLL("user32", use_last_error=True)
    g = ctypes.WinDLL("gdi32", use_last_error=True)
    # 64 位下句柄是 void*：不声明 restype 会被截成 32 位 int，截图直接黑屏
    u.GetDC.restype = ctypes.c_void_p
    u.GetDC.argtypes = [ctypes.c_void_p]
    u.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    u.GetForegroundWindow.restype = ctypes.c_void_p
    g.CreateCompatibleDC.restype = ctypes.c_void_p
    g.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    g.CreateCompatibleBitmap.restype = ctypes.c_void_p
    g.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    g.SelectObject.restype = ctypes.c_void_p
    g.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    g.BitBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_uint32]
    g.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
    g.DeleteObject.argtypes = [ctypes.c_void_p]
    g.DeleteDC.argtypes = [ctypes.c_void_p]
    return ctypes, wintypes, u, g


def screen_size() -> tuple[int, int]:
    ctypes, wintypes, u, _ = _win()
    return int(u.GetSystemMetrics(0)), int(u.GetSystemMetrics(1))


def foreground() -> str:
    try:
        ctypes, _, u, _ = _win()
        hwnd = u.GetForegroundWindow()
        n = u.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value
    except Exception:
        return ""


def screenshot_desktop(home: Home, target: str = "desktop", *, region: tuple[int, int, int, int] | None = None) -> Path:
    ctypes, wintypes, u, g = _win()
    x, y, w, h = region or (0, 0, *screen_size())
    hdc = u.GetDC(0)
    if not hdc:
        raise DesktopError("拿不到屏幕 DC")
    try:
        mem = g.CreateCompatibleDC(hdc)
        bmp = g.CreateCompatibleBitmap(hdc, w, h)
        g.SelectObject(mem, bmp)
        SRCCOPY = 0x00CC0020
        if not g.BitBlt(mem, 0, 0, w, h, hdc, x, y, SRCCOPY):
            raise DesktopError("BitBlt 失败（可能是锁屏或无桌面会话）")

        class BIH(ctypes.Structure):
            _fields_ = [
                ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
            ]

        bi = BIH()
        bi.biSize = ctypes.sizeof(BIH)
        bi.biWidth = w
        bi.biHeight = -h  # 负数 = 自上而下
        bi.biPlanes = 1
        bi.biBitCount = 32
        bi.biCompression = 0

        buf = ctypes.create_string_buffer(w * h * 4)
        if not g.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0):
            raise DesktopError("GetDIBits 失败")
        raw = buf.raw
        rgb = bytearray(w * h * 3)
        for i in range(w * h):
            b, gg, r = raw[i * 4], raw[i * 4 + 1], raw[i * 4 + 2]
            rgb[i * 3] = r
            rgb[i * 3 + 1] = gg
            rgb[i * 3 + 2] = b
        g.DeleteObject(bmp)
        g.DeleteDC(mem)
    finally:
        u.ReleaseDC(0, hdc)
    return write_png(_shot_path(home, target), w, h, bytes(rgb))


def click(x: int, y: int, *, double: bool = False) -> None:
    ctypes, _, u, _ = _win()
    MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
    u.SetCursorPos(int(x), int(y))
    time.sleep(0.05)
    for _ in range(2 if double else 1):
        u.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.03)
        u.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        time.sleep(0.08 if double else 0)


def type_text(text: str) -> None:
    """按 Unicode 逐字打，兼容中文（不用剪贴板，避免污染他的剪贴板）。"""
    ctypes, wintypes, u, _ = _win()

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    KEYEVENTF_UNICODE, KEYEVENTF_KEYUP = 0x0004, 0x0002
    for ch in text:
        for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
            inp = INPUT()
            inp.type = 1
            inp.ki.wVk = 0
            inp.ki.wScan = ord(ch)
            inp.ki.dwFlags = flags
            u.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
            time.sleep(0.004)


