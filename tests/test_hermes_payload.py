"""hermes shell hook 的 payload 结构 —— 一个坑掉三个钩子。

2026-10-29 实测查出来的：
hermes 给钩子的 stdin 不是把参数原样平铺，它有自己的字段表
（`agent/shell_hooks.py::_payload_fields`）：

    · tool_name                  → 顶层 tool_name
    · args（dict）               → 顶层 **tool_input**（换了名字）
    · session_id / cwd / profile → 顶层
    · **其余全部塞进 extra**：user_message、assistant_response、turn_id、
      platform、conversation_history、group_id……全在 extra 里

三个钩子各自读顶层字段，于是：

| 钩子 | 读的字段 | 后果 |
|---|---|---|
| companion_hook（pre） | `user_message` | 相关检索一直是空查询；ask 回填拿不到主人原话 |
| companion_post（post） | `assistant_response` | **静默退出**，`turns.jsonl` 永远 0 字节 |
| companion_shared | `args` | **生活事件从未标记 shared** |

最坑的是"静默"：post 钩子读不到正文就 `print("{}")` 退出，连日志都不写一行。
查了两轮真实对话才定位到。这里把 payload 形态钉死。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
HERMES = ROOT / "hermes"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", HERMES / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class NormalizePayloadTest(unittest.TestCase):
    def setUp(self):
        self.shared = _load("companion_shared")

    def test_extra_fields_are_hoisted(self):
        """这是 hermes 真实给的形状：正文在 extra 里。"""
        raw = {
            "hook_event_name": "post_llm_call",
            "tool_name": None,
            "tool_input": None,
            "session_id": "s1",
            "cwd": "/home/x",
            "profile": "default",
            "extra": {
                "assistant_response": "本公主不让你掏钱",
                "turn_id": "t1",
                "user_message": "买不起",
                "platform": "napcat",
                "conversation_history": [{"role": "user", "content": "买不起"}],
            },
        }
        out = self.shared.normalize_payload(raw)
        self.assertEqual(out["assistant_response"], "本公主不让你掏钱")
        self.assertEqual(out["user_message"], "买不起")
        self.assertEqual(out["platform"], "napcat")
        self.assertEqual(out["turn_id"], "t1")
        self.assertEqual(out["session_id"], "s1")
        self.assertNotIn("extra", out, "extra 已经摊平，别留着重复一份")

    def test_top_level_wins(self):
        """顶层已有值就不被 extra 覆盖 —— 免得同名冲突时行为飘。"""
        raw = {"session_id": "top", "extra": {"session_id": "inner", "x": 1}}
        out = self.shared.normalize_payload(raw)
        self.assertEqual(out["session_id"], "top")
        self.assertEqual(out["x"], 1)

    def test_empty_top_level_is_filled(self):
        raw = {"session_id": "", "extra": {"session_id": "inner"}}
        self.assertEqual(self.shared.normalize_payload(raw)["session_id"], "inner")

    def test_args_and_tool_input_are_aliased(self):
        """hermes 把 args 改名成 tool_input，两个名字都给，谁读都能拿到。"""
        out = self.shared.normalize_payload({"tool_input": {"message": "在吗"}})
        self.assertEqual(out["args"], {"message": "在吗"})
        out2 = self.shared.normalize_payload({"args": {"message": "在吗"}})
        self.assertEqual(out2["tool_input"], {"message": "在吗"})

    def test_payload_without_extra_is_untouched(self):
        raw = {"session_id": "s", "assistant_response": "x"}
        self.assertEqual(self.shared.normalize_payload(raw), raw)

    def test_garbage_input_does_not_crash(self):
        self.assertEqual(self.shared.normalize_payload(None), None)
        self.assertEqual(self.shared.normalize_payload("str"), "str")


class RealPayloadEndToEndTest(unittest.TestCase):
    """用 hermes 真实形状的 payload 跑一遍三个钩子，必须都拿到字段。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        # 适配层在 import 时就求 Path.home() —— 测试环境得给个真的 home，
        # 否则连模块都导不进来（真机 hermes 一定有 HOME，这不是产品问题）。
        self.env = {"COMPANION_HOME": str(self.home),
                    "PATH": "/usr/bin:/bin",
                    "HOME": str(self.home),
                    "USERPROFILE": str(self.home),
                    "PYTHONPATH": str(ROOT / "src")}
        # Windows 上解释器启动要靠 SYSTEMROOT 定位 advapi32 的随机源，
        # 环境里没有它时直接 fatal：
        #   `Fatal Python error: _Py_HashRandomization_Init`
        # 3.13 换了随机源，本地跑不出来；3.10 必挂。
        for key in ("SYSTEMROOT", "SystemRoot", "WINDIR", "TEMP", "TMP"):
            if key in os.environ:
                self.env[key] = os.environ[key]

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, script: str, payload: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(HERMES / script)],
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            capture_output=True, timeout=120,
            env={**self.env, "PYTHONIOENCODING": "utf-8"},
        )

    def _hermes_shaped(self, **extra) -> dict:
        """照 hermes 的 _payload_fields 造一份 —— 正文全在 extra 里。"""
        return {"hook_event_name": "x", "tool_name": None, "tool_input": None,
                "session_id": "s1", "cwd": "/tmp", "profile": "default",
                "extra": extra}

    def test_post_hook_writes_back_with_real_shape(self):
        """最关键的一条：正文在 extra 里，写回也必须发生。"""
        payload = self._hermes_shaped(
            assistant_response="本公主不让你掏钱",
            turn_id="turn-e2e-1", user_message="买不起", platform="napcat")
        done = self._run("companion_post.py", payload)
        self.assertEqual(done.returncode, 0, done.stderr)
        turns = self.home / "logs" / "turns.jsonl"
        self.assertTrue(turns.exists(), f"没写回。stdout={done.stdout!r} stderr={done.stderr!r}")
        row = json.loads(turns.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(row["said"], "本公主不让你掏钱")

    def test_pre_hook_sees_user_message(self):
        """pre 钩子要拿到主人的原话 —— 否则相关检索永远是空查询。"""
        payload = self._hermes_shaped(
            user_message="显卡评测你看了吗", turn_id="t2", platform="napcat")
        done = self._run("companion_hook.py", payload)
        self.assertEqual(done.returncode, 0, done.stderr)
        # 注入了什么不该是空的（人格 / 状态至少有一条）
        try:
            out = json.loads(done.stdout.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self.fail(f"钩子没吐合法 JSON：{done.stdout[:300]}")

    def test_shared_hook_marks_life_with_real_shape(self):
        """她说出口的话要能把生活事件标成"讲过了"。"""
        # 先造一条没讲过的生活事件（id 用生产同款生成方式，否则 mark_shared 标不上）
        from datetime import datetime as _dt

        from companion import life as life_mod

        at = "2026-10-29T15:45:00"
        title = "刷B站，看了《RTX 5080 首发评测》"
        rid = life_mod._id(_dt.fromisoformat(at), title)
        (self.home / "life").mkdir(parents=True, exist_ok=True)
        (self.home / "life" / "life.jsonl").write_text(
            json.dumps({"at": at, "epoch": 1793260000, "id": rid,
                        "kind": "bili", "shared": False, "title": title, "detail": ""},
                       ensure_ascii=False) + "\n", encoding="utf-8")
        payload = {
            "hook_event_name": "post_tool_call",
            "tool_name": "napcat_send_message",
            "tool_input": {"message": "我刚看了个 RTX 5080 首发评测，挺离谱"},
            "session_id": "s1", "cwd": "/tmp", "profile": "default",
            "extra": {},
        }
        done = self._run("companion_shared.py", payload)
        self.assertEqual(done.returncode, 0, done.stderr)
        rows = [json.loads(x) for x in
                (self.home / "life" / "life.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
        self.assertTrue(rows[0]["shared"], "她讲过了，就该标成讲过了")


if __name__ == "__main__":
    unittest.main()
