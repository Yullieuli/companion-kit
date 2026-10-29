"""ask 的安全边界：模型不能自己替主人答应自己。

这一组测试守的是一个**实测出来的真漏洞**，不是假想：

    ① 模型调 device_open → 被"先请示"挡住，拿到 ask_id
    ② 模型调 companion_ask allow → 自己批准了
    ③ 模型拿 ask_id 再调 device_open → 设备闸放行，程序真的开了

修复前 ①②③ 全通。那等于"模型自己替主人答应自己"——
恰恰是 ask 这套流程存在的理由的反面。

修完之后的性质：
  · 模型自批的票**开不了设备**（can_act=False）
  · 主人在场说的话（聊天里回"可以"）才能开
  · 拒绝优先于同意；读不懂、多条待办都不乱动
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from companion import ask, device, mcp  # noqa: E402
from companion.store import Home  # noqa: E402

NOW = datetime(2026, 10, 28, 16, 0)


def _mcp_ready(home: Home) -> None:
    """一台"主人亲手配好的"MCP 机器：设备开着，桌面开了 chrome 白名单。"""
    cfg = device.config(home)
    cfg["enabled"] = True
    cfg["mcp_owner"] = True
    cfg["allow"]["desktop"]["windows"] = ["chrome"]
    cfg["ask"] = {"enabled": True, "actions": ["open"], "targets": []}
    home.save_json("devices.json", cfg)


class ModelCannotSelfApproveTest(unittest.TestCase):
    """模型自己批的票，不能开设备。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()
        _mcp_ready(self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def test_model_allow_does_not_open_device(self):
        """完整复现漏洞链，断言第③步被挡住。"""
        # ① 模型要开程序，被请示挡住
        first = mcp.HANDLERS["device_open"](self.home, {"target": "desktop", "what": "chrome"})
        self.assertFalse(first["ok"])
        self.assertTrue(first.get("ask_required"))
        ask_id = first["ask"]["id"]

        # ② 模型自己批准这条请示
        approved = mcp.HANDLERS["companion_ask"](self.home, {"action": "allow", "id": ask_id})
        self.assertTrue(approved["ok"], "模型自批这一步本身是允许记录的")
        self.assertFalse(approved.get("can_act"), "但这张票不能开设备")
        self.assertIn("不能用来开设备", approved.get("note", ""))

        # ③ 拿自批的票再开程序 —— 必须仍然被挡
        with patch.object(device.subprocess, "Popen") as popen, \
                patch.object(device.os, "startfile", create=True):
            second = mcp.HANDLERS["device_open"](
                self.home, {"target": "desktop", "what": "chrome", "ask_id": ask_id})
        self.assertFalse(second["ok"], "模型自批的票把设备打开了 —— 漏洞还在")
        self.assertTrue(second.get("ask_required"))
        self.assertFalse(popen.called, "不该真的启动程序")

    def test_model_self_approval_is_recorded_but_not_usable(self):
        r = ask.request(self.home, "open", {"what": "chrome"}, target="desktop", now=NOW)
        out = ask.decide(self.home, r["id"], True, now=NOW, by="model")
        self.assertTrue(out["ok"])
        self.assertFalse(out["can_act"])
        # 查询时能看到"模型批的"这件事，不是静悄悄地放行
        row = {x["id"]: x for x in ask.list_all(self.home, now=NOW)}[r["id"]]
        self.assertEqual(row["decided_by"], "model")
        self.assertIn("模型自己批准", row.get("why_not", ""))
        # 票拿不到
        self.assertEqual(ask.token(self.home, "open", {"what": "chrome"},
                                   target="desktop", now=NOW), "")

    def test_owner_approval_can_open_device(self):
        """主人在场说的话 → 票有效 → 设备能开。这是修复后的正路。"""
        first = mcp.HANDLERS["device_open"](self.home, {"target": "desktop", "what": "chrome"})
        ask_id = first["ask"]["id"]
        # 主人在聊天里回"可以"
        r = ask.decide_by_reply(self.home, "可以，你开吧", now=NOW, by="owner")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["intent"], "allow")
        self.assertTrue(r.get("can_act"))
        with patch.object(device.subprocess, "Popen") as popen, \
                patch.object(device.os, "startfile", create=True):
            second = mcp.HANDLERS["device_open"](
                self.home, {"target": "desktop", "what": "chrome", "ask_id": ask_id})
        self.assertTrue(second["ok"], second)


class ReplyAutoFillTest(unittest.TestCase):
    """主人原话 → 自动回填。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_intent(self):
        self.assertEqual(ask.read_intent("可以"), "allow")
        self.assertEqual(ask.read_intent("行吧"), "allow")
        self.assertEqual(ask.read_intent("去吧"), "allow")
        self.assertEqual(ask.read_intent("别"), "deny")
        self.assertEqual(ask.read_intent("算了"), "deny")
        self.assertEqual(ask.read_intent("不要"), "deny")
        # 读不懂的就不猜
        self.assertEqual(ask.read_intent("今天天气不错"), "")
        self.assertEqual(ask.read_intent(""), "")
        self.assertEqual(ask.read_intent("我在想晚上吃什么" * 5), "")

    def test_deny_wins_over_allow(self):
        """一句话里既有"可以"又有"不要"时，按拒绝算。"""
        self.assertEqual(ask.read_intent("可以不要吗"), "deny")
        self.assertEqual(ask.read_intent("不要，可以吗"), "deny")

    def test_reply_fills_pending_ask(self):
        r = ask.request(self.home, "open", {"what": "chrome"}, target="desktop", now=NOW)
        out = ask.decide_by_reply(self.home, "可以", now=NOW)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["ask"]["id"], r["id"])
        self.assertEqual(out["ask"]["status"], "allowed")
        self.assertTrue(out["ask"]["can_act"])

    def test_reply_denies(self):
        r = ask.request(self.home, "open", {"what": "chrome"}, target="desktop", now=NOW)
        out = ask.decide_by_reply(self.home, "算了别开", now=NOW)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["intent"], "deny")
        row = {x["id"]: x for x in ask.list_all(self.home, now=NOW)}[r["id"]]
        self.assertEqual(row["status"], "denied")
        self.assertFalse(row.get("can_act"))

    def test_unclear_reply_does_nothing(self):
        ask.request(self.home, "open", {"what": "chrome"}, target="desktop", now=NOW)
        out = ask.decide_by_reply(self.home, "今天真冷啊", now=NOW)
        self.assertFalse(out["ok"])
        self.assertIn("读不出", out["why"])
        self.assertEqual(len(ask.pending(self.home, now=NOW)), 1, "不该被误判消耗掉")

    def test_multiple_pending_does_not_guess(self):
        ask.request(self.home, "open", {"what": "chrome"}, target="desktop", now=NOW)
        ask.request(self.home, "open", {"what": "notepad"}, target="desktop", now=NOW)
        out = ask.decide_by_reply(self.home, "可以", now=NOW)
        self.assertFalse(out["ok"])
        self.assertIn("说不清", out["why"])
        self.assertEqual(len(ask.pending(self.home, now=NOW)), 2)

    def test_no_pending_is_noop(self):
        out = ask.decide_by_reply(self.home, "可以", now=NOW)
        self.assertFalse(out["ok"])
        self.assertIn("没有待答复", out["why"])

    def test_expired_ask_cannot_be_filled(self):
        ask.request(self.home, "open", {"what": "chrome"}, target="desktop",
                    ttl_min=1, now=NOW)
        out = ask.decide_by_reply(self.home, "可以", now=NOW + timedelta(minutes=5))
        self.assertFalse(out["ok"], "过期的不该补票")


class SpokenEvidenceTest(unittest.TestCase):
    """批准前要能证明：她真的把这句话说出去了。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()

    def tearDown(self):
        self.tmp.cleanup()

    def test_not_spoken_by_default(self):
        r = ask.request(self.home, "open", {"what": "chrome"}, target="desktop", now=NOW)
        self.assertFalse(ask.spoken(self.home, r["id"]))

    def test_mark_spoken(self):
        r = ask.request(self.home, "open", {"what": "chrome"}, target="desktop", now=NOW)
        self.assertTrue(ask.mark_spoken(self.home, r["id"]))
        self.assertTrue(ask.spoken(self.home, r["id"]))
        # 幂等
        self.assertFalse(ask.mark_spoken(self.home, r["id"]))

    def test_said_log_counts_as_spoken(self):
        r = ask.request(self.home, "open", {"what": "chrome"}, target="desktop", now=NOW)
        self.home.append_jsonl("logs/said.jsonl",
                               {"at": NOW.isoformat(), "text": r["say"], "source": "say"})
        self.assertTrue(ask.spoken(self.home, r["id"]))

    def test_turns_log_counts_as_spoken(self):
        """hermes 不走 say 出口，话记在 turns.jsonl 里 —— 也算说出口了。"""
        r = ask.request(self.home, "open", {"what": "chrome"}, target="desktop", now=NOW)
        self.home.append_jsonl("logs/turns.jsonl",
                               {"at": NOW.isoformat(), "role": "assistant", "said": r["say"]})
        self.assertTrue(ask.spoken(self.home, r["id"]))


if __name__ == "__main__":
    unittest.main()
