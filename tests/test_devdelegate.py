"""devdelegate 的测试：WSL 里的 device 交给 Windows 跑。

这一层最容易出两类错，两类都在这里钉住：
  · 路径：`C:\\a\\b.png` 或 `/mnt/c/a/b.png` 混着用，最后谁都打不开
  · 常量：WSLENV 不写，Windows 那边的 python 就导入不到 companion，
    报出来是"没有这个模块"，看起来像装错了
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from companion import devdelegate as dd  # noqa: E402
from companion.store import Home  # noqa: E402

BS = chr(92)   # 反斜杠。写 chr 是为了不在源码里跟转义打架


class PathTest(unittest.TestCase):
    def test_win_to_wsl(self):
        self.assertEqual(dd.win_to_wsl("C:" + BS + "Users" + BS + "Anran" + BS + "a.exe"),
                         "/mnt/c/Users/Anran/a.exe")
        self.assertEqual(dd.win_to_wsl("D:" + BS + "x"), "/mnt/d/x")

    def test_win_to_wsl_leaves_posix_alone(self):
        self.assertEqual(dd.win_to_wsl("/usr/bin/python3"), "/usr/bin/python3")
        self.assertEqual(dd.win_to_wsl(""), "")

    def test_wsl_to_win(self):
        self.assertEqual(dd.wsl_to_win("/mnt/c/Users/Anran/.companion"),
                         "C:" + BS + "Users" + BS + "Anran" + BS + ".companion")

    def test_wsl_to_win_passthrough_when_not_mnt(self):
        self.assertEqual(dd.wsl_to_win("/home/a/.companion", "/home/a/.companion"),
                         "/home/a/.companion")

    def test_translate_normalizes_separators(self):
        pairs = [("C:" + BS + "X", "/mnt/c/X")]
        got = dd._translate({"shot": "C:" + BS + "X" + BS + "shots" + BS + "a.png"}, pairs)
        self.assertEqual(got["shot"], "/mnt/c/X/shots/a.png")

    def test_translate_walks_nested(self):
        pairs = [("C:" + BS + "X", "/mnt/c/X")]
        got = dd._translate({"a": ["C:" + BS + "X" + BS + "1.png", 3], "b": {"c": "ok"}}, pairs)
        self.assertEqual(got["a"][0], "/mnt/c/X/1.png")
        self.assertEqual(got["a"][1], 3)
        self.assertEqual(got["b"]["c"], "ok")


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()

    def tearDown(self):
        self.tmp.cleanup()

    def test_not_wanted_on_windows(self):
        self.home.save_json("devices.json", {"windows": {"python": "/usr/bin/python3", "src": "/x"}})
        with unittest.mock.patch.object(dd.sys, "platform", "win32"):
            self.assertFalse(dd.wanted(self.home))

    def test_not_wanted_without_config(self):
        self.home.save_json("devices.json", {})
        with unittest.mock.patch.object(dd, "in_wsl", return_value=True):
            self.assertFalse(dd.wanted(self.home))

    def test_not_wanted_when_python_missing(self):
        self.home.save_json("devices.json", {"windows": {"python": "/nope/python.exe", "src": "/x"}})
        with unittest.mock.patch.object(dd, "in_wsl", return_value=True), \
                unittest.mock.patch.object(dd.sys, "platform", "linux"):
            self.assertFalse(dd.wanted(self.home))

    def test_env_carries_wslenv(self):
        self.home.save_json("devices.json", {"windows": {
            "python": "C:" + BS + "py" + BS + "python.exe",
            "src": "C:" + BS + "src"}})
        env = dd._env(self.home, "C:" + BS + "src")
        self.assertIn("PYTHONPATH/p", env["WSLENV"])
        self.assertIn("COMPANION_HOME/p", env["WSLENV"])
        self.assertTrue(env["PYTHONPATH"].startswith("/mnt/c/"))   # 给 WSL 翻成 C: 用
        self.assertEqual(env["COMPANION_HOME"], str(self.home.root))


import unittest.mock  # noqa: E402  (放在末尾，避免顶部挡住常量)

if __name__ == "__main__":
    unittest.main()
