"""Windows 桌面：GDI 截图 + SendInput 输入。

单独成文件的理由见 docs/AV.md：ctypes 调用和网络代码放在同一个模块里，
杀软会按"加载器"特征打分。这里只碰窗口与输入，不碰网络。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

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


def screen_size(virtual: bool = True) -> tuple[int, int]:
    """屏幕尺寸。默认给**虚拟桌面**（所有显示器拼起来）。

    只报主屏会出两种错：双屏截图只截到一半；游戏在副屏时坐标全偏。
    """
    ctypes, wintypes, u, _ = _win()
    if virtual:
        w, h = int(u.GetSystemMetrics(78)), int(u.GetSystemMetrics(79))   # SM_CXVIRTUALSCREEN / CY
        if w > 0 and h > 0:
            return w, h
    return int(u.GetSystemMetrics(0)), int(u.GetSystemMetrics(1))


def screen_origin() -> tuple[int, int]:
    """虚拟桌面的左上角。副屏在左边时是负数，截图要带上这个偏移。"""
    ctypes, wintypes, u, _ = _win()
    try:
        return int(u.GetSystemMetrics(76)), int(u.GetSystemMetrics(77))   # SM_XVIRTUALSCREEN / Y
    except Exception:
        return 0, 0


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


def _flush_png(home: Home, target: str, mem: int, bmp: int, w: int, h: int, g) -> Path:
    """把内存位图导成 PNG。截图与窗口捕获共用这一份，免得两处各写一遍 DIB。"""
    ctypes, wintypes, _, _ = _win()

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
    return write_png(_shot_path(home, target), w, h, bytes(rgb))


def screenshot_desktop(home: Home, target: str = "desktop", *, region: tuple[int, int, int, int] | None = None) -> Path:
    ctypes, wintypes, u, g = _win()
    if region:
        x, y, w, h = region
    else:
        ox, oy = screen_origin()
        w, h = screen_size()
        x, y = ox, oy
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
        return _flush_png(home, target, mem, bmp, w, h, g)
    finally:
        u.ReleaseDC(0, hdc)


def screenshot_window(home: Home, want: str, *, target: str = "window") -> Path:
    """按标题抓一个窗口，哪怕它被别的窗口盖着。

    为什么不用屏幕截图裁：被盖住的部分裁出来是别人的窗口，她会对着别人的内容说话。
    PrintWindow + PW_RENDERFULLCONTENT 能让窗口自己画一遍自己。
    """
    ctypes, wintypes, u, g = _win()
    u.PrintWindow.restype = wintypes.BOOL
    u.PrintWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
    rect = window_rect(want)
    if not rect:
        raise DesktopError(f"没找到标题含「{want}」的窗口")
    w, h = int(rect["w"]), int(rect["h"])
    if w < 8 or h < 8:
        raise DesktopError("窗口太小，截不了")
    hwnd = ctypes.c_void_p(int(rect["hwnd"]))
    hdc = u.GetDC(0)
    mem = g.CreateCompatibleDC(hdc)
    bmp = g.CreateCompatibleBitmap(hdc, w, h)
    g.SelectObject(mem, bmp)
    try:
        ok = u.PrintWindow(hwnd, mem, 2)  # PW_RENDERFULLCONTENT：全内容，含 DirectComposition
        if not ok:
            # 某些老程序只认 BitBlt；退一步，至少把可见区域拿到
            if not g.BitBlt(mem, 0, 0, w, h, hdc, int(rect["x"]), int(rect["y"]), 0x00CC0020):
                raise DesktopError("PrintWindow 与 BitBlt 都失败")
        return _flush_png(home, target, mem, bmp, w, h, g)
    finally:
        u.ReleaseDC(0, hdc)


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


def drag(x1: int, y1: int, x2: int, y2: int, *, ms: int = 300, steps: int = 12) -> None:
    """按住拖动。给「滑屏 / 拖拽 / 拉进度条」用。

    为什么用插值而不是一次跳过去：多数程序只在 WM_MOUSEMOVE 的连续帧里
    才认得出这是一次拖动，一步跳到终点会被当成点击。
    """
    ctypes, _, u, _ = _win()
    LEFTDOWN, LEFTUP = 0x0002, 0x0004
    delay = max(0.008, (ms / 1000) / max(1, steps))
    u.SetCursorPos(int(x1), int(y1))
    time.sleep(0.06)
    u.mouse_event(LEFTDOWN, 0, 0, 0, 0)
    try:
        for i in range(1, steps + 1):
            u.SetCursorPos(int(x1 + (x2 - x1) * i / steps), int(y1 + (y2 - y1) * i / steps))
            time.sleep(delay)
    finally:
        u.mouse_event(LEFTUP, 0, 0, 0, 0)


# 组合键能用的名字。写全一点，省得她猜「回车该写什么」
VK = {
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "backspace": 0x08, "delete": 0x2E, "insert": 0x2D,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B,
    "capslock": 0x14, "printscreen": 0x2C,
}
KEYUP = 0x0002


def _vk(name: str) -> int:
    n = name.strip().lower()
    if n in VK:
        return VK[n]
    if len(n) == 1 and n.isalnum():
        return ord(n.upper())
    if n.startswith("f") and n[1:].isdigit() and 1 <= int(n[1:]) <= 24:
        return 0x70 + int(n[1:]) - 1
    raise DesktopError(f"不认识的键：{name}（可用：单个字母数字、f1-f24、enter/esc/tab/space/方向键/ctrl/shift/alt）")


def key(combo: str) -> None:
    """按一个键或一组组合键：`enter` / `ctrl+s` / `alt+tab`。"""
    _, _, u, _ = _win()
    parts = [p for p in str(combo or "").replace(" ", "").split("+") if p]
    if not parts:
        raise DesktopError("没给键名")
    codes = [_vk(p) for p in parts]
    for c in codes:
        u.keybd_event(c, 0, 0, 0)
        time.sleep(0.02)
    for c in reversed(codes):
        u.keybd_event(c, 0, KEYUP, 0)
        time.sleep(0.02)


def windows(limit: int = 40) -> list[dict[str, Any]]:
    """列出可见的顶层窗口。给「切到某个程序」和「游戏在前台吗」用。"""
    ctypes, wintypes, u, _ = _win()
    u.EnumWindows.restype = wintypes.BOOL
    u.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
    u.IsWindowVisible.argtypes = [ctypes.c_void_p]
    u.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]

    found: list[dict[str, Any]] = []

    def cb(hwnd, _lparam):
        if not u.IsWindowVisible(hwnd):
            return True
        n = u.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value.strip()
        if not title:
            return True
        r = wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(r))
        w, h = int(r.right - r.left), int(r.bottom - r.top)
        if w < 80 or h < 60:
            return True
        found.append({"hwnd": int(hwnd), "title": title[:80], "w": w, "h": h,
                      "x": int(r.left), "y": int(r.top)})
        return True

    u.EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, ctypes.c_void_p)(cb), 0)
    found.sort(key=lambda x: x["w"] * x["h"], reverse=True)
    return found[:limit]


def focus(want: str) -> dict[str, Any]:
    """把匹配标题的窗口提到前台。返回被切过去的那个窗口。"""
    ctypes, wintypes, u, _ = _win()

    key_ = str(want or "").strip().lower()
    if not key_:
        raise DesktopError("没给窗口名")
    cands = [w for w in windows(200) if key_ in w["title"].lower()]
    if not cands:
        raise DesktopError(f"没找到标题含「{want}」的窗口")
    win = cands[0]
    hwnd = ctypes.c_void_p(win["hwnd"])
    u.ShowWindow(hwnd, 9)          # SW_RESTORE：最小化的也能提上来
    time.sleep(0.1)
    u.SetForegroundWindow(hwnd)
    time.sleep(0.2)
    return {"ok": True, "hwnd": win["hwnd"], "title": win["title"],
            "foreground": foreground(), "matched": len(cands),
            "others": [w["title"] for w in cands[1:4]]}


def window_rect(want: str) -> dict[str, Any]:
    """查某个窗口的位置与大小（游戏要按这个算坐标）。"""
    key_ = str(want or "").strip().lower()
    cands = [w for w in windows(200) if key_ and key_ in w["title"].lower()]
    return cands[0] if cands else {}



