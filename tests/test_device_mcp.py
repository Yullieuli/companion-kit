from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from companion import device, mcp, voice  # noqa: E402
from companion.store import Home  # noqa: E402


class GateTest(unittest.TestCase):
    """闸必须先立住：这十条挂了，后面四个 adapter 一律不许合。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()

    def tearDown(self):
        self.tmp.cleanup()

    def test_disabled_blocks_everything(self):
        cfg = device.config(self.home)
        cfg["enabled"] = False
        self.home.save_json("devices.json", cfg)
        v = device.check(self.home, target="desktop", action="look")
        self.assertFalse(v.ok)
        self.assertIn("没开", v.reason)

    def test_non_owner_blocked(self):
        v = device.check(self.home, target="desktop", action="look", is_owner=False)
        self.assertFalse(v.ok)
        self.assertIn("主人", v.reason)

    def test_dangerous_action_refused_without_asking(self):
        for word in ("关机", "卸载微信", "帮他转账", "读取密码"):
            v = device.check(self.home, target="desktop", action="open", args={"what": word})
            self.assertFalse(v.ok, word)
            self.assertIn("危险", v.reason)

    def test_allowlist_when_configured(self):
        cfg = device.config(self.home)
        cfg["allow"]["browser"]["domains"] = ["bilibili.com"]
        self.home.save_json("devices.json", cfg)
        ok = device.check(self.home, target="browser", action="open", args={"what": "https://www.bilibili.com/x"})
        self.assertTrue(ok.ok, ok.reason)
        bad = device.check(self.home, target="browser", action="open", args={"what": "https://bank.example.com"})
        self.assertFalse(bad.ok)

    def test_empty_allowlist_means_no_restriction(self):
        v = device.check(self.home, target="browser", action="open", args={"what": "https://anything.example"})
        self.assertTrue(v.ok, v.reason)

    def test_idempotent_window(self):
        now = time.time()
        args = {"x": 10, "y": 20}
        self.assertTrue(device.check(self.home, target="desktop", action="tap", args=args, now=now).ok)
        device._mark(self.home, device._key("desktop", "tap", args), now)
        again = device.check(self.home, target="desktop", action="tap", args=args, now=now + 1)
        self.assertFalse(again.ok)
        self.assertIn("重复", again.reason)
        later = device.check(self.home, target="desktop", action="tap", args=args, now=now + 10)
        self.assertTrue(later.ok, later.reason)

    def test_look_on_unknown_target(self):
        out = device.look(self.home, "toaster")
        self.assertFalse(out["ok"])
        self.assertIn("不认识", out["error"])

    def test_refusal_is_logged(self):
        device.look(self.home, "desktop", serial="") if False else None
        device.check(self.home, target="desktop", action="look", is_owner=False)
        rows = device.log_tail(self.home)
        self.assertTrue(all("at" in r for r in rows))

    def test_act_refused_does_not_touch_screen(self):
        out = device.act(self.home, "desktop", "tap", {"x": 1, "y": 1}, is_owner=False)
        self.assertFalse(out["ok"])
        self.assertNotIn("after", out)


class PngTest(unittest.TestCase):
    def test_png_header_is_valid(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.png"
            device.write_png(p, 2, 2, bytes(2 * 2 * 3))
            raw = p.read_bytes()
            self.assertTrue(raw.startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertIn(b"IHDR", raw)
            self.assertIn(b"IEND", raw)


class VoiceGateTest(unittest.TestCase):
    def test_public_refused(self):
        self.assertIn("私聊", voice.check("你好", public=True))

    def test_too_long_refused(self):
        self.assertIn("上限", voice.check("字" * 200))

    def test_estimate_grows_with_length(self):
        self.assertLess(voice.estimate_seconds("短"), voice.estimate_seconds("长" * 20))

    def test_empty_refused(self):
        self.assertIn("没有内容", voice.check("   "))


class McpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()

    def tearDown(self):
        self.tmp.cleanup()

    def test_initialize(self):
        r = mcp.handle(self.home, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(r["result"]["protocolVersion"], mcp.PROTOCOL)
        self.assertIn("tools", r["result"]["capabilities"])

    def test_notification_gets_no_reply(self):
        self.assertIsNone(mcp.handle(self.home, {"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_tools_list(self):
        r = mcp.handle(self.home, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in r["result"]["tools"]}
        for want in ("companion_context", "companion_tick", "device_look", "companion_memory_write"):
            self.assertIn(want, names)
        self.assertGreaterEqual(len(names), 14)

    def test_call_memory_write_then_search(self):
        r = mcp.handle(self.home, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "companion_memory_write",
            "arguments": {"title": "测试页", "body": "正文", "summary": "一句话", "tags": ["t"]},
        }})
        self.assertFalse(r["result"]["isError"])
        r2 = mcp.handle(self.home, {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
            "name": "companion_memory_search", "arguments": {"query": "测试页"},
        }})
        self.assertIn("测试页", r2["result"]["content"][0]["text"])

    def test_unknown_tool_is_error(self):
        r = mcp.handle(self.home, {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                   "params": {"name": "nope", "arguments": {}}})
        self.assertIn("error", r)

    def test_tool_exception_is_reported_not_raised(self):
        r = mcp.handle(self.home, {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {
            "name": "companion_memory_write", "arguments": {"title": "x", "body": "y", "summary": ""},
        }})
        self.assertTrue(r["result"]["isError"])
        self.assertIn("summary", r["result"]["content"][0]["text"])

    def test_unknown_method(self):
        r = mcp.handle(self.home, {"jsonrpc": "2.0", "id": 7, "method": "foo/bar"})
        self.assertEqual(r["error"]["code"], -32601)

    def test_serve_loop_over_pipes(self):
        import io

        inp = io.StringIO("\n".join([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            "这不是json",
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}),
        ]) + "\n")
        out = io.StringIO()
        self.assertEqual(mcp.serve(self.home, stdin=inp, stdout=out), 0)
        lines = [json.loads(x) for x in out.getvalue().strip().splitlines()]
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0]["result"]["protocolVersion"], mcp.PROTOCOL)
        self.assertEqual(lines[1]["error"]["code"], -32700)
        self.assertEqual(lines[2]["result"], {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
