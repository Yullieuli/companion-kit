from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from companion.store import Home  # noqa: E402


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_init_creates_expected_files(self):
        created = self.home.init()
        self.assertIn("SOUL.md", created)
        self.assertIn("mood/state.json", created)
        self.assertEqual(len(created), 18)
        for rel in ("SOUL.md", "MEMORY.md", "memory/index.md", "rhythm.json",
                    "mood/state.json", "opinions.jsonl", "quota.json", "logs/turns.jsonl",
                    "logs/cost.jsonl", "logs/initiatives.jsonl", "logs/events.jsonl",
                    "logs/said.jsonl", "logs/voice.jsonl", "logs/device.jsonl", "life/life.jsonl",
                    "logs/bili.jsonl", "bili/state.json", "bili/config.json"):
            self.assertTrue((self.home.root / rel).exists(), rel)

    def test_init_is_idempotent_and_never_overwrites(self):
        self.home.init()
        soul = self.home.root / "SOUL.md"
        soul.write_text("# 我是小鲸\n", encoding="utf-8")
        created = self.home.init()
        self.assertEqual(created, [])
        self.assertEqual(self.home.soul(), "# 我是小鲸\n")

    def test_corrupted_json_falls_back(self):
        self.home.init()
        (self.home.root / "rhythm.json").write_text("{not json", encoding="utf-8")
        rhythm = self.home.rhythm()
        self.assertIn("windows", rhythm)
        self.assertEqual(rhythm["windows"][0]["start"], "20:00")

    def test_save_json_is_atomic(self):
        home = Home.at(self.tmp.name)
        home.init()
        home.save_json("mood/state.json", {"axes": {"satisfaction": 5}})
        self.assertEqual(home.mood()["axes"]["satisfaction"], 5)
        self.assertFalse((home.root / "mood/state.json.tmp").exists())

    def test_jsonl_roundtrip_skips_bad_lines(self):
        home = Home.at(self.tmp.name)
        home.init()
        home.append_jsonl("logs/turns.jsonl", {"a": 1})
        with (home.root / "logs/turns.jsonl").open("a", encoding="utf-8") as fh:
            fh.write("坏行\n")
        home.append_jsonl("logs/turns.jsonl", {"a": 2})
        rows = home.read_jsonl("logs/turns.jsonl")
        self.assertEqual([r["a"] for r in rows], [1, 2])

    def test_env_home(self):
        import os

        old = os.environ.get("COMPANION_HOME")
        os.environ["COMPANION_HOME"] = self.tmp.name
        try:
            self.assertEqual(Home.at().root, Path(self.tmp.name))
        finally:
            if old is None:
                os.environ.pop("COMPANION_HOME", None)
            else:
                os.environ["COMPANION_HOME"] = old


if __name__ == "__main__":
    unittest.main(verbosity=2)
