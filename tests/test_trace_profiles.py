from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xdb import cli
from xdb.config import set_config_file
from xdb.errors import XdbError
from xdb.sim.trace_profiles import get_trace_profile, list_trace_profiles


class TraceProfileTests(unittest.TestCase):
    def tearDown(self) -> None:
        set_config_file(None)

    def test_lists_profiles_from_explicit_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile_file = Path(tmp) / "profiles.json"
            profile_file.write_text(
                json.dumps({"profiles": {"smoke": {"transactions": True, "step": "5 ns"}}}),
                encoding="utf-8",
            )

            result = list_trace_profiles(str(profile_file))

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["profiles"][0]["name"], "smoke")

    def test_profile_file_can_come_from_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile_file = Path(tmp) / "profiles.json"
            profile_file.write_text(
                json.dumps({"profiles": {"env-profile": {"transactions": True}}}),
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"XDB_TRACE_PROFILE_FILE": str(profile_file)}, clear=False):
                result = get_trace_profile("env-profile")

        self.assertTrue(result["config"]["transactions"])
        self.assertEqual(result["source"], str(profile_file))

    def test_profile_lookup_requires_explicit_file_or_environment(self) -> None:
        with patch.dict("os.environ", {"XDB_TRACE_PROFILE_FILE": "", "XDB_CONFIG_FILE": ""}, clear=False):
            with self.assertRaises(XdbError):
                get_trace_profile("missing")

    def test_get_profile_returns_named_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile_file = Path(tmp) / "profiles.json"
            profile_file.write_text(
                json.dumps({"profiles": {"smoke": {"axis": ["/dut/in"], "decode_bytes": True}}}),
                encoding="utf-8",
            )

            result = get_trace_profile("smoke", str(profile_file))

        self.assertEqual(result["name"], "smoke")
        self.assertEqual(result["config"]["axis"], ["/dut/in"])

    def test_with_trace_cli_applies_profile_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile_file = Path(tmp) / "profiles.json"
            profile_file.write_text(
                json.dumps(
                    {
                        "profiles": {
                            "smoke": {
                                "transactions": True,
                                "axis": ["/dut/in"],
                                "duration": "20 ns",
                                "step": "5 ns",
                                "decode_bytes": True,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            argv = [
                "xdb",
                "sim",
                "with-trace",
                "--profile",
                "smoke",
                "--profile-file",
                str(profile_file),
                "--",
                "xdb",
                "sim",
                "coyote-status",
            ]
            with (
                patch.object(sys, "argv", argv),
                patch("xdb.cli.with_trace_session", return_value={"ok": True}) as with_trace,
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                cli.main()

        call = with_trace.call_args
        self.assertEqual(call.args[2], ["20", "ns"])
        self.assertEqual(call.kwargs["step_tokens"], ["5", "ns"])
        self.assertTrue(call.kwargs["transactions"])
        self.assertEqual(call.kwargs["axis_paths"], ["/dut/in"])
        self.assertTrue(call.kwargs["decode_bytes"])

    def test_axis_trace_cli_uses_profile_paths_and_duration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile_file = Path(tmp) / "profiles.json"
            profile_file.write_text(
                json.dumps(
                    {
                        "profiles": {
                            "axis-rx": {
                                "paths": ["/dut/rx"],
                                "duration": "40 ns",
                                "step": "10 ns",
                                "only_handshakes": True,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            argv = [
                "xdb",
                "sim",
                "axis",
                "trace",
                "--profile",
                "axis-rx",
                "--profile-file",
                str(profile_file),
            ]
            with (
                patch.object(sys, "argv", argv),
                patch("xdb.cli.axis_trace_session", return_value={"records": []}) as axis_trace,
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                cli.main()

        call = axis_trace.call_args
        self.assertEqual(call.args[1], ["/dut/rx"])
        self.assertEqual(call.args[2], ["40", "ns"])
        self.assertEqual(call.kwargs["step_tokens"], ["10", "ns"])
        self.assertTrue(call.kwargs["only_handshakes"])


if __name__ == "__main__":
    unittest.main()
