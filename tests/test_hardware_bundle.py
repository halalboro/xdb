from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xdb.errors import XdbError
from xdb.hardware_bundle import create_hardware_bundle


class HardwareBundleTests(unittest.TestCase):
    def make_waveform_manifest(self, root: Path) -> Path:
        waveform = root / "capture.vcd"
        waveform.write_bytes(b"waveform")
        manifest = root / "capture.vcd.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema": "xdb.ila-waveform/v1",
                    "output": str(waveform),
                    "output_sha256": hashlib.sha256(b"waveform").hexdigest(),
                }
            ),
            encoding="utf-8",
        )
        return manifest

    def test_bundle_copies_declared_evidence_with_relative_hashed_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source_manifest = self.make_waveform_manifest(root)
            result = create_hardware_bundle(
                str(root / "bundle"),
                [str(source_manifest)],
                session_context={"backend": "chipscopy", "state": "closed"},
            )
            manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
            copied = [root / "bundle" / item["path"] for item in manifest["artifacts"]]
            copied_exist = all(path.is_file() for path in copied)

        self.assertEqual(manifest["schema"], "xdb.hardware-debug-bundle/v1")
        self.assertEqual(manifest["session"]["backend"], "chipscopy")
        self.assertEqual(len(manifest["artifacts"]), 2)
        self.assertTrue(copied_exist)
        self.assertTrue(all(not Path(item["path"]).is_absolute() for item in manifest["artifacts"]))

    def test_bundle_rejects_size_overflow_and_symlink_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source_manifest = self.make_waveform_manifest(root)
            with self.assertRaisesRegex(XdbError, "exceeds"):
                create_hardware_bundle(str(root / "too-small"), [str(source_manifest)], max_bytes=1)

            target = root / "target.json"
            target.write_text(source_manifest.read_text(encoding="utf-8"), encoding="utf-8")
            symlink = root / "linked.json"
            os.symlink(target, symlink)
            with self.assertRaisesRegex(XdbError, "non-symlink"):
                create_hardware_bundle(str(root / "linked-bundle"), [str(symlink)])


if __name__ == "__main__":
    unittest.main()
