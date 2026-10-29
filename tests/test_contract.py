"""契约测试：同一份 store，三个入口拼出来的上下文必须**逐字节一致**。

为什么这是唯一硬保证：dsh 走 CLI、hermes 走 CLI（hook）、codex 走 MCP。
三方各自拼一遍，只要有一处顺序或渲染不同，"同一个人换个宿主就变性格"。
所以这里不测"看起来对不对"，测**字节相等**。

第二个测试是回放：真实一轮里注入了什么，几个月后还能用当时的时钟重建出来。
它防的不是崩溃，是**行为漂移**——改了渲染逻辑导致她突然语气变了，这里会红。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from companion import (context as ctx_mod, life, mcp, memory, opinion, people,  # noqa: E402
                       react)
from companion.store import Home  # noqa: E402

NOW = datetime(2026, 10, 28, 21, 0)
PIN = NOW.isoformat()


def seed(home: Home) -> Home:
    """一份固定的 store：人格、一页记忆、两条观点、一张人物卡、两件生活事件。"""
    (home.root / "SOUL.md").write_text(
        "# 我是谁\n\n我叫柚璃，一个住在屏幕里的家伙。\n\n## 说话\n\n- 自称「本公主」\n- 句子短\n",
        encoding="utf-8")
    memory.write(home, "影之刃零", "战斗像武术不像回合，杀气值把体力槽变成了主动资源。",
                 tags=["游戏", "设计"], summary="杀气值让战斗有韵律")
    opinion.add(home, "它敢砍经验值，是为了逼玩家真学招式", tags=["游戏"], confidence=0.8)
    opinion.add(home, "国产 3A 缺的不是技术，是大型项目的经验", tags=["行业"], confidence=0.6)
    people.learn(home, people.OWNER, "讨厌开放式结局")
    life.record(home, "刷B站，看了《影之刃零》评测", kind="watch", at=NOW,
                detail="IGN 给 10 分", extra={"bvid": "BV19bHK6AEBD"})
    life.record(home, "把九宫格抽帧拼好了", kind="make", at=NOW)
    home.save_json("rhythm.json", {
        "timezone": "Asia/Shanghai",
        "windows": [{"start": "08:30", "end": "24:00", "wakes": 8}],
        "sleep": {"at": "01:00", "wake": "08:30"},
        "greeting": {"morning": True, "night": True, "active_days": 3},
    })
    home.save_json("mood/state.json", {
        "axes": {"satisfaction": 12, "closeness": 30, "energy": 5},
        "label": "平静", "last_change": PIN, "history": [],
    })
    home.save_json("quota.json", {"day": "2026-10-28", "count": 3, "last": NOW.timestamp() - 3600})
    return home


class ContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = seed(Home.at(self.tmp.name))
        self.home.init()

    def tearDown(self):
        self.tmp.cleanup()

    def _via_cli(self, *, budget: int = 900, no_soul: bool = False) -> dict:
        env = {**os.environ, "COMPANION_HOME": str(self.home.root),
               "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
        cmd = [sys.executable, "-m", "companion", "--json", "context",
               "--budget", str(budget), "--now", PIN]
        if no_soul:
            cmd.append("--no-soul")
        done = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def _via_mcp(self, *, budget: int = 900, no_soul: bool = False) -> dict:
        args = {"budget": budget, "now": PIN}
        if no_soul:
            args["no_soul"] = True
        out = mcp.HANDLERS["companion_context"](self.home, args)
        if no_soul:
            # MCP 这一版没有 no_soul 入参，用同样的组装方式自己算一遍
            out = ctx_mod.build(
                self.home, now=NOW, budget=budget, include_soul=False,
                memory_text=memory.render(self.home, ""),
                opinion_text=opinion.render(self.home, ""),
                person_text=people.render(self.home, people.OWNER),
                life_text=life.render(self.home),
            ).as_dict()
        return out

    def _direct(self, *, budget: int = 900, no_soul: bool = False) -> dict:
        # 走 compose 而不是自己拼一遍 render 参数：
        # 自己拼就等于第五种拼法，口径一漂契约测试就会红（2026-10-29 真红过一次）。
        # 契约测试要保证的正是"只有一种拼法"。
        return ctx_mod.compose(
            self.home, now=NOW, budget=budget, include_soul=not no_soul,
        ).as_dict()

    def test_three_entries_are_byte_identical(self):
        cli, mc, direct = self._via_cli(), self._via_mcp(), self._direct()
        self.assertEqual(cli["now"], PIN)
        self.assertEqual(cli, mc, "CLI 与 MCP 拼出来的不一样")
        self.assertEqual(cli, direct, "CLI 与直接 build 拼出来的不一样")

    def test_hermes_variant_without_soul(self):
        cli = self._via_cli(budget=700, no_soul=True)
        self.assertEqual(cli, self._direct(budget=700, no_soul=True))
        self.assertNotIn("soul", [b["name"] for b in cli["blocks"]])

    def test_block_order_is_fixed(self):
        order = [b["name"] for b in self._via_cli()["blocks"]]
        self.assertEqual(order, [n for n in ("soul", "state", "memory", "opinion", "person", "life")
                                 if n in order])
        self.assertEqual(order[0], "soul")
        self.assertEqual(order[1], "state")

    def test_budget_is_respected_and_tail_dropped_first(self):
        full = self._via_cli(budget=4000)
        tight = self._via_cli(budget=300)
        self.assertLessEqual(tight["total_chars"], 300)
        tight_names = [b["name"] for b in tight["blocks"]]
        full_names = [b["name"] for b in full["blocks"]]
        # 保留下来的永远是前缀：不会出现「砍了人格留了生活」这种
        self.assertEqual(tight_names, full_names[:len(tight_names)])
        self.assertIn("soul", tight_names)

    def test_same_input_same_bytes(self):
        a, b = self._via_cli(), self._via_cli()
        self.assertEqual(a, b)

    def test_cache_reports_only_changed_blocks(self):
        cache = ctx_mod.Cache()

        def blocks():
            return ctx_mod.build(
                self.home, now=NOW,
                memory_text=memory.render(self.home, ""),
                opinion_text=opinion.render(self.home, ""),
                person_text=people.render(self.home, people.OWNER),
                life_text=life.render(self.home),
            ).blocks

        first = cache.changed("s1", blocks())
        self.assertIn("soul", first)
        self.assertEqual(cache.changed("s1", blocks()), [])   # 同一份输入不该重复注入
        life.record(self.home, "刚看完一期讲生命起源的科普", kind="watch", at=NOW)
        self.assertEqual(cache.changed("s1", blocks()), ["life"])


class ReplayTest(unittest.TestCase):
    """回放：留档的是「注入了什么 + 当时几点」，重建必须一模一样。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = seed(Home.at(self.tmp.name))
        self.home.init()

    def tearDown(self):
        self.tmp.cleanup()

    def _build(self, now: datetime = NOW) -> dict:
        return ctx_mod.build(
            self.home, now=now,
            memory_text=memory.render(self.home, ""),
            opinion_text=opinion.render(self.home, ""),
            person_text=people.render(self.home, people.OWNER),
            life_text=life.render(self.home),
        ).as_dict()

    def test_turn_records_injection_and_replays(self):
        before = self._build()
        react.react(self.home, said="刚看了个影之刃的评测，杀气值那套真有意思",
                    context=before, now=NOW)

        row = self.home.read_jsonl("logs/turns.jsonl")[-1]
        self.assertEqual(row["context"]["now"], PIN)
        self.assertEqual([b["name"] for b in row["context"]["blocks"]],
                         [b["name"] for b in before["blocks"]])
        self.assertEqual([b["hash"] for b in row["context"]["blocks"]],
                         [b["hash"] for b in before["blocks"]])

        # 用留档的时钟重建：同一份 store 必须给出同一份注入
        replayed = self._build(datetime.fromisoformat(row["context"]["now"]))
        self.assertEqual(replayed["text"], before["text"])
        self.assertEqual(replayed["blocks"], before["blocks"])

    def test_replay_detects_drift(self):
        before = self._build()
        react.react(self.home, context=before, now=NOW)
        row = self.home.read_jsonl("logs/turns.jsonl")[-1]

        # 她自己的生活变了，重建出来的 life 块就该变 —— 这正是"漂移"该被发现的地方
        life.record(self.home, "下午把耳机拆了又装回去", kind="make", at=NOW)
        replayed = self._build(datetime.fromisoformat(row["context"]["now"]))
        old = {b["name"]: b["hash"] for b in row["context"]["blocks"]}
        new = {b["name"]: b["hash"] for b in replayed["blocks"]}
        self.assertNotEqual(old["life"], new["life"])
        for name in old:
            if name != "life":
                self.assertEqual(old[name], new[name], f"{name} 不该变")

    def test_react_without_context_still_works(self):
        react.react(self.home, said="嗯", now=NOW)
        row = self.home.read_jsonl("logs/turns.jsonl")[-1]
        self.assertEqual(row["context"], {})


if __name__ == "__main__":
    unittest.main()
