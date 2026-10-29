"""安装器给 hermes config.yaml 注入钩子的逻辑。

为什么单独测它：这段 Python 嵌在 shell 里，出错时症状是
"hermes 启动直接崩"（config 变成非法 YAML）或者
"钩子静默不生效"（条目插到了错误的段里）。

2026-10-29 实测踩过两个：
  · 手写替换把 "\n" 写成了字面量 → `post_llm_call:/n` → YAML 直接非法
  · `ensure_list` 用了一次性算出的 `end` 索引 → 第二条 hook 插错段，
    变成 pre_llm_call 的条目跑到 post_llm_call 段里

所以这里直接把安装器里那段 Python 抠出来跑，测它到底插在哪。
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "hermes" / "install_hermes.sh"

BASE_CONFIG = """hooks:
  post_tool_call:
    - matcher: "napcat_send_message"
      command: "/usr/bin/python3 said.py hook-post"
      timeout: 10
  pre_llm_call:
    - command: "/usr/bin/python3 said.py hook-pre"
      timeout: 10
"""

NO_HOOKS_CONFIG = """model: something
temperature: 0.7
"""


def _parse_hooks(text: str) -> dict[str, list[dict[str, str]]]:
    """最小 YAML 解析：只认 hooks 下的二级段与 command。

    为什么不用 pyyaml：容器（Dockerfile 的 test 阶段）没装它，
    而 CI 的三个平台都装 —— 那测试就会在容器里红、在别处绿，
    这种"看环境"的测试最没用。
    这里自己解析，行为在任何地方都一致。
    """
    out: dict[str, list[dict[str, str]]] = {}
    in_hooks = False
    section = ""
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if indent == 0:
            in_hooks = stripped == "hooks:"
            section = ""
            continue
        if not in_hooks:
            continue
        if indent == 2 and stripped.endswith(":"):
            section = stripped[:-1]
            out.setdefault(section, [])
            continue
        # 条目有两种写法：`- command: "x"` 和 `- matcher: "y"` + 下一行 command
        if indent == 4 and stripped.startswith("- "):
            key, _, val = stripped[2:].partition(":")
            val = val.strip().strip('"').strip("'")
            out.setdefault(section, []).append({key.strip(): val})
            continue
        if indent == 6 and ":" in stripped:
            key, _, val = stripped.partition(":")
            val = val.strip().strip('"').strip("'")
            if out.get(section):
                out[section][-1][key.strip()] = val
    return out


def _injector_source() -> str:
    """从安装器里抠出那段注入 Python。"""
    raw = INSTALLER.read_text(encoding="utf-8")
    m = re.search(r'"\$COMPANION_PY" - "\$CFG" .*?<<\'PY\'\n(.*?)\nPY\n', raw, re.S)
    assert m, "安装器里找不到注入段"
    return m.group(1)


class ConfigInjectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = Path(self.tmp.name) / "inject.py"
        self.src.write_text(_injector_source(), encoding="utf-8")
        self.cfg = Path(self.tmp.name) / "config.yaml"

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, base: str) -> dict:
        self.cfg.write_text(base, encoding="utf-8")
        done = subprocess.run(
            [sys.executable, str(self.src), str(self.cfg),
             "/tmp/companion_hook.sh", "/tmp/companion_post.sh"],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        text = self.cfg.read_text(encoding="utf-8")
        # 统一用自己的最小解析器：容器里没 pyyaml，
        # 用 yaml.safe_load 会让这个测试"看环境"（容器红、别处绿）。
        return {"hooks": _parse_hooks(text)}

    def test_adds_both_hooks_to_existing_hooks(self):
        d = self._run(BASE_CONFIG)
        hooks = d["hooks"]
        pre = [x["command"] for x in hooks["pre_llm_call"]]
        post = [x["command"] for x in hooks["post_llm_call"]]
        self.assertIn("/tmp/companion_hook.sh", pre)
        self.assertIn("/tmp/companion_post.sh", post)

    def test_hook_lands_in_the_right_section(self):
        """这正是 2026-10-29 踩的坑：第二条插到了别的段里。"""
        d = self._run(BASE_CONFIG)
        hooks = d["hooks"]
        pre_cmds = [x["command"] for x in hooks["pre_llm_call"]]
        post_cmds = [x["command"] for x in hooks["post_llm_call"]]
        self.assertNotIn("/tmp/companion_hook.sh", post_cmds,
                         "pre 的 hook 跑到了 post 段里")
        self.assertNotIn("/tmp/companion_post.sh", pre_cmds,
                         "post 的 hook 跑到了 pre 段里")

    def test_existing_entries_preserved(self):
        """不能把用户原有的钩子挤掉。"""
        d = self._run(BASE_CONFIG)
        post_tool = [x["command"] for x in d["hooks"]["post_tool_call"]]
        self.assertIn("/usr/bin/python3 said.py hook-post", post_tool)
        pre = [x["command"] for x in d["hooks"]["pre_llm_call"]]
        self.assertIn("/usr/bin/python3 said.py hook-pre", pre)

    def test_idempotent(self):
        """跑两次不会插出两条一样的。"""
        self._run(BASE_CONFIG)
        d = self._run(BASE_CONFIG)
        post = [x["command"] for x in d["hooks"]["post_llm_call"]]
        self.assertEqual(post.count("/tmp/companion_post.sh"), 1)

    def test_config_without_hooks_section(self):
        d = self._run(NO_HOOKS_CONFIG)
        hooks = d["hooks"]
        self.assertIn("/tmp/companion_hook.sh",
                      [x["command"] for x in hooks["pre_llm_call"]])
        self.assertIn("/tmp/companion_post.sh",
                      [x["command"] for x in hooks["post_llm_call"]])

    def test_yaml_stays_valid(self):
        """最要紧的一条：改坏 config 的话 hermes 启动直接崩。"""
        self._run(BASE_CONFIG)
        text = self.cfg.read_text(encoding="utf-8")
        self.assertNotIn(":/n", text, "换行被写成了字面量 /n")
        try:
            import yaml
            yaml.safe_load(text)
        except ImportError:
            self.skipTest("没装 pyyaml")


if __name__ == "__main__":
    unittest.main()
