from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xdb.capture_profiles import load_capture_profile, profile_value
from xdb.errors import XdbError


class CaptureProfileTests(unittest.TestCase):
    def test_json_profile_loads_named_defaults_and_resolves_tsm_relative_to_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            profile_path = root / "profiles.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "schema": "xdb.ila-capture-profiles/v1",
                        "profiles": {
                            "smoke": {
                                "samples": 256,
                                "windows": 2,
                                "tsm_path": "trigger.tsm",
                                "triggers": [{"probe": "valid", "operator": "==", "value": 1}],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            profile = load_capture_profile(str(profile_path), "smoke")

        self.assertEqual(profile["samples"], 256)
        self.assertEqual(profile["windows"], 2)
        self.assertEqual(profile["tsm_path"], str((root / "trigger.tsm").resolve()))

    def test_toml_profile_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "profiles.toml"
            path.write_text(
                "\n".join(
                    [
                        'schema = "xdb.ila-capture-profiles/v1"',
                        "[profiles.smoke]",
                        "samples = 1024",
                        "windows = 2",
                        "[[profiles.smoke.triggers]]",
                        'probe = "valid"',
                        'operator = "=="',
                        "value = 1",
                    ]
                ),
                encoding="utf-8",
            )
            profile = load_capture_profile(str(path), "smoke")
        self.assertEqual(profile["samples"], 1024)
        self.assertEqual(profile["triggers"][0]["probe"], "valid")

    def test_cli_value_overrides_profile_default(self) -> None:
        args = type("Args", (), {"samples": 512})()
        self.assertEqual(profile_value(args, {"samples": 256}, "samples", 2048), 512)
        args.samples = None
        self.assertEqual(profile_value(args, {"samples": 256}, "samples", 2048), 256)

    def test_unknown_profile_fields_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "profiles.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": "xdb.ila-capture-profiles/v1",
                        "profiles": {"bad": {"toolchain_specific_path": "/tmp/result"}},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(XdbError, "unsupported fields"):
                load_capture_profile(str(path), "bad")


if __name__ == "__main__":
    unittest.main()
