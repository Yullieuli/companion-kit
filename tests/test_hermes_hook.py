"""hermes 钩子的写回：她能记住自己说过什么。

为什么必须有这一层：读侧（注入）早就通了，写侧一直是空的——
`logs/turns.jsonl` 在她真实 store 里 0 行，等于"每轮都没发生过"。
没有 write-back 就没有回放、没有"上次说过什么"、沉默衰减也会算错。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "hermes"))

import companion_hook as hook  # noqa: E402
from companion.store import Home  # noqa: E402


class TextTest(unittest.TestCase):
    def test_content_forms(self):
        self.assertEqual(hook._msg_text("直说"), "直说")
        self.assertEqual(hook._msg_text({"content": "包一层"}), "包一层")
        self.assertEqual(hook._msg_text([{"type": "text", "text": "分"}, {"type": "text", "text": "片"}]),
                         "分 片")
        self.assertEqual(hook._msg_text(None), "")

    def test_last_assistant_skips_trailing_user_turn(self):
        history = [
            {"role": "user", "content": "早"},
            {"role": "assistant", "content": "早啊"},
            {"role": "user", "content": "在吗"},
        ]
        self.assertEqual(hook._last_assistant(history), "早啊")

    def test_last_assistant_prefers_newest(self):
        history = [
            {"role": "assistant", "content": "第一句"},
            {"role": "assistant", "content": "第二句"},
        ]
        self.assertEqual(hook._last_assistant(history), "第二句")

    def test_no_history(self):
        self.assertEqual(hook._last_assistant(None), "")
        self.assertEqual(hook._last_assistant([]), "")
        self.assertEqual(hook._last_assistant([{"role": "user", "content": "x"}]), "")


class RecordTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()

    def tearDown(self):
        self.tmp.cleanup()

    def test_silent_is_not_recorded(self):
        with patch("subprocess.run", side_effect=AssertionError("不该调用")):
            hook._record_turn(self.home, "[SILENT]", {}, "t1", "napcat")
            hook._record_turn(self.home, "", {}, "t1", "napcat")

    def test_dedupe_by_turn_and_text(self):
        calls = []

        class Done:
            returncode = 0
            stdout = "{}"
            stderr = ""

        with patch("subprocess.run", side_effect=lambda *a, **k: calls.append(a) or Done()):
            hook._record_turn(self.home, "嗯嗯", {}, "t1", "napcat")
            hook._record_turn(self.home, "嗯嗯", {}, "t1", "napcat")   # 同一轮同一句：跳过
            hook._record_turn(self.home, "嗯嗯", {}, "t2", "napcat")   # 下一轮同一句：要记
        self.assertEqual(len(calls), 2)
        payload = json.loads((self.home.root / "logs" / ".hook_said.json").read_text(encoding="utf-8"))
        self.assertEqual(len(payload), 2)

    def test_payload_contains_context(self):
        seen = {}

        class Done:
            returncode = 0
            stdout = "{}"
            stderr = ""

        def fake(*a, **k):
            path = a[0][a[0].index("--from") + 1]
            seen.update(json.loads(Path(path).read_text(encoding="utf-8")))
            return Done()

        with patch("subprocess.run", side_effect=fake):
            hook._record_turn(self.home, "说完了", {"now": "2026-10-28T21:00:00", "blocks": []},
                              "t9", "napcat")
        self.assertEqual(seen["said"], "说完了")
        self.assertEqual(seen["context"]["now"], "2026-10-28T21:00:00")


if __name__ == "__main__":
    unittest.main()
