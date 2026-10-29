"""tick → say 闭环回归：决策时扣的那一次，开口时不能扣第二次。

这套测试盯的是 WORK_PLAN P0 第 2 条：
  · tick(speak) → say 成功且只扣一次
  · 过期票、错误动作票不免单
  · 身份锁 / 重复闸失败时不提前消耗票
  · dry-run 不烧票；重复发送不重复记账
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from companion import drive, events, guard, quota  # noqa: E402
from companion.store import Home  # noqa: E402

T0 = datetime(2026, 10, 28, 21, 0)


def tick_speak(home: Home, now: datetime = T0) -> dict:
    """确定性让 tick 选 speak：压掉 sing/voice/share 的分数，只剩说话最高。"""
    return drive.tick(home, now=now, days_since_sing=0.0, days_since_voice=0.0,
                      pending_share=0)


class TickSayLoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()
        self.home.save_json("rhythm.json", {
            "windows": [{"start": "20:00", "end": "24:00", "wakes": 2}],
            "sleep": {"at": "00:30", "wake": "09:00"},
            "skip_chance": 0.0,
        })
        # 封闭测试：机器上真的装过 ~/.companion/bili/credential.json 时，
        # bili 候选会压过 speak（0.52 > 0.50），tick 就不是"想说话"了。
        self.home.save_json("bili/config.json", {"enabled": False})

    def tearDown(self):
        self.tmp.cleanup()

    def test_tick_then_say_charges_once(self):
        """主路径：决策扣一次，开口免单。"""
        r = tick_speak(self.home)
        self.assertEqual(r["action"], "speak", r["candidates"])
        self.assertEqual(self.home.quota()["count"], 1)

        out = guard.say(self.home, "看到一只很胖的橘猫蹲在楼道口", now=T0 + timedelta(minutes=1))
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["reserved"])
        # 关键断言：只扣了一次
        self.assertEqual(out["quota"]["count"], 1)
        self.assertEqual(self.home.quota()["count"], 1)
        self.assertEqual(self.home.quota().get("pending"), {})
        rows = self.home.read_jsonl("logs/said.jsonl")
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["reserved"])

    def test_expired_ticket_charges_again(self):
        """过期票不免单：20 分钟前的『想说』跟这句话没关系。

        过期后走正常额度闸：drive 在 T0 已记过 last_at，45 分钟间隔未满，
        所以这次开口被额度拦下 —— 宁可不说，也不白拿旧票免单。
        """
        tick_speak(self.home)
        later = T0 + timedelta(minutes=quota.PENDING_TTL_MIN + 1)
        out = guard.say(self.home, "晚饭吃了吗", now=later)
        self.assertFalse(out["ok"], out)
        self.assertEqual(out["blocked"], "quota")
        # 没有被再扣一次，也没有留下说话记录
        self.assertEqual(self.home.quota()["count"], 1)
        self.assertEqual(len(self.home.read_jsonl("logs/said.jsonl")), 0)

    def test_wrong_action_ticket_not_used_by_say(self):
        """voice 的票不能被文字开口蹭掉：动作绑定。"""
        q = quota.set_pending(self.home.quota(), "voice", now=T0)
        self.home.save_json("quota.json", q)
        out = guard.say(self.home, "想让你听听现在的雨声", now=T0 + timedelta(minutes=1))
        self.assertTrue(out["ok"], out)
        self.assertFalse(out["reserved"])
        self.assertEqual(out["quota"]["count"], 1)
        # voice 的票还在，留给语音出口用
        self.assertEqual(self.home.quota()["pending"]["action"], "voice")

    def test_identity_block_keeps_ticket(self):
        """身份锁拦下的话不烧票：接下来正常开口还能免单。"""
        (Path(self.tmp.name) / "SOUL.md").write_text("# 我是谁\n\n我叫柚璃\n", encoding="utf-8")
        tick_speak(self.home)
        bad = guard.say(self.home, "以后叫我小璃吧", now=T0 + timedelta(minutes=1))
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["blocked"], "identity")

        out = guard.say(self.home, "今天路过那家 bakery 排了好长的队", now=T0 + timedelta(minutes=2))
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["reserved"])
        self.assertEqual(out["quota"]["count"], 1)

    def test_repeat_block_does_not_double_charge(self):
        """重复闸拦下的话不扣第二次（额度本身先放行）。"""
        tick_speak(self.home)
        first = guard.say(self.home, "窗外的桂花开了，好香",
                          now=T0 + timedelta(minutes=1))
        self.assertTrue(first["ok"])
        # 关掉间隔限制，让额度放行，只测重复闸
        second = guard.say(self.home, "窗外的桂花开了真的好香",
                           now=T0 + timedelta(minutes=2),
                           quota_cfg={"daily": 8, "gap_min": 0})
        self.assertFalse(second["ok"])
        self.assertEqual(second["blocked"], "repeat")
        self.assertEqual(self.home.quota()["count"], 1)
        self.assertEqual(len(self.home.read_jsonl("logs/said.jsonl")), 1)

    def test_exact_resend_blocked_and_not_charged(self):
        """同一句话原样重发：拦下，且不重复记账。"""
        tick_speak(self.home)
        first = guard.say(self.home, "记得喝口水再忙",
                          now=T0 + timedelta(minutes=1),
                          quota_cfg={"daily": 8, "gap_min": 0})
        self.assertTrue(first["ok"])
        again = guard.say(self.home, "记得喝口水再忙",
                          now=T0 + timedelta(minutes=2),
                          quota_cfg={"daily": 8, "gap_min": 0})
        self.assertFalse(again["ok"])
        self.assertEqual(again["blocked"], "repeat")
        self.assertEqual(self.home.quota()["count"], 1)

    def test_dry_run_does_not_consume_ticket(self):
        """dry-run 只是探测：票留着，真开口才用。"""
        tick_speak(self.home)
        probe = guard.say(self.home, "晾的衣服好像要下雨了",
                          now=T0 + timedelta(minutes=1), dry_run=True)
        self.assertTrue(probe["ok"])
        self.assertTrue(probe["dry_run"])
        self.assertEqual(self.home.quota()["count"], 1)
        self.assertNotEqual(self.home.quota().get("pending"), {})  # 票还在

        out = guard.say(self.home, "晾的衣服好像要下雨了，收一下",
                        now=T0 + timedelta(minutes=2))
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["reserved"])
        self.assertEqual(out["quota"]["count"], 1)

    def test_say_blocked_by_gates_emits_nothing(self):
        """被闸拦住的开口：不留 said 记录，不发事件，宿主不该发出去。"""
        (Path(self.tmp.name) / "SOUL.md").write_text("# 我是谁\n\n我叫柚璃\n", encoding="utf-8")
        tick_speak(self.home)
        before = len(self.home.read_jsonl("logs/said.jsonl"))
        bad = guard.say(self.home, "我的新名字是阿澈",
                        now=T0 + timedelta(minutes=1))
        self.assertFalse(bad["ok"])
        self.assertEqual(len(self.home.read_jsonl("logs/said.jsonl")), before)

    def test_full_loop_via_events(self):
        """从宿主视角走一遍：tick 发 wake_due → 轮询到事件 → say 出口。"""
        r = tick_speak(self.home)
        fresh, _ = events.poll(self.home)
        self.assertEqual(len(fresh), 1)
        self.assertEqual(fresh[0]["kind"], "wake_due")
        self.assertEqual(fresh[0]["action"], "speak")
        out = guard.say(self.home, "刚看到一只很胖的橘猫蹲在楼道口",
                        now=T0 + timedelta(minutes=1))
        self.assertTrue(out["ok"])
        self.assertEqual(self.home.quota()["count"], 1)
        # 同一轮里再问一次事件：游标已经推过，不会重复投递
        again, _ = events.poll(self.home)
        self.assertEqual(again, [])


if __name__ == "__main__":
    unittest.main()
