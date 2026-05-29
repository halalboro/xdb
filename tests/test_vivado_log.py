from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xdb.cli import main
from xdb.vivado_log import format_vivado_log_summary, summarize_vivado_log_text


FAILED_LOG = """INFO: [Common 17-349] Got license for feature 'Implementation'
WARNING: [Constraints 18-4434] Global Clock Buffer 'clk0' is LOCed to site 'BUFGCE_X0Y1'.
44 Infos, 4 Warnings, 0 Critical Warnings and 1 Errors encountered.
write_device_image failed
\x1b[1m\x1b[91m** CERR: ERROR: [Common 17-70] Application Exception: Not found in path: gmake
\x1b[m\x0f
INFO: [Common 17-206] Exiting Vivado
"""

CRITICAL_LOG = """INFO: scanning
CRITICAL WARNING: [HDL 9-3136] 'MEM_DONE' is not declared [/tmp/cnfg_slave.sv:857]
CRITICAL WARNING: [Route 35-39] The design did not meet timing requirements. Please run report_timing_summary for detailed reports.
INFO: done
"""

NIX_EMBEDDED_VIVADO_CRASH_LOG = """CRITICAL WARNING: [Vivado 12-2285] Cannot set LOC property of instance 'inst_shell/inst_peer_backend_aurora_qsfp1/inst_aurora_module/inst_aurora/inst/aurora_loopback_ip_core_i/aurora_loopback_ip_wrapper_i/aurora_loopback_ip_multi_gt_i/aurora_loopback_ip_gt_i/inst/gen_gtwizard_gtye4_top.aurora_loopback_ip_gt_gtwizard_gtye4_inst/gen_gtwizard_gtye4.gen_channel_container[24].gen_enabled_channel.gtye4_channel_wrapper_inst/channel_inst/gtye4_channel_gen.gen_gtye4_channel_inst[3].GTYE4_CHANNEL_PRIM_INST'. Instance inst_shell/inst_peer_backend_aurora_qsfp1/inst_aurora_module/inst_aurora/inst/aurora_loopback_ip_core_i/aurora_loopback_ip_wrapper_i/aurora_loopback_ip_multi_gt_i/aurora_loopback_ip_gt_i/inst/gen_gtwizard_gtye4_top.aurora_loopback_ip_gt_gtwizard_gtye4_inst/gen_gtwizard_gtye4.gen_channel_container[24].gen_enabled_channel.gtye4_channel_wrapper_inst/channel_inst/gtye4_channel_gen.gen_gtye4_channel_inst[3].GTYE4_CHANNEL_PRIM_INST can not be placed in GTYE4_CHANNEL of site GTYE4_CHANNEL_X1Y3 because the bel is occupied by inst_static/inst_int_static/xdma_0/inst/pcie4c_ip_i/inst/design_static_xdma_0_0_pcie4c_ip_gt_top_i/diablo_gt.diablo_gt_phy_wrapper/gt_wizard.gtwizard_top_i/design_static_xdma_0_0_pcie4c_ip_gt_i/inst/gen_gtwizard_gtye4_top.design_static_xdma_0_0_pcie4c_ip_gt_gtwizard_gtye4_inst/gen_gtwizard_gtye4.gen_channel_container[24].gen_enabled_channel.gtye4_channel_wrapper_inst/channel_inst/gtye4_channel_gen.gen_gtye4_channel_inst[3].GTYE4_CHANNEL_PRIM_INST. This could be caused by bel constraint conflict [/build/ip/aurora.xdc:102]
56 Infos, 103 Warnings, 1 Critical Warnings and 0 Errors encountered.
opt_design completed successfully
Abnormal program termination (11)
Please check '/build/source/.nix-hw-u280/hs_err_pid967.log' for details
segfault in /share/xilinx/Vivado/2023.2/bin/unwrapped/lnx64.o/vivado -exec vivado -mode tcl -source /build/source/.nix-hw-u280/pnr_shell.tcl -notrace, exiting...
make[3]: *** [CMakeFiles/shell.dir/build.make:73: checkpoints/shell_routed.dcp] Error 139
make: *** [Makefile:176: shell] Error 2
"""


class VivadoLogTests(unittest.TestCase):
    def test_summarize_failed_log_prioritizes_real_error(self) -> None:
        summary = summarize_vivado_log_text(FAILED_LOG, source="vivado.log")

        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["counts"]["errors"], 1)
        self.assertEqual(summary["counts"]["failures"], 1)
        self.assertEqual(summary["reported_counts"]["errors"], 1)
        self.assertEqual(summary["root_cause_candidates"][0]["category"], "missing_executable")
        self.assertIn("gmake", summary["root_cause_candidates"][0]["message"])

    def test_summarize_critical_warnings_categories(self) -> None:
        summary = summarize_vivado_log_text(CRITICAL_LOG, source="stdin")
        categories = summary["categories"]

        self.assertEqual(summary["status"], "critical_warnings")
        self.assertEqual(summary["counts"]["critical_warnings"], 2)
        self.assertEqual(categories["hdl_compile"], 1)
        self.assertEqual(categories["timing_not_met"], 1)
        self.assertEqual(summary["root_cause_candidates"][0]["category"], "timing_not_met")

    def test_embedded_vivado_log_detects_crash_after_zero_error_summary(self) -> None:
        summary = summarize_vivado_log_text(NIX_EMBEDDED_VIVADO_CRASH_LOG, source="stdin")

        self.assertEqual(summary["status"], "failed")
        self.assertTrue(summary["looks_like_vivado_log"])
        self.assertEqual(summary["reported_counts"]["errors"], 0)
        self.assertEqual(summary["counts"]["failures"], 4)
        self.assertEqual(summary["root_cause_candidates"][0]["category"], "vivado_crash")
        self.assertIn("vivado", summary["root_cause_candidates"][0]["message"].lower())

    def test_text_summary_compacts_long_constraint_conflict_lines(self) -> None:
        summary = summarize_vivado_log_text(NIX_EMBEDDED_VIVADO_CRASH_LOG, source="stdin")
        text = format_vivado_log_summary(summary, max_items=8)

        self.assertIn("Cannot set LOC:", text)
        self.assertIn("GTYE4_CHANNEL_X1Y3", text)
        self.assertIn("occupied by", text)
        self.assertNotIn("aurora_loopback_ip_multi_gt_i/aurora_loopback_ip_gt_i/inst", text)
        self.assertTrue(all(len(line) <= 124 for line in text.splitlines()))

    def test_verbose_text_summary_keeps_full_constraint_conflict_message(self) -> None:
        summary = summarize_vivado_log_text(NIX_EMBEDDED_VIVADO_CRASH_LOG, source="stdin")
        text = format_vivado_log_summary(summary, max_items=8, verbose=True)

        self.assertIn("Cannot set LOC property of instance", text)
        self.assertIn("aurora_loopback_ip_multi_gt_i/aurora_loopback_ip_gt_i/inst", text)
        self.assertIn("This could be caused by bel constraint conflict", text)

    def test_unlimited_text_summary_omits_more_marker(self) -> None:
        summary = summarize_vivado_log_text(NIX_EMBEDDED_VIVADO_CRASH_LOG, source="stdin")
        text = format_vivado_log_summary(summary, max_items=None, verbose=True)

        self.assertNotIn(" more", text)
        self.assertIn("CMakeFiles/shell.dir/build.make", text)
        self.assertIn("Cannot set LOC property of instance", text)

    def test_warns_when_input_does_not_look_like_vivado_log(self) -> None:
        summary = summarize_vivado_log_text("hello\nERROR: plain tool failed\n", source="notes.txt")
        text = format_vivado_log_summary(summary)

        self.assertEqual(summary["status"], "unrecognized")
        self.assertFalse(summary["looks_like_vivado_log"])
        self.assertIn("does not look like a Vivado log", summary["input_warnings"][0])
        self.assertEqual(text, "not a Vivado log")

    def test_warns_for_vivado_journal_without_log_diagnostics(self) -> None:
        journal = """# Vivado v2023.2 (64-bit)
# Start of session at: Fri May 29 11:24:23 2026
# Log file: /tmp/vivado.log
# Journal file: /tmp/vivado.jou
source /tmp/check_syntax.tcl -notrace
"""
        summary = summarize_vivado_log_text(journal, source="vivado.backup.jou")
        text = format_vivado_log_summary(summary)

        self.assertEqual(summary["status"], "unrecognized")
        self.assertFalse(summary["looks_like_vivado_log"])
        self.assertIn("Vivado journal", summary["input_warnings"][0])
        self.assertEqual(text, "not a Vivado log")

    def test_cli_summarize_log_rejects_non_log_input(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        stdin = io.StringIO("hello\nERROR: plain tool failed\n")
        with patch.object(sys, "argv", ["xdb", "vivado", "summarize-log", "-"]):
            with patch("sys.stdin", stdin), patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                with self.assertRaises(SystemExit) as cm:
                    main()

        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "not a Vivado log\n")

    def test_format_text_summary(self) -> None:
        summary = summarize_vivado_log_text(FAILED_LOG, source="vivado.log")
        text = format_vivado_log_summary(summary, max_items=1)

        self.assertIn("vivado log summary", text)
        self.assertIn("status: failed", text)
        self.assertIn("root-cause candidates:", text)
        self.assertIn("missing_executable", text)

    def test_cli_summarize_log_json_from_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vivado.log"
            path.write_text(FAILED_LOG, encoding="utf-8")
            stdout = io.StringIO()
            with patch.object(sys, "argv", ["xdb", "vivado", "summarize-log", "--json", str(path)]):
                with patch("sys.stdout", stdout):
                    main()

        result = json.loads(stdout.getvalue())
        self.assertEqual(result["source"], str(path))
        self.assertEqual(result["root_cause_candidates"][0]["category"], "missing_executable")

    def test_cli_summarize_log_reads_stdin(self) -> None:
        stdout = io.StringIO()
        stdin = io.StringIO(CRITICAL_LOG)
        with patch.object(sys, "argv", ["xdb", "vivado", "summarize-log", "-"]):
            with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
                main()

        text = stdout.getvalue()
        self.assertIn("source: stdin", text)
        self.assertIn("timing_not_met", text)


if __name__ == "__main__":
    unittest.main()
