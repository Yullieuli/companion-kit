"""P1 闭环回归：同一 turn 重复 react 不重复改变状态或费用；真实用量进成本账本。

对应 WORK_PLAN 验收清单：
  · 同一 turn 重复 react/记账不会重复改变状态或费用
  · 真实宿主用量进入成本账本（本文件验证 core→账本链路；宿主真机验收另列）
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from companion import cost, mcp, react  # noqa: E402
from companion.store import Home  # noqa: E402

T0 = datetime(2026, 10, 28, 21, 0)
USAGE = {"model": "agnes-3.0-flash", "fresh_tokens": 3000,
         "cached_tokens": 20000, "output_tokens": 400}


class ReactIdemTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()

    def tearDown(self):
        self.tmp.cleanup()

    def test_duplicate_react_does_not_change_state_twice(self):
        """同一 turn_id 重试：心情只动一次，记忆/生活/turn 日志都只留一份。"""
        kwargs = dict(
            mood_delta={"satisfaction": 12}, why="他说想我",
            memory_page={"title": "一个测试", "summary": "只该写一页", "body": "正文"},
            life_event={"title": "看到一只猫", "kind": "watch"},
            said="今天挺开心的", turn_id="t-001", now=T0,
        )
        first = react.react(self.home, **kwargs)
        self.assertTrue(first["ok"])
        self.assertNotIn("duplicate", first)
        axes1 = self.home.mood()["axes"]
        self.assertEqual(axes1["satisfaction"], 12)

        second = react.react(self.home, **kwargs)
        self.assertTrue(second["duplicate"])
        self.assertEqual(self.home.mood()["axes"]["satisfaction"], 12)  # 不是 24
        self.assertEqual(second["changed"], first["changed"])
        turns = [r for r in self.home.read_jsonl("logs/turns.jsonl") if r.get("turn_id") == "t-001"]
        self.assertEqual(len(turns), 1)
        self.assertEqual(len(self.home.read_jsonl("logs/said.jsonl")), 1)

    def test_without_turn_id_keeps_append_only(self):
        """不传 turn_id 是旧行为：两轮就是两轮。"""
        react.react(self.home, mood_delta={"satisfaction": 5}, now=T0)
        react.react(self.home, mood_delta={"satisfaction": 5}, now=T0 + timedelta(minutes=1))
        self.assertEqual(self.home.mood()["axes"]["satisfaction"], 10)

    def test_usage_writes_cost_ledger_once(self):
        """真实用量进账本；同一 turn 重试不重复扣钱。"""
        kwargs = dict(usage=USAGE, turn_id="t-cost-1", said="一句话", now=T0)
        out = react.react(self.home, **kwargs)
        self.assertIn("cost", out)
        self.assertFalse(out["cost"]["duplicate"])
        total1 = cost.today_total(self.home, T0)
        self.assertGreater(total1, 0)

        again = react.react(self.home, **kwargs)
        self.assertTrue(again["duplicate"])
        self.assertEqual(cost.today_total(self.home, T0), total1)
        rows = [r for r in cost.load_rows(self.home) if r.get("turn_id") == "t-cost-1"]
        self.assertEqual(len(rows), 1)

    def test_different_turns_accumulate(self):
        react.react(self.home, usage=USAGE, turn_id="t-a", now=T0)
        react.react(self.home, usage=USAGE, turn_id="t-b", now=T0 + timedelta(minutes=1))
        rows = cost.load_rows(self.home)
        self.assertEqual(len(rows), 2)
        self.assertEqual(cost.today_total(self.home, T0),
                         round(2 * rows[0]["cost"], 6))

    def test_mcp_react_passes_turn_id_and_usage(self):
        """MCP 工具层：turn_id/usage 直通 core，重试幂等。"""
        a = {"said": "从 MCP 来的一句话", "turn_id": "mcp-1", "usage": USAGE}
        out = mcp._t_react(self.home, a)
        self.assertTrue(out["ok"])
        self.assertFalse(out["cost"]["duplicate"])
        # MCP 走真实时钟，所以按"账本里有没有这一行"断言，不按固定日期过滤
        rows = [r for r in cost.load_rows(self.home) if r.get("turn_id") == "mcp-1"]
        self.assertEqual(len(rows), 1)
        self.assertGreater(float(rows[0]["cost"]), 0)

        again = mcp._t_react(self.home, a)
        self.assertTrue(again["duplicate"])
        rows2 = [r for r in cost.load_rows(self.home) if r.get("turn_id") == "mcp-1"]
        self.assertEqual(len(rows2), 1)
        self.assertEqual(len(self.home.read_jsonl("logs/said.jsonl")), 1)

    def test_cost_only_turn_is_not_empty(self):
        """只写用量的一轮也是真发生过的一轮，不该回『什么都没写』。"""
        out = react.react(self.home, usage=USAGE, turn_id="t-only", now=T0)
        self.assertNotIn("note", out)
        self.assertIn("cost", out["changed"])


if __name__ == "__main__":
    unittest.main()


class StickerMarkSentTest(unittest.TestCase):
    """斗图上限要靠宿主发完图后记一次，否则永远不生效。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()

    def tearDown(self):
        self.tmp.cleanup()

    def test_mcp_mark_sent_counts(self):
        import json as _json
        from pathlib import Path as _Path

        png = _Path(self.tmp.name) / "a.png"
        png.write_bytes(_json.dumps({"name": "a"}).encode())
        added = mcp._t_sticker_add(self.home, {"path": str(png), "mood": "开心"})
        self.assertTrue(added["ok"], added)
        name = added["name"]

        out = mcp._t_sticker_mark_sent(self.home, {"name": name})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["total"], 1)
        self.assertEqual(out["sent"], 1)

    def test_unknown_sticker_is_reported(self):
        out = mcp._t_sticker_mark_sent(self.home, {"name": "nope"})
        self.assertFalse(out["ok"])
