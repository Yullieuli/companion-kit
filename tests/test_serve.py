"""serve：事件流的 HTTP 只读出口。

本地宿主直接读文件，用不到这个；它服务的是**别的进程**（看板、手机、远程机器）。
所以两条硬规矩：默认只绑 127.0.0.1、没有任何写入口。

测试真起一个 server（随机端口），走真实 HTTP —— 这一层不 mock，
因为要验的正是"协议对不对"，mock 掉就把被测对象换掉了。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from companion import events, serve  # noqa: E402
from companion.store import Home  # noqa: E402


class ServeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()
        self.srv = serve.make_server(self.home, "127.0.0.1", 0)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=3)
        self.tmp.cleanup()

    def get(self, path: str) -> dict:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    def test_health(self):
        got = self.get("/health")
        self.assertTrue(got["ok"])
        self.assertEqual(got["events"], 0)

    def test_events_since(self):
        events.emit(self.home, "wake_due", action="speak", reason="想说话")
        events.emit(self.home, "mood_shift", frm="平静", to="开心")
        got = self.get("/events?since=0")
        self.assertEqual(len(got["events"]), 2)
        self.assertEqual(got["cursor"], 2)
        self.assertEqual(got["events"][0]["kind"], "wake_due")

        only_new = self.get("/events?since=1")
        self.assertEqual([e["kind"] for e in only_new["events"]], ["mood_shift"])

    def test_long_poll_returns_when_new_event_arrives(self):
        def later():
            import time

            time.sleep(0.4)
            events.emit(self.home, "wake_due", action="speak")

        threading.Thread(target=later, daemon=True).start()
        got = self.get("/events?since=0&wait=5")
        self.assertEqual(len(got["events"]), 1)
        self.assertEqual(got["events"][0]["kind"], "wake_due")

    def test_unknown_path_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/shell")
        self.assertEqual(ctx.exception.code, 404)

    def test_bad_params_are_400(self):
        for path in ("/events?since=abc", "/events?wait=abc"):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.get(path)
            self.assertEqual(ctx.exception.code, 400)

    def test_reading_does_not_steal_cursor(self):
        events.emit(self.home, "wake_due", action="speak")
        before = events.cursor(self.home)
        self.get("/events?since=0")
        self.assertEqual(events.cursor(self.home), before)


if __name__ == "__main__":
    unittest.main()
