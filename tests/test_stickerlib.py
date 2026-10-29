"""表情包的内置库与自动提取。

为什么要有这些测试：内置库是**程序画出来的**，画坏了不会报错，
只会变成一堆看不出情绪的图；而自动提取的判据（关键词 / 像素）
一旦偏了，主人发来的图会被收进错的情绪目录。

所以这里不只测"跑通"，还测：
  · 16 张内置表情都是**合法 PNG**，且能解码
  · 每个情绪目录都有货（空目录 = 那个情绪永远发不出去）
  · 关键词优先于像素（主人自己命名的最准）
  · 拒绝优先于同意
  · 同一张图不重复入库
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from companion import imgpng, sticker, stickerlib  # noqa: E402
from companion.store import Home  # noqa: E402


def _make_png(path: Path, w: int = 8, h: int = 8, rgb: tuple[int, int, int] = (255, 0, 0)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    imgpng.write_png(path, w, h, bytes(rgb) * (w * h))
    return path


class BuiltinLibraryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()

    def tearDown(self):
        self.tmp.cleanup()

    def test_every_builtin_is_valid_png(self):
        """画坏了不会报错，只会变成看不出情绪的图 —— 所以逐张解码验。"""
        d = Path(self.tmp.name)
        for mood, name in stickerlib.BUILTIN:
            with self.subTest(sticker=name):
                p = d / f"{name}.png"
                p.write_bytes(stickerlib.builtin_png(mood, name))
                w, h, rgb = imgpng.read_png(p)
                self.assertEqual((w, h), (96, 96))
                self.assertEqual(len(rgb), 96 * 96 * 3)

    def test_seed_fills_every_mood(self):
        """每个情绪都得有货 —— 空目录意味着那个情绪永远发不出去。"""
        out = stickerlib.seed_builtin(self.home)
        self.assertEqual(len(out["written"]), len(stickerlib.BUILTIN))
        st = sticker.stats(self.home)
        self.assertEqual(st["total"], len(stickerlib.BUILTIN))
        for mood in sticker.MOODS:
            with self.subTest(mood=mood):
                self.assertGreaterEqual(st["by_mood"].get(mood, 0), 1,
                                        f"{mood} 目录是空的")
                self.assertTrue(sticker.pick(self.home, mood), f"{mood} 挑不到")

    def test_seed_is_idempotent(self):
        stickerlib.seed_builtin(self.home)
        again = stickerlib.seed_builtin(self.home)
        self.assertEqual(again["written"], [], "第二次不该重写")
        self.assertEqual(len(again["skipped"]), len(stickerlib.BUILTIN))
        self.assertEqual(sticker.stats(self.home)["total"], len(stickerlib.BUILTIN))

    def test_seed_does_not_overwrite_edited_files(self):
        stickerlib.seed_builtin(self.home)
        target = self.home.root / sticker.DIR / "开心" / "builtin_smile.png"
        target.write_bytes(b"not-a-png")
        stickerlib.seed_builtin(self.home)
        self.assertEqual(target.read_bytes(), b"not-a-png", "主人改过的内置表情被覆盖了")


class AutoExtractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home.at(self.tmp.name)
        self.home.init()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_keyword_beats_pixels(self):
        """主人自己命名的最准：文件名说"困"，就归到困，不管像素什么色。"""
        p = _make_png(self.d / "好困啊.png", rgb=(255, 0, 0))   # 像素看着像生气
        out = stickerlib.extract(self.home, p)
        self.assertEqual(out["mood"], "困")
        self.assertEqual(out["auto"]["mood_from"], "keyword")

    def test_pixel_fallback_when_no_keyword(self):
        p = _make_png(self.d / "img001.png", rgb=(120, 120, 120))  # 灰
        out = stickerlib.extract(self.home, p)
        self.assertEqual(out["auto"]["mood_from"], "pixels")
        self.assertIn(out["mood"], sticker.MOODS)

    def test_given_mood_wins(self):
        p = _make_png(self.d / "困.png", rgb=(255, 0, 0))
        out = stickerlib.extract(self.home, p, mood="开心")
        self.assertEqual(out["mood"], "开心")
        self.assertEqual(out["auto"]["mood_from"], "given")

    def test_same_image_not_added_twice(self):
        p = _make_png(self.d / "a.png")
        first = stickerlib.extract(self.home, p)
        self.assertFalse(first.get("skipped"))
        again = stickerlib.extract(self.home, p)
        self.assertTrue(again.get("skipped"))
        self.assertEqual(sticker.stats(self.home)["total"], 1)

    def test_extract_dir(self):
        for i, nm in enumerate(("开心1.png", "困2.png", "生气3.png")):
            _make_png(self.d / nm, rgb=(200 - i * 50, 100, 100))
        out = stickerlib.extract_dir(self.home, self.d)
        self.assertEqual(len(out["added"]), 3)
        self.assertEqual(out["failed"], [])
        self.assertEqual(sticker.stats(self.home)["total"], 3)

    def test_extract_rejects_unsupported_format(self):
        p = self.d / "note.txt"
        p.write_text("x", encoding="utf-8")
        with self.assertRaises(ValueError):
            stickerlib.extract(self.home, p)

    def test_extract_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            stickerlib.extract(self.home, self.d / "nope.png")

    def test_extract_leaves_trace(self):
        p = _make_png(self.d / "笑.png")
        stickerlib.extract(self.home, p, now_iso="2026-10-29T10:00:00")
        rows = self.home.read_jsonl("logs/sticker.jsonl")
        self.assertTrue(any(r.get("event") == "auto_extract" for r in rows))


class PngCodecTest(unittest.TestCase):
    """PNG 编解码往返 —— 自动提取的判据全靠它。"""

    def test_write_read_roundtrip(self):
        d = Path(tempfile.mkdtemp())
        w, h = 5, 3
        rgb = bytes(v for i in range(w * h)
                    for v in (i * 7 % 256, i * 13 % 256, i * 29 % 256))
        p = d / "t.png"
        imgpng.write_png(p, w, h, rgb)
        gw, gh, grgb = imgpng.read_png(p)
        self.assertEqual((gw, gh), (w, h))
        self.assertEqual(grgb, rgb)

    def test_rejects_non_png(self):
        d = Path(tempfile.mkdtemp())
        p = d / "x.png"
        p.write_bytes(b"not a png at all")
        with self.assertRaises(ValueError):
            imgpng.read_png(p)

    def test_rejects_unsupported_depth(self):
        d = Path(tempfile.mkdtemp())
        p = d / "y.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
        with self.assertRaises(ValueError):
            imgpng.read_png(p)


def _png_with_filter(path: Path, w: int, h: int, ftype: int, channels: int,
                     color_type: int, pixels: bytes) -> Path:
    """手工造一张带指定滤波的 PNG，用来覆盖解码器的各个分支。

    注意：`pixels` 是**滤波后**的数据（PNG 文件里存的就是滤波后的）。
    所以要测"解码器能还原"，得先把原始像素按该滤波规则编码一遍。
    """
    import struct
    import zlib

    stride = w * channels
    raw = bytearray()
    prev = bytearray(stride)
    for y in range(h):
        line = bytearray(pixels[y * stride : (y + 1) * stride])
        enc = bytearray(stride)
        for i in range(stride):
            a = line[i - channels] if i >= channels else 0
            b = prev[i]
            c = prev[i - channels] if i >= channels else 0
            if ftype == 0:
                enc[i] = line[i]
            elif ftype == 1:
                enc[i] = (line[i] - a) & 0xFF
            elif ftype == 2:
                enc[i] = (line[i] - b) & 0xFF
            elif ftype == 3:
                enc[i] = (line[i] - ((a + b) >> 1)) & 0xFF
            else:  # Paeth
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                enc[i] = (line[i] - pr) & 0xFF
        raw.append(ftype)
        raw.extend(enc)
        prev = line

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, color_type, 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                     + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))
    return path


class PngFilterTest(unittest.TestCase):
    """5 种行滤波都要能还原 —— 真实世界的 PNG 五种都用。"""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def _roundtrip(self, ftype: int) -> None:
        w, h = 4, 4
        # 像素要"不平"，不然滤波还原出错也看不出来
        pixels = bytes((x * 37 + y * 11) % 256 for y in range(h) for x in range(w * 3))
        p = _png_with_filter(self.d / f"f{ftype}.png", w, h, ftype, 3, 2, pixels)
        gw, gh, rgb = imgpng.read_png(p)
        self.assertEqual((gw, gh), (w, h))
        self.assertEqual(rgb, pixels, f"滤波 {ftype} 还原错了")

    def test_filter_none(self):
        self._roundtrip(0)

    def test_filter_sub(self):
        self._roundtrip(1)

    def test_filter_up(self):
        self._roundtrip(2)

    def test_filter_average(self):
        self._roundtrip(3)

    def test_filter_paeth(self):
        self._roundtrip(4)

    def test_grayscale(self):
        w, h = 3, 3
        gray = bytes((i * 40) % 256 for i in range(w * h))
        p = _png_with_filter(self.d / "g.png", w, h, 0, 1, 0, gray)
        gw, gh, rgb = imgpng.read_png(p)
        self.assertEqual((gw, gh), (w, h))
        self.assertEqual(len(rgb), w * h * 3)
        for i in range(w * h):
            self.assertEqual(tuple(rgb[i * 3 : i * 3 + 3]), (gray[i],) * 3)

    def test_grayscale_alpha(self):
        w, h = 2, 2
        ga = bytes(v for i in range(w * h) for v in (100 + i, 255))
        p = _png_with_filter(self.d / "ga.png", w, h, 0, 2, 4, ga)
        _gw, _gh, rgb = imgpng.read_png(p)
        for i in range(w * h):
            self.assertEqual(tuple(rgb[i * 3 : i * 3 + 3]), (100 + i,) * 3)

    def test_rgba(self):
        w, h = 2, 2
        rgba = bytes(v for i in range(w * h) for v in (10 * i, 20 * i, 30 * i, 255))
        p = _png_with_filter(self.d / "rgba.png", w, h, 0, 4, 6, rgba)
        _gw, _gh, rgb = imgpng.read_png(p)
        for i in range(w * h):
            self.assertEqual(tuple(rgb[i * 3 : i * 3 + 3]), (10 * i, 20 * i, 30 * i))

    def test_interlaced_rejected(self):
        import struct
        d = Path(tempfile.mkdtemp())
        p = d / "i.png"
        ihdr = struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 1)
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr
                     + struct.pack(">I", 0))
        with self.assertRaises(ValueError):
            imgpng.read_png(p)

    def test_unsupported_color_type_rejected(self):
        import struct
        d = Path(tempfile.mkdtemp())
        p = d / "c.png"
        ihdr = struct.pack(">IIBBBBB", 2, 2, 8, 3, 0, 0, 0)  # color type 3 = 调色板
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr
                     + struct.pack(">I", 0))
        with self.assertRaises(ValueError):
            imgpng.read_png(p)


if __name__ == "__main__":
    unittest.main()
