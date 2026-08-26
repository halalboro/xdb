from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xdb.backend.base import Capability
from xdb.cli import main


class HardwareCliTests(unittest.TestCase):
    def test_program_does_not_require_ltx_and_records_artifact_identity(self) -> None:
        backend = MagicMock()
        backend.name = "chipscopy"
        backend.capabilities.return_value = {Capability.PROGRAM}
        backend.program.return_value = {
            "ok": True,
            "target": "rose-cable:XFL1EZVSAG4SA",
            "part": "xcv80-lsva4737-2MHP-e-S",
        }

        with tempfile.TemporaryDirectory() as td:
            pdi = Path(td) / "design.pdi"
            pdi.write_bytes(b"pdi-without-ltx")
            with (
                patch("xdb.cli.select_backend", return_value=backend),
                patch("xdb.cli._print") as output,
                patch.dict(os.environ, {}, clear=True),
                patch.object(
                    sys,
                    "argv",
                    ["xdb", "program", "--bit", str(pdi), "--part-hint", "xcv80"],
                ),
            ):
                main()

        backend.program.assert_called_once_with(str(pdi), None, "xcv80", timeout=300)
        result = output.call_args.args[0]
        self.assertEqual(result["backend"], "chipscopy")
        self.assertIsNone(result["ltx"])
        self.assertEqual(result["bitstream_sha256"], hashlib.sha256(b"pdi-without-ltx").hexdigest())


if __name__ == "__main__":
    unittest.main()
