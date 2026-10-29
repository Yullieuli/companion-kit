"""文档里的数字必须和代码真值一致。

为什么守这个：文档里"26 个 MCP 工具"飘成了"30 个"，读的人会以为少注册了
四个工具；更糟的是 SPEC 里写着"实际 27 个"——三份文档三个数。
这种事不会让程序崩，但它会让"验收过没有"变得没法信。

这里不靠人肉同步：直接从 `mcp.TOOLS` 取真值，再扫文档里所有"N 个工具"式的
表述，数字对不上就红。加一个工具就自动红一次，逼着人一起改。
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from companion import mcp  # noqa: E402

DOCS = ["README.md", "docs/SPEC.md", "docs/VERIFY.md", "docs/WORK_PLAN.md"]

# 文档里描述"实际对外有多少个工具"的说法。
# 注意：SPEC §9 说的是"规格里的 8 个"（那是需求，不是实现数），
# 上面已经把那句话改写掉了，不在这里白名单。
COUNT_PATTERNS = [
    re.compile(r"(\d+)\s*个\s*MCP\s*工具"),
    re.compile(r"(\d+)\s*个\s*工具"),
    re.compile(r"(\d+)\s*工具"),
    re.compile(r"实际对外\s*(\d+)\s*个"),
    re.compile(r"实际\s*(\d+)\s*个"),
    re.compile(r"对外\s*(\d+)\s*个\s*工具"),
    re.compile(r"全部\s*(\d+)\s*个\s*工具"),
]

# 这些表述里的数字不是"工具个数"，不能拿来比。
# （比如 "8 个工具"指 SPEC 的 8 个必选工具，或 "4 个平台"之类。）
SKIP_CONTEXT = ("规格里的", "必选工具", "八个 MCP 工具")


class ToolCountTruthTest(unittest.TestCase):
    def setUp(self):
        self.real = len(mcp.TOOLS)
        self.assertGreaterEqual(self.real, 26, "工具数量异常，TOOLS 注册表可能坏了")

    def test_doc_tool_counts_match_code(self):
        bad: list[str] = []
        for rel in DOCS:
            p = ROOT / rel
            if not p.exists():
                continue
            for lineno, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if any(s in line for s in SKIP_CONTEXT):
                    continue
                for pat in COUNT_PATTERNS:
                    for m in pat.finditer(line):
                        n = int(m.group(1))
                        # 只盯"看起来像工具数"的量级，避免误伤正文里的其它数字
                        if n < 20:
                            continue
                        if n != self.real:
                            bad.append(f"{rel}:{lineno} 写了 {n}，实际 {self.real} → {line.strip()[:90]}")
        self.assertEqual(bad, [], "文档里的工具数与代码不一致：\n" + "\n".join(bad))

    def test_every_tool_has_annotations(self):
        """每个工具都要有安全注解——客户端靠它决定要不要为只读查询弹批准。"""
        for t in mcp.TOOLS:
            with self.subTest(tool=t["name"]):
                ann = t.get("annotations")
                self.assertIsInstance(ann, dict, f"{t['name']} 缺 annotations")
                for k in ("readOnlyHint", "destructiveHint", "openWorldHint"):
                    self.assertIn(k, ann, f"{t['name']} 缺 {k}")

    def test_tool_names_are_unique(self):
        names = [t["name"] for t in mcp.TOOLS]
        self.assertEqual(len(names), len(set(names)), "有重名工具，MCP 客户端会只认一个")

    def test_spec_eight_required_tools_present(self):
        """SPEC §5.1 点名的 8 个工具必须都在。"""
        required = {
            "companion_context", "companion_react", "companion_say",
            "companion_life_record", "companion_mood", "companion_person",
            "companion_tick", "companion_bili",
        }
        names = {t["name"] for t in mcp.TOOLS}
        self.assertEqual(required - names, set(), "SPEC 要求的工具不见了")


if __name__ == "__main__":
    unittest.main()
