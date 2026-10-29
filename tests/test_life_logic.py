"""她说话有没有逻辑 —— 2026-10-29 真机那轮的回归测试。

原话：

    笨蛋仆从，今天一口气刷了六条硬件评测，显卡跌了价还看什么50系…
    …你倒是关心关心本公主呢？

数据其实一条没错：她那天真的有 6 条显卡相关记录，其中 1 条是价格资讯，
`detail` 里写着「5090单日跌4500…50系全线离高点…厂商建议买30系」。
错的是**读法**：

  1. 6 条平铺成 6 行，每行都标「可以聊」——一张六项待办清单，
     模型面对清单只会汇总播报，于是「一口气刷了六条」；
  2. 跨 30 小时的记录排在一段里、时间还倒着走——「一口气」在字面上成立了；
  3. `detail` 里「厂商建议买30系」是**别人的营销主张**，原样回灌后
     她把它当成自己的判断，再和「跌价」推出「还看什么50系」；
  4. 唤醒理由写的是「有 6 件生活里的事还没跟他说」——数字直接进了台词。

这四条各自钉一个测试。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from companion import drive, initiative, life  # noqa: E402
from companion.store import Home  # noqa: E402


class LifeLogicTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()

    def tearDown(self):
        self.tmp.cleanup()

    def _add(self, when: datetime, title: str, **kw):
        return life.record(self.home, title, at=when, **kw)

    # ── ① 别把一天的事说成"一口气" ────────────────────────────
    def test_groups_by_day(self):
        now = datetime(2026, 10, 29, 17, 30)
        self._add(now - timedelta(hours=2), "刷B站，看了《AMD Zen5台式机评测》")
        self._add(now - timedelta(days=1, hours=2), "刷B站，看了《RTX 5080 FE首发评测》")
        text = life.render(self.home, days=3, now=now)
        self.assertIn("今天", text)
        self.assertIn("昨天", text)
        self.assertLess(text.index("今天"), text.index("昨天"))

    def test_same_day_folds_beyond_per_day(self):
        """同一天超过 per_day 条就折叠 —— 六个并列项等于一张待办清单。"""
        now = datetime(2026, 10, 29, 17, 30)
        for i, name in enumerate(["RTX4060", "RX9070XT", "9800X3D", "Zen5", "5080", "4060Ti"]):
            self._add(now - timedelta(minutes=30 * (i + 1)), f"刷B站，看了《{name}评测》")
        text = life.render(self.home, days=2, now=now, per_day=3)
        self.assertIn("还有 3 件没列", text)
        # 明细行（"  15:00（…）看了"）只能是 3 条
        detail_lines = [ln for ln in text.splitlines() if ln.startswith("  ") and "（" in ln
                        and "没列" not in ln]
        self.assertEqual(len(detail_lines), 3)

    def test_only_two_items_are_chatty(self):
        """六条全喊「可以聊」，她就会六条全讲；只给两条。"""
        now = datetime(2026, 10, 29, 17, 30)
        for i in range(4):
            self._add(now - timedelta(minutes=20 * (i + 1)), f"看了第{i}个评测")
        text = life.render(self.home, days=2, now=now, per_day=4)
        self.assertEqual(text.count("可以聊"), 2)
        self.assertIn("还没跟他说", text, "其余的仍然知道，只是不催她讲")

    # ── ② 别人的主张不能变成她的判断 ──────────────────────────
    def test_detail_drops_someone_elses_advice(self):
        d = "显卡日报2026.10.29：5090单日跌4500创最大跌幅，50系全线离高点，厂商建议买30系｜grid=/tmp/x.jpg"
        out = life.core_detail(d)
        self.assertIn("5090单日跌4500", out, "事实要留")
        self.assertNotIn("建议买30系", out, "别人的建议一转述就成了她的判断")
        self.assertNotIn("/tmp", out, "抓取脚本的路径不该进她的回忆")

    def test_news_is_not_a_video(self):
        """没有 BV 号也不是书名的，是「刷到条消息」，不是「看了个视频」。"""
        news = {"kind": "watch", "title": "显卡价格大面积回调：11连涨被终结", "shared": False}
        video = {"kind": "watch", "title": "刷B站，看了《AMD RX9070XT显卡评测》",
                 "bvid": "BV1sb9ZYmESM", "shared": False}
        self.assertEqual(life._verb_for(news), "刷到")
        self.assertEqual(life._verb_for(video), "看了")

    def test_title_drops_bili_noise(self):
        t = life.core_title("刷B站，看了《RTX4060快速评测：比RTX3060快15%》（UP：极客湾Geekerwan，12分37秒）")
        self.assertEqual(t, "RTX4060快速评测：比RTX3060快15%")

    # ── ③ 立场要接得上，接不上就别接 ──────────────────────────
    def test_stances_refuses_single_generic_tag(self):
        """只共享一个「极客湾」，不足以把电视评测的立场接到显卡评测上。"""
        from companion import opinion as op

        now = datetime.now()
        # 3 小时前：超出 ±90 分钟的时间窗，这条只能靠 tag 接上
        self._add(now - timedelta(hours=3), "刷B站，看了《AMD Zen5台式机评测》", tags=["极客湾"])
        op.add(self.home, "数码评测该讲原理图而不是甩参数表，讲原理才能服人。",
               tags=["极客湾", "RGB Mini-LED"], confidence=0.5)
        self.assertEqual(life.stances(self.home, life.recent(self.home)), [])

    def test_stances_accepts_same_moment(self):
        """distill 提炼的观点和对应那条 life 是同一分钟，这层关系要用上。"""
        from companion import opinion as op

        at = datetime(2026, 10, 28, 10, 43)
        self._add(at, "刷B站，看了《旗舰手机芯片大横评》")
        op.add(self.home, "横评的价值在解释为什么，不是给排名。", tags=["微架构"], confidence=0.5, now=at)
        hits = life.stances(self.home, life.recent(self.home))
        self.assertEqual(len(hits), 1)
        self.assertIn("解释为什么", hits[0][0])
        self.assertTrue(hits[0][1], "还没想定的要标出来，不能冒充定论")


    def test_stances_accepts_two_shared_tags(self):
        """两个 tag 对上就是强信号，可以接。"""
        from companion import opinion as op

        now = datetime.now()
        self._add(now - timedelta(hours=3), "刷B站，看了《显卡评测》", tags=["显卡", "极客湾"])
        op.add(self.home, "显卡评测该看功耗曲线，不看首发跑分。",
               tags=["显卡", "极客湾"], confidence=0.5)
        hits = life.stances(self.home, life.recent(self.home))
        self.assertEqual(len(hits), 1)
        self.assertIn("功耗曲线", hits[0][0])

    def test_stances_accepts_long_text_overlap(self):
        """标题和 claim 共享 6 字以上，说明讲的是同一件事。"""
        from companion import opinion as op

        now = datetime.now()
        self._add(now - timedelta(hours=3), "刷B站，看了《双人抬棺协作游戏》")
        op.add(self.home, "双人抬棺协作游戏的门槛在机制，不在美术。",
               tags=["无关标签"], confidence=0.5)
        hits = life.stances(self.home, life.recent(self.home))
        self.assertEqual(len(hits), 1)

    def test_render_skips_entries_older_than_two_days(self):
        """recent 的 days 挡过一层，外部直接写进来的还得再挡一次。"""
        now = datetime(2026, 10, 29, 17, 30)
        self._add(now - timedelta(days=3), "刷B站，看了《很久以前的评测》")
        self.assertEqual(life.render(self.home, days=5, now=now), "")

    def test_render_respects_tiny_budget(self):
        now = datetime(2026, 10, 29, 17, 30)
        self._add(now - timedelta(hours=1), "刷B站，看了《某个很长的评测标题用来撑爆预算》")
        self.assertEqual(life.render(self.home, days=2, now=now, max_chars=1), "")

    def test_render_carries_her_stance(self):
        """事实和判断要在同一段里 —— 只看得到「看了什么」，她就只会数条数。"""
        from companion import opinion as op

        now = datetime(2026, 10, 29, 17, 30)
        self._add(now - timedelta(hours=1), "刷B站，看了《显卡评测》", tags=["显卡", "极客湾"])
        op.add(self.home, "显卡评测该看功耗曲线，不看首发跑分。",
               tags=["显卡", "极客湾"], confidence=0.5, now=now - timedelta(hours=1))
        text = life.render(self.home, days=2, now=now)
        self.assertIn("你自己琢磨出来的", text)
        self.assertIn("功耗曲线", text)
        self.assertLess(text.index("显卡评测"), text.index("功耗曲线"), "先有事，再有看法")

    def test_day_key_survives_bad_epoch(self):
        """外部写入的 epoch 可能是 -1 或天文数字，不能把整段生活流搞崩。"""
        self.assertEqual(life._day_key(-1, datetime(2026, 10, 29)), 9)
        self.assertEqual(life._day_key(1e18, datetime(2026, 10, 29)), 9)


class OpinionVisibilityTest(unittest.TestCase):
    """她的观点必须真的进得了上下文 —— 否则她手里只有流水，只能数条数。

    死结：BELIEF_AT=0.70，add() 默认 0.45。观点要够 0.70 得被反复 reinforce，
    reinforce 要她先说出来，说出来又要观点先在上下文里。2026-10-29 真机上
    她 4 条观点全是 0.5，一条都没进过上下文。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_hunch_is_visible(self):
        from companion import opinion as op

        op.add(self.home, "评测该讲原理，不是给排名。", tags=["评测"], confidence=0.45)
        text = op.render(self.home, settled_only=True)
        self.assertIn("评测该讲原理", text)
        self.assertIn("还没想定", text, "没验证过的要标明，不能冒充定论")

    def test_casual_guess_still_hidden(self):
        """0.3 那种随口一猜仍然不进 —— settled_only 不是形同虚设。"""
        from companion import opinion as op

        op.add(self.home, "这个大概不错吧。", tags=["随便"], confidence=0.3)
        self.assertEqual(op.render(self.home, settled_only=True), "")

    def test_recent_fills_when_query_misses(self):
        """她刚想的事，就算和这句话字面无关，也是"此刻在想的"。"""
        from companion import opinion as op

        op.add(self.home, "显卡评测该看功耗曲线。", tags=["显卡"], confidence=0.5)
        self.assertEqual(op.render(self.home, "今天天气不错", settled_only=True), "",
                         "没有 recent_days 时不硬塞")
        text = op.render(self.home, "今天天气不错", settled_only=True, recent_days=3)
        self.assertIn("功耗曲线", text)


class WakeReasonTest(unittest.TestCase):
    """唤醒理由不能把条数报给模型 —— 报了她就数给主人听。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()

    def tearDown(self):
        self.tmp.cleanup()

    def test_hint_is_one_concrete_thing(self):
        now = datetime.now()
        for i in range(6):
            life.record(self.home, f"刷B站，看了《显卡评测第{i}期》", at=now - timedelta(minutes=10 * i))
        hint = drive._pending_life_hint(self.home)
        self.assertIn("看了「", hint)
        self.assertNotIn("6", hint, "理由里不许出现条数")

    def test_why_never_contains_a_count(self):
        s = initiative.Situation(
            now=datetime.now(), mood_state={"label": "平静", "axes": {}},
            quota_state={}, rhythm={}, pending_life=6,
            extra={"pending_life_hint": "看了「AMD Zen5台式机评测」"},
        )
        speak = [c for c in initiative._candidates(s) if c.action == "speak"][0]
        self.assertIn("AMD Zen5台式机评测", speak.why)
        self.assertNotIn("6 件", speak.why)

    def test_hint_falls_back_when_empty(self):
        self.assertEqual(drive._pending_life_hint(self.home), "")
        s = initiative.Situation(
            now=datetime.now(), mood_state={"label": "平静", "axes": {}},
            quota_state={}, rhythm={}, pending_life=2,
        )
        speak = [c for c in initiative._candidates(s) if c.action == "speak"][0]
        self.assertIn("没跟他说", speak.why)


if __name__ == "__main__":
    unittest.main()
