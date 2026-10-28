from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from companion import cost, quota  # noqa: E402
from companion.store import Home  # noqa: E402

T0 = datetime(2026, 10, 27, 9, 0)


class PriceTest(unittest.TestCase):
    def test_flash_price(self):
        # 2 万新输入 + 6 百输出，全价
        c = cost.cost_cny(20000, 0, 600, "deepseek-v4.1-flash")
        self.assertAlmostEqual(c, 0.0448, places=4)

    def test_cache_cheaper(self):
        # 只比输入部分：输出不打折，混在一起比会失真
        full_in = cost.cost_cny(20000, 0, 0)
        cached_in = cost.cost_cny(0, 20000, 0)
        self.assertLess(cached_in, full_in / 10)

    def test_unknown_model_falls_back(self):
        self.assertGreater(cost.cost_cny(1000, 0, 100, "no-such-model"), 0)


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_and_total(self):
        # 显式给模型：这条测的是算术，不该被"默认模型改成谁"带跑
        cost.record_turn(self.home, now=T0, model="deepseek-v4.1-flash",
                         fresh_tokens=20000, output_tokens=600)
        cost.record_turn(self.home, now=T0, model="deepseek-v4.1-flash",
                         fresh_tokens=10000, output_tokens=300)
        self.assertAlmostEqual(cost.today_total(self.home, T0), 0.0448 + 0.0224, places=4)

    def test_default_model_is_the_one_she_runs(self):
        # 默认跟着她实际在跑的模型走，否则记账金额会系统性偏离账单
        row = cost.record_turn(self.home, now=T0, fresh_tokens=1_000_000)
        self.assertEqual(row.get("model"), "agnes-3.0-flash")

    def test_only_today_counts(self):
        cost.record_turn(self.home, now=datetime(2026, 10, 26, 9, 0), fresh_tokens=20000, output_tokens=600)
        self.assertEqual(cost.today_total(self.home, T0), 0.0)

    def test_allow_turn_blocks_over_budget(self):
        cost.record_turn(self.home, now=T0, fresh_tokens=10_000_000, output_tokens=1000)
        ok, why, est = cost.allow_turn(self.home, now=T0, budget={"daily_cny": 1.0})
        self.assertFalse(ok)
        self.assertIn("超过", why)
        self.assertGreater(est, 0)

    def test_allow_turn_passes_when_cheap(self):
        ok, why, est = cost.allow_turn(self.home, now=T0, budget={"daily_cny": 3.0})
        self.assertTrue(ok)
        self.assertEqual(why, "")
        self.assertLess(est, 0.1)

    def test_summary(self):
        cost.record_turn(self.home, now=T0, fresh_tokens=20000, output_tokens=600)
        text = cost.summary(self.home, T0, {"daily_cny": 3.0})
        self.assertIn("1 轮", text)
        self.assertIn("3.00", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class RouteTest(unittest.TestCase):
    """分级模型：同一件事花多少钱差几十倍，档位必须能查、能改。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_tiers(self):
        self.assertEqual(cost.route("bili_pick", self.home)["tier"], "cheap")
        self.assertEqual(cost.route("chat", self.home)["tier"], "work")
        self.assertEqual(cost.route("bili_debrief", self.home)["tier"], "deep")
        self.assertEqual(cost.route("从来没听过的任务", self.home)["tier"], "work")

    def test_cheap_is_much_cheaper_than_deep(self):
        cheap = cost.route("bili_pick", self.home)["est_cny"]
        deep = cost.route("bili_debrief", self.home)["est_cny"]
        self.assertLess(cheap, deep / 5)

    def test_store_can_override_task_and_model(self):
        self.home.save_json(cost.ROUTE_FILE, {
            "tasks": {"bili_pick": "deep"},
            "tiers": {"deep": {"model": "deepseek-v4.1"}},
        })
        r = cost.route("bili_pick", self.home)
        self.assertEqual(r["tier"], "deep")
        self.assertEqual(r["model"], "deepseek-v4.1")

    def test_bad_override_is_ignored(self):
        self.home.save_json(cost.ROUTE_FILE, {"tasks": {"chat": "不存在的档"}})
        self.assertEqual(cost.route("chat", self.home)["tier"], "work")

    def test_route_has_price_and_tokens(self):
        r = cost.route("chat", self.home)
        self.assertIn("input", r["price_per_mtok"])
        self.assertGreater(r["tokens"]["cached_tokens"], 0)   # 估价值必须按缓存命中算
        self.assertGreater(r["est_cny"], 0)
