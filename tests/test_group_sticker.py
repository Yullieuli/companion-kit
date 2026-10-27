from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from companion import device, group, sticker  # noqa: E402
from companion.store import Home  # noqa: E402


class ZeroRnd:
    """random() 返回 0 → 一定落到"想接话"那一支。"""

    def random(self) -> float:
        return 0.0


class OneRnd:
    def random(self) -> float:
        return 0.999


class GroupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()
        self.T = datetime(2026, 10, 27, 21, 0)

    def tearDown(self):
        self.tmp.cleanup()

    def test_too_short_is_ignored(self):
        out = group.judge(self.home, "。", now=self.T)
        self.assertEqual(out["mode"], "ignore")

    def test_addressed_must_reply(self):
        out = group.judge(self.home, "你们觉得这游戏怎么样", addressed_to_me=True, now=self.T)
        self.assertEqual(out["mode"], "reply")
        self.assertIn("点名", out["reason"])

    def test_address_words_from_config(self):
        self.home.save_json("group/state.json", {"config": {"address_words": ["小鲸"]}})
        self.assertTrue(group.addressed("小鲸你在吗", group.config(self.home)))
        self.assertFalse(group.addressed("今天天气不错", group.config(self.home)))

    def test_cooldown_forces_lurk(self):
        group.note_reply(self.home, self.T)
        out = group.judge(self.home, "这游戏真好玩啊", now=self.T + timedelta(minutes=2), rnd=ZeroRnd())
        self.assertEqual(out["mode"], "lurk")
        self.assertIn("再等", out["reason"])

    def test_hour_cap(self):
        cfg = group.config(self.home)
        cfg["max_per_hour"] = 2
        self.home.save_json("group/state.json", {"config": cfg, "hour_key": self.T.strftime("%Y-%m-%d %H"),
                                                 "hour_count": 2, "last_at": 0})
        out = group.judge(self.home, "这游戏真好玩啊", now=self.T + timedelta(minutes=30), rnd=ZeroRnd())
        self.assertEqual(out["mode"], "lurk")
        self.assertIn("这一小时", out["reason"])

    def test_lurk_streak_turns_into_ignore(self):
        self.home.save_json("group/state.json", {"lurk_streak": 5})
        out = group.judge(self.home, "这游戏真好玩啊", now=self.T, rnd=ZeroRnd())
        self.assertEqual(out["mode"], "ignore")

    def test_reply_resets_streak_and_counts(self):
        self.home.save_json("group/state.json", {"lurk_streak": 2})
        group.note_reply(self.home, self.T)
        st = group.status(self.home, self.T)
        self.assertEqual(st["lurk_streak"], 0)
        self.assertEqual(st["hour_count"], 1)

    def test_random_branch(self):
        quiet = group.judge(self.home, "这游戏真好玩啊", now=self.T, rnd=OneRnd())
        self.assertEqual(quiet["mode"], "lurk")
        talk = group.judge(self.home, "这游戏真好玩啊", now=self.T + timedelta(minutes=30), rnd=ZeroRnd())
        self.assertEqual(talk["mode"], "reply")

    def test_owner_raises_chance(self):
        # 0.3 落在基础概率 0.22 之上、加主人权重 0.57 之下
        class MidRnd:
            def random(self) -> float:
                return 0.3

        without = group.judge(self.home, "这游戏真好玩啊", now=self.T, rnd=MidRnd())
        with_owner = group.judge(self.home, "这游戏真好玩啊", from_owner=True, now=self.T, rnd=MidRnd())
        self.assertEqual(without["mode"], "lurk")
        self.assertEqual(with_owner["mode"], "reply")


class StickerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()
        self.src = Path(self.tmp.name) / "src.png"
        device.write_png(self.src, 2, 2, bytes(2 * 2 * 3))

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_library_returns_none(self):
        self.assertIsNone(sticker.pick(self.home))

    def test_add_and_pick_by_mood(self):
        sticker.add(self.home, self.src, mood="无语", tags=["翻白眼"])
        hit = sticker.pick(self.home, "无语")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["mood"], "无语")
        self.assertTrue(Path(hit["path"]).exists())

    def test_pick_other_mood_returns_none(self):
        sticker.add(self.home, self.src, mood="无语")
        self.assertIsNone(sticker.pick(self.home, "生气"))

    def test_tag_match_boosts(self):
        sticker.add(self.home, self.src, mood="开心", tags=["鼓掌"])
        other = Path(self.tmp.name) / "b.png"
        device.write_png(other, 2, 2, bytes([255] * (2 * 2 * 3)))
        sticker.add(self.home, other, mood="开心", tags=["猫"])
        hit = sticker.pick(self.home, "开心", ["鼓掌"])
        self.assertIn("鼓掌", hit["tags"])

    def test_recent_sent_is_avoided(self):
        sticker.add(self.home, self.src, mood="开心", tags=["a"], name="one")
        other = Path(self.tmp.name) / "b.png"
        device.write_png(other, 2, 2, bytes([9] * (2 * 2 * 3)))
        sticker.add(self.home, other, mood="开心", tags=["b"], name="two")
        sticker.mark_sent(self.home, "one")
        picks = {sticker.pick(self.home, "开心", rnd=ZeroRnd())["name"] for _ in range(4)}
        self.assertIn("two", picks)

    def test_mark_sent_counts(self):
        rec = sticker.add(self.home, self.src, mood="困")
        self.assertTrue(sticker.mark_sent(self.home, rec["name"]))
        self.assertFalse(sticker.mark_sent(self.home, "不存在"))
        self.assertEqual(sticker.stats(self.home)["by_mood"].get("困"), 1)

    def test_bad_extension_and_missing_file(self):
        bad = Path(self.tmp.name) / "x.txt"
        bad.write_text("hi", encoding="utf-8")
        with self.assertRaises(ValueError):
            sticker.add(self.home, bad)
        with self.assertRaises(FileNotFoundError):
            sticker.add(self.home, Path(self.tmp.name) / "没有这张.png")

    def test_remove(self):
        rec = sticker.add(self.home, self.src, mood="开心")
        self.assertTrue(sticker.remove(self.home, rec["name"]))
        self.assertEqual(sticker.stats(self.home)["total"], 0)
        self.assertFalse(sticker.remove(self.home, rec["name"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
