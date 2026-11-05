from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from companion import relay  # noqa: E402


class RelayTest(unittest.TestCase):
    """文件必须先进 NapCat 共享目录，否则容器读不到、发出去是空的。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.share = Path(self.tmp.name) / "napcat-config"
        self.share.mkdir()
        self._old = os.environ.get("COMPANION_NAPCAT_SHARE")
        os.environ["COMPANION_NAPCAT_SHARE"] = str(self.share)
        self.src = Path(self.tmp.name) / "art" / "开心.png"
        self.src.parent.mkdir()
        self.src.write_bytes(b"\x89PNG fake")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("COMPANION_NAPCAT_SHARE", None)
        else:
            os.environ["COMPANION_NAPCAT_SHARE"] = self._old
        self.tmp.cleanup()

    def test_relay_copies_into_share_dir(self):
        out = relay.relay(self.src)
        self.assertIn("cq", out, out)
        self.assertEqual(out["path"], "/app/napcat/config/out/开心.png")
        self.assertEqual(out["cq"], "[CQ:image,file=/app/napcat/config/out/开心.png]")
        self.assertTrue((self.share / "out" / "开心.png").is_file())

    def test_relay_avoids_name_collision(self):
        a = relay.relay(self.src)
        b = relay.relay(self.src)
        self.assertNotEqual(a["path"], b["path"])
        self.assertTrue(b["path"].endswith("-1.png"))

    def test_relay_record_kind(self):
        wav = self.src.with_suffix(".wav")
        wav.write_bytes(b"RIFF")
        out = relay.relay(wav, kind="record")
        self.assertTrue(out["cq"].startswith("[CQ:record,"))

    def test_missing_file_reports_error(self):
        out = relay.relay(self.src.with_name("nope.png"))
        self.assertIn("error", out)
        self.assertNotIn("cq", out)

    def test_attach_only_touches_ok_results(self):
        r = {"ok": False, "error": "x"}
        self.assertIs(relay.attach(r), r)
        self.assertNotIn("cq", r)
        r2 = relay.attach({"ok": True, "path": str(self.src)})
        self.assertIn("cq", r2)
