from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xdb.sim.mem_tools import diff_memory_files, dump_memory_session


class MemToolsTests(unittest.TestCase):
    def test_dump_memory_writes_binary_file_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "dump.bin"
            with patch(
                "xdb.sim.mem_tools.coyote_mem_read_session",
                return_value={"data_hex": "000102ff"},
            ) as read_mem:
                result = dump_memory_session("unit", "host", 0x1000, 4, str(out))

            self.assertEqual(out.read_bytes(), b"\x00\x01\x02\xff")
            self.assertEqual(read_mem.call_args.args, ("unit", "host", 0x1000, 4))
            self.assertEqual(result["addr_hex"], "0x1000")
            self.assertEqual(result["size"], 4)
            self.assertEqual(len(result["sha256"]), 64)

    def test_diff_memory_files_reports_changed_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            before = Path(tmp) / "before.bin"
            after = Path(tmp) / "after.bin"
            before.write_bytes(bytes.fromhex("000102030405"))
            after.write_bytes(bytes.fromhex("0001fffe0406"))

            result = diff_memory_files(str(before), str(after))

        self.assertFalse(result["same"])
        self.assertEqual(result["changed_range_count"], 2)
        self.assertEqual(result["changed_ranges"][0]["offset"], 2)
        self.assertEqual(result["changed_ranges"][0]["size"], 2)
        self.assertEqual(result["changed_ranges"][0]["before_hex"], "0203")
        self.assertEqual(result["changed_ranges"][0]["after_hex"], "fffe")
        self.assertEqual(result["changed_ranges"][1]["offset"], 5)

    def test_diff_memory_files_reports_size_extension(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            before = Path(tmp) / "before.bin"
            after = Path(tmp) / "after.bin"
            before.write_bytes(b"abc")
            after.write_bytes(b"abcde")

            result = diff_memory_files(str(before), str(after))

        self.assertEqual(result["changed_range_count"], 1)
        self.assertEqual(result["changed_ranges"][0]["offset"], 3)
        self.assertEqual(result["changed_ranges"][0]["before_hex"], "----")
        self.assertEqual(result["changed_ranges"][0]["after_hex"], "6465")


if __name__ == "__main__":
    unittest.main()
