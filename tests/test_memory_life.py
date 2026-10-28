from __future__ import annotations
import json

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from companion import life, memory, people  # noqa: E402
from companion.store import Home  # noqa: E402


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()

    def tearDown(self):
        self.tmp.cleanup()

    def test_summary_is_required(self):
        with self.assertRaises(ValueError):
            memory.write(self.home, "标题", "正文", summary="  ")

    def test_write_roundtrip_and_index(self):
        memory.write(self.home, "影之刃零", "它把经验值砍了。", tags=["游戏", "设计"],
                     summary="这游戏敢砍经验值，是为了让玩家真学招式")
        p = memory.get(self.home, "影之刃零") or memory.get(self.home, "影之刃零".lower())
        self.assertIsNotNone(p)
        self.assertIn("游戏", p.tags)
        index = (self.home.root / "memory/index.md").read_text(encoding="utf-8")
        self.assertIn("敢砍经验值", index)
        self.assertIn("按标签", index)

    def test_search_prefers_tag_then_body(self):
        memory.write(self.home, "a", "无关内容", tags=["摄影"], summary="讲镜头")
        memory.write(self.home, "b", "弹幕里有人提到摄影", tags=["游戏"], summary="讲玩法")
        hits = memory.search(self.home, "摄影")
        self.assertEqual(hits[0][1].slug, "a")

    def test_search_returns_empty_when_nothing_matches(self):
        memory.write(self.home, "a", "x", tags=["游戏"], summary="讲玩法")
        self.assertEqual(memory.search(self.home, "量子力学"), [])

    def test_append_and_touch(self):
        memory.write(self.home, "页", "第一行", tags=["t"], summary="一句话")
        memory.append(self.home, "页", "第二行")
        self.assertIn("第二行", (memory.get(self.home, "页") or memory.parse("")).body)
        self.assertTrue(memory.touch(self.home, "页"))
        self.assertFalse(memory.touch(self.home, "不存在的页"))

    def test_append_missing_page_raises(self):
        with self.assertRaises(KeyError):
            memory.append(self.home, "没有这页", "x")

    def test_orphans_and_links(self):
        memory.write(self.home, "孤岛", "谁也不链", tags=["x"], summary="孤立")
        memory.write(self.home, "桥", "链到 [[孤岛]] 了", tags=["x"], summary="连着")
        orph = memory.orphans(self.home)
        self.assertNotIn("桥", orph)
        self.assertNotIn("孤岛", orph)  # 被链到了，不算孤岛

    def test_stale_reports_but_never_deletes(self):
        memory.write(self.home, "旧", "x", tags=["t"], summary="旧页")
        old = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")
        p = memory.get(self.home, "旧")
        p.last_access = old
        (self.home.root / "memory/pages/旧.md").write_text(memory.dump(p), encoding="utf-8")
        self.assertIn("旧", memory.stale(self.home))
        self.assertTrue((self.home.root / "memory/pages/旧.md").exists())

    def test_parse_tolerates_bad_frontmatter(self):
        page = memory.parse("---\ntags: [a, b\nsummary 没有冒号\n---\n# 标题\n正文")
        self.assertIn("标题", page.title)

    def test_render_has_summary_not_body(self):
        memory.write(self.home, "页", "正文很长" * 100, tags=["t"], summary="一句话结论")
        text = memory.render(self.home)
        self.assertIn("一句话结论", text)
        self.assertNotIn("正文很长", text)


class PeopleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()

    def tearDown(self):
        self.tmp.cleanup()

    def test_learn_dedupes_and_touches(self):
        people.learn(self.home, people.OWNER, "喜欢看纪录片")
        people.learn(self.home, people.OWNER, "喜欢看纪录片")
        p = people.get(self.home, people.OWNER)
        self.assertEqual(p.facts.count("喜欢看纪录片"), 1)
        self.assertTrue(p.last_seen)

    def test_resolve_by_alias(self):
        people.upsert(self.home, "owner", name="阿岚", aliases=["岚"])
        self.assertIsNotNone(people.resolve(self.home, "阿岚"))
        self.assertIsNotNone(people.resolve(self.home, "岚"))
        self.assertIsNone(people.resolve(self.home, "路人"))

    def test_closeness_is_clamped(self):
        people.upsert(self.home, "x", closeness=999)
        self.assertEqual(people.get(self.home, "x").closeness, 100)
        people.upsert(self.home, "x", closeness=-5)
        self.assertEqual(people.get(self.home, "x").closeness, 0)

    def test_render_empty_for_unknown(self):
        self.assertEqual(people.render(self.home, "不存在"), "")


class LifeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()

    def tearDown(self):
        self.tmp.cleanup()

    def test_extra_cannot_clobber_identity(self):
        # 外部（hermes 的 life.py）会把 bvid 传进来。传错名不能把主键顶掉，
        # 否则之后按 id 回填、按 id 标记已讲，全都失效。
        row = life.record(self.home, "看完了", extra={"id": "BV1xx411c7mD", "bvid": "BV1xx411c7mD"})
        self.assertNotEqual(row["id"], "BV1xx411c7mD")
        self.assertEqual(len(row["id"]), 12)
        self.assertEqual(row["bvid"], "BV1xx411c7mD")

    def test_record_and_dedupe(self):
        at = datetime(2026, 10, 27, 20, 31)
        a = life.record(self.home, "看完一部片子", kind="watch", at=at)
        b = life.record(self.home, "看完一部片子", kind="watch", at=at)
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(len(self.home.read_jsonl("life/life.jsonl")), 1)

    def test_unshared_then_mark(self):
        life.record(self.home, "玩了会儿游戏", kind="play")
        rows = life.unshared(self.home)
        self.assertEqual(len(rows), 1)
        self.assertEqual(life.mark_shared(self.home, [rows[0]["id"]]), 1)
        self.assertEqual(life.unshared(self.home), [])
        self.assertEqual(life.mark_shared(self.home, [rows[0]["id"]]), 0)

    def test_render_uses_relative_time(self):
        now = datetime(2026, 10, 27, 22, 0)
        life.record(self.home, "看完了", kind="watch", at=now - timedelta(hours=2))
        text = life.render(self.home, days=2, now=now)
        self.assertIn("小时前", text)
        self.assertIn("还没跟他说", text)

    def test_recent_respects_window(self):
        old = datetime.now() - timedelta(days=10)
        life.record(self.home, "很久以前", kind="read", at=old)
        self.assertEqual(life.recent(self.home, days=2), [])


    def test_match_shared_by_title_overlap(self):
        from companion.life import match_shared
        a = life.record(self.home, "看完极客湾旗舰手机芯片大横评", kind="watch")
        life.record(self.home, "刷到双人抬棺那个协作游戏", kind="watch")
        hits = match_shared(self.home, "刚看了极客湾那个芯片横评，能效曲线才是重点")
        self.assertEqual(hits, [a["id"]])

    def test_match_shared_returns_empty_when_unrelated(self):
        from companion.life import match_shared
        life.record(self.home, "看完极客湾旗舰手机芯片大横评", kind="watch")
        self.assertEqual(match_shared(self.home, "今天天气不错，出去走了走"), [])

    def test_match_shared_handles_long_titles(self):
        """真实标题往往一长串，按比例算阈值会高到匹配不上。"""
        from companion.life import match_shared
        a = life.record(
            self.home,
            "刷B站热门，下载了'双人抬棺'（Fallbearers，Z贤者），抽帧九宫格两张看清黑白哥特手绘、双人协作抬棺物理机制、葬礼NPC围观",
            kind="watch",
        )
        hits = match_shared(self.home, "本公主昨晚看的那个双人抬棺，两个人各压一边棺材往墓地挪")
        self.assertEqual(hits, [a["id"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TurnsRotateTest(unittest.TestCase):
    """turns 会一直长，必须轮转；不轮转一年几万行，备份和 grep 都变脏。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()

    def tearDown(self):
        self.tmp.cleanup()

    def test_trim_keeps_tail(self):
        from companion import react as react_mod

        p = self.home.root / "logs/turns.jsonl"
        rows = [{"at": f"2026-10-28T10:{i % 60:02d}:00", "role": "assistant", "said": f"第{i}句",
                 "pad": "x" * 100} for i in range(2100)]
        p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        react_mod._trim_turns(self.home, keep=2000)
        left = p.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(left), 2000)
        self.assertIn("第2099句", left[-1])
        self.assertNotIn("第0句", left[0])

    def test_trim_noop_when_small(self):
        from companion import react as react_mod

        p = self.home.root / "logs/turns.jsonl"
        p.write_text('{"said": "只有一句"}\n', encoding="utf-8")
        react_mod._trim_turns(self.home, keep=2000)
        self.assertEqual(len(p.read_text(encoding="utf-8").splitlines()), 1)
