from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from companion import mood, quota  # noqa: E402

T0 = datetime(2026, 10, 27, 21, 0)


class MoodTest(unittest.TestCase):
    def test_cap_per_call(self):
        st = mood.default_state()
        st = mood.apply_delta(st, {"satisfaction": 40}, "她说想我", T0)
        self.assertEqual(st["axes"]["satisfaction"], 12.0)

    def test_delta_is_skipped_when_zero(self):
        st = mood.default_state()
        st = mood.apply_delta(st, {"satisfaction": 0}, "没什么", T0)
        self.assertEqual(st["history"], [])

    def test_label_thresholds(self):
        self.assertEqual(mood.label_for({"satisfaction": 60, "energy": 0}), "雀跃")
        self.assertEqual(mood.label_for({"satisfaction": 20, "energy": 0}), "开心")
        self.assertEqual(mood.label_for({"satisfaction": -20, "energy": 0}), "低落")
        self.assertEqual(mood.label_for({"satisfaction": -50, "energy": 0}), "难过")
        self.assertEqual(mood.label_for({"satisfaction": 0, "energy": 40}), "兴奋")
        self.assertEqual(mood.label_for({"satisfaction": 0, "energy": -40}), "疲惫")
        self.assertEqual(mood.label_for({"satisfaction": 0, "energy": 0}), "平静")

    def test_low_period_blocks_descent(self):
        st = mood.default_state()
        st["axes"]["satisfaction"] = -40
        st["label"] = "低落"
        out = mood.apply_delta(st, {"satisfaction": -10}, "被冷落", T0)
        self.assertEqual(out["axes"]["satisfaction"], -40)
        self.assertEqual(out["history"], [])

    def test_low_period_allows_rise(self):
        st = mood.default_state()
        st["axes"]["satisfaction"] = -40
        st["label"] = "低落"
        out = mood.apply_delta(st, {"satisfaction": 8}, "他哄了一句", T0)
        self.assertEqual(out["axes"]["satisfaction"], -32)

    def test_decay_to_zero(self):
        st = mood.default_state()
        st["axes"] = {"satisfaction": 10, "closeness": 10, "energy": 10}
        st["label"] = "平静"
        st["last_change"] = (T0 - timedelta(hours=24)).isoformat()
        out = mood.decay(st, T0)
        self.assertAlmostEqual(out["axes"]["energy"], 0.0, places=1)
        self.assertAlmostEqual(out["axes"]["satisfaction"], 0.0, places=1)

    def test_decay_does_not_cross_band(self):
        st = mood.default_state()
        st["axes"] = {"satisfaction": 30, "closeness": 0, "energy": 0}
        st["label"] = "开心"
        st["last_change"] = (T0 - timedelta(hours=24)).isoformat()
        out = mood.decay(st, T0)
        self.assertAlmostEqual(out["axes"]["satisfaction"], 20.0, places=1)
        self.assertEqual(out["label"], "开心")

    def test_history_is_capped(self):
        st = mood.default_state()
        for i in range(60):
            st = mood.apply_delta(st, {"closeness": 1}, f"第{i}次", T0)
        self.assertLessEqual(len(st["history"]), 50)

    def test_summary_text(self):
        st = mood.default_state()
        st["axes"] = {"satisfaction": 3, "closeness": -2, "energy": 0}
        text = mood.summary(st)
        self.assertIn("平静", text)
        self.assertIn("+3", text)


class QuotaTest(unittest.TestCase):
    CFG = {"daily": 3, "gap_min": 45}
    # 用同一天内的时刻，避免跨午夜重置干扰
    T = datetime(2026, 10, 27, 9, 0)

    def test_allow_then_gap_blocks(self):
        st = quota.default_state()
        ok, why = quota.allow(st, self.CFG, self.T)
        self.assertTrue(ok, why)
        st = quota.record(st, self.T, "想说话")
        ok, why = quota.allow(st, self.CFG, self.T + timedelta(minutes=10))
        self.assertFalse(ok)
        self.assertIn("分钟", why)
        ok, _ = quota.allow(st, self.CFG, self.T + timedelta(minutes=46))
        self.assertTrue(ok)

    def test_daily_cap(self):
        st = quota.default_state()
        for i in range(3):
            st = quota.record(st, self.T + timedelta(hours=i * 2), f"第{i}次")
        ok, why = quota.allow(st, self.CFG, self.T + timedelta(hours=7))
        self.assertFalse(ok)
        self.assertIn("上限", why)

    def test_day_reset(self):
        st = quota.record(quota.default_state(), T0)
        ok, _ = quota.allow(st, self.CFG, T0 + timedelta(days=1))
        self.assertTrue(ok)

    def test_silence_soft_decay(self):
        now = T0
        seen = (now - timedelta(days=4)).timestamp()
        self.assertEqual(quota.cap_for(self.CFG, seen, now), 1)

    def test_silence_hard_decay(self):
        now = T0
        seen = (now - timedelta(days=8)).timestamp()
        self.assertEqual(quota.cap_for(self.CFG, seen, now), 0)
        ok, why = quota.allow(quota.default_state(), self.CFG, now, seen)
        self.assertFalse(ok)
        self.assertIn("很久没回应", why)


if __name__ == "__main__":
    unittest.main(verbosity=2)
