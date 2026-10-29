"""增量投影：同一份 store，宿主每轮只注入变化的部分。

对应 SPEC §5.2「投影去重：每块算 hash，宿主侧缓存；未变化不重复注入」与
WORK_PLAN P1 第 3 项「MCP context 增加 session_id 和增量投影；未变化时不重复返回
完整前缀，并保留周期性全量恢复」。

两条不能省的性质：
  · 状态没变 → 这次一个字都不注入（省 token 的全部意义就在这）
  · 每 N 轮强制全量（会话历史会被压缩，压缩后她可能忘了自己是谁）
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from companion import context as ctx_mod, mcp  # noqa: E402
from companion.store import Home  # noqa: E402

T0 = datetime(2026, 10, 28, 21, 0)
T1 = datetime(2026, 10, 28, 21, 30)   # 只是时间推进，状态块内容不变


class ProjectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self, now=None):
        return ctx_mod.compose(self.home, now=now or T0, budget=2000)

    def test_first_call_is_full(self):
        proj = ctx_mod.project(self.home, "s1", self._ctx())
        self.assertTrue(proj.full)
        self.assertEqual(proj.text, self._ctx().text)
        self.assertIn("首次注入", proj.reason)

    def test_sticky_blocks_are_always_injected(self):
        """常驻块（人格/生活）每轮都注入 —— 模型不会跨轮记住注入内容。

        2026-10-29 的教训：投影原本对所有块"没变就不给"。结果第二轮注入为空，
        她连自己刚看过三条显卡评测都不知道（主人一眼看出来的）。
        省 token 不能省掉"她是谁"。
        """
        ctx_mod.project(self.home, "s1", self._ctx())
        proj = ctx_mod.project(self.home, "s1", self._ctx(T1))   # 只过了半小时
        self.assertFalse(proj.full)
        self.assertIn("soul", proj.changed, "人格是常驻块，必须每轮带上")
        self.assertTrue(proj.text.strip(), "不能是空注入")
        self.assertIn("常驻块", proj.reason)

    def test_state_alone_would_be_empty_without_sticky(self):
        """只有 state 这种真·变量才走增量：没变的部分不该重复。"""
        ctx = ctx_mod.compose(self.home, now=T0, budget=2000, include_soul=False)
        self.assertEqual([b.name for b in ctx.blocks if b.sticky], [],
                         "include_soul=False 且没别的常驻块时，应该没有 sticky")
        ctx_mod.project(self.home, "s2", ctx)
        proj = ctx_mod.project(self.home, "s2", ctx_mod.compose(
            self.home, now=T1, budget=2000, include_soul=False))
        self.assertEqual(proj.text, "")
        self.assertIn("没变", proj.reason)

    def test_changed_block_is_injected_alone(self):
        ctx_mod.project(self.home, "s1", self._ctx())
        # 走真实写入路径（life.record），life 块才会变。
        from companion import life

        life.record(self.home, "看到一只很胖的橘猫", kind="watch",
                    detail="蹲在楼道口", at=T0.replace(minute=5))
        proj = ctx_mod.project(self.home, "s1", self._ctx(T1))
        self.assertFalse(proj.full)
        self.assertIn("life", proj.changed)
        self.assertIn("橘猫", proj.text)

    def test_periodic_full_recovery(self):
        """每 full_every 轮强制全量：历史被压缩后她不会忘了自己是谁。"""
        first = ctx_mod.project(self.home, "s1", self._ctx(), full_every=4)
        self.assertTrue(first.full, "第一轮就该全量")
        for i in range(3):
            proj = ctx_mod.project(self.home, "s1", self._ctx(), full_every=4)
            self.assertFalse(proj.full, f"第 {i + 2} 轮不该全量")
            # 不是空 —— 常驻块（人格）每轮都带上，只是不给全量
            self.assertTrue(proj.text.strip())
            self.assertLess(len(proj.text), len(self._ctx().text), "增量该比全量短")
        proj = ctx_mod.project(self.home, "s1", self._ctx(), full_every=4)
        self.assertTrue(proj.full)
        self.assertIn("周期性全量", proj.reason)
        self.assertEqual(proj.text, self._ctx().text)

    def test_force_full(self):
        ctx_mod.project(self.home, "s1", self._ctx())
        proj = ctx_mod.project(self.home, "s1", self._ctx(), force=True)
        self.assertTrue(proj.full)
        self.assertIn("强制全量", proj.reason)

    def test_sessions_are_independent(self):
        ctx_mod.project(self.home, "s1", self._ctx())
        proj2 = ctx_mod.project(self.home, "s2", self._ctx())
        self.assertTrue(proj2.full, "新会话应当第一次全量")
        self.assertNotEqual(proj2.rounds, 0)

    def test_state_survives_restart(self):
        """指纹存在 store 里，不是进程内存：重启后仍然认得上一轮。"""
        ctx_mod.project(self.home, "s1", self._ctx())
        again = Home.at(self.tmp.name)
        proj = ctx_mod.project(again, "s1", ctx_mod.compose(again, now=T1, budget=2000))
        self.assertFalse(proj.full)
        self.assertIn("soul", proj.changed, "重启后仍认得：人格照旧带上，不走全量")

    def test_mcp_context_with_session_id_projects(self):
        out = mcp._t_context(self.home, {"session_id": "mcp-s", "budget": 800})
        self.assertTrue(out["full"])
        self.assertEqual(out["session_id"], "mcp-s")
        again = mcp._t_context(self.home, {"session_id": "mcp-s", "budget": 800})
        self.assertFalse(again["full"])
        # 常驻块每轮都带：模型不会跨轮记住注入内容（2026-10-29 的教训）
        self.assertGreater(again["total_chars"], 0)
        self.assertLess(again["total_chars"], out["total_chars"], "增量该比全量短")

    def test_mcp_context_without_session_is_unchanged(self):
        """不传 session_id 的调用必须与以前逐字节一致（契约测试不许破）。"""
        out = mcp._t_context(self.home, {"budget": 800})
        self.assertNotIn("full", out)
        self.assertEqual(out["text"], ctx_mod.compose(self.home, budget=800).text)

    def test_cli_context_session(self):
        """CLI 走同一套投影：常驻块照旧带，但不再给全量。"""
        import io
        from contextlib import redirect_stdout

        from companion.cli import main

        buf = io.StringIO()
        with redirect_stdout(buf):
            main(["--home", self.tmp.name, "context", "--session", "cli-s"])
        first = buf.getvalue()
        self.assertIn("【现在】", first)

        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            main(["--home", self.tmp.name, "context", "--session", "cli-s"])
        second = buf2.getvalue()
        # 不是空：人格/生活这些常驻块每轮都得在，否则她就"忘了自己是谁"
        self.assertTrue(second.strip())
        self.assertLess(len(second), len(first), "增量该比全量短")


if __name__ == "__main__":
    unittest.main()
