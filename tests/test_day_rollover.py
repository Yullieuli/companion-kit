"""跨天语义：昨天记的账，今天一律不算。

为什么要单独测：账本按天归零这件事，最容易在"真正跨过零点的那一分钟"才暴露问题。
所以这里全部用**显式日期**（昨天/今天），不碰真实时钟——
测试要在任何一天、任何时刻跑出同一个结果。

覆盖：额度计数、额度桶（B 站）、成本账本。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from companion import bili, cost, quota  # noqa: E402
from companion.store import Home  # noqa: E402

DAY = datetime(2026, 10, 28, 21, 0)
NEXT = DAY + timedelta(days=1)


class DayRolloverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()

    def tearDown(self):
        self.tmp.cleanup()

    def test_speech_count_resets_next_day(self):
        st = quota.default_state(DAY.date().isoformat())
        st["count"] = 3
        self.home.save_json("quota.json", st)
        self.assertEqual(quota.bucket_used(self.home.quota(), "x", DAY), 0)
        self.assertEqual(int(self.home.quota().get("count", 0)), 3)
        # 换一天读：计数归零
        rolled = quota._roll(self.home.quota(), NEXT)
        self.assertEqual(int(rolled["count"]), 0)

    def test_bili_bucket_resets_next_day(self):
        st = quota.default_state(DAY.date().isoformat())
        st["buckets"] = {"bili": {"count": 2, "last_at": DAY.timestamp()}}
        self.home.save_json("quota.json", st)
        self.assertEqual(quota.bucket_used(self.home.quota(), "bili", DAY), 2)
        self.assertEqual(quota.bucket_used(self.home.quota(), "bili", NEXT), 0)

    def test_cost_only_counts_today(self):
        cost.record_turn(self.home, now=DAY, fresh_tokens=1000, output_tokens=100)
        self.assertGreater(cost.today_total(self.home, DAY), 0)
        self.assertEqual(cost.today_total(self.home, NEXT), 0)

    def test_bili_old_day_is_not_carried_over(self):
        """旧账本里"昨天刷了 3 条"，今天不该被搬进今天的桶。"""
        st = bili.load(self.home)
        st["watched_today"] = 3
        st["day"] = DAY.date().isoformat()
        bili.save(self.home, st)
        # bili.load 会把非当天的账归一化掉（day 归今天、计数清零）
        self.assertEqual(int(bili.load(self.home).get("watched_today") or 0), 0)

    def test_cost_record_uses_given_day(self):
        row = cost.record_turn(self.home, now=DAY, fresh_tokens=10, output_tokens=1)
        self.assertEqual(row["day"], DAY.date().isoformat())
        self.assertEqual(cost.load_rows(self.home)[0]["day"], DAY.date().isoformat())


if __name__ == "__main__":
    unittest.main()
