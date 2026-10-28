"""mood_rules：心情得会动，但不能变成计数器。

两条性质最关键：
  · 有推力 —— 聊天/回来/刷到喜欢的/分享，都会真的改 `mood/state.json`
  · 有上限 —— 一天聊 50 句不会把 closeness 推到 30；否则那是计数不是关系
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from companion import events, mood as mood_mod, mood_rules  # noqa: E402
from companion.store import Home  # noqa: E402

NOW = datetime(2026, 10, 28, 21, 0)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()

    def tearDown(self):
        self.tmp.cleanup()

    def axes(self) -> dict:
        return self.home.mood().get("axes") or {}


class NudgeTest(Base):
    def test_turn_moves_closeness_and_energy(self):
        r = mood_rules.on_turn(self.home, now=NOW)
        self.assertTrue(r["ok"])
        self.assertAlmostEqual(self.axes()["closeness"], 0.6, places=2)
        self.assertAlmostEqual(self.axes()["energy"], -0.2, places=2)
        self.assertEqual(self.axes()["satisfaction"], 0)

    def test_return_after_silence_adds_a_lift(self):
        mood_rules.on_turn(self.home, gap_hours=1, now=NOW)
        mood_rules.on_turn(self.home, gap_hours=20, now=NOW)
        ax = self.axes()
        self.assertGreaterEqual(ax["closeness"], 1.8)      # 两次开口 + 回来的那一下
        self.assertGreaterEqual(ax["satisfaction"], 2.5)   # 只有第二下有

    def test_daily_cap(self):
        for _ in range(30):
            mood_rules.on_turn(self.home, now=NOW)
        ax = self.axes()
        self.assertLessEqual(ax["closeness"], mood_rules.CAP_PER_DAY["closeness"] + 0.01)
        self.assertGreaterEqual(ax["energy"], mood_rules.CAP_PER_DAY["energy"] - 0.01)
        left = mood_rules.used_today(self.home, NOW)
        self.assertGreaterEqual(left["closeness"], mood_rules.CAP_PER_DAY["closeness"] - 0.01)

    def test_cap_resets_next_day(self):
        for _ in range(20):
            mood_rules.on_turn(self.home, now=NOW)
        tomorrow = NOW + timedelta(days=1)
        self.assertEqual(mood_rules.used_today(self.home, tomorrow), {})
        r = mood_rules.on_turn(self.home, now=tomorrow)
        self.assertTrue(r["applied"])

    def test_watched_needs_to_be_liked(self):
        self.assertFalse(mood_rules.on_watched(self.home, 3, now=NOW)["ok"])
        self.assertEqual(self.axes().get("satisfaction", 0), 0)
        self.assertTrue(mood_rules.on_watched(self.home, 7, now=NOW)["ok"])
        self.assertGreater(self.axes()["satisfaction"], 0)

    def test_shared_lifts_satisfaction(self):
        r = mood_rules.on_shared(self.home, now=NOW)
        self.assertTrue(r["ok"])
        self.assertGreater(self.axes()["satisfaction"], 0)

    def test_unknown_event_is_ignored(self):
        r = mood_rules.nudge(self.home, "被外星人绑架", now=NOW)
        self.assertFalse(r["ok"])
        self.assertEqual(self.axes()["closeness"], 0)
        self.assertEqual(self.axes()["satisfaction"], 0)

    def test_label_shift_emits_event_only_when_it_shifts(self):
        before = self.home.mood().get("label")
        mood_rules.on_shared(self.home, now=NOW)
        after = self.home.mood().get("label")
        shifts = [e for e in events.read_all(self.home) if e["kind"] == "mood_shift"]
        self.assertEqual(len(shifts), 0 if after == before else 1)
        if shifts:
            self.assertEqual(shifts[0]["to"], after)


class SnapshotTest(Base):
    def test_snapshot_shows_budget_left(self):
        mood_rules.on_turn(self.home, now=NOW)
        snap = mood_rules.snapshot(self.home, NOW)
        self.assertAlmostEqual(snap["left_today"]["closeness"],
                               mood_rules.CAP_PER_DAY["closeness"] - 0.6, places=2)
        self.assertIn("turn", snap["rules"])
        self.assertIn("label", snap)


if __name__ == "__main__":
    unittest.main()
