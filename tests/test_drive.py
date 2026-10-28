from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from companion import drive, events, quota  # noqa: E402
from companion.store import Home  # noqa: E402

T0 = datetime(2026, 10, 27, 21, 0)


class EventsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()

    def tearDown(self):
        self.tmp.cleanup()

    def test_emit_and_poll(self):
        events.emit(self.home, "wake_due", T0, action="speak", reason="想他")
        fresh, cur = events.poll(self.home, since=0)
        self.assertEqual(len(fresh), 1)
        self.assertEqual(fresh[0]["action"], "speak")
        self.assertEqual(cur, 1)

    def test_explicit_since_does_not_move_shared_cursor(self):
        # dsh 每轮传 since；如果它顺手把全局 cursor 推到最新，
        # 人跑 `companion events` 就永远看不到东西了。
        events.emit(self.home, "idle", T0)
        events.poll(self.home, since=0)
        self.assertEqual(events.cursor(self.home), 0)
        events.poll(self.home)
        self.assertEqual(events.cursor(self.home), 1)

    def test_cursor_prevents_redelivery(self):
        events.emit(self.home, "idle", T0)
        _, cur = events.poll(self.home)
        fresh, _ = events.poll(self.home, since=cur)
        self.assertEqual(fresh, [])

    def test_cursor_survives_restart(self):
        events.emit(self.home, "idle", T0)
        events.poll(self.home)
        again = Home.at(self.tmp.name)
        self.assertEqual(events.cursor(again), 1)

    def test_wait_returns_on_timeout(self):
        rows, cur = events.wait(self.home, timeout=0.2, interval=0.05)
        self.assertEqual(rows, [])
        self.assertEqual(cur, 0)


class TickTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()
        self.home.save_json("rhythm.json", {
            "windows": [{"start": "20:00", "end": "24:00", "wakes": 2}],
            "sleep": {"at": "00:30", "wake": "09:00"},
            "skip_chance": 0.0,
        })

    def tearDown(self):
        self.tmp.cleanup()

    def test_sleep_emits_nothing(self):
        r = drive.tick(self.home, now=datetime(2026, 10, 28, 2, 0))
        self.assertEqual(r["action"], "silent")
        self.assertEqual(r["blocked"], "sleep")
        self.assertEqual(events.read_all(self.home), [])

    def test_speaking_tick_emits_wake_due_and_records_quota(self):
        r = drive.tick(self.home, now=T0, pending_share=2)
        self.assertIn(r["action"], ("speak", "voice", "share"))
        kinds = [e["kind"] for e in events.read_all(self.home)]
        self.assertIn("wake_due", kinds)
        self.assertEqual(self.home.quota()["count"], 1)

    def test_second_tick_within_gap_is_blocked(self):
        drive.tick(self.home, now=T0, pending_share=2)
        r = drive.tick(self.home, now=T0 + timedelta(minutes=5), pending_share=2)
        # 额度用完了就只是「不能找他说话」：可以写日记、可以自己刷会儿视频
        self.assertNotIn(r["action"], ("speak", "voice", "sing", "share"))
        self.assertEqual(self.home.quota()["count"], 1)

    def test_bili_outside_window_is_not_chosen(self):
        # 作息窗口是 20:00-24:00，下午三点不该去刷视频
        r = drive.tick(self.home, now=datetime(2026, 10, 28, 15, 0), host="test")
        self.assertNotEqual(r["action"], "bili")

    def test_budget_exhausted_blocks_speaking(self):
        self.home.save_json("quota.json", quota.default_state("2026-10-27"))
        r = drive.tick(self.home, now=T0, budget={"daily_cny": 0.0}, emit_silent=True)
        self.assertEqual(r["action"], "silent")
        self.assertEqual(r["blocked"], "budget")
        kinds = [e["kind"] for e in events.read_all(self.home)]
        self.assertIn("spend_guard", kinds)

    def test_initiative_is_logged(self):
        drive.tick(self.home, now=T0, pending_share=1)
        rows = self.home.read_jsonl("logs/initiatives.jsonl")
        self.assertEqual(len(rows), 1)
        self.assertIn("action", rows[0])
        self.assertIn("spent_today", rows[0])

    def test_mood_is_persisted(self):
        self.home.save_json("mood/state.json", {
            "axes": {"satisfaction": 30, "closeness": 0, "energy": 0},
            "label": "开心",
            "last_change": (T0 - timedelta(hours=24)).isoformat(),
            "history": [],
        })
        drive.tick(self.home, now=T0)
        axes = self.home.mood()["axes"]["satisfaction"]
        self.assertLess(axes, 30)  # 衰减被写回去了


    def test_second_host_cannot_steal_the_clock(self):
        """两个宿主共用一个 store：谁先 tick 谁当主人，另一个只读。"""
        from companion import drive
        first = drive.tick(self.home, host="hermes", now=T0)
        self.assertEqual(first.get("host"), "hermes")
        second = drive.tick(self.home, host="dsh", now=T0 + timedelta(minutes=1))
        self.assertEqual(second["action"], "silent")
        self.assertEqual(second.get("blocked"), "host")
        self.assertIn("hermes", second["why"])

    def test_owner_expires_so_another_host_can_take_over(self):
        from companion import drive
        drive.tick(self.home, host="hermes", now=T0)
        later = drive.tick(self.home, host="dsh", now=T0 + timedelta(minutes=31))
        self.assertNotEqual(later.get("blocked"), "host")
        self.assertEqual(later.get("host"), "dsh")


if __name__ == "__main__":
    unittest.main(verbosity=2)
