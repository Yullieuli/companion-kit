"""hermes 真实对话之后修的东西。

这一组对应的真实事故（2026-10-29）：主人跟她说了一整轮对话，
她六次回复全部在分析"你为什么没动力"，从没回答"做完有啥用"。
查下来是三件事叠在一起：

  1. **写回断了** —— `turns.jsonl` 0 字节。她说过的话一个字没落账，
     于是每轮都在失忆。根因：写回挂在 `pre_llm_call`（生成**之前**），
     只能靠 `conversation_history` 猜上一句；纯文本回复不走工具，
     `post_tool_call` 也不触发。修法：新增 `post_llm_call` 通道。
  2. **上下文全是噪音** —— 注入里 opinion 占 443 字、life 占 553 字，
     全是"我刷了六个显卡评测"。他问"做这个有啥用"，她拿这堆碎事硬答。
  3. **她不知道他是谁** —— 人物卡从来没建过。

这里守的就是这三条。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from companion import context as ctx_mod, life, opinion, people, react  # noqa: E402
from companion.store import Home  # noqa: E402

HERMES = ROOT / "hermes"


def _load_post():
    """直接 import 适配层里的 _text_of —— 它得能吃下 hermes 给的各種形态。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "companion_post_under_test", HERMES / "companion_post.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class AssistantResponseShapeTest(unittest.TestCase):
    """`assistant_response` 的形态处理。

    hermes 的 `final_response` 不是固定类型：可能是 str、可能是一段 content、
    可能是 Responses API 的 parts 列表（`{"type":"output_text","text":...}`）。
    处理不对的后果是**把正文记成空**（她说了话但账本里是空的），
    或者**把图片的 base64 当正文记进去**（账本里一堆乱码）。

    这里的形态表照抄 hermes 自己的 `agent/message_content.py`，
    免得两边口径不一致。
    """

    def setUp(self):
        self.mod = _load_post()

    def test_plain_string(self):
        self.assertEqual(self.mod._text_of("你好"), "你好")

    def test_dict_with_content(self):
        self.assertEqual(self.mod._text_of({"content": "你好"}), "你好")

    def test_dict_with_text(self):
        self.assertEqual(self.mod._text_of({"text": "你好"}), "你好")

    def test_responses_api_output_text(self):
        """Responses API 给的是 output_text —— 早先的实现会把它丢掉。"""
        self.assertEqual(
            self.mod._text_of({"type": "output_text", "text": "你好"}), "你好")

    def test_input_text_and_summary_text(self):
        self.assertEqual(self.mod._text_of({"input_text": "甲"}), "甲")
        self.assertEqual(self.mod._text_of({"summary_text": "乙"}), "乙")

    def test_parts_list(self):
        raw = [{"type": "output_text", "text": "第一段"},
               {"type": "output_text", "text": "第二段"}]
        self.assertIn("第一段", self.mod._text_of(raw))
        self.assertIn("第二段", self.mod._text_of(raw))

    def test_image_part_is_excluded(self):
        """图片 part 必须排除 —— base64 进账本就是垃圾。"""
        raw = [{"type": "output_text", "text": "看这张图"},
               {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]
        text = self.mod._text_of(raw)
        self.assertIn("看这张图", text)
        self.assertNotIn("base64", text)

    def test_audio_part_is_excluded(self):
        raw = [{"type": "text", "text": "听到了"},
               {"type": "input_audio", "input_audio": {"data": "AAAA"}}]
        text = self.mod._text_of(raw)
        self.assertIn("听到了", text)
        self.assertNotIn("AAAA", text)

    def test_empty_and_none(self):
        self.assertEqual(self.mod._text_of(None), "")
        self.assertEqual(self.mod._text_of([]), "")
        self.assertEqual(self.mod._text_of(""), "")


class PostHookTest(unittest.TestCase):
    """post_llm_call 写回通道。"""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.home.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "COMPANION_HOME": str(self.home),
               "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
        subprocess.run([sys.executable, "-m", "companion", "init"],
                       capture_output=True, env=env, timeout=60)

    def tearDown(self):
        self.tmp.cleanup()

    def _run_post(self, payload: dict) -> str:
        env = {**os.environ, "COMPANION_HOME": str(self.home),
               "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
        done = subprocess.run(
            [sys.executable, str(HERMES / "companion_post.py")],
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            capture_output=True, env=env, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace")[:400])
        return done.stdout.decode("utf-8", "replace")

    def _turns(self) -> list[dict]:
        p = self.home / "logs" / "turns.jsonl"
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]

    def test_writes_her_reply(self):
        """她这一轮说的话必须落进 turns.jsonl —— 这是"不失忆"的底线。"""
        self._run_post({
            "session_id": "t1", "platform": "napcat", "turn_id": "x1",
            "assistant_response": "笨蛋仆从，刚才显卡那事我记着呢。",
            "user_message": "在吗",
        })
        rows = self._turns()
        self.assertEqual(len(rows), 1, f"没落账：{rows}")
        self.assertIn("笨蛋仆从", rows[0]["said"])

    def test_creates_owner_card(self):
        """主人卡片要自动建 —— 她得知道在跟谁说话。"""
        self._run_post({
            "session_id": "t1", "platform": "napcat", "turn_id": "x1",
            "assistant_response": "在呢。",
        })
        p = self.home / "people" / "owner.json"
        self.assertTrue(p.exists(), "人物卡没建")
        card = json.loads(p.read_text(encoding="utf-8"))
        self.assertTrue(card.get("last_seen"), "没记下他刚来过")

    def test_idempotent_same_turn(self):
        """同一轮重复触发不能重复记账（重试、压缩重建都会重复触发）。"""
        payload = {"session_id": "t1", "platform": "napcat", "turn_id": "x1",
                   "assistant_response": "在呢。"}
        self._run_post(payload)
        self._run_post(payload)
        self._run_post(payload)
        self.assertEqual(len(self._turns()), 1, "同一轮记了多遍")

    def test_different_turn_both_recorded(self):
        self._run_post({"session_id": "t1", "platform": "napcat", "turn_id": "x1",
                        "assistant_response": "第一句。"})
        self._run_post({"session_id": "t1", "platform": "napcat", "turn_id": "x2",
                        "assistant_response": "第二句。"})
        rows = self._turns()
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["said"] for r in rows], ["第一句。", "第二句。"])

    def test_cron_session_ignored(self):
        """cron / 子会话不是跟主人说的话，不该写。"""
        self._run_post({"session_id": "local-abc", "platform": "napcat",
                        "turn_id": "x1", "assistant_response": "自言自语。"})
        self.assertEqual(self._turns(), [])

    def test_empty_response_ignored(self):
        self._run_post({"session_id": "t1", "platform": "napcat",
                        "turn_id": "x1", "assistant_response": ""})
        self._run_post({"session_id": "t1", "platform": "napcat",
                        "turn_id": "x2", "assistant_response": "[SILENT]"})
        self.assertEqual(self._turns(), [])

    def test_parts_list_response(self):
        """assistant_response 可能是 parts 列表，不能漏。"""
        self._run_post({
            "session_id": "t1", "platform": "napcat", "turn_id": "x1",
            "assistant_response": [{"type": "text", "text": "分条的一条。"}],
        })
        rows = self._turns()
        self.assertEqual(len(rows), 1)
        self.assertIn("分条的一条", rows[0]["said"])


class NoiseFilterTest(unittest.TestCase):
    """上下文不该被她的私事与未定型的猜测占满。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()

    def tearDown(self):
        self.tmp.cleanup()

    def test_life_keeps_her_own_memories(self):
        """她做过的事**全部**都要记得 —— 包括还没跟他讲过的。

        2026-10-29 的反复：这里曾经改成只给 shared=True（"没跟他说的私事不该
        占上下文"），结果主人问她的时候，她根本不知道自己刷过显卡评测。
        把"防重复讲"错做成了"不许知道"，方向反了。

        正确语义：shared 只决定**标注**，不决定去留。
        没讲过的标"可以聊"，讲过的标"已跟他说过"（防重复）。
        """
        self.home.append_jsonl("life/life.jsonl", {
            "at": "2026-10-29T10:00:00", "epoch": 1793260000,
            "kind": "bili", "title": "看了显卡评测", "shared": True, "detail": "",
        })
        self.home.append_jsonl("life/life.jsonl", {
            "at": "2026-10-29T11:00:00", "epoch": 1793263600,
            "kind": "bili", "title": "刷了抖音", "shared": False, "detail": "",
        })
        text = life.render(self.home)
        self.assertIn("看了显卡评测", text)
        self.assertIn("刷了抖音", text, "她做过的事她必须知道")
        self.assertIn("已跟他说过", text)
        self.assertIn("可以聊", text)

    def test_unshared_comes_first(self):
        """没讲过的排前面 —— 她更该想起"还有什么没说"，而不是翻旧账。"""
        self.home.append_jsonl("life/life.jsonl", {
            "at": "2026-10-29T10:00:00", "epoch": 1793260000,
            "kind": "bili", "title": "讲过的旧事", "shared": True, "detail": "",
        })
        self.home.append_jsonl("life/life.jsonl", {
            "at": "2026-10-29T11:00:00", "epoch": 1793263600,
            "kind": "bili", "title": "还没说的新事", "shared": False, "detail": "",
        })
        text = life.render(self.home)
        self.assertLess(text.index("还没说的新事"), text.index("讲过的旧事"))

    def test_opinion_settled_only(self):
        """hunch 是"还没想定"的猜测，不该冒充她的立场。"""
        from companion import opinion as op

        op.add(self.home, "这是一条还没定型的猜测", tags=["测试"], confidence=0.3)
        both = op.render(self.home)
        self.assertIn("还没想定", both)
        settled = op.render(self.home, settled_only=True)
        self.assertNotIn("还没想定", settled, "没定型的猜测不该进上下文")

    def test_context_shows_her_own_life(self):
        """core 的默认拼装（CLI/MCP/HTTP/钩子都走这里）必须带上她自己的生活。

        她做过的事她得知道 —— 哪怕还没跟他讲过。那条是"可以聊的素材"，
        不是要藏起来的隐私（2026-10-29 修过一次方向错误）。
        """
        from datetime import datetime as _dt

        at = "2026-10-29T11:00:00"
        self.home.append_jsonl("life/life.jsonl", {
            "at": at, "epoch": 1793263600, "id": life._id(_dt.fromisoformat(at), "刷了抖音"),
            "kind": "bili", "title": "刷了抖音", "shared": False, "detail": "",
        })
        text = ctx_mod.compose(self.home).text
        self.assertIn("刷了抖音", text, "她自己做过的事必须在上下文里")
        self.assertIn("可以聊", text, "没讲过的事要标出来，她才知道能提")

    def test_context_hides_unsettled_opinions(self):
        """但"还没想定"的猜测不该冒充她的立场。"""
        from companion import opinion as op

        op.add(self.home, "这个游戏应该不错吧", tags=["测试"], confidence=0.3)
        text = ctx_mod.compose(self.home).text
        self.assertNotIn("这个游戏应该不错吧", text)


class OwnerCardTest(unittest.TestCase):
    """给了 person 就该建卡，哪怕一条事实都没有。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()

    def tearDown(self):
        self.tmp.cleanup()

    def test_react_creates_card_without_facts(self):
        react.react(self.home, said="在呢。", person="owner")
        p = people.get(self.home, "owner")
        self.assertIsNotNone(p, "给了 person 却没有建卡")
        self.assertTrue(p.last_seen)

    def test_react_learns_facts(self):
        react.react(self.home, said="在呢。", person="owner", facts=["他最近在开发一个插件"])
        p = people.get(self.home, "owner")
        self.assertIn("他最近在开发一个插件", p.facts)

    def test_card_shows_up_in_context(self):
        """建了卡，上下文里就该有他 —— 她要知道在跟谁说话。"""
        react.react(self.home, said="在呢。", person="owner")
        text = ctx_mod.compose(self.home).text
        self.assertIn("owner", text)


if __name__ == "__main__":
    unittest.main()
