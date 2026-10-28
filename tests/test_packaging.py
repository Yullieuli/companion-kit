"""打包与安装脚本：版本一致、换行正确。

这两条都是踩过的真坑，而且失败方式很隐蔽：
  · 安装脚本里的版本号和代码版本各写一份 → 用户装到半年前的包
  · 工作区把 .sh 存成 CRLF → Linux 上 `sh` 报 `word unexpected (expecting "in")`
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from companion import __version__  # noqa: E402


class VersionDriftTest(unittest.TestCase):
    def test_install_sh_pins_current_version(self):
        raw = (ROOT / "install.sh").read_text(encoding="utf-8")
        m = re.search(r'VERSION="\$\{COMPANION_VERSION:-([0-9.]+)\}"', raw)
        self.assertIsNotNone(m, "install.sh 里找不到 VERSION 默认值")
        self.assertEqual(m.group(1), __version__,
                         "install.sh 的默认版本和代码不一致：用户会装到旧包")

    def test_install_ps1_pins_current_version(self):
        raw = (ROOT / "install.ps1").read_text(encoding="utf-8")
        m = re.search(r'"v([0-9.]+)"\s*\}', raw)
        self.assertIsNotNone(m, "install.ps1 里找不到版本默认值")
        self.assertEqual(m.group(1), __version__)

    def test_wheel_url_is_derived_not_hardcoded(self):
        raw = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn('/releases/download/v$VERSION/companion_kit-$VERSION-', raw)
        self.assertIsNone(re.search(r"companion_kit-\d+\.\d+\.\d+-py3", raw),
                          "whl 文件名被写死了，改版本时必然漏")


class LineEndingTest(unittest.TestCase):
    def test_shell_scripts_are_lf(self):
        for rel in ("install.sh", "hermes/install_hermes.sh", "hermes/companion.sh",
                    "hermes/companion_bili.sh"):
            p = ROOT / rel
            if not p.exists():
                continue
            with self.subTest(script=rel):
                self.assertNotIn(b"\r\n", p.read_bytes(),
                                 f"{rel} 含 CRLF，Linux 上跑不起来")


class PackageMetaTest(unittest.TestCase):
    def test_no_dead_package_data(self):
        raw = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        if "templates/*.md" in raw:
            self.assertTrue((ROOT / "src" / "companion" / "templates").exists(),
                            "声明了 package-data 但目录不存在")


if __name__ == "__main__":
    unittest.main()
