from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xdb.backend import vivado


class VivadoBackendTests(unittest.TestCase):
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

    def test_capture_passes_ltx_and_sets_probes_before_refresh(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(tcl: str, args: list[str], timeout: int = 120) -> vivado.VivadoResult:
            captured["tcl"] = tcl
            captured["args"] = args
            captured["timeout"] = timeout
            return vivado.VivadoResult(
                stdout=(
                    'XDB_JSON_BEGIN\n'
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
