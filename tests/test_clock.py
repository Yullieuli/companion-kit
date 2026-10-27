from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from companion import clock  # noqa: E402


class ParseTest(unittest.TestCase):
    def test_hhmm(self):
        self.assertEqual(clock.parse_hhmm("20:00"), 1200)
        self.assertEqual(clock.parse_hhmm("24:00"), 1440)
        self.assertEqual(clock.parse_hhmm("00:30"), 30)

    def test_bad_hhmm(self):
        for bad in ("25:00", "20:70", "abc", "20"):
            with self.assertRaises(ValueError):
                clock.parse_hhmm(bad)


class AgoTest(unittest.TestCase):
    def test_buckets(self):
        self.assertEqual(clock.ago_text(30), "刚刚")
        self.assertEqual(clock.ago_text(90), "1 分钟前")
        self.assertEqual(clock.ago_text(3600 * 6), "6 小时前")
        self.assertEqual(clock.ago_text(86400 * 3), "3 天前")
        self.assertEqual(clock.ago_text(-50), "刚刚")


class DescribeTest(unittest.TestCase):
    def test_weekday_and_gap(self):
        now = datetime(2026, 10, 27, 21, 7)  # 周二
        text = clock.describe(now, now.timestamp() - 6 * 3600)
        self.assertIn("周二 21:07", text)
        self.assertIn("6 小时前", text)

    def test_without_last_chat(self):
        self.assertEqual(clock.describe(datetime(2026, 10, 27, 9, 5)), "周二 09:05")


class WindowTest(unittest.TestCase):
    cfg = {"windows": [{"start": "20:00", "end": "24:00", "wakes": 2}]}

    def test_inside_and_outside(self):
        self.assertTrue(clock.in_window(self.cfg, datetime(2026, 10, 27, 21, 0)))
        self.assertFalse(clock.in_window(self.cfg, datetime(2026, 10, 27, 10, 0)))

    def test_cross_midnight(self):
        cfg = {"windows": [{"start": "22:00", "end": "02:00", "wakes": 1}]}
        self.assertTrue(clock.in_window(cfg, datetime(2026, 10, 27, 23, 30)))
        self.assertTrue(clock.in_window(cfg, datetime(2026, 10, 28, 1, 0)))
        self.assertFalse(clock.in_window(cfg, datetime(2026, 10, 27, 12, 0)))

    def test_zero_wakes_is_valid(self):
        cfg = {"windows": [{"start": "20:00", "end": "24:00", "wakes": 0}]}
        self.assertEqual(clock.windows(cfg)[0]["wakes"], 0)


class SleepTest(unittest.TestCase):
    cfg = {"sleep": {"at": "00:30", "wake": "09:00"}}

    def test_sleep_window(self):
        self.assertTrue(clock.in_sleep(self.cfg, datetime(2026, 10, 28, 2, 0)))
        self.assertTrue(clock.in_sleep(self.cfg, datetime(2026, 10, 28, 0, 40)))
        self.assertFalse(clock.in_sleep(self.cfg, datetime(2026, 10, 28, 10, 0)))

    def test_sleep_until_next_morning(self):
        wake = clock.sleep_until(self.cfg, datetime(2026, 10, 28, 2, 0))
        self.assertEqual((wake.hour, wake.minute), (9, 0))
        self.assertEqual(wake.day, 28)

    def test_no_sleep_config(self):
        self.assertFalse(clock.in_sleep({}, datetime(2026, 10, 28, 2, 0)))


class RandomMomentTest(unittest.TestCase):
    class Fixed:
        def __init__(self, v):
            self.v = v

        def random(self):
            return self.v

    def test_bounds(self):
        self.assertEqual(clock.random_moment(1200, 1440, self.Fixed(0.0)), 1200)
        self.assertEqual(clock.random_moment(1200, 1440, self.Fixed(0.999)), 1439)

    def test_next_window(self):
        nxt = clock.next_window_after(WindowTest.cfg, datetime(2026, 10, 27, 18, 0))
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt["starts_at"].hour, 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)
