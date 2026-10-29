"""H 组补齐 + 记忆自动提炼 的测试。

H1 打字延迟 · H2 自动重连（心跳）· H5 断点续传 · H7 僵尸看门狗
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from companion.store import Home
from companion import (distill, guard, memory, react as react_mod, resume as resume_mod,
                       typing as typing_mod, watchdog as wd_mod)


class _Rnd:
    """确定性随机：固定落在区间低端。"""

    def random(self):
        return 0.0


class TypingDelayTest(unittest.TestCase):
    """H1 打字延迟：秒回像机器人。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()

    def tearDown(self):
        self.tmp.cleanup()

    def test_short_message_is_fast(self):
        ms = typing_mod.delay_ms("在吗", rnd=_Rnd())
        self.assertGreaterEqual(ms, 800)
        self.assertLessEqual(ms, 1800)

    def test_long_message_is_slower(self):
        short = typing_mod.delay_ms("在吗", rnd=_Rnd())
        long = typing_mod.delay_ms("今天天气特别好我们出去走走吧好不好", rnd=_Rnd())
        self.assertGreater(long, short)

    def test_busy_multiplies(self):
        normal = typing_mod.delay_ms("在吗", rnd=_Rnd())
        busy = typing_mod.delay_ms("在吗", busy=True, rnd=_Rnd())
        self.assertGreater(busy, normal)

    def test_night_is_slower(self):
        day = typing_mod.delay_ms("在吗", now=datetime(2026, 10, 29, 14, 0), rnd=_Rnd())
        night = typing_mod.delay_ms("在吗", now=datetime(2026, 10, 29, 1, 0), rnd=_Rnd())
        self.assertGreater(night, day)

    def test_delay_has_upper_bound(self):
        ms = typing_mod.delay_ms("啊" * 5000, busy=True, now=datetime(2026, 10, 29, 1, 0), rnd=_Rnd())
        self.assertLessEqual(ms, typing_mod.MAX_MS)

    def test_say_returns_typing_advice(self):
        """出口要给宿主一个"等多久"的建议。"""
        out = guard.say(self.home, "在吗")
        self.assertTrue(out["ok"], out)
        self.assertIn("typing_ms", out)
        self.assertGreater(out["typing_ms"], 0)
        self.assertIn("typing_seconds", out)

    def test_seconds_helper(self):
        s = typing_mod.seconds("在吗", rnd=_Rnd())
        self.assertGreater(s, 0)
        self.assertLess(s, 10)


class ResumeTest(unittest.TestCase):
    """H5 断点续传：说了但没写回的轮次要找得回来。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()

    def tearDown(self):
        self.tmp.cleanup()

    def test_say_registers_pending_turn(self):
        out = guard.say(self.home, "刚说了一句")
        self.assertTrue(out["ok"], out)
        rows = resume_mod.pending(self.home)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["turn_id"], out["turn_id"])

    def test_react_settles_the_turn(self):
        """写回成功 → 销掉登记，不再是待办。"""
        out = guard.say(self.home, "刚说了一句")
        react_mod.react(self.home, said="刚说了一句", turn_id=out["turn_id"],
                        mood_delta={"satisfaction": 1})
        self.assertEqual(resume_mod.pending(self.home), [])

    def test_pending_survives_crash(self):
        """模拟崩溃：说了但没 react → 仍然能查出来。"""
        guard.say(self.home, "崩之前说的")
        rows = resume_mod.pending(self.home)
        self.assertEqual(len(rows), 1)
        self.assertIn("崩之前说的", rows[0]["text"])

    def test_settle_is_idempotent(self):
        """settle 是幂等的：重复调用都返回成功，不报错（重试场景）。"""
        out = guard.say(self.home, "说一句")
        self.assertTrue(resume_mod.settle(self.home, out["turn_id"]))
        self.assertTrue(resume_mod.settle(self.home, out["turn_id"]))
        self.assertEqual(resume_mod.pending(self.home), [])

    def test_old_turns_are_not_pending(self):
        """太久以前的（超过陈旧窗口）不再算待办——补了也没意义。"""
        old = datetime.now() - timedelta(hours=100)
        resume_mod.register(self.home, "很久以前", turn_id="old-1", now=old)
        self.assertEqual(resume_mod.pending(self.home, max_age_hours=72), [])

    def test_summary_counts(self):
        # 注意：连续两次 say 会被"45 分钟间隔"闸拦住（这是对的），
        # 所以这里直接登记两条，测的是"计数"本身。
        resume_mod.register(self.home, "一", turn_id="t1")
        resume_mod.register(self.home, "二", turn_id="t2")
        s = resume_mod.summary(self.home)
        self.assertEqual(s["pending"], 2)

    def test_turn_id_is_stable_for_same_minute(self):
        at = datetime(2026, 10, 29, 21, 30)
        a = resume_mod.turn_id_for("同一句", at)
        b = resume_mod.turn_id_for("同一句", at)
        self.assertEqual(a, b)


class WatchdogTest(unittest.TestCase):
    """H2/H7：心跳 + 看门狗。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()

    def tearDown(self):
        self.tmp.cleanup()

    def test_beat_writes_heartbeat(self):
        row = wd_mod.beat(self.home, host="hermes")
        self.assertEqual(row["host"], "hermes")
        self.assertGreater(row["pid"], 0)
        self.assertTrue((self.home.root / "logs" / "heartbeat.json").exists())

    def test_check_healthy(self):
        wd_mod.beat(self.home, host="hermes")
        out = wd_mod.check(self.home)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["state"], "alive")
        self.assertTrue(out["alive"])

    def test_check_never_beat(self):
        """从没心跳过：不算"出问题"（新 store 也这样），但要说清状态。"""
        out = wd_mod.check(self.home)
        self.assertEqual(out["state"], "never")
        self.assertTrue(out["ok"])
        self.assertIn("还没有心跳", out["why"])

    def test_check_dead_process(self):
        """心跳里的 pid 不在了 → dead（该重启，不是重连）。"""
        wd_mod.beat(self.home, host="hermes")
        hb = self.home.read_json("logs/heartbeat.json")
        hb["pid"] = 999999      # 一个几乎不可能存在的 pid
        self.home.save_json("logs/heartbeat.json", hb)
        out = wd_mod.check(self.home)
        self.assertEqual(out["state"], "dead")
        self.assertFalse(out["ok"])

    def test_check_stale_but_alive(self):
        """进程还在但很久没心跳 → stale（该重连，不是重启）。"""
        wd_mod.beat(self.home, host="hermes")
        hb = self.home.read_json("logs/heartbeat.json")
        hb["epoch"] = (datetime.now() - timedelta(minutes=45)).timestamp()
        self.home.save_json("logs/heartbeat.json", hb)
        out = wd_mod.check(self.home, stale_minutes=20)
        self.assertEqual(out["state"], "stale")
        self.assertTrue(out["alive"])
        self.assertFalse(out["ok"])

    def test_stale_hosts_lists_multiple(self):
        """多个宿主时，能分别报出谁卡住了。"""
        for host in ("hermes", "dsh"):
            self.home.append_jsonl("logs/heartbeats.jsonl", {
                "host": host, "pid": os.getpid(),
                "at": datetime.now().isoformat(timespec="seconds"),
                "epoch": (datetime.now() - timedelta(minutes=60)).timestamp()})
        out = wd_mod.stale_hosts(self.home, stale_minutes=20)
        self.assertEqual(len(out), 2)
        self.assertTrue(all(r["state"] == "stale" for r in out))

    def test_alive_check_on_real_pid(self):
        self.assertTrue(wd_mod.alive(os.getpid()))
        self.assertFalse(wd_mod.alive(0))
        self.assertFalse(wd_mod.alive(-1))


class DistillTest(unittest.TestCase):
    """记忆自动提炼：挖模式，但只给候选。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()

    def tearDown(self):
        self.tmp.cleanup()

    def _say(self, text, hour=22, minute=0):
        at = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
        self.home.append_jsonl("logs/said.jsonl", {
            "at": at.isoformat(timespec="seconds"),
            "epoch": at.timestamp(), "text": text, "source": "say"})

    def test_no_candidates_when_empty(self):
        self.assertEqual(distill.candidates(self.home), [])
        self.assertIn("没有发现", distill.review(self.home))

    def test_time_pattern_found(self):
        """他总在深夜找我 → 挖出时间模式。"""
        for i in range(4):
            self._say(f"你还没睡吗 {i}", hour=22, minute=i * 5)
        cands = distill.candidates(self.home)
        self.assertTrue(cands, "应该挖出时间模式")
        self.assertEqual(cands[0]["kind"], "time_pattern")
        self.assertGreaterEqual(cands[0]["count"], 4)

    def test_single_occurrence_is_not_a_pattern(self):
        """只出现一次的不算模式——那是巧合。"""
        self._say("就这一次", hour=22)
        cands = distill.candidates(self.home)
        self.assertFalse([c for c in cands if c["kind"] == "time_pattern"])

    def test_topic_pattern_found(self):
        """无词典新词发现：能从"我们聊聊奶茶吧"里认出"奶茶"。"""
        samples = ["我们聊聊奶茶吧", "今天喝的奶茶真好喝", "奶茶喝完了",
                   "我想喝奶茶了", "这家的奶茶不错"]
        for i, s in enumerate(samples):
            self._say(s, hour=22, minute=i * 3)
        cands = distill.candidates(self.home)
        topics = [c for c in cands if c["kind"] == "topic_pattern"]
        self.assertTrue(topics, "应该挖出话题模式")
        self.assertTrue(any("奶茶" in t["summary"] for t in topics),
                        f"应该认出『奶茶』，实际挖到：{[t['summary'] for t in topics]}")

    def test_already_in_memory_is_skipped(self):
        """已经在记忆页里的词不重复提。"""
        memory.write(self.home, "奶茶", "他喜欢喝奶茶", summary="他喜欢喝奶茶", tags=["偏好"])
        samples = ["我们聊聊奶茶吧", "今天喝的奶茶真好喝", "奶茶喝完了",
                   "我想喝奶茶了", "这家的奶茶不错"]
        for i, s in enumerate(samples):
            self._say(s, hour=22, minute=i * 3)
        cands = distill.candidates(self.home)
        topics = [c for c in cands if c["kind"] == "topic_pattern"]
        self.assertFalse(any("奶茶" in t["summary"] for t in topics),
                         "已经在记忆里的词不该再被提一次")

    def test_accept_writes_page(self):
        out = distill.accept(self.home, "他经常在深夜找我")
        self.assertTrue(out["ok"], out)
        self.assertEqual(len(memory.pages(self.home)), 1)
        self.assertEqual(memory.pages(self.home)[0].summary, "他经常在深夜找我")

    def test_accept_requires_summary(self):
        with self.assertRaises(ValueError):
            distill.accept(self.home, "   ")

    def test_review_is_readable(self):
        for i in range(4):
            self._say(f"你还没睡吗 {i}", hour=22, minute=i * 5)
        text = distill.review(self.home)
        self.assertIn("可能值得记住的事", text)
        self.assertIn("出现", text)


class NewCommandsTest(unittest.TestCase):
    """新子命令的 CLI 契约。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            from companion.cli import main
            main(["--home", self.home, "init"])

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *args):
        import io
        from contextlib import redirect_stdout
        from companion.cli import main
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["--home", self.home, *args])
        return code, buf.getvalue()

    def test_resume_pending(self):
        code, out = self._run("resume")
        self.assertEqual(code, 0)

    def test_resume_settle(self):
        code, out = self._run("say", "--text", "说一句")
        self.assertEqual(code, 0)
        code, out = self._run("resume", "settle", "--turn-id", "nope")
        self.assertEqual(code, 1)

    def test_distill_review(self):
        code, out = self._run("distill")
        self.assertEqual(code, 0)

    def test_distill_candidates(self):
        code, out = self._run("distill", "candidates")
        self.assertEqual(code, 0)

    def test_distill_accept_requires_summary(self):
        code, out = self._run("distill", "accept")
        self.assertEqual(code, 1)

    def test_distill_accept(self):
        code, out = self._run("distill", "accept", "--summary", "他喜欢喝奶茶")
        self.assertEqual(code, 0)

    def test_heartbeat(self):
        code, out = self._run("heartbeat", "--host", "test")
        self.assertEqual(code, 0)
        self.assertIn("心跳", out)


class McpNewToolsTest(unittest.TestCase):
    """新工具在 MCP 层能调。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()

    def tearDown(self):
        self.tmp.cleanup()

    def test_tools_registered(self):
        from companion import mcp
        names = [t["name"] for t in mcp.TOOLS]
        for n in ("companion_resume", "companion_distill", "companion_watchdog"):
            self.assertIn(n, names)
            self.assertIn(n, mcp.HANDLERS)

    def test_resume_tool(self):
        from companion import mcp
        out = mcp._t_resume(self.home, {"action": "pending"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["pending"], 0)

    def test_distill_tool(self):
        from companion import mcp
        out = mcp._t_distill(self.home, {"action": "review"})
        self.assertTrue(out["ok"], out)
        acc = mcp._t_distill(self.home, {"action": "accept", "summary": "他喜欢喝奶茶"})
        self.assertTrue(acc["ok"], acc)

    def test_watchdog_tool(self):
        from companion import mcp
        out = mcp._t_watchdog(self.home, {})
        self.assertIn("state", out)
        self.assertIn("hosts", out)

    def test_say_tool_returns_turn_id(self):
        from companion import mcp
        out = mcp._t_say(self.home, {"text": "从 MCP 来说一句"})
        self.assertTrue(out["ok"], out)
        self.assertIn("turn_id", out)
        self.assertIn("typing_ms", out)


if __name__ == "__main__":
    unittest.main()
