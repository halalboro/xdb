from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xdb.cli import main
from xdb.errors import XdbError
from xdb.reports.cips import (
    discover_cips_artifacts,
    format_cips_report,
    inspect_cips,
    inspect_cips_checkpoint,
    parse_bif,
)


MANAGEMENT_BIF = """bitstream_master:
{
 boot_device { pcie }
 image
 {
  name = pmc_subsys
  partition
  {
   id = 0x01
   type = bootloader
   file = bitstreams/gen_files/plm.elf
  }
 }
 image
 {
  name = lpd
  partition
  {
   id = 0x0B
   core = psm
   file = bitstreams/static_files/psm_fw.elf
  }
 }
}
"""

CPU_BIF = """boot_image:
{
 boot_device { jtag }
 image
 {
  name = cpu_subsystem
  partition
  {
   core = a72-0
   file = firmware/cpu-probe.elf
  }
  partition
  {
   core = r5-0
   file = firmware/safety-probe.elf
  }
 }
}
"""

VIVADO_RECORDS = """Vivado banner
XDB_CIPS_BEGIN
META\tdesign\tcyt_top
META\tdevice\txcv80-lsva4737-2MHP-e-S
CELL\tinst_static/versal_cips_0\tversal_cips_0\tversal_cips\t
PROP\tinst_static/versal_cips_0\tCONFIG.PS_USE_A72\t1
PROP\tinst_static/versal_cips_0\tCONFIG.PS_USE_R5\t0
PIN\tinst_static/versal_cips_0\tM_AXI_FPD\tOUT\tps_to_pl_axi
PIN\tinst_static/versal_cips_0\tPL0_REF_CLK\tOUT\tpl0_ref_clk
PIN\tinst_static/versal_cips_0\tPL_RESETN0\tOUT\tpl_resetn0
PIN\tinst_static/versal_cips_0\tPL_PS_IRQ0\tIN\tcpu_irq
XDB_CIPS_END
"""


class CipsReportTests(unittest.TestCase):
    def _write(self, root: Path, relative: str, data: str | bytes) -> Path:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            path.write_bytes(data)
        else:
            path.write_text(data, encoding="utf-8")
        return path

    def test_parse_management_bif_distinguishes_management_firmware(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bif = self._write(Path(tmp), "cyt_top.bif", MANAGEMENT_BIF)
            parsed = parse_bif(bif)

        self.assertEqual(parsed["boot_devices"], ["pcie"])
        self.assertEqual(len(parsed["partitions"]), 2)
        self.assertTrue(all(item["management_firmware"] for item in parsed["partitions"]))
        self.assertTrue(all(item["processor"] is None for item in parsed["partitions"]))

    def test_parse_bif_identifies_application_processor_partitions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bif = self._write(Path(tmp), "cpu.bif", CPU_BIF)
            parsed = parse_bif(bif)

        self.assertEqual(
            [item["processor"] for item in parsed["partitions"]],
            ["a72", "r5"],
        )
        self.assertFalse(any(item["management_firmware"] for item in parsed["partitions"]))

    def test_directory_discovery_finds_checkpoint_and_top_level_bif(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkpoint = self._write(root, "checkpoints/shell_routed.dcp", b"dcp")
            bif = self._write(root, "bitstreams/cyt_top.bif", MANAGEMENT_BIF)
            artifacts = discover_cips_artifacts(root)

        self.assertEqual(artifacts, {"checkpoint": checkpoint, "bif": bif})

    def test_missing_artifacts_produce_xdb_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(XdbError, "no DCP checkpoint or BIF"):
                discover_cips_artifacts(tmp)

    def test_checkpoint_inspection_parses_vivado_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = self._write(Path(tmp), "shell_routed.dcp", b"dcp")
            result = SimpleNamespace(stdout=VIVADO_RECORDS)
            with patch("xdb.reports.cips._run_vivado_tcl", return_value=result) as run:
                parsed = inspect_cips_checkpoint(checkpoint, timeout=17)

        run.assert_called_once()
        self.assertEqual(run.call_args.args[1], [str(checkpoint)])
        self.assertEqual(run.call_args.kwargs["timeout"], 17)
        self.assertEqual(parsed["design"], "cyt_top")
        self.assertEqual(parsed["device"], "xcv80-lsva4737-2MHP-e-S")
        self.assertEqual(len(parsed["cells"]), 1)
        self.assertTrue(parsed["pins"][0]["connected"])
        self.assertEqual(len(parsed["sha256"]), 64)

    def test_combined_inspection_summarizes_processor_and_boot_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkpoint = self._write(root, "checkpoints/shell_routed.dcp", b"dcp")
            self._write(root, "bitstreams/cyt_top.bif", MANAGEMENT_BIF)
            result = SimpleNamespace(stdout=VIVADO_RECORDS)
            with patch("xdb.reports.cips._run_vivado_tcl", return_value=result):
                data = inspect_cips(root)

        self.assertEqual(data["checkpoint"]["source"], str(checkpoint))
        self.assertEqual(data["findings"]["processors"]["a72"]["status"], "configured")
        self.assertEqual(data["findings"]["processors"]["r5"]["status"], "observed")
        self.assertEqual(len(data["findings"]["connections"]["axi_noc"]), 1)
        self.assertEqual(data["findings"]["processor_boot_partitions"], [])
        self.assertEqual(len(data["findings"]["management_firmware_partitions"]), 2)

    def test_human_report_warns_that_not_observed_is_not_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bif = self._write(Path(tmp), "cyt_top.bif", MANAGEMENT_BIF)
            data = inspect_cips(bif)
            text = format_cips_report(data)

        self.assertIn("Checkpoint: not inspected", text)
        self.assertIn("A72 not_observed", text)
        self.assertIn("Boot device: pcie", text)
        self.assertIn("Processor application partitions: 0", text)
        self.assertIn("not proof of unsupported hardware", text)

    def test_cli_bif_json_does_not_select_hardware_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bif = self._write(Path(tmp), "cyt_top.bif", MANAGEMENT_BIF)
            stdout = io.StringIO()
            with patch.object(sys, "argv", ["xdb", "reports", "cips", str(bif), "--json"]):
                with patch("sys.stdout", stdout):
                    main()
            result = json.loads(stdout.getvalue())

        self.assertEqual(result["schema"], "xdb-cips-inspection-v1")
        self.assertIsNone(result["findings"]["cips_present"])
        self.assertEqual(result["boot_image"]["boot_devices"], ["pcie"])


if __name__ == "__main__":
    unittest.main()
