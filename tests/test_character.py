"""性格层的聚合规则。

盯住三件容易错的事：
1. 泛标签（b站）不能聚成性格
2. 次数不够的不能算倾向
3. 常看的对象和在意方向要分开说，不能混成一句广告词
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from companion import character, life, opinion
from companion.store import Home


class CharacterTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Home(Path(self._tmp.name))
        self.home.init()
        self.now = datetime(2026, 11, 1, 12, 0)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _life(self, title: str, tags: list[str], *, kind: str = "watch", days_ago: float = 1) -> None:
        life.record(self.home, title, kind=kind, tags=tags,
                    at=self.now - timedelta(days=days_ago))

    def test_noise_tags_never_become_traits(self) -> None:
        """只带 b站 这种泛标签，聚出来必须是空的。"""
        for i in range(6):
            self._life(f"刷B站看了第{i}个", ["b站"], days_ago=i)
        data = character.rebuild(self.home, now=self.now)
        self.assertEqual(data["traits"], {})

    def test_needs_enough_occurrences(self) -> None:
        """出现一次不算倾向，三次才算。"""
        self._life("看了显卡评测", ["显卡价格"])
        data = character.rebuild(self.home, now=self.now)
        self.assertNotIn("显卡价格", data["traits"])

        self._life("又看了显卡评测", ["显卡价格"], days_ago=2)
        self._life("还是显卡评测", ["显卡价格"], days_ago=3)
        data = character.rebuild(self.home, now=self.now)
        self.assertIn("显卡价格", data["traits"])
        self.assertGreater(data["traits"]["显卡价格"]["strength"], 0.3)

    def test_evidence_is_traceable(self) -> None:
        """每条倾向都要能说出是哪几条经历推出来的。"""
        for i, t in enumerate(("A 评测", "B 评测", "C 评测")):
            self._life(t, ["芯片能效"], days_ago=i + 1)
        data = character.rebuild(self.home, now=self.now)
        ev = data["traits"]["芯片能效"]["evidence"]
        self.assertGreaterEqual(len(ev), 3)
        # days_ago=i+1，所以 i=0 的 "A 评测" 是最新的，应排第一
        self.assertEqual(ev[0]["what"], "A 评测")
        self.assertTrue(ev[0]["at"])

    def test_diy_outweighs_watch(self) -> None:
        """自己动手做的事，比看过的更塑形。"""
        for i in range(3):
            self._life(f"刷片{i}", ["手工"], kind="watch", days_ago=i + 1)
            self._life(f"动手{i}", ["手工"], kind="diy", days_ago=i + 1)
        data = character.rebuild(self.home, now=self.now)
        # 6 条（3 watch + 3 diy）的强度应明显高于纯 3 条 watch 的水平
        self.assertGreater(data["traits"]["手工"]["strength"], 0.6)

    def test_opinion_feeds_character(self) -> None:
        """成形的判断本身就该算进性格——她信什么就是她是什么人。"""
        for i in range(3):
            opinion.add(self.home, f"评测要看原理图 {i}", tags=["评测方法论"], confidence=0.7,
                        now=self.now - timedelta(days=i))
        data = character.rebuild(self.home, now=self.now)
        self.assertIn("评测方法论", data["traits"])

    def test_decay_halves_over_half_life(self) -> None:
        """45 天没碰，强度掉一半；但不会被删。"""
        for i in range(4):
            self._life(f"刷片{i}", ["纪录片"], days_ago=i + 1)
        character.rebuild(self.home, now=self.now)
        before = character.load(self.home)["traits"]["纪录片"]["strength"]

        later = self.now + timedelta(days=45)
        character.decay(self.home, now=later)
        after = character.load(self.home)["traits"]["纪录片"]["strength"]

        self.assertAlmostEqual(after / before, 0.5, delta=0.08)
        self.assertGreater(after, 0)

    def test_decay_waits_a_week(self) -> None:
        """一周内不衰减：短期波动不算性格变化。"""
        for i in range(4):
            self._life(f"刷片{i}", ["独立游戏"], days_ago=i + 1)
        character.rebuild(self.home, now=self.now)
        before = character.load(self.home)["traits"]["独立游戏"]["strength"]
        character.decay(self.home, now=self.now + timedelta(days=3))
        after = character.load(self.home)["traits"]["独立游戏"]["strength"]
        self.assertEqual(before, after)

    def test_render_splits_subjects_from_fields(self) -> None:
        """常看的对象和在意方向要分开说，不能混成一句广告词。"""
        for i in range(4):
            self._life(f"刷片{i}", ["极客湾", "芯片能效"], days_ago=i + 1)
        character.rebuild(self.home, now=self.now)
        text = character.render(self.home)
        self.assertIn("在意", text)
        self.assertIn("常看", text)
        self.assertIn("极客湾", text)
        self.assertNotIn("越来越在意极客湾", text)

    def test_render_empty_when_nothing_strong(self) -> None:
        self.assertEqual(character.render(self.home), "")

    def test_stats_reports_top(self) -> None:
        for i in range(4):
            self._life(f"刷片{i}", ["显卡价格"], days_ago=i + 1)
        character.rebuild(self.home, now=self.now)
        s = character.stats(self.home)
        self.assertGreaterEqual(s["total"], 1)
        self.assertEqual(s["top"][0][0], "显卡价格")

    def test_explain_returns_evidence(self) -> None:
        for i in range(4):
            self._life(f"刷片{i}", ["芯片能效"], days_ago=i + 1)
        character.rebuild(self.home, now=self.now)
        e = character.explain(self.home, "芯片能效")
        self.assertTrue(e["found"])
        self.assertTrue(e["evidence"])
        self.assertFalse(character.explain(self.home, "不存在")["found"])


if __name__ == "__main__":
    unittest.main()
