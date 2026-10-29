"""hermes 发送前闸 + napcat 补丁的回归。

对应 WORK_PLAN「做了一半」清单第 1 条：
  hermes 的说话三道闸 —— 之前只有"注入提示"，拦不住已经生成的消息。

这里验两件事：
  1. 闸本身：改名被拦、同话题被拦、正常话放行；回复模式不吃额度闸
  2. 补丁：幂等（打两次只插一次）、能回滚、插在正确的位置
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from companion import guard  # noqa: E402
from companion.store import Home  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


gate_mod = _load("companion_gate", ROOT / "hermes" / "companion_gate.py")
patch_mod = _load("patch_napcat_guard", ROOT / "hermes" / "patch_napcat_guard.py")

NAPCAT_FIXTURE = '''\
class Adapter:
    async def send(self, chat_id: str, content: str = "", **kwargs) -> SendResult:
        """发送文本消息。"""
        if content:
            _去Markdown(content)
        return SendResult(success=True)
'''


class GateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()
        (Path(self.tmp.name) / "SOUL.md").write_text("# 我是谁\n\n我叫柚璃\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_rename_is_blocked(self):
        out = gate_mod.evaluate(self.home, "以后叫我小璃吧")
        self.assertFalse(out["ok"])
        self.assertEqual(out["blocked"], "identity")

    def test_normal_text_passes(self):
        out = gate_mod.evaluate(self.home, "今天路过那家 bakery 排了好长的队")
        self.assertTrue(out["ok"], out)

    def test_repeat_is_blocked(self):
        now = datetime.now()
        self.home.append_jsonl("logs/said.jsonl", {
            "at": now.isoformat(timespec="seconds"),
            "text": "窗外的桂花开了，好香", "source": "say",
        })
        out = gate_mod.evaluate(self.home, "窗外的桂花开了真的好香")
        self.assertFalse(out["ok"])
        self.assertEqual(out["blocked"], "repeat")

    def test_reply_mode_ignores_quota(self):
        """回复主人不占主动开口额度：刚说过话也不该被拦。"""
        from companion import quota as quota_mod

        st = quota_mod.default_state(datetime.now().date().isoformat())
        st["count"] = 8
        st["last_at"] = datetime.now().timestamp()
        self.home.save_json("quota.json", st)
        out = gate_mod.evaluate(self.home, "在吗", mode="reply")
        self.assertTrue(out["ok"], out)
        # 主动开口模式则会被额度拦下
        blocked = gate_mod.evaluate(self.home, "在吗", mode="active")
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["blocked"], "quota")

    def test_empty_is_blocked(self):
        out = gate_mod.evaluate(self.home, "   ")
        self.assertFalse(out["ok"])
        self.assertEqual(out["blocked"], "empty")

    def test_cli_protocol(self):
        """stdin JSON → stdout JSON，退出码 1 表示拦下。"""
        import io
        import subprocess

        script = ROOT / "hermes" / "companion_gate.py"
        env = {**dict(__import__("os").environ), "COMPANION_HOME": self.tmp.name,
               "PYTHONPATH": str(ROOT / "src")}
        p = subprocess.run([sys.executable, str(script)],
                           input=json.dumps({"text": "以后叫我小璃吧"}),
                           capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 1)
        out = json.loads(p.stdout)
        self.assertFalse(out["ok"])
        self.assertEqual(out["blocked"], "identity")

        p2 = subprocess.run([sys.executable, str(script)],
                            input=json.dumps({"text": "在吗"}),
                            capture_output=True, text=True, env=env)
        self.assertEqual(p2.returncode, 0)
        self.assertTrue(json.loads(p2.stdout)["ok"])


class AskTodoTest(unittest.TestCase):
    """请示单要有人呈现：主人得看见她在等什么。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()

    def tearDown(self):
        self.tmp.cleanup()

    def _hook(self, payload: dict) -> dict:
        import os
        import subprocess

        env = {**os.environ, "COMPANION_HOME": self.tmp.name,
               "PYTHONPATH": str(ROOT / "src")}
        p = subprocess.run([sys.executable, str(ROOT / "hermes" / "companion_hook.py")],
                           input=json.dumps(payload, ensure_ascii=False),
                           capture_output=True, text=True, env=env)
        out = p.stdout.strip()
        return json.loads(out) if out.startswith("{") else {}

    def test_pending_ask_is_shown_to_owner(self):
        from companion import ask

        ask.request(self.home, "open", {"what": "chrome"}, target="desktop")
        out = self._hook({"session_id": "t1", "platform": "napcat", "user_message": "在吗"})
        ctx = str(out.get("context") or "")
        self.assertIn("等你点头", ctx)
        self.assertIn("打开", ctx)

    def test_no_ask_no_block(self):
        out = self._hook({"session_id": "t2", "platform": "napcat", "user_message": "在吗"})
        self.assertNotIn("等你点头", str(out.get("context") or ""))


class NapcatPatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.target = Path(self.tmp.name) / "main.py"
        self.target.write_text(NAPCAT_FIXTURE, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_patch_is_idempotent(self):
        changed, why = patch_mod.apply_patch(self.target, "/tmp/gate.py", "python3")
        self.assertTrue(changed, why)
        text = self.target.read_text(encoding="utf-8")
        self.assertEqual(text.count(patch_mod.MARKER), 1)
        # 再打一次：不动
        changed2, why2 = patch_mod.apply_patch(self.target, "/tmp/gate.py", "python3")
        self.assertFalse(changed2)
        self.assertIn("已经打过补丁", why2)
        self.assertEqual(self.target.read_text(encoding="utf-8").count(patch_mod.MARKER), 1)

    def test_patch_lands_after_send_def(self):
        patch_mod.apply_patch(self.target, "/tmp/gate.py", "python3")
        lines = self.target.read_text(encoding="utf-8").splitlines()
        idx_def = next(i for i, l in enumerate(lines) if "async def send" in l)
        idx_gate = next(i for i, l in enumerate(lines) if patch_mod.MARKER in l)
        self.assertGreater(idx_gate, idx_def)
        # 闸必须在 return 之前
        self.assertLess(idx_gate, next(i for i, l in enumerate(lines) if "return SendResult" in l))

    def test_backup_and_revert(self):
        original = self.target.read_text(encoding="utf-8")
        patch_mod.apply_patch(self.target, "/tmp/gate.py", "python3")
        self.assertTrue(list(self.target.parent.glob("main.py.bak-gate-*")))
        ok, why = patch_mod.revert(self.target)
        self.assertTrue(ok, why)
        self.assertEqual(self.target.read_text(encoding="utf-8"), original)

    def test_missing_anchor_is_reported(self):
        self.target.write_text("class X:\n    pass\n", encoding="utf-8")
        changed, why = patch_mod.apply_patch(self.target, "/tmp/gate.py", "python3")
        self.assertFalse(changed)
        self.assertIn("找不到发送函数", why)


if __name__ == "__main__":
    unittest.main()
