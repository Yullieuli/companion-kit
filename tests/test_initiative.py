from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from companion import initiative, mood, quota  # noqa: E402

T0 = datetime(2026, 10, 27, 21, 0)
RHYTHM = {"windows": [{"start": "20:00", "end": "24:00", "wakes": 2}], "sleep": {"at": "00:30", "wake": "09:00"}}


class Fixed:
    def __init__(self, v):
        self.v = v

    def random(self):
        return self.v


def sit(**kw):
    base = dict(
        now=T0,
        mood_state=mood.default_state(),
        quota_state=quota.default_state("2026-10-27"),
        rhythm={**RHYTHM, "skip_chance": 0.0},
        quota_cfg={"daily": 8, "gap_min": 45},
        pending_life=0,
        pending_share=0,
        days_since_sing=99,
        days_since_voice=99,
        remaining_cny=99,
    )
    base.update(kw)
    return initiative.Situation(**base)


class GateTest(unittest.TestCase):
    def test_sleep_means_silent(self):
        r = initiative.decide(sit(now=datetime(2026, 10, 28, 2, 0)))
        self.assertEqual(r["action"], "silent")
        self.assertEqual(r["blocked"], "sleep")

    def test_quota_blocked_falls_back_to_diary(self):
        st = quota.record(quota.default_state("2026-10-27"), T0 - timedelta(minutes=5))
        st["count"] = 8
        r = initiative.decide(sit(quota_state=st))
        self.assertEqual(r["action"], "diary")
        self.assertEqual(r["blocked"], "quota")

    def test_silence_decay_blocks_speaking(self):
        seen = (T0 - timedelta(days=8)).timestamp()
        r = initiative.decide(sit(owner_last_seen=seen))
        self.assertEqual(r["action"], "diary")

    def test_dice_can_say_nothing(self):
        r = initiative.decide(sit(rhythm={**RHYTHM, "skip_chance": 0.9}), rnd=Fixed(0.1))
        self.assertEqual(r["action"], "silent")
        self.assertEqual(r["blocked"], "dice")

    def test_owner_waiting_overrides_dice(self):
        r = initiative.decide(sit(rhythm={**RHYTHM, "skip_chance": 0.9}, owner_waiting=True), rnd=Fixed(0.1))
        self.assertNotEqual(r["blocked"], "dice")

    def test_budget_exhausted_keeps_it_cheap(self):
        r = initiative.decide(sit(remaining_cny=0.0))
        self.assertEqual(r["action"], "silent")
        self.assertEqual(r["blocked"], "budget")


class ScoreTest(unittest.TestCase):
    def test_pending_life_pushes_speak(self):
        r = initiative.decide(sit(pending_life=2))
        self.assertEqual(r["action"], "speak")
        self.assertIn("没跟他说", r["why"])

    def test_low_mood_suppresses_speak(self):
        m = mood.default_state()
        m["axes"] = {"satisfaction": -40, "closeness": 40, "energy": 0}
        m["label"] = "低落"
        r = initiative.decide(sit(mood_state=m, pending_life=2))
        self.assertNotEqual(r["action"], "speak")

    def test_high_energy_happy_wants_to_sing(self):
        m = mood.default_state()
        m["axes"] = {"satisfaction": 30, "closeness": 20, "energy": 45}
        m["label"] = "开心"
        r = initiative.decide(sit(mood_state=m, days_since_sing=4, days_since_voice=0))
        self.assertEqual(r["action"], "sing")

    def test_sang_yesterday_does_not_sing_again(self):
        m = mood.default_state()
        m["axes"] = {"satisfaction": 30, "closeness": 20, "energy": 45}
        m["label"] = "开心"
        r = initiative.decide(sit(mood_state=m, days_since_sing=0.5))
        self.assertNotEqual(r["action"], "sing")

    def test_close_and_energetic_picks_voice(self):
        m = mood.default_state()
        m["axes"] = {"satisfaction": 5, "closeness": 50, "energy": 25}
        m["label"] = "平静"
        r = initiative.decide(sit(mood_state=m, days_since_voice=5, pending_life=0, pending_share=0))
        self.assertIn(r["action"], ("voice", "speak"))

    def test_pending_share_is_considered(self):
        r = initiative.decide(sit(pending_share=3))
        actions = {c["action"] for c in r["candidates"]}
        self.assertIn("share", actions)
        share = [c for c in r["candidates"] if c["action"] == "share"][0]
        self.assertGreater(share["score"], 0.5)

    def test_candidates_are_sorted(self):
        r = initiative.decide(sit(pending_life=1))
        scores = [c["score"] for c in r["candidates"]]
        self.assertEqual(scores, sorted(scores, reverse=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class FeedbackTest(unittest.TestCase):
    """被回应加分：只认"他后来接话了吗"这一个信号，且样本太少不加不减。"""

    def setUp(self):
        import tempfile

        from companion.store import Home

        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()

    def tearDown(self):
        self.tmp.cleanup()

    def _log_initiative(self, action: str, at: datetime):
        self.home.append_jsonl("logs/initiatives.jsonl", {"at": at.isoformat(timespec="seconds"),
                                                          "action": action, "why": "测试"})

    def _log_user(self, at: datetime):
        self.home.append_jsonl("logs/turns.jsonl", {"at": at.isoformat(timespec="seconds"),
                                                    "role": "user", "chars": 3})

    def test_settle_counts_reply(self):
        self._log_initiative("speak", T0)
        self._log_user(T0 + timedelta(minutes=8))
        fb = initiative.settle(self.home, now=T0 + timedelta(hours=1))
        self.assertEqual(fb["actions"]["speak"]["answered"], 1)
        self.assertEqual(fb["actions"]["speak"]["total"], 1)

    def test_settle_without_reply(self):
        self._log_initiative("speak", T0)
        fb = initiative.settle(self.home, now=T0 + timedelta(hours=1))
        self.assertEqual(fb["actions"]["speak"]["answered"], 0)
        self.assertEqual(fb["actions"]["speak"]["total"], 1)

    def test_settle_is_idempotent(self):
        self._log_initiative("speak", T0)
        self._log_user(T0 + timedelta(minutes=3))
        initiative.settle(self.home, now=T0 + timedelta(hours=1))
        again = initiative.settle(self.home, now=T0 + timedelta(hours=2))
        self.assertEqual(again["actions"]["speak"]["total"], 1)

    def test_quiet_actions_are_not_scored(self):
        self._log_initiative("diary", T0)
        self._log_user(T0 + timedelta(minutes=3))
        fb = initiative.settle(self.home, now=T0 + timedelta(hours=1))
        self.assertEqual(fb["actions"], {})

    def test_bonus_needs_samples(self):
        few = {"actions": {"speak": {"answered": 2, "total": 2}}}
        self.assertEqual(initiative.bonus(few, "speak"), 0.0)
        many = {"actions": {"speak": {"answered": 3, "total": 4}}}
        self.assertGreater(initiative.bonus(many, "speak"), 0)
        cold = {"actions": {"speak": {"answered": 0, "total": 5}}}
        self.assertLess(initiative.bonus(cold, "speak"), 0)

    def test_feedback_moves_candidate_score(self):
        from companion import mood as mood_mod

        base = initiative.Situation(now=T0, mood_state=mood_mod.default_state(), quota_state=quota.default_state(),
                                    rhythm=RHYTHM, quota_cfg={}, remaining_cny=1.0)
        warm = dict(base.extra)
        warm["feedback"] = {"actions": {"speak": {"answered": 4, "total": 4}}}
        hot = initiative.Situation(now=T0, mood_state=mood_mod.default_state(), quota_state=quota.default_state(),
                                   rhythm=RHYTHM, quota_cfg={}, remaining_cny=1.0, extra=warm)
        a = {c.action: c.score for c in initiative._candidates(base)}
        b = {c.action: c.score for c in initiative._candidates(hot)}
        self.assertGreater(b["speak"], a["speak"])
        self.assertEqual(b["diary"], a["diary"])
