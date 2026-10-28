"""guard：说话前的三道闸。

为什么这三条必须在 core：它们写在提示词里只会得到"好的，我不会重复"，
然后下一轮继续重复。判定必须是确定性的——算得出来就说不行。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from companion import guard, quota  # noqa: E402
from companion.store import Home  # noqa: E402

NOW = datetime(2026, 10, 28, 21, 0)
SOUL = """# 我是谁

我叫柚璃，一个住在屏幕里的家伙。

## 说话

- 自称「本公主」
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()
        (self.home.root / "SOUL.md").write_text(SOUL, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def said(self, text: str, minutes_ago: float = 1) -> None:
        at = NOW - timedelta(minutes=minutes_ago)
        self.home.append_jsonl("logs/said.jsonl", {"at": at.isoformat(timespec="seconds"),
                                                  "text": text, "source": "test"})


class NameLockTest(Base):
    def test_soul_name(self):
        self.assertEqual(guard.soul_name(self.home), "柚璃")

    def test_blocks_renaming(self):
        r = guard.name_lock(self.home, "以后叫我小璃吧")
        self.assertTrue(r["blocked"])
        self.assertIn("柚璃", r["why"])

    def test_allows_normal_use_of_name(self):
        for text in ("我叫了外卖", "柚璃在这里", "本公主今天不想说话"):
            self.assertFalse(guard.name_lock(self.home, text)["blocked"], text)

    def test_without_soul_name_never_blocks(self):
        (self.home.root / "SOUL.md").write_text("# 我是谁\n\n（还没写）\n", encoding="utf-8")
        self.assertFalse(guard.name_lock(self.home, "以后叫我小璃")["blocked"])


class TopicTest(Base):
    def test_same_topic_blocks(self):
        self.said("这个游戏的战斗系统真的很有节奏感")
        r = guard.same_topic(self.home, "这游戏的战斗系统节奏感很棒", now=NOW)
        self.assertTrue(r["dup"])
        self.assertGreaterEqual(r["common"], 6)
        self.assertIn("战斗系统", r["prev"])

    def test_different_topic_passes(self):
        self.said("这个游戏的战斗系统真的很有节奏感")
        self.assertFalse(guard.same_topic(self.home, "你晚饭吃了什么", now=NOW)["dup"])

    def test_window_expires(self):
        self.said("这个游戏的战斗系统真的很有节奏感", minutes_ago=11)
        self.assertFalse(guard.same_topic(self.home, "这游戏的战斗系统节奏感很棒", now=NOW)["dup"])

    def test_short_text_never_treated_as_repeat(self):
        self.said("嗯")
        self.assertFalse(guard.same_topic(self.home, "嗯", now=NOW)["dup"])


class SayTest(Base):
    def test_empty(self):
        self.assertFalse(guard.say(self.home, "  ", now=NOW)["ok"])

    def test_identity_blocks_before_quota(self):
        out = guard.say(self.home, "以后叫我小璃", now=NOW)
        self.assertFalse(out["ok"])
        self.assertEqual(out["blocked"], "identity")
        self.assertEqual(len(self.home.read_jsonl("logs/said.jsonl")), 0)

    def test_quota_blocks(self):
        st = quota.default_state("2026-10-28")
        st["count"] = 8          # 今天已经说满
        self.home.save_json("quota.json", st)
        out = guard.say(self.home, "在吗", now=NOW)
        self.assertFalse(out["ok"])
        self.assertEqual(out["blocked"], "quota")

    def test_repeat_blocks(self):
        self.said("这个游戏的战斗系统真的很有节奏感", minutes_ago=2)
        out = guard.say(self.home, "这游戏的战斗系统节奏感很棒", now=NOW)
        self.assertFalse(out["ok"])
        self.assertEqual(out["blocked"], "repeat")
        self.assertIn("prev", out)

    def test_dry_run_does_not_write(self):
        out = guard.say(self.home, "今天风挺大", now=NOW, dry_run=True)
        self.assertTrue(out["ok"])
        self.assertTrue(out["dry_run"])
        self.assertEqual(len(self.home.read_jsonl("logs/said.jsonl")), 0)

    def test_ok_records_and_charges_quota(self):
        out = guard.say(self.home, "今天风挺大", now=NOW, channel="napcat")
        self.assertTrue(out["ok"])
        rows = self.home.read_jsonl("logs/said.jsonl")
        self.assertEqual(rows[-1]["text"], "今天风挺大")
        self.assertEqual(rows[-1]["channel"], "napcat")
        self.assertEqual(self.home.quota().get("count"), 1)


if __name__ == "__main__":
    unittest.main()
