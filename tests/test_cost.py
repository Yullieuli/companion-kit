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
        cost.record_turn(self.home, now=T0, fresh_tokens=20000, output_tokens=600)
        cost.record_turn(self.home, now=T0, fresh_tokens=10000, output_tokens=300)
        self.assertAlmostEqual(cost.today_total(self.home, T0), 0.0448 + 0.0224, places=4)

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
