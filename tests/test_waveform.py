from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xdb.cli import _compare_waveform_manifests
from xdb.errors import XdbError


class WaveformManifestTests(unittest.TestCase):
    def write_manifest(self, root: Path, name: str, payload: bytes, export_format: str) -> Path:
        output = root / name
        output.write_bytes(payload)
        manifest = root / f"{name}.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema": "xdb.ila-waveform/v1",
                    "output": str(output),
                    "output_sha256": hashlib.sha256(payload).hexdigest(),
                    "export_format": export_format,
                    "selection": {"start_window": 0},
                }
            ),
            encoding="utf-8",
        )
        return manifest

    def test_compare_reports_waveform_and_metadata_changes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            baseline = self.write_manifest(root, "before.csv", b"same", "CSV")
            same = self.write_manifest(root, "same.csv", b"same", "CSV")
            changed = self.write_manifest(root, "after.vcd", b"different", "VCD")
            identical = _compare_waveform_manifests(str(baseline), str(same))
            difference = _compare_waveform_manifests(str(baseline), str(changed))

        self.assertTrue(identical["identical_waveform"])
        self.assertEqual(identical["metadata_changes"], {})
        self.assertFalse(difference["identical_waveform"])
        self.assertIn("export_format", difference["metadata_changes"])

    def test_compare_rejects_modified_waveform(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = self.write_manifest(root, "capture.csv", b"original", "CSV")
            (root / "capture.csv").write_bytes(b"modified")
            with self.assertRaisesRegex(XdbError, "missing or modified"):
                _compare_waveform_manifests(str(manifest), str(manifest))


if __name__ == "__main__":
    unittest.main()
