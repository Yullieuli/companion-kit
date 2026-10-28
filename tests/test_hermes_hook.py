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


class DeltaInjectTest(unittest.TestCase):
    """块级去重：状态没变就别重发，上一轮那份还在历史里。

    省钱的关键不是"少写"，是"别把同一段 700 字的状态每轮重发一遍"。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self, state_text: str, life_text: str = ""):
        from companion import context as ctx_mod

        blocks = [ctx_mod.Block("state", state_text, 20)]
        if life_text:
            blocks.append(ctx_mod.Block("life", life_text, 80))
        return ctx_mod.Context(text="\n\n".join(b.text for b in blocks), blocks=blocks)

    def test_first_time_sends_everything(self):
        ctx = self._ctx("【现在】周三 17:57\n【状态】不在作息窗口")
        self.assertEqual(hook._render_delta(self.home, "s1", ctx), ctx.text)

    def test_unchanged_sends_nothing(self):
        ctx = self._ctx("【现在】周三 17:57\n【状态】不在作息窗口")
        hook._render_delta(self.home, "s1", ctx)
        same = self._ctx("【现在】周三 18:03\n【状态】不在作息窗口")   # 时间变了，其余没变
        self.assertEqual(hook._render_delta(self.home, "s1", same), "")

    def test_only_changed_blocks_sent(self):
        a = self._ctx("【现在】周三 17:57\n【状态】在")
        hook._render_delta(self.home, "s1", a)
        b = self._ctx("【现在】周三 17:57\n【状态】在", "【你最近在做的事】\n- 20:31 看了《影之刃零》")
        out = hook._render_delta(self.home, "s1", b)
        self.assertTrue(out.startswith("【状态更新】"))
        self.assertIn("影之刃零", out)
        self.assertNotIn("【状态】在", out)          # 没变的块不重发

    def test_full_refresh_every_n_turns(self):
        ctx = self._ctx("【现在】周三 17:57\n【状态】在")
        old = hook.FULL_EVERY
        hook.FULL_EVERY = 2
        try:
            first = hook._render_delta(self.home, "s1", ctx)     # count 0 → full
            second = hook._render_delta(self.home, "s1", ctx)    # count 1 → delta(空)
            third = hook._render_delta(self.home, "s1", ctx)     # count 2 → full
        finally:
            hook.FULL_EVERY = old
        self.assertTrue(first)
        self.assertEqual(second, "")
        self.assertEqual(third, ctx.text)

    def test_sessions_are_independent(self):
        ctx = self._ctx("【现在】周三 17:57\n【状态】在")
        hook._render_delta(self.home, "s1", ctx)
        self.assertEqual(hook._render_delta(self.home, "s2", ctx), ctx.text)
