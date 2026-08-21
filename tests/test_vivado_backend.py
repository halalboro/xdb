from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xdb.backend import vivado


class VivadoBackendTests(unittest.TestCase):
    def test_vivado_timeout_is_reported_as_vivado_error(self) -> None:
        expired = subprocess.TimeoutExpired(
            ["vivado"],
            3,
            output="partial output",
            stderr="partial error",
        )
        process = MagicMock()
        process.pid = 123
        process.poll.return_value = None
        process.communicate.side_effect = [expired, ("partial output", "partial error")]
        with patch("xdb.backend.vivado.subprocess.Popen", return_value=process) as popen:
            with patch("xdb.backend.vivado.os.killpg") as killpg:
                with self.assertRaisesRegex(
                    vivado.VivadoError,
                    "timed out after 3 seconds",
                ) as error:
                    vivado._run_vivado_tcl("exit 0", [], timeout=3)

        self.assertIn("partial output", str(error.exception))
        self.assertIn("partial error", str(error.exception))
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        killpg.assert_called_once_with(123, vivado.signal.SIGTERM)

    def test_list_ilas_passes_ltx_and_sets_probes_before_refresh(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(tcl: str, args: list[str], timeout: int = 120) -> vivado.VivadoResult:
            captured["tcl"] = tcl
            captured["args"] = args
            captured["timeout"] = timeout
            return vivado.VivadoResult(
                stdout='XDB_JSON_BEGIN\n{"target":"t","part":"p","ilas":[]}\nXDB_JSON_END\n',
                stderr="",
            )

        with patch("xdb.backend.vivado._run_vivado_tcl", side_effect=fake_run):
            result = vivado.list_ilas("xcu", timeout=17, ltx="/tmp/debug.ltx")

        self.assertEqual(result["ilas"], [])
        self.assertEqual(captured["args"], ["xcu", "/tmp/debug.ltx"])
        self.assertEqual(captured["timeout"], 17)
        tcl = str(captured["tcl"])
        self.assertIn('if {$ltx ne ""} { set_property PROBES.FILE $ltx $dev }', tcl)
        self.assertLess(tcl.index("set_property PROBES.FILE"), tcl.index("refresh_hw_device"))

    def test_list_ilas_guards_missing_probe_port_width(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(tcl: str, args: list[str], timeout: int = 120) -> vivado.VivadoResult:
            del args, timeout
            captured["tcl"] = tcl
            return vivado.VivadoResult(
                stdout=(
                    "XDB_JSON_BEGIN\n"
                    '{"target":"t","part":"p","ilas":[{"name":"ila0","probes":[{"name":"const_probe","width":null}]}]}'
                    "\nXDB_JSON_END\n"
                ),
                stderr="",
            )

        with patch("xdb.backend.vivado._run_vivado_tcl", side_effect=fake_run):
            result = vivado.list_ilas("xcu")

        self.assertIsNone(result["ilas"][0]["probes"][0]["width"])
        tcl = str(captured["tcl"])
        self.assertIn("list_property $p", tcl)
        self.assertIn('set w "null"', tcl)
        self.assertIn("set w [get_property PORT_WIDTH $p]", tcl)

    def test_capture_passes_ltx_and_sets_probes_before_refresh(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(tcl: str, args: list[str], timeout: int = 120) -> vivado.VivadoResult:
            captured["tcl"] = tcl
            captured["args"] = args
            captured["timeout"] = timeout
            return vivado.VivadoResult(
                stdout=(
                    "XDB_JSON_BEGIN\n"
                    '{"ok":true,"target":"t","part":"p","ila":"ila0","csv":"out.csv","samples":1024}'
                    "\nXDB_JSON_END\n"
                ),
                stderr="",
            )

        with patch("xdb.backend.vivado._run_vivado_tcl", side_effect=fake_run):
            result = vivado.capture(
                "xcu",
                "ila0",
                "out.csv",
                1024,
                timeout=23,
                ltx="/tmp/debug.ltx",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(captured["args"], ["xcu", "ila0", "out.csv", "1024", "/tmp/debug.ltx"])
        self.assertEqual(captured["timeout"], 23)
        tcl = str(captured["tcl"])
        self.assertIn('if {$ltx ne ""} { set_property PROBES.FILE $ltx $dev }', tcl)
        self.assertLess(tcl.index("set_property PROBES.FILE"), tcl.index("refresh_hw_device"))


if __name__ == "__main__":
    unittest.main()
