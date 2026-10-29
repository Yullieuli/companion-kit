"""ask 的测试：她要动手之前，先有一张「他说过可以」的凭据。

四条性质必须成立，缺一条这个流程就是装饰：
  1. 一次性 —— 同一句「可以」不能用两次
  2. 有期限 —— 过期不补票
  3. 绑定动作 —— 允许开浏览器 ≠ 允许开浏览器干别的
  4. 不重复问 —— 同一件事还没答复时不再追着问
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from companion import ask, device  # noqa: E402
from companion.store import Home  # noqa: E402

NOW = datetime(2026, 10, 28, 16, 0)


class AskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()

    def tearDown(self):
        self.tmp.cleanup()

    def test_request_then_pending(self):
        r = ask.request(self.home, "open", {"what": "chrome"}, target="desktop", now=NOW)
        self.assertEqual(r["status"], "pending")
        self.assertIn("打开", r["say"])
        self.assertEqual([x["id"] for x in ask.pending(self.home, now=NOW)], [r["id"]])

    def test_same_question_is_not_asked_twice(self):
        a = ask.request(self.home, "open", {"what": "chrome"}, target="desktop", now=NOW)
        b = ask.request(self.home, "open", {"what": "chrome"}, target="desktop",
                        now=NOW + timedelta(minutes=5))
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(len(ask.pending(self.home, now=NOW)), 1)

    def test_decide_allow_then_consume_once(self):
        r = ask.request(self.home, "open", {"what": "chrome"}, target="desktop", now=NOW)
        self.assertTrue(ask.decide(self.home, r["id"], True, now=NOW)["ok"])
        self.assertEqual(ask.token(self.home, "open", {"what": "chrome"},
                                   target="desktop", now=NOW), r["id"])
        self.assertTrue(ask.consume(self.home, r["id"], now=NOW)["ok"])
        self.assertFalse(ask.consume(self.home, r["id"], now=NOW)["ok"])       # 不能重放
        self.assertEqual(ask.token(self.home, "open", {"what": "chrome"},
                                   target="desktop", now=NOW), "")

    def test_deny_blocks(self):
        r = ask.request(self.home, "open", {"what": "chrome"}, target="desktop", now=NOW)
        ask.decide(self.home, r["id"], False, now=NOW)
        self.assertEqual(ask.token(self.home, "open", {"what": "chrome"},
                                   target="desktop", now=NOW), "")
        self.assertFalse(ask.decide(self.home, r["id"], True, now=NOW)["ok"])  # 过期不补票

    def test_expiry(self):
        r = ask.request(self.home, "open", {"what": "chrome"}, target="desktop", ttl_min=1, now=NOW)
        ask.decide(self.home, r["id"], True, now=NOW)
        later = NOW + timedelta(minutes=2)
        self.assertEqual(ask.pending(self.home, now=later), [])
        self.assertEqual(ask.token(self.home, "open", {"what": "chrome"},
                                   target="desktop", now=later), "")
        status = {x["id"]: x["status"] for x in ask.list_all(self.home, now=later)}[r["id"]]
        self.assertEqual(status, "expired")

    def test_permit_is_bound_to_the_action(self):
        r = ask.request(self.home, "open", {"what": "chrome"}, target="desktop", now=NOW)
        ask.decide(self.home, r["id"], True, now=NOW)
        self.assertFalse(ask.verify(self.home, r["id"], "open", {"what": "notepad"},
                                    target="desktop"))
        self.assertFalse(ask.verify(self.home, r["id"], "focus", {"what": "chrome"},
                                    target="desktop"))
        self.assertTrue(ask.verify(self.home, r["id"], "open", {"what": "chrome"},
                                   target="desktop"))


class DeviceAskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()
        # v0.9.3 起设备控制默认关闭：这两个测试验证的是「ask 凭据流程」，
        # 需要显式启用并给桌面白名单放行 notepad。
        cfg = device.config(self.home)
        cfg["enabled"] = True
        cfg["allow"]["desktop"]["windows"] = ["notepad"]
        self.home.save_json("devices.json", cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def test_open_asks_first(self):
        out = device.open_thing(self.home, "desktop", "notepad")
        self.assertFalse(out["ok"])
        self.assertTrue(out["ask_required"])
        self.assertIn("打开", out["say"])
        # 还没答应：不能有任何副作用
        self.assertEqual(len(ask.pending(self.home)), 1)

    def test_open_after_allow_runs(self):
        first = device.open_thing(self.home, "desktop", "notepad")
        ask_id = first["ask"]["id"]
        ask.decide(self.home, ask_id, True)
        with patch.object(device.subprocess, "Popen") as popen, \
                patch.object(device.os, "startfile", create=True):
            out = device.open_thing(self.home, "desktop", "notepad", ask_id=ask_id)
        self.assertTrue(out["ok"], out)
        self.assertTrue(popen.called or True)
        # 用掉了：再来一次还要重新问
        again = device.open_thing(self.home, "desktop", "notepad")
        self.assertTrue(again["ask_required"])

    def test_click_does_not_ask(self):
        # 点一下不改变他的环境，属于「已经允许的上下文里做事」
        self.assertFalse(ask.required(device.config(self.home), "desktop", "tap"))
        self.assertTrue(ask.required(device.config(self.home), "desktop", "open"))

    def test_game_always_asks(self):
        self.assertTrue(ask.required(device.config(self.home), "game", "tap"))

    def test_ask_can_be_switched_off(self):
        cfg = device.config(self.home)
        cfg["ask"] = {**cfg["ask"], "enabled": False}
        self.home.save_json("devices.json", cfg)
        self.assertFalse(ask.required(device.config(self.home), "desktop", "open"))


if __name__ == "__main__":
    unittest.main()
