"""device：手。看屏幕、点、打字、开程序。

四类目标共用一个闸，各自一套实现：

    目标      看                    动                     开
    phone    adb screencap         adb shell input        am start
    desktop  GDI BitBlt            SendInput（ctypes）    ShellExecute
    browser  CDP Page.capture…     CDP Input.*            /json/new
    game     同 desktop（按窗口）   同 desktop + 坐标映射   前台窗口置顶

**先有闸，再有手。** 五条：
  1. 白名单：`devices.json` 里列出的包名/窗口/域名才可控
  2. 危险动作：关机、卸载、转账、支付、删除、改密码、装 APK —— 直接拒，不二次询问
     （二次询问会被话术绕过，"不询问"本身就是防线）
  3. 主人限定：只认 owner 会话，群里 @ 她、陌生人私聊一律不触发
  4. 留痕：每个动作写 logs/device.jsonl，可回放
  5. 幂等 + 超时：同一动作 3 秒内不重复；单动作 10 秒中止

边界：只做屏幕与输入。不注入进程、不读内存、不碰反作弊。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import time
import zlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .store import Home

LOG = "logs/device.jsonl"
STATE = "devices.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "owner_only": True,
    "allow": {
        "phone": {"packages": []},
        "desktop": {"windows": []},
        "browser": {"domains": []},
        "game": {"windows": []},
    },
    "deny_words": [
        "关机", "重启", "卸载", "删除", "格式化", "转账", "支付", "付款", "汇款",
        "密码", "验证码", "身份证", "安装包", "apk", "shutdown", "format", "uninstall",
    ],
    "idempotent_seconds": 3,
    "timeout_seconds": 10,
    "max_screenshot_kb": 3000,
}


# ── 闸 ─────────────────────────────────────────────────────
@dataclass
class Verdict:
    ok: bool
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "reason": self.reason}


def config(home: Home) -> dict[str, Any]:
    p = home.root / STATE
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
        return json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        d = json.loads(p.read_text(encoding="utf-8") or "{}")
    except Exception:
        return json.loads(json.dumps(DEFAULT_CONFIG))
    out = json.loads(json.dumps(DEFAULT_CONFIG))
    out.update({k: v for k, v in d.items() if k in DEFAULT_CONFIG})
    return out


def _state(home: Home) -> dict[str, Any]:
    p = home.root / "logs/device_state.json"
    try:
        return json.loads(p.read_text(encoding="utf-8") or "{}")
    except Exception:
        return {}


def _save_state(home: Home, st: dict[str, Any]) -> None:
    p = home.root / "logs/device_state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def check(
    home: Home,
    *,
    target: str,
    action: str,
    args: dict[str, Any] | None = None,
    is_owner: bool = True,
    now: float | None = None,
) -> Verdict:
    cfg = config(home)
    args = args or {}
    now = now or time.time()

    if not cfg.get("enabled", True):
        return Verdict(False, "设备控制没开（devices.json: enabled=false）")
    if cfg.get("owner_only", True) and not is_owner:
        return Verdict(False, "只在主人会话里能动手；群聊/陌生人一律不触发")

    blob = _norm(action + " " + " ".join(str(v) for v in args.values()))
    for w in cfg.get("deny_words") or []:
        if _norm(w) and _norm(w) in blob:
            return Verdict(False, f"危险动作被拒：出现「{w}」。这类事必须本人亲手做")

    allow = (cfg.get("allow") or {}).get(target) or {}
    keys = {"open": ("packages", "windows", "domains"), "launch": ("packages", "windows", "domains")}.get(action, ())
    for k in keys:
        listed = [str(x).lower() for x in (allow.get(k) or [])]
        if not listed:
            continue
        want = ""
        if k == "packages":
            want = _norm(args.get("package", ""))
        elif k == "windows":
            want = _norm(args.get("window") or args.get("app") or args.get("what") or "")
        elif k == "domains":
            want = _norm(args.get("url") or args.get("what") or "")
        if want and not any(x in want for x in listed):
            return Verdict(False, f"不在白名单里：{want}（devices.json 的 allow.{target}.{k}）")

    st = _state(home)
    key = hashlib.sha1(f"{target}|{action}|{json.dumps(args, sort_keys=True, ensure_ascii=False)}".encode()).hexdigest()[:16]
    last = float((st.get("recent") or {}).get(key) or 0)
    if now - last < float(cfg.get("idempotent_seconds", 3)):
        return Verdict(False, f"重复动作（{cfg.get('idempotent_seconds')} 秒内），没有再执行一次")
    return Verdict(True)


def _mark(home: Home, key: str, now: float) -> None:
    st = _state(home)
    recent = st.get("recent") or {}
    recent = {k: v for k, v in recent.items() if now - float(v) < 60}
    st["recent"] = recent
    st["recent"][key] = now
    _save_state(home, st)


def _log(home: Home, row: dict[str, Any]) -> None:
    home.append_jsonl(LOG, row)


def _key(target: str, action: str, args: dict[str, Any]) -> str:
    return hashlib.sha1(f"{target}|{action}|{json.dumps(args, sort_keys=True, ensure_ascii=False)}".encode()).hexdigest()[:16]


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


# ── Android（adb）──────────────────────────────────────────
class PhoneError(RuntimeError):
    pass


def _adb(*args: str, timeout: float = 10) -> subprocess.CompletedProcess:
    exe = shutil.which("adb") or shutil.which("adb.exe")
    if not exe:
        raise PhoneError("找不到 adb（装 platform-tools 或把它放进 PATH）")
    return subprocess.run([exe, *args], capture_output=True, timeout=timeout)


def phone_devices() -> list[str]:
    try:
        r = _adb("devices")
    except Exception:
        return []
    out = []
    for line in r.stdout.decode("utf-8", "ignore").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            out.append(parts[0])
    return out


def screenshot_phone(home: Home, *, serial: str = "") -> Path:
    args = (["-s", serial] if serial else []) + ["exec-out", "screencap", "-p"]
    r = _adb(*args)
    if r.returncode != 0 or not r.stdout.startswith(b"\x89PNG"):
        raise PhoneError("adb 截图失败：" + r.stderr.decode("utf-8", "ignore")[:120] + "（手机连着吗？adb devices 有输出吗）")
    p = _shot_path(home, "phone")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(r.stdout)
    return p


def phone_tap(x: int, y: int, *, serial: str = "") -> None:
    _adb(*(["-s", serial] if serial else []), "shell", "input", "tap", str(int(x)), str(int(y)))


def phone_swipe(x1: int, y1: int, x2: int, y2: int, ms: int = 300, *, serial: str = "") -> None:
    _adb(*(["-s", serial] if serial else []), "shell", "input", "swipe",
         str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)), str(int(ms)))


def phone_type(text: str, *, serial: str = "") -> None:
    _adb(*(["-s", serial] if serial else []), "shell", "input", "text", text.replace(" ", "%s"))


def phone_open(package: str, *, serial: str = "") -> str:
    r = _adb(*(["-s", serial] if serial else []), "shell", "monkey", "-p", package,
             "-c", "android.intent.category.LAUNCHER", "1")
    return (r.stdout or b"").decode("utf-8", "ignore")[-200:]


# ── 浏览器（CDP，最小 WebSocket 客户端）────────────────────
class BrowserError(RuntimeError):
    pass


CDP_PORTS = (9222, 9223, 9224)


def _http_json(url: str, method: str = "GET", timeout: float = 3) -> Any:
    import urllib.request

    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore") or "{}")


def cdp_target(port: int | None = None) -> tuple[int, str] | None:
    """找一个开了 remote-debugging 的页面。返回 (端口, webSocketDebuggerUrl)。"""
    ports = (port,) if port else CDP_PORTS
    for p in ports:
        try:
            tabs = _http_json(f"http://127.0.0.1:{p}/json/list")
        except Exception:
            continue
        for t in tabs if isinstance(tabs, list) else []:
            if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                return p, t["webSocketDebuggerUrl"]
    return None


class _WS:
    """够用就行的 WebSocket 客户端：只做文本帧、只处理单帧消息。"""

    def __init__(self, url: str, timeout: float = 8):
        import base64
        import socket
        from urllib.parse import urlparse

        u = urlparse(url)
        host, port = u.hostname or "127.0.0.1", u.port or 80
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + (("?" + u.query) if u.query else "")
        self.sock.sendall(
            (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
             f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode()
        )
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise BrowserError("WS 握手被拒")
            buf += chunk
        if b" 101 " not in buf.split(b"\r\n")[0]:
            raise BrowserError("WS 握手失败：" + buf.split(b"\r\n")[0].decode("utf-8", "ignore"))
        self._rest = buf.split(b"\r\n\r\n", 1)[1]

    def send(self, text: str) -> None:
        import os as _os

        data = text.encode("utf-8")
        head = bytearray([0x81])
        n = len(data)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        mask = _os.urandom(4)
        head += mask
        self.sock.sendall(bytes(head) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self) -> str:
        buf = self._rest

        def read(n: int) -> bytes:
            nonlocal buf
            while len(buf) < n:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise BrowserError("连接断了")
                buf += chunk
            out, buf = buf[:n], buf[n:]
            return out

        try:
            b1, b2 = read(2)
            op = b1 & 0x0F
            ln = b2 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", read(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", read(8))[0]
            payload = read(ln)
        finally:
            self._rest = buf
        if op == 0x8:
            raise BrowserError("服务端关闭了连接")
        if op == 0x9:
            return self.recv()
        return payload.decode("utf-8", "ignore")

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:
            pass


def cdp_call(method: str, params: dict[str, Any] | None = None, *, port: int | None = None, timeout: float = 8) -> Any:
    found = cdp_target(port)
    if not found:
        raise BrowserError("没有找到开了 remote-debugging 的浏览器（用 --remote-debugging-port=9222 启动）")
    p, url = found
    ws = _WS(url, timeout=timeout)
    try:
        ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        for _ in range(60):
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                if "error" in msg:
                    raise BrowserError(str(msg["error"])[:200])
                return msg.get("result")
    finally:
        ws.close()
    raise BrowserError("CDP 没有回包")


def browser_open(url: str, *, port: int | None = None) -> str:
    """先试 CDP 新建标签，失败就交给系统默认浏览器。"""
    try:
        p = port or (cdp_target() or (0, ""))[0] or 9222
        _http_json(f"http://127.0.0.1:{p}/json/new?{url}", method="PUT")
        return f"cdp:{p}"
    except Exception:
        import webbrowser

        webbrowser.open(url)
        return "system"


def browser_look(home: Home) -> Path:
    try:
        res = cdp_call("Page.captureScreenshot", {"format": "png"})
        import base64

        p = _shot_path(home, "browser")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(base64.b64decode(res.get("data", "")))
        return p
    except Exception:
        # 没有 CDP 就退回截屏桌面（浏览器在前台时一样能看）
        return screenshot_desktop(home, "browser")


def browser_click(x: int, y: int) -> None:
    for kind, extra in (("mousePressed", {"clickCount": 1}), ("mouseReleased", {"clickCount": 1})):
        cdp_call("Input.dispatchMouseEvent",
                 {"type": kind, "x": int(x), "y": int(y), "button": "left", **extra})


def browser_type(text: str) -> None:
    cdp_call("Input.insertText", {"text": text})


# ── 对外三个动作：看 / 动 / 开 ──────────────────────────────
def look(home: Home, target: str = "desktop", *, serial: str = "") -> dict[str, Any]:
    v = check(home, target=target, action="look")
    if not v.ok:
        _log(home, {"at": datetime.now().isoformat(timespec="seconds"), "target": target,
                    "action": "look", "ok": False, "reason": v.reason})
        return {"ok": False, "error": v.reason}
    try:
        if target == "phone":
            p = screenshot_phone(home, serial=serial)
            meta = {"devices": phone_devices()}
        elif target == "browser":
            p = browser_look(home)
            meta = {"cdp": bool(cdp_target())}
        elif target in ("desktop", "game"):
            p = screenshot_desktop(home, target)
            meta = {"foreground": foreground(), "screen": list(screen_size())}
        else:
            return {"ok": False, "error": f"不认识的目标：{target}（phone/desktop/browser/game）"}
        size_kb = max(1, p.stat().st_size // 1024)
        _log(home, {"at": datetime.now().isoformat(timespec="seconds"), "target": target,
                    "action": "look", "ok": True, "shot": str(p), "kb": size_kb})
        return {"ok": True, "target": target, "shot": str(p), "kb": size_kb, **meta}
    except Exception as e:
        _log(home, {"at": datetime.now().isoformat(timespec="seconds"), "target": target,
                    "action": "look", "ok": False, "reason": str(e)})
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def act(
    home: Home,
    target: str,
    action: str,
    args: dict[str, Any] | None = None,
    *,
    is_owner: bool = True,
    serial: str = "",
) -> dict[str, Any]:
    args = args or {}
    v = check(home, target=target, action=action, args=args, is_owner=is_owner)
    if not v.ok:
        _log(home, {"at": datetime.now().isoformat(timespec="seconds"), "target": target,
                    "action": action, "ok": False, "reason": v.reason})
        return {"ok": False, "error": v.reason}
    key = _key(target, action, args)
    started = time.time()
    try:
        if target == "phone":
            if action in ("tap", "click"):
                phone_tap(args["x"], args["y"], serial=serial)
            elif action == "swipe":
                phone_swipe(args["x"], args["y"], args["x2"], args["y2"], int(args.get("ms", 300)), serial=serial)
            elif action in ("type", "text"):
                phone_type(str(args.get("text", "")), serial=serial)
            elif action == "key":
                _adb(*(["-s", serial] if serial else []), "shell", "input", "keyevent", str(args.get("key", "")))
            else:
                return {"ok": False, "error": f"phone 不支持：{action}"}
        elif target in ("desktop", "game"):
            if action in ("tap", "click"):
                click(args["x"], args["y"], double=bool(args.get("double")))
            elif action in ("type", "text"):
                type_text(str(args.get("text", "")))
            else:
                return {"ok": False, "error": f"{target} 不支持：{action}（只做点击/输入）"}
        elif target == "browser":
            if action in ("tap", "click"):
                browser_click(args["x"], args["y"])
            elif action in ("type", "text"):
                browser_type(str(args.get("text", "")))
            else:
                return {"ok": False, "error": f"browser 不支持：{action}"}
        else:
            return {"ok": False, "error": f"不认识的目标：{target}"}

        dt = time.time() - started
        if dt > config(home).get("timeout_seconds", 10):
            return {"ok": False, "error": f"动作超时（{dt:.1f}s）"}
        _mark(home, key, time.time())
        after = look(home, "browser" if target == "browser" else target, serial=serial)
        _log(home, {"at": datetime.now().isoformat(timespec="seconds"), "target": target,
                    "action": action, "args": args, "ok": True, "ms": int(dt * 1000)})
        return {"ok": True, "target": target, "action": action, "ms": int(dt * 1000),
                "after": after.get("shot"), "foreground": foreground() if target != "phone" else ""}
    except Exception as e:
        _log(home, {"at": datetime.now().isoformat(timespec="seconds"), "target": target,
                    "action": action, "ok": False, "reason": str(e)})
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def open_thing(home: Home, target: str, what: str, *, is_owner: bool = True) -> dict[str, Any]:
    args = {"what": what, "url": what, "package": what, "window": what}
    v = check(home, target=target, action="open", args=args, is_owner=is_owner)
    if not v.ok:
        _log(home, {"at": datetime.now().isoformat(timespec="seconds"), "target": target,
                    "action": "open", "what": what, "ok": False, "reason": v.reason})
        return {"ok": False, "error": v.reason}
    try:
        if target == "phone":
            how = "adb:" + phone_open(what)[-60:]
        elif target == "browser":
            how = browser_open(what)
        elif target in ("desktop", "game"):
            if sys.platform == "win32":
                os.startfile(what)  # type: ignore[attr-defined]
                how = "shell"
            else:
                subprocess.Popen([what])
                how = "exec"
        else:
            return {"ok": False, "error": f"不认识的目标：{target}"}
        _log(home, {"at": datetime.now().isoformat(timespec="seconds"), "target": target,
                    "action": "open", "what": what, "ok": True, "how": how})
        return {"ok": True, "target": target, "what": what, "how": how}
    except Exception as e:
        _log(home, {"at": datetime.now().isoformat(timespec="seconds"), "target": target,
                    "action": "open", "what": what, "ok": False, "reason": str(e)})
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def log_tail(home: Home, n: int = 20) -> list[dict[str, Any]]:
    return home.read_jsonl(LOG, limit=n)
