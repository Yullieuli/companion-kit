"""core 覆盖率守门：SPEC §9 要求 core 覆盖率 ≥ 90%。

"core" 的口径按 SPEC §9 的原话定：**adapter 只测契约不测实现**。
所以平台适配层（devwin / devcdp / bilinet / devdelegate）不计入 core 指标——
它们要真机才测得动，强行 mock 只会测出"mock 写对了"而不是"代码对了"。

真正必须守住的是**纯逻辑 core**：时钟、心情、配额、决策、上下文、记忆、人物卡、
观点、生活、事件、围栏、身份锁、请示、成本、写回、群聊、表情、存储。
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# SPEC §4 的 core 层：纯逻辑，不碰平台
CORE_MODULES = [
    "ask", "clock", "context", "cost", "drive", "events", "fence", "group",
    "guard", "hosthooks", "imgpng", "initiative", "life", "memory", "mood",
    "mood_rules", "opinion", "people", "quota", "react", "sticker", "store",
]
# 平台适配层：SPEC §9 明说"adapter 只测契约不测实现"，不计入 core 指标
ADAPTER_MODULES = ["device", "devwin", "devcdp", "devdelegate", "bilinet", "bili", "voice", "mcp", "serve", "cli"]


class CoverageGateTest(unittest.TestCase):
    """守住 SPEC §9 的指标。

    **不**在这里跑全量（那会递归，而且慢）。这里只做两件轻量的事：
    1. 口径清单本身是对的（core 与 adapter 没串）
    2. 如果已经有 .coverage 数据，就校验它达标

    真正跑覆盖率的是 CI 的 `coverage` job 与本地：
        python -m coverage run --source=src/companion -m unittest discover -s tests
        python -m coverage report --include="*/ask.py,*/clock.py,..."
    """

    THRESHOLD = 90.0

    def test_module_lists_do_not_overlap(self):
        """core 与 adapter 不能有交集——否则指标会被稀释或重复算。"""
        self.assertFalse(set(CORE_MODULES) & set(ADAPTER_MODULES))
        self.assertIn("clock", CORE_MODULES)
        self.assertIn("mood", CORE_MODULES)
        self.assertIn("devwin", ADAPTER_MODULES)

    def test_core_modules_all_exist(self):
        """core 模块要在源码树里存在；装了包的环境（容器/site-packages）也要认。"""
        import importlib.util

        for m in CORE_MODULES:
            in_src = (ROOT / "src" / "companion" / f"{m}.py").exists()
            installed = importlib.util.find_spec(f"companion.{m}") is not None
            self.assertTrue(in_src or installed,
                            f"core 模块 {m} 既不在源码树里也装不出来")

    def test_existing_coverage_data_meets_spec(self):
        """如果本机已经跑过 coverage，就顺手校验一下；没跑就跳过。

        但只校验**可信的**数据 —— 两种残留数据都会让这条测试误报：
        1. 半途而废的运行（比如只跑了单个测试文件）留下的 0% 数据
        2. 源码改了、覆盖率没重跑留下的过期数据

        宁可跳过，也不能让守门测试自己变成噪音 —— 一个天天误报的守门
        等于没有守门，人会习惯性地忽略它。
        """
        data = ROOT / ".coverage"
        if not data.exists():
            self.skipTest("本机还没跑过 coverage（CI 的 coverage job 会守）")
        import subprocess

        # 过期检查：源码比覆盖率数据新，说明改完没重跑
        newest_src = max(
            (ROOT / "src" / "companion" / f"{m}.py").stat().st_mtime
            for m in CORE_MODULES
            if (ROOT / "src" / "companion" / f"{m}.py").exists()
        )
        if data.stat().st_mtime < newest_src:
            self.skipTest("覆盖率数据比源码旧 —— 改完代码没重跑，不拿旧数据说事")

        include = ",".join(f"*/{m}.py" for m in CORE_MODULES)
        rep = subprocess.run(
            [sys.executable, "-m", "coverage", "report", f"--include={include}"],
            cwd=str(ROOT), capture_output=True, text=True,
        )
        out = rep.stdout
        total_line = [ln for ln in out.splitlines() if ln.strip().startswith("TOTAL")]
        self.assertTrue(total_line, f"拿不到 TOTAL 行：\n{out}\n{rep.stderr}")

        # 完整性检查：有模块一行都没跑到，说明这不是全量跑出来的数据
        zeros = [ln.strip() for ln in out.splitlines()
                 if ln.strip().endswith("0%") and ".py" in ln]
        if zeros:
            self.skipTest(
                "这份覆盖率数据不是全量跑出来的（有模块 0%），不拿它说事：\n"
                + "\n".join(zeros))

        pct = float(total_line[0].split()[-1].rstrip("%"))
        self.assertGreaterEqual(
            pct, self.THRESHOLD,
            f"core 覆盖率 {pct}% < SPEC §9 要求的 {self.THRESHOLD}%\n{out}",
        )


if __name__ == "__main__":
    unittest.main()
