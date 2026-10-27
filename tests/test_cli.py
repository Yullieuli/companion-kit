from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from companion.cli import main  # noqa: E402


def run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(argv)
    return code, buf.getvalue()


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_init_then_context(self):
        code, out = run(["--home", self.home, "--json", "init"])
        self.assertEqual(code, 0)
        self.assertIn("SOUL.md", out)

        code, out = run(["--home", self.home, "context", "--budget", "600"])
        self.assertEqual(code, 0)
        self.assertIn("现在", out)
        self.assertIn("心情", out)
        self.assertLessEqual(len(out), 700)

    def test_tick_emits_event(self):
        run(["--home", self.home, "init"])
        code, out = run(["--home", self.home, "tick"])
        self.assertEqual(code, 0)
        self.assertIn("动作：", out)

        code, out = run(["--home", self.home, "--json", "events"])
        self.assertEqual(code, 0)
        self.assertIn("cursor", out)

    def test_cost_empty(self):
        run(["--home", self.home, "init"])
        code, out = run(["--home", self.home, "cost"])
        self.assertEqual(code, 0)
        self.assertIn("今天 ¥0.000", out)

    def test_say_is_dry(self):
        code, out = run(["--home", self.home, "--json", "say", "--text", "在吗"])
        self.assertEqual(code, 0)
        self.assertIn('"dry_run": true', out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
