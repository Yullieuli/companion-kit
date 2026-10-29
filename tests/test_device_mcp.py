from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def _enable(self):
        cfg = device.config(self.home)
        cfg["enabled"] = True
        self.home.save_json("devices.json", cfg)

    def test_default_disables_device_control(self):
        cfg = device.config(self.home)
        self.assertFalse(cfg["enabled"])
        v = device.check(self.home, target="desktop", action="look")
        self.assertFalse(v.ok)
        self.assertIn("没开", v.reason)

    def test_disabled_blocks_everything(self):
        self._enable()
        cfg = device.config(self.home)
        cfg["enabled"] = False
        self.home.save_json("devices.json", cfg)
        v = device.check(self.home, target="desktop", action="look")
        self.assertFalse(v.ok)
        self.assertIn("没开", v.reason)

    def test_non_owner_blocked(self):
        self._enable()
        v = device.check(self.home, target="desktop", action="look", is_owner=False)
        self.assertFalse(v.ok)
        self.assertIn("主人", v.reason)

    def test_dangerous_action_refused_without_asking(self):
        self._enable()
        for word in ("关机", "卸载微信", "帮他转账", "读取密码"):
            v = device.check(self.home, target="desktop", action="open", args={"what": word})
            self.assertFalse(v.ok, word)
            self.assertIn("危险", v.reason)

    def test_allowlist_when_configured(self):
        self._enable()
        cfg = device.config(self.home)
        cfg["allow"]["browser"]["domains"] = ["bilibili.com"]
        cfg["allow"]["phone"]["packages"] = ["com.example.app"]
        cfg["allow"]["desktop"]["windows"] = ["记事本"]
        self.home.save_json("devices.json", cfg)
        ok = device.check(self.home, target="browser", action="open", args={"what": "https://www.bilibili.com/x"})
        self.assertTrue(ok.ok, ok.reason)
        launch = device.check(self.home, target="phone", action="launch", args={"package": "com.example.app"})
        self.assertTrue(launch.ok, launch.reason)
        focus = device.check(self.home, target="desktop", action="focus", args={"window": "记事本"})
        self.assertTrue(focus.ok, focus.reason)
        bad = device.check(self.home, target="browser", action="open", args={"what": "https://bank.example.com"})
        self.assertFalse(bad.ok)

    def test_empty_allowlist_rejects_open_launch_and_focus(self):
        self._enable()
        cases = [
            ("browser", "open", {"what": "https://anything.example"}),
            ("phone", "launch", {"package": "com.example.app"}),
            ("desktop", "focus", {"window": "记事本"}),
        ]
        for target, action, args in cases:
            with self.subTest(target=target, action=action):
                v = device.check(self.home, target=target, action=action, args=args)
                self.assertFalse(v.ok)
                self.assertIn("白名单为空", v.reason)

    def test_partial_allow_config_keeps_default_targets(self):
        self.home.save_json("devices.json", {
            "enabled": True,
            "allow": {"browser": {"domains": ["example.com"]}},
        })
        cfg = device.config(self.home)
        self.assertEqual(cfg["allow"]["browser"]["domains"], ["example.com"])
        self.assertEqual(cfg["allow"]["phone"], {"packages": []})
        self.assertEqual(cfg["allow"]["desktop"], {"windows": []})
        self.assertEqual(cfg["allow"]["game"], {"windows": []})

    def test_idempotent_window(self):
        self._enable()
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
        self._enable()
        out = device.look(self.home, "toaster")
        self.assertFalse(out["ok"])
        self.assertIn("不认识", out["error"])

    def test_refusal_is_logged(self):
        self._enable()
        device.look(self.home, "desktop", serial="") if False else None
        device.check(self.home, target="desktop", action="look", is_owner=False)
        rows = device.log_tail(self.home)
        self.assertTrue(all("at" in r for r in rows))

    def test_act_refused_does_not_touch_screen(self):
        self._enable()
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
        for want in ("companion_context", "companion_tick", "device_look", "companion_memory_write",
                     "companion_life_mark_shared", "companion_group_judge", "companion_sticker_pick"):
            self.assertIn(want, names)
        self.assertEqual(len(names), len(mcp.TOOLS))
        self.assertGreaterEqual(len(names), 20)

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


class InputTest(unittest.TestCase):
    """新的输入能力：滑动、按键、切窗口、游戏坐标换算。都只测不碰硬件的部分。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()
        cfg = device.config(self.home)
        cfg["enabled"] = True
        self.home.save_json("devices.json", cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def test_map_point_scales(self):
        self.assertEqual(device.map_point(960, 540, (1920, 1080), (1920, 1080)), (960, 540))
        self.assertEqual(device.map_point(960, 540, (1920, 1080), (2560, 1440)), (1280, 720))
        self.assertEqual(device.map_point(0, 0, (1920, 1080), (1280, 720)), (0, 0))

    def test_map_point_survives_zero_size(self):
        self.assertEqual(device.map_point(10, 10, (0, 0), (0, 0)), (10, 10))

    def test_vk_names(self):
        from companion import devwin

        self.assertEqual(devwin._vk("enter"), 0x0D)
        self.assertEqual(devwin._vk("A"), ord("A"))
        self.assertEqual(devwin._vk("f5"), 0x74)
        with self.assertRaises(devwin.DesktopError):
            devwin._vk("这个键不存在")

    def test_focus_respects_window_whitelist(self):
        cfg = device.config(self.home)
        cfg.setdefault("allow", {}).setdefault("desktop", {})["windows"] = ["记事本"]
        self.home.save_json("devices.json", cfg)
        out = device.focus_window_by(self.home, "Chrome")
        self.assertFalse(out["ok"])
        self.assertIn("白名单", out["error"])

    def test_focus_needs_owner(self):
        out = device.focus_window_by(self.home, "记事本", is_owner=False)
        self.assertFalse(out["ok"])
        self.assertIn("主人", out["error"])

    def test_act_rejects_unknown_desktop_action(self):
        out = device.act(self.home, "desktop", "format_disk", {"x": 1, "y": 1})
        self.assertFalse(out["ok"])


class ObserveTest(unittest.TestCase):
    """看：窗口级截图 + 本地 OCR。测试不碰真实屏幕，走假图。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()
        cfg = device.config(self.home)
        cfg["enabled"] = True
        self.home.save_json("devices.json", cfg)
        self.shot = self.home.root / "fake.png"
        self.shot.write_bytes(b"\x89PNG\r\n\x1a\n fake")

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_ocr_tsv_groups_lines_and_drops_noise(self):
        tsv = "\n".join([
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
            "5\t1\t1\t1\t1\t1\t10\t10\t30\t12\t96\t设置",
            "5\t1\t1\t1\t1\t2\t45\t10\t30\t12\t88\t通用",
            "5\t1\t1\t1\t2\t1\t10\t30\t30\t12\t91\t音量",
            "5\t1\t1\t1\t2\t2\t45\t30\t30\t12\t12\t（噪点）",
        ])
        got = device.parse_ocr_tsv(tsv, min_conf=40)
        self.assertEqual(got["text"], "设置 通用\n音量")
        self.assertEqual(len(got["lines"]), 2)
        self.assertGreater(got["lines"][0]["conf"], 90)

    def test_ocr_without_engine_is_honest(self):
        with patch.object(device, "ocr_bin", return_value=""):
            got = device.ocr_image(self.home, self.shot)
        self.assertEqual(got["backend"], "none")
        self.assertEqual(got["text"], "")
        self.assertIn("vision", got["hint"])

    def test_ocr_can_be_switched_off(self):
        cfg = device.config(self.home)
        cfg["ocr"] = {**cfg["ocr"], "enabled": False}
        self.home.save_json("devices.json", cfg)
        with patch.object(device, "ocr_bin", return_value="/usr/bin/tesseract"):
            got = device.ocr_image(self.home, self.shot)
        self.assertEqual(got["backend"], "off")

    def test_observe_returns_shot_meta_and_hint(self):
        fake = {"ok": True, "target": "desktop", "shot": str(self.shot), "kb": 1,
                "foreground": "记事本", "windows": ["记事本"], "screen": [1920, 1080], "window": ""}
        with patch.object(device, "look", return_value=dict(fake)), \
                patch.object(device, "ocr_bin", return_value=""):
            got = device.observe(self.home, "desktop")
        self.assertTrue(got["ok"])
        self.assertEqual(got["foreground"], "记事本")
        self.assertEqual(got["ocr"]["backend"], "none")
        self.assertIn("别概括", got["describe_hint"])
        self.assertEqual(self.home.read_jsonl("logs/device.jsonl")[-1]["action"], "observe")

    def test_look_with_window_uses_window_capture(self):
        calls = {}

        def fake_window(home, want, *, target="window"):
            calls["want"] = want
            return self.shot

        with patch.object(device, "screenshot_window", side_effect=fake_window), \
                patch.object(device, "screenshot_desktop") as desk, \
                patch.object(device, "foreground", return_value="记事本"), \
                patch.object(device, "list_windows", return_value=[{"title": "记事本"}]), \
                patch.object(device, "screen_size", return_value=(1920, 1080)), \
                patch.object(device, "screen_origin", return_value=(0, 0)):
            got = device.look(self.home, "desktop", window="记事本")
        self.assertTrue(got["ok"], got)
        self.assertEqual(calls["want"], "记事本")
        self.assertEqual(got["window"], "记事本")
        self.assertFalse(desk.called)


class ShotPruneTest(unittest.TestCase):
    """截图会堆：一张双屏 PNG 就 2MB，不清三天就一个 G。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()
        self.dir = self.home.root / "shots"
        self.dir.mkdir(exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_keeps_newest_and_drops_old(self):
        import os
        import time as _t

        now = _t.time()
        for i in range(8):
            p = self.dir / f"s{i}.png"
            p.write_bytes(b"x")
            os.utime(p, (now - i * 60, now - i * 60))
        old = self.dir / "ancient.png"
        old.write_bytes(b"x")
        os.utime(old, (now - 30 * 86400, now - 30 * 86400))

        removed = device._prune_shots(self.home, keep=5, days=7)
        left = {p.name for p in self.dir.glob("*.png")}
        self.assertEqual(len(left), 5)
        self.assertNotIn("ancient.png", left)      # 太老，即使没超数量也删
        self.assertIn("s0.png", left)              # 最新的留着
        self.assertGreaterEqual(removed, 4)

    def test_does_not_touch_bili_subdir(self):
        sub = self.dir / "bili" / "frames"
        sub.mkdir(parents=True, exist_ok=True)
        keep = sub / "BV1.png"
        keep.write_bytes(b"x")
        device._prune_shots(self.home, keep=0, days=0)
        self.assertTrue(keep.exists())


class ReplayTest(unittest.TestCase):
    """回放默认只看不点：屏幕早变了，同一个坐标现在可能是「删除」。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()

    def tearDown(self):
        self.tmp.cleanup()

    def _log(self, action: str, **kw):
        row = {"at": "2026-10-28T18:00:00", "target": kw.pop("target", "desktop"),
               "action": action, "ok": True, **kw}
        self.home.append_jsonl("logs/device.jsonl", row)

    def test_dry_run_lists_without_executing(self):
        self._log("tap", args={"x": 10, "y": 20})
        self._log("look")                        # 看不算动作，不该进回放
        self._log("type", args={"text": "hello"})
        with patch.object(device, "act") as act_mock:
            out = device.replay(self.home, 5)
        self.assertTrue(out["dry_run"])
        self.assertEqual([s["action"] for s in out["steps"]], ["tap", "type"])
        self.assertTrue(out["steps"][1]["has_text"])
        self.assertFalse(act_mock.called)

    def test_execute_skips_text_unless_asked(self):
        self._log("tap", args={"x": 10, "y": 20})
        self._log("type", args={"text": "secret"})
        with patch.object(device, "act",
                          return_value={"ok": True, "before": "b.png", "after": "a.png"}) as act_mock:
            out = device.replay(self.home, 5, execute=True)
        self.assertEqual(act_mock.call_count, 1)       # 文本那条被跳过
        self.assertTrue(out["steps"][0]["ok"])
        self.assertIn("skipped", out["steps"][1])
        self.assertTrue(out["ok"])

    def test_execute_with_text_runs_both(self):
        self._log("tap", args={"x": 1, "y": 2})
        self._log("type", args={"text": "hi"})
        with patch.object(device, "act",
                          return_value={"ok": True, "before": "b.png", "after": "a.png"}) as act_mock:
            out = device.replay(self.home, 5, execute=True, with_text=True)
        self.assertEqual(act_mock.call_count, 2)
        self.assertTrue(all(s.get("before") == "b.png" for s in out["steps"]))

    def test_empty_log_is_honest(self):
        out = device.replay(self.home, 5)
        self.assertEqual(out["steps"], [])
        self.assertIn("没有可回放", out["hint"])


class McpOwnerTest(unittest.TestCase):
    """MCP 层的主人识别：只信 devices.json 的 mcp_owner，不信模型传的布尔值。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()

    def tearDown(self):
        self.tmp.cleanup()

    def _enable(self, mcp_owner=False):
        cfg = device.config(self.home)
        cfg["enabled"] = True
        cfg["mcp_owner"] = mcp_owner
        cfg["allow"]["desktop"]["windows"] = ["notepad"]
        self.home.save_json("devices.json", cfg)

    def test_default_denies_everything(self):
        # 新 store：MCP 上看/动/开一律拒绝
        look = mcp._t_look(self.home, {"target": "desktop"})
        self.assertFalse(look["ok"])
        self.assertIn("没开", look["error"])
        act = mcp._t_act(self.home, {"target": "desktop", "action": "tap", "x": 1, "y": 2})
        self.assertFalse(act["ok"])
        opened = mcp._t_open(self.home, {"target": "desktop", "what": "notepad"})
        self.assertFalse(opened["ok"])

    def test_model_claimed_owner_is_ignored(self):
        """模型把 is_owner 传成 true 也没用：那是参数，不是凭据。"""
        self._enable(mcp_owner=False)
        act = mcp._t_act(self.home, {"target": "desktop", "action": "tap",
                                     "x": 1, "y": 2, "is_owner": True})
        self.assertFalse(act["ok"])
        self.assertIn("主人", act["error"])
        opened = mcp._t_open(self.home, {"target": "desktop", "what": "notepad",
                                         "is_owner": True})
        self.assertFalse(opened["ok"])
        look = mcp._t_look(self.home, {"target": "desktop", "is_owner": True})
        self.assertFalse(look["ok"])
        self.assertIn("主人", look["error"])

    def test_owner_config_enables_mcp_session(self):
        self._enable(mcp_owner=True)
        with patch.object(device, "act",
                          return_value={"ok": True, "before": "b.png", "after": "a.png"}) as act_mock:
            act = mcp._t_act(self.home, {"target": "desktop", "action": "tap", "x": 1, "y": 2})
        self.assertTrue(act["ok"])
        # 传给 core 的 is_owner 来自配置，不是参数
        self.assertTrue(act_mock.call_args.kwargs["is_owner"])
        with patch.object(device, "open_thing", return_value={"ok": True}) as open_mock:
            opened = mcp._t_open(self.home, {"target": "desktop", "what": "notepad"})
        self.assertTrue(opened["ok"])
        self.assertTrue(open_mock.call_args.kwargs["is_owner"])

    def test_look_passes_gate_when_owner(self):
        self._enable(mcp_owner=True)
        with patch.object(device, "look", return_value={"ok": True, "shot": "x.png"}) as look_mock:
            out = mcp._t_look(self.home, {"target": "desktop"})
        self.assertTrue(out["ok"])
        look_mock.assert_called_once()
