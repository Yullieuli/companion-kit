from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from companion import context  # noqa: E402
from companion.store import Home  # noqa: E402

T0 = datetime(2026, 10, 27, 21, 7)


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()
        (self.home.root / "SOUL.md").write_text("# 我是小鲸\n\n示例人格：说话简短，不用敬语。", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_order_is_fixed(self):
        ctx = context.build(
            self.home, now=T0, budget=3000,
            memory_text="她记得：主人喜欢硬核动作游戏",
            person_text="对方：好感 35，熟悉 20",
            life_text="21:00 看完一支评测",
            last_chat=T0.timestamp() - 3600,
        )
        names = [b.name for b in ctx.blocks]
        self.assertEqual(names, ["soul", "state", "memory", "person", "life"])

    def test_budget_is_respected(self):
        for budget in (200, 500, 1500):
            ctx = context.build(
                self.home, now=T0, budget=budget,
                memory_text="记忆" * 2000, person_text="人物卡" * 500, life_text="事件" * 500,
            )
            self.assertLessEqual(len(ctx.text), budget, f"budget={budget}")

    def test_empty_home_does_not_crash(self):
        empty = Home.at(tempfile.mkdtemp())
        ctx = context.build(empty, now=T0, budget=800)
        self.assertIn("现在", ctx.text)
        self.assertGreater(len(ctx.blocks), 0)

    def test_state_block_has_mood_and_quota(self):
        ctx = context.build(self.home, now=T0, budget=2000)
        self.assertIn("心情", ctx.text)
        self.assertIn("开口额度", ctx.text)

    def test_sleep_notice(self):
        ctx = context.build(self.home, now=datetime(2026, 10, 28, 2, 0), budget=2000)
        self.assertIn("睡觉", ctx.text)

    def test_hash_changes_only_for_changed_block(self):
        a = context.build(self.home, now=T0, budget=2000, memory_text="旧记忆")
        b = context.build(self.home, now=T0, budget=2000, memory_text="新记忆")
        ha = {x.name: x.hash for x in a.blocks}
        hb = {x.name: x.hash for x in b.blocks}
        self.assertNotEqual(ha["memory"], hb["memory"])
        self.assertEqual(ha["soul"], hb["soul"])
        self.assertEqual(ha["state"], hb["state"])

    def test_include_soul_false_drops_persona_block(self):
        """宿主自己有人格时（hermes 把它当系统提示），不该再注入一份。"""
        from companion import context as ctx_mod
        self.home.init()
        (self.home.root / "SOUL.md").write_text("# 我是谁\n\n人格正文。", encoding="utf-8")
        with_soul = ctx_mod.build(self.home, budget=800)
        without = ctx_mod.build(self.home, budget=800, include_soul=False)
        self.assertIn("人格正文", with_soul.text)
        self.assertNotIn("人格正文", without.text)
        self.assertNotIn("soul", [b.name for b in without.blocks])
        self.assertIn("state", [b.name for b in without.blocks])


class CacheTest(unittest.TestCase):
    def test_only_changed_blocks_reported(self):
        cache = context.Cache()
        b1 = [context.Block("soul", "a"), context.Block("state", "b")]
        self.assertEqual(sorted(cache.changed("s1", b1)), ["soul", "state"])
        b2 = [context.Block("soul", "a"), context.Block("state", "b2")]
        self.assertEqual(cache.changed("s1", b2), ["state"])
        self.assertEqual(cache.changed("s1", b2), [])

    def test_missing_block_reported(self):
        cache = context.Cache()
        cache.changed("s1", [context.Block("soul", "a"), context.Block("life", "x")])
        self.assertEqual(cache.changed("s1", [context.Block("soul", "a")]), ["life"])

if __name__ == "__main__":
    unittest.main(verbosity=2)

