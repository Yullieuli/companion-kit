"""浏览器控制：CDP 的 HTTP 端点 + 一个够用的 WebSocket 客户端。

单独成文件的理由见 docs/AV.md：裸 socket + base64 + struct 同时出现是典型的
"回连"特征，和 ctypes 放一起会被杀软判成 ShellLoader。这里只碰网络。
"""

from __future__ import annotations

import json
import os
import struct
from pathlib import Path
from typing import Any

from .imgpng import _shot_path
from .devwin import screenshot_desktop


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


