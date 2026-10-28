"""serve：把事件流开成 HTTP。

本地宿主（hermes / dsh）用不到——它们要么读文件、要么直接调 CLI。
真正需要的是**另一个进程**：看板、手机、远程机器。

三条约束，都是为了"只读、不越界"：
  · 只绑 127.0.0.1（要远程自己加 --host，并且知道自己在干什么）
  · 只有两个 GET：`/health`、`/events`
  · 没有任何写入口。事件是 append-only 的，HTTP 侧不许造事件
"""

from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import events
from .store import Home

MAX_WAIT = 30.0
DEFAULT_PORT = 7788


def _payload_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """事件行里有 epoch 之类的东西，原样给出去，不裁剪 —— 消费方各取所需。"""
    return rows


class _Handler(BaseHTTPRequestHandler):
    home: Home = None  # type: ignore[assignment]
    server_version = "companion-events/1"

    def log_message(self, fmt: str, *args: Any) -> None:  # 别把访问日志打到她的 stderr
        return

    def _send(self, code: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(raw)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 — http.server 的约定
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        if parsed.path == "/health":
            self._send(200, {"ok": True, "home": str(self.home.root),
                             "events": len(events.read_all(self.home))})
            return
        if parsed.path != "/events":
            self._send(404, {"ok": False, "error": "只有 /health 和 /events"})
            return
        try:
            since = int((q.get("since") or ["0"])[0])
        except ValueError:
            self._send(400, {"ok": False, "error": "since 要是整数"})
            return
        try:
            wait = min(MAX_WAIT, max(0.0, float((q.get("wait") or ["0"])[0])))
        except ValueError:
            self._send(400, {"ok": False, "error": "wait 要是秒数"})
            return
        rows = events.read_all(self.home)
        if len(rows) <= since and wait > 0:
            # 自己等，不用 events.wait：那个会推进共享 cursor，
            # HTTP 上的一个读者不该把事件从别的读者手里抢走
            deadline = time.time() + wait
            while time.time() < deadline:
                rows = events.read_all(self.home)
                if len(rows) > since:
                    break
                time.sleep(0.3)
        fresh = rows[since:]
        self._send(200, {"ok": True, "events": _payload_events(fresh), "cursor": len(rows)})


def make_server(home: Home, host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    handler = type("Handler", (_Handler,), {"home": home})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv


def serve(home: Home, host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> int:
    home.init()
    srv = make_server(home, host, port)
    real_host, real_port = srv.server_address[0], srv.server_address[1]
    print(f"事件流已开：http://{real_host}:{real_port}/events?since=0   （Ctrl-C 停）")
    if real_host not in ("127.0.0.1", "::1", "localhost"):
        print(f"⚠ 绑在 {real_host}：任何能连到这个地址的人都能读事件，确认这是你要的")
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0
