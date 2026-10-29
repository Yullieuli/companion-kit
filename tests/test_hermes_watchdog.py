"""hermes 看门狗的测试：判断卡住/死了，重启命令可控。"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from companion.store import Home
from companion import watchdog as wd_mod

WATCHDOG = ROOT / "hermes" / "companion_watchdog.py"


def _load_watchdog_module():
    spec = importlib.util.spec_from_file_location("_hermes_watchdog", WATCHDOG)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class HermessWatchdogTest(unittest.TestCase):
    """hermes 侧的看门狗脚本。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.mod = _load_watchdog_module()

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *args, env_extra=None):
        env = {**os.environ, "COMPANION_HOME": self.home,
               "PYTHONPATH": str(ROOT / "src")}
        env.update(env_extra or {})
        proc = subprocess.run([sys.executable, str(WATCHDOG), *args],
                              capture_output=True, text=True, env=env, timeout=60)
        return proc

    def test_never_beat_is_reported(self):
        out = self._run("--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        data = json.loads(out.stdout)
        self.assertEqual(data["state"], "never")
        self.assertIn("pending_turns", data)

    def test_healthy_reports_alive(self):
        h = Home.at(self.home)
        h.init()
        wd_mod.beat(h, host="hermes")
        out = self._run("--json")
        data = json.loads(out.stdout)
        self.assertEqual(data["state"], "alive", data)
        self.assertTrue(data["ok"])

    def test_dead_host_exits_nonzero_with_exit_code(self):
        """给 cron/monitoring 用：有问题时退出码非 0。"""
        h = Home.at(self.home)
        h.init()
        wd_mod.beat(h, host="hermes")
        hb = h.read_json("logs/heartbeat.json")
        hb["pid"] = 999999
        h.save_json("logs/heartbeat.json", hb)
        out = self._run("--exit-code")
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("dead", out.stdout)

    def test_healthy_exits_zero(self):
        h = Home.at(self.home)
        h.init()
        wd_mod.beat(h, host="hermes")
        out = self._run("--exit-code")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_fix_without_restart_cmd_only_reports(self):
        """没配重启命令时只报不改——重启宿主是破坏性操作。"""
        h = Home.at(self.home)
        h.init()
        wd_mod.beat(h, host="hermes")
        hb = h.read_json("logs/heartbeat.json")
        hb["pid"] = 999999
        h.save_json("logs/heartbeat.json", hb)
        out = self._run("--fix", "--json", env_extra={"COMPANION_RESTART_CMD": ""})
        data = json.loads(out.stdout)
        self.assertIn("没配 COMPANION_RESTART_CMD", data.get("fix", ""))

    def test_fix_with_restart_cmd_runs_it(self):
        """配了重启命令就真跑（用一个安全的命令验证它被执行了）。"""
        h = Home.at(self.home)
        h.init()
        wd_mod.beat(h, host="hermes")
        hb = h.read_json("logs/heartbeat.json")
        hb["pid"] = 999999
        h.save_json("logs/heartbeat.json", hb)
        marker = Path(self.home) / "restarted.txt"
        out = self._run("--fix", "--json",
                        env_extra={"COMPANION_RESTART_CMD": f"touch {marker}"})
        data = json.loads(out.stdout)
        self.assertTrue(marker.exists(), "重启命令应该被执行了")
        self.assertIn("已执行重启", data.get("fix", ""))

    def test_pending_turns_reported(self):
        """待写回的轮次也要报出来——那是另一种"她没做完事"。"""
        from companion import resume as resume_mod

        h = Home.at(self.home)
        h.init()
        resume_mod.register(h, "说了但没写回", turn_id="t-x")
        out = self._run("--json")
        data = json.loads(out.stdout)
        self.assertEqual(data["pending_turns"], 1)
        # 人话模式里要给出"去哪看"的指引
        out2 = self._run()
        self.assertIn("companion resume", out2.stdout)


class HeartbeatIntegrationTest(unittest.TestCase):
    """宿主侧：hook / tick 会写心跳。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()

    def tearDown(self):
        self.tmp.cleanup()

    def test_tick_writes_heartbeat(self):
        from companion import drive

        drive.tick(self.home, host="dsh")
        hb = self.home.read_json("logs/heartbeat.json")
        self.assertEqual(hb["host"], "dsh")
        self.assertGreater(hb["pid"], 0)

    def test_heartbeats_jsonl_accumulates(self):
        from companion import drive

        drive.tick(self.home, host="dsh")
        drive.tick(self.home, host="hermes")
        rows = self.home.read_jsonl("logs/heartbeats.jsonl")
        self.assertGreaterEqual(len(rows), 2)
        hosts = {r["host"] for r in rows}
        self.assertIn("dsh", hosts)
        self.assertIn("hermes", hosts)


if __name__ == "__main__":
    unittest.main()
