"""MCP 协议层：真的起一个子进程，走真实的 stdio。

为什么不只测 handle()：codex / openclaw 挂上来时，出错的地方几乎都在协议边缘——
编码、分帧、通知要不要回包、版本协商。这些在进程内单测里全被绕过。

其中编码那条是真踩过的：Windows 控制台把 stdio 设成 GBK 时，中文一写出去
客户端连 JSON 都解析不了。所以这里显式用 PYTHONIOENCODING=gbk 起进程。
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


class StdioTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, messages: list[dict], *, encoding: str = "gbk") -> tuple[list[dict], bytes, str]:
        env = {
            **os.environ,
            "COMPANION_HOME": self.tmp.name,
            "PYTHONPATH": str(ROOT / "src"),
            # 故意模拟 Windows 控制台；服务端必须自己转成 UTF-8
            "PYTHONIOENCODING": encoding,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        payload = "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages)
        done = subprocess.run([sys.executable, "-m", "companion", "mcp"],
                              input=payload.encode("utf-8"),
                              capture_output=True, env=env, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace")[:400])
        # 关键断言：无论宿主环境是什么编码，输出必须是合法 UTF-8
        text = done.stdout.decode("utf-8")
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        return rows, done.stdout, done.stderr.decode("utf-8", "replace")

    def test_handshake_and_tools(self):
        rows, _, _ = self._run([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "probe", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ])
        self.assertEqual(len(rows), 2, "通知不该回包")
        init = rows[0]["result"]
        self.assertEqual(init["protocolVersion"], "2025-06-18")
        self.assertEqual(init["serverInfo"]["name"], "companion-kit")
        self.assertNotEqual(init["serverInfo"]["version"], "0.1.0")
        tools = rows[1]["result"]["tools"]
        self.assertGreaterEqual(len(tools), 26)
        for need in ("companion_context", "companion_say", "companion_bili", "companion_ask"):
            self.assertIn(need, [t["name"] for t in tools])
        # 每个工具都要有安全注解：缺了客户端会为每次只读查询弹批准
        by_name = {t["name"]: t for t in tools}
        for t in tools:
            self.assertIn("annotations", t, t["name"])
        self.assertTrue(by_name["companion_context"]["annotations"]["readOnlyHint"])
        self.assertTrue(by_name["device_act"]["annotations"]["destructiveHint"])
        self.assertTrue(by_name["companion_bili"]["annotations"]["openWorldHint"])
        self.assertFalse(by_name["companion_say"]["annotations"]["readOnlyHint"])

    def test_unknown_protocol_falls_back(self):
        rows, _, _ = self._run([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "1999-01-01"}},
        ])
        self.assertEqual(rows[0]["result"]["protocolVersion"], "2024-11-05")

    def test_context_returns_utf8_json(self):
        rows, raw, _ = self._run([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "companion_context", "arguments": {"budget": 300}}},
        ])
        result = rows[0]["result"]
        self.assertFalse(result["isError"])
        payload = json.loads(result["content"][0]["text"])
        self.assertGreater(payload["total_chars"], 0)
        # 中文必须以 UTF-8 出现，而不是 GBK 字节
        self.assertIn("现在", raw.decode("utf-8"))

    def test_unknown_tool_is_protocol_error(self):
        rows, _, _ = self._run([
            {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
             "params": {"name": "没有这个工具", "arguments": {}}},
        ])
        self.assertEqual(rows[0]["error"]["code"], -32601)

    def test_bad_json_does_not_kill_the_server(self):
        env = {
            **os.environ,
            "COMPANION_HOME": self.tmp.name,
            "PYTHONPATH": str(ROOT / "src"),
            "PYTHONIOENCODING": "gbk",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        payload = "{这不是 JSON}\n" + json.dumps(
            {"jsonrpc": "2.0", "id": 2, "method": "ping"}, ensure_ascii=False) + "\n"
        done = subprocess.run([sys.executable, "-m", "companion", "mcp"],
                              input=payload.encode("utf-8"), capture_output=True,
                              env=env, timeout=30)
        rows = [json.loads(l) for l in done.stdout.decode("utf-8").splitlines() if l.strip()]
        self.assertEqual(rows[0]["error"]["code"], -32700)
        self.assertEqual(rows[1]["result"], {})


if __name__ == "__main__":
    unittest.main()
