import importlib.util
import struct
from pathlib import Path
import tempfile
import unittest
import zlib

spec = importlib.util.spec_from_file_location("fbmap", Path(__file__).with_name("decode_fb_map.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class FbMapTests(unittest.TestCase):
    def test_parse_and_png(self):
        rows = "\n".join(f"SWEET FB MAP 3 {y} " + "ff" * 48 for y in range(106))
        maps = module.parse(rows)
        self.assertEqual(sorted(maps), [3])
        self.assertEqual(len(maps[3]), 106)
        from io import BytesIO
        import zlib as z
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.png"
            module.write_png(maps, 3, path)
            data = path.read_bytes()
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        width, height = struct.unpack(">II", data[16:24])
        self.assertEqual((width, height), (480, 1060))

    def test_hd_parse_and_png(self):
        rows = "\n".join(f"SWEET FB HDMAP {y} " + "f800" * 56 for y in range(120))
        hd = module.parse_hd(rows)
        self.assertEqual(len(hd), 120)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hd.png"
            module.write_hd_png(hd, path)
            data = path.read_bytes()
        width, height = struct.unpack(">II", data[16:24])
        self.assertEqual((width, height), (280, 600))

    def test_hd_parse_survives_interleaved_log(self):
        line = "SWEET FB HDMAP 0 " + "f800" * 40 + "SWEET USB: setup 02 01 0000 0083 len 0" + "f800" * 16
        hd = module.parse_hd(line)
        self.assertEqual(len(hd[0]), 112)

    def test_bad_rows_are_ignored(self):
        maps = module.parse("SWEET FB MAP 1 0 ff\nSWEET FB MAP 1 200 " + "00" * 48)
        self.assertEqual(maps, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
