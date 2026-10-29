"""宿主 shell 钩子的 consent（allowlist）机制。

2026-10-29 实测踩过的坑：
config.yaml 里明明写了 `post_llm_call: - command: .../companion_post.sh`，
hermes 却**静默跳过**这个钩子 —— 不报错、不进日志、钩子就是不跑。
原因是 hermes 对 shell 钩子有 consent 机制：每个 (事件, 命令) 对还得在
`shell-hooks-allowlist.json` 里被批准过。

这个测试锁住三件事：
1. allowlist 的读写、幂等、缺失检测都对
2. 安装器一定会处理 allowlist —— 否则照文档装的人永远踩这个坑
3. `companion doctor` 能把这类静默失败报出来
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from companion import hosthooks as al  # noqa: E402

ALLOWLIST_SRC = REPO / "hermes" / "allowlist_hooks.py"
INSTALLER = REPO / "hermes" / "install_hermes.sh"


class AllowlistBasicsTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())

    def test_missing_file_is_not_an_error(self):
        """第一次装的时候这个文件根本不存在 —— 不能因此报错退出。"""
        r = al.check(self.home)
        self.assertTrue(r["ok"])          # 没有 config，也没有钩子，不算缺
        self.assertEqual(r["missing"], [])

    def test_approve_then_check_passes(self):
        r = al.approve(self.home, "post_llm_call", "/x/companion_post.sh")
        self.assertTrue(r["ok"])
        self.assertTrue(r["added"])
        self.assertIn(("post_llm_call", "/x/companion_post.sh"),
                      al.approved_pairs(al.load(al.allowlist_path(self.home))))

    def test_approve_is_idempotent(self):
        al.approve(self.home, "post_llm_call", "/x/companion_post.sh")
        before = al.allowlist_path(self.home).read_text(encoding="utf-8")
        r2 = al.approve(self.home, "post_llm_call", "/x/companion_post.sh")
        self.assertFalse(r2["added"])
        self.assertEqual(before, al.allowlist_path(self.home).read_text(encoding="utf-8"))

    def test_check_finds_unapproved_hook(self):
        """这就是静默失败的现场：config 里有，allowlist 里没有。"""
        (self.home / "config.yaml").write_text(
            "hooks:\n"
            "  post_llm_call:\n"
            "    - command: \"/x/companion_post.sh\"\n"
            "      timeout: 10\n",
            encoding="utf-8")
        r = al.check(self.home)
        self.assertFalse(r["ok"])
        self.assertEqual(r["missing"], [("post_llm_call", "/x/companion_post.sh")])

    def test_check_passes_after_approve(self):
        (self.home / "config.yaml").write_text(
            "hooks:\n"
            "  post_llm_call:\n"
            "    - command: \"/x/companion_post.sh\"\n",
            encoding="utf-8")
        al.approve(self.home, "post_llm_call", "/x/companion_post.sh")
        r = al.check(self.home)
        self.assertTrue(r["ok"], r["missing"])
        self.assertEqual(r["total"], 1)

    def test_approve_backs_up_existing_table(self):
        al.approve(self.home, "pre_llm_call", "/x/h.sh")
        al.approve(self.home, "post_llm_call", "/x/p.sh")
        baks = list(self.home.glob(ALLOWLIST_BACKUP_GLOB))
        self.assertTrue(baks, "第二次批准前应该留了备份")


ALLOWLIST_BACKUP_GLOB = "shell-hooks-allowlist.json.bak-companion"


class ConfigParsingTest(unittest.TestCase):
    """没装 pyyaml 的时候也得能查 —— 这个检查是静默失败的最后一道防线。"""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())

    def test_parses_without_pyyaml(self):
        (self.home / "config.yaml").write_text(
            "hooks:\n"
            "  pre_llm_call:\n"
            "    - command: \"/x/h.sh\"\n"
            "      timeout: 10\n"
            "  post_tool_call:\n"
            "    - matcher: \"napcat_send_message\"\n"
            "      command: \"/x/said.py hook-post\"\n"
            "      timeout: 10\n"
            "model: something\n",
            encoding="utf-8")
        pairs = al.configured_hooks(self.home)
        self.assertIn(("pre_llm_call", "/x/h.sh"), pairs)
        self.assertIn(("post_tool_call", "/x/said.py hook-post"), pairs)

    def test_parse_hooks_block_with_parsed_yaml(self):
        """已经有 yaml 对象时的解析路径 —— 与文本兜底结果应当一致。"""
        block = {"pre_llm_call": [{"command": "/x/h.sh", "timeout": 10}],
                 "post_llm_call": [{"command": "/x/p.sh"}]}
        pairs = al.parse_hooks_block(block)
        self.assertEqual(sorted(pairs),
                         [("post_llm_call", "/x/p.sh"), ("pre_llm_call", "/x/h.sh")])

    def test_parse_hooks_block_ignores_garbage(self):
        self.assertEqual(al.parse_hooks_block(None), [])
        self.assertEqual(al.parse_hooks_block("not a dict"), [])
        self.assertEqual(al.parse_hooks_block({"e": "not a list"}), [])
        self.assertEqual(al.parse_hooks_block({"e": [{"no_command": 1}]}), [])

    def test_corrupt_allowlist_does_not_crash(self):
        al.allowlist_path(self.home).write_text("{ not json", encoding="utf-8")
        r = al.check(self.home)          # 不该抛异常
        self.assertIsInstance(r, dict)

    def test_hooks_outside_hooks_block_are_ignored(self):
        (self.home / "config.yaml").write_text(
            "somewhere:\n"
            "  post_llm_call:\n"
            "    - command: \"/x/not-a-hook.sh\"\n",
            encoding="utf-8")
        self.assertEqual(al.configured_hooks(self.home), [])

    def test_broken_config_falls_back_to_text_parser(self):
        """config 坏掉时不能整体崩 —— 退回文本解析，至少还能报出钩子。"""
        (self.home / "config.yaml").write_text(
            "hooks:\n"
            "  post_llm_call:\n"
            "    - command: \"/x/p.sh\"\n"
            "  bad: [unclosed\n",
            encoding="utf-8")
        pairs = al.configured_hooks(self.home)
        self.assertIn(("post_llm_call", "/x/p.sh"), pairs)


class ScriptMetadataTest(unittest.TestCase):
    """批准记录里带脚本 mtime —— 宿主用它检测「批准之后脚本被换过」。"""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())

    def test_command_path_finds_existing_script(self):
        script = self.home / "companion_post.sh"
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        self.assertEqual(al.command_path(f"/bin/sh {script}"), script)

    def test_command_path_ignores_missing_paths(self):
        self.assertIsNone(al.command_path("/nope/does-not-exist.sh"))
        self.assertIsNone(al.command_path(""))

    def test_approval_records_script_mtime(self):
        script = self.home / "companion_post.sh"
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        al.approve(self.home, "post_llm_call", str(script))
        entry = al.load(al.allowlist_path(self.home))["approvals"][0]
        self.assertIsNotNone(entry.get("script_mtime_at_approval"))
        self.assertTrue(entry.get("approved_at"))

    def test_approval_without_script_still_records(self):
        r = al.approve(self.home, "post_llm_call", "/nope/missing.sh")
        self.assertTrue(r["ok"])
        entry = al.load(al.allowlist_path(self.home))["approvals"][0]
        self.assertIsNone(entry.get("script_mtime_at_approval"))


class DoctorReportsHooksTest(unittest.TestCase):
    """doctor 必须能把"配了但没批准"报出来 —— 静默失败最怕没人查。"""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.env = {**os.environ,
                    "PYTHONPATH": str(REPO / "src"),
                    "COMPANION_HOME": str(self.home / "store"),
                    "HERMES_HOME": str(self.home / "hermes")}
        (self.home / "hermes").mkdir(parents=True, exist_ok=True)

    def _doctor(self):
        done = subprocess.run([sys.executable, "-m", "companion", "--json", "doctor"],
                              capture_output=True, text=True, timeout=120, env=self.env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return json.loads(done.stdout)

    def test_doctor_lists_host_hook_state(self):
        d = self._doctor()
        hosts = d.get("host_hooks")
        self.assertIsInstance(hosts, list)
        self.assertTrue(hosts, "HERMES_HOME 存在就该有钩子检查项")
        self.assertEqual(hosts[0]["host"], "hermes")

    def test_doctor_reports_missing_approval(self):
        (self.home / "hermes" / "config.yaml").write_text(
            "hooks:\n"
            "  post_llm_call:\n"
            "    - command: \"/x/companion_post.sh\"\n",
            encoding="utf-8")
        d = self._doctor()
        h = d["host_hooks"][0]
        self.assertFalse(h["ok"])
        self.assertEqual(h["missing"][0]["event"], "post_llm_call")

    def test_doctor_ok_when_approved(self):
        (self.home / "hermes" / "config.yaml").write_text(
            "hooks:\n"
            "  post_llm_call:\n"
            "    - command: \"/x/companion_post.sh\"\n",
            encoding="utf-8")
        al.approve(self.home / "hermes", "post_llm_call", "/x/companion_post.sh")
        d = self._doctor()
        self.assertTrue(d["host_hooks"][0]["ok"])


class InstallerHandlesAllowlistTest(unittest.TestCase):
    """安装器必须处理 allowlist —— 否则照文档装的人永远踩这个坑。"""

    def setUp(self):
        self.text = INSTALLER.read_text(encoding="utf-8")

    def test_installer_mentions_allowlist(self):
        self.assertIn("allowlist", self.text)

    def test_installer_approves_both_hooks(self):
        for event in ("pre_llm_call", "post_llm_call"):
            self.assertIn(event, self.text)
        self.assertIn("allowlist_hooks.py", self.text)

    def test_installer_runs_check_after_installing(self):
        """装完必须自检：还差批准就大声报出来，别让它悄悄不跑。"""
        self.assertIn("check", self.text)

    def test_installer_ships_the_script(self):
        self.assertTrue(ALLOWLIST_SRC.exists(), "allowlist_hooks.py 必须一起发布")

    def test_script_is_syntactically_valid(self):
        done = subprocess.run([sys.executable, "-m", "py_compile", str(ALLOWLIST_SRC)],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())

    def _run(self, *args):
        done = subprocess.run(
            [sys.executable, str(ALLOWLIST_SRC), *args, "--home", str(self.home)],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "PYTHONPATH": str(REPO / "src")})
        return done

    def test_cli_add_then_check(self):
        done = self._run("add", "--event", "post_llm_call", "--command", "/x/p.sh")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(json.loads(done.stdout)["added"])

        done2 = self._run("check")
        self.assertEqual(done2.returncode, 0, done2.stdout + done2.stderr)

    def test_cli_check_exits_nonzero_when_missing(self):
        (self.home / "config.yaml").write_text(
            "hooks:\n  post_llm_call:\n    - command: \"/x/p.sh\"\n", encoding="utf-8")
        done = self._run("check")
        self.assertEqual(done.returncode, 1)
        self.assertIn("没批准", done.stdout)

    def test_cli_list_shows_approved(self):
        self._run("add", "--event", "pre_llm_call", "--command", "/x/h.sh")
        done = self._run("list")
        self.assertEqual(done.returncode, 0)
        self.assertIn("pre_llm_call", done.stdout)


if __name__ == "__main__":
    unittest.main()
