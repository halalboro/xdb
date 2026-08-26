from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xdb.cli_parser import build_parser


class CliParserTests(unittest.TestCase):
    def test_private_simd_command_is_hidden_from_top_level_help(self) -> None:
        help_text = build_parser().format_help()

        self.assertNotIn("_simd", help_text)
        self.assertNotIn("==SUPPRESS==", help_text)
        self.assertIn("timing", help_text)
        self.assertIn("hls", help_text)
        self.assertIn("sim", help_text)

    def test_private_simd_command_remains_parseable(self) -> None:
        args = build_parser().parse_args(
            [
                "_simd",
                "--anchor-dir",
                "/tmp/xdb",
                "--simset",
                "sim_1",
                "--mode",
                "behavioral",
            ]
        )

        self.assertEqual(args.cmd, "_simd")
        self.assertEqual(args.anchor_dir, "/tmp/xdb")

    def test_vivado_summarize_log_command_is_parseable(self) -> None:
        args = build_parser().parse_args(
            [
                "vivado",
                "summarize-log",
                "vivado.log",
                "--json",
                "--max-items",
                "3",
                "--full",
            ]
        )

        self.assertEqual(args.cmd, "vivado")
        self.assertEqual(args.vivado_cmd, "summarize-log")
        self.assertEqual(args.log, "vivado.log")
        self.assertTrue(args.json)
        self.assertEqual(args.max_items, 3)
        self.assertTrue(args.full)

    def test_vivado_ip_info_command_is_parseable(self) -> None:
        args = build_parser().parse_args(
            [
                "vivado",
                "ip-info",
                "build-dir",
                "--all",
                "--param",
                "GT*",
                "--param",
                "LINE_RATE",
            ]
        )

        self.assertEqual(args.cmd, "vivado")
        self.assertEqual(args.vivado_cmd, "ip-info")
        self.assertEqual(args.path, "build-dir")
        self.assertTrue(args.all)
        self.assertEqual(args.param, ["GT*", "LINE_RATE"])

    def test_reports_util_alias_is_parseable_but_hidden_from_help_usage(self) -> None:
        parser = build_parser()
        reports = parser.parse_args(["reports", "util", "build-output"])
        canonical = parser.parse_args(["reports", "utilization", "build-output"])
        reports_help = parser._subparsers._group_actions[0].choices["reports"].format_help()  # noqa: SLF001

        self.assertEqual(reports.cmd, "reports")
        self.assertEqual(reports.reports_cmd, "util")
        self.assertEqual(canonical.reports_cmd, "utilization")
        self.assertIn("{utilization,compare,cips,floorplan}", reports_help)
        self.assertNotIn("{utilization,util", reports_help)

    def test_reports_compare_command_is_parseable(self) -> None:
        args = build_parser().parse_args(
            [
                "reports",
                "compare",
                "old-build",
                "new-build-a",
                "new-build-b",
                "--report",
                "shell",
                "--new-name",
                "a",
                "--new-name",
                "b",
            ]
        )

        self.assertEqual(args.cmd, "reports")
        self.assertEqual(args.reports_cmd, "compare")
        self.assertEqual(args.old, "old-build")
        self.assertEqual(args.new, ["new-build-a", "new-build-b"])
        self.assertEqual(args.report, "shell")
        self.assertEqual(args.new_name, ["a", "b"])

    def test_reports_floorplan_command_is_parseable(self) -> None:
        args = build_parser().parse_args(
            [
                "reports",
                "floorplan",
                "build-output",
                "--dcp",
                "checkpoints/shell_routed.dcp",
                "--out",
                "figure.svg",
                "--hierarchy-depth",
                "2",
                "--max-groups",
                "32",
                "--no-pblocks",
                "--force",
                "--json",
            ]
        )

        self.assertEqual(args.cmd, "reports")
        self.assertEqual(args.reports_cmd, "floorplan")
        self.assertEqual(args.path, "build-output")
        self.assertEqual(args.dcp, "checkpoints/shell_routed.dcp")
        self.assertEqual(args.out, "figure.svg")
        self.assertEqual(args.hierarchy_depth, 2)
        self.assertEqual(args.max_groups, 32)
        self.assertFalse(args.show_pblocks)
        self.assertTrue(args.force)
        self.assertTrue(args.json)

    def test_hls_command_family_is_parseable(self) -> None:
        sim = build_parser().parse_args(
            [
                "hls",
                "sim",
                "runtime",
                "--workspace",
                "workspace",
                "--all",
                "--continue-on-failure",
                "--timeout",
                "12.5",
                "--restage",
                "--summary",
            ]
        )
        provenance = build_parser().parse_args(
            ["hls", "provenance", "runtime", "--case", "empty", "--summary"]
        )
        doctor = build_parser().parse_args(["hls", "doctor", "runtime", "--summary"])
        bundle = build_parser().parse_args(
            ["hls", "bundle", "runtime", "--out", "failure", "--max-bytes", "4096"]
        )

        self.assertEqual(sim.hls_cmd, "sim")
        self.assertTrue(sim.all)
        self.assertTrue(sim.continue_on_failure)
        self.assertEqual(sim.timeout, 12.5)
        self.assertTrue(sim.restage)
        self.assertEqual(provenance.case, "empty")
        self.assertEqual(doctor.hls_cmd, "doctor")
        self.assertEqual(bundle.max_bytes, 4096)

    def test_resident_service_csr_commands_are_parseable(self) -> None:
        read = build_parser().parse_args(
            ["sim", "service-csr", "read", "0x138", "--timeout", "2.5"]
        )
        write = build_parser().parse_args(["sim", "service-csr", "write", "0x100", "0x1234"])

        self.assertEqual(read.sim_cmd, "service-csr")
        self.assertEqual(read.sim_service_csr_cmd, "read")
        self.assertEqual(read.addr, "0x138")
        self.assertEqual(read.timeout, 2.5)
        self.assertEqual(write.sim_service_csr_cmd, "write")
        self.assertEqual(write.value, "0x1234")

    def test_vio_commands_are_parseable(self) -> None:
        listed = build_parser().parse_args(["vio", "list"])
        read = build_parser().parse_args(["vio", "read", "--vio", "vio0", "--probe", "status"])
        write = build_parser().parse_args(
            ["vio", "write", "--vio", "vio0", "--set", "enable=1", "--yes"]
        )
        self.assertEqual(listed.vio_cmd, "list")
        self.assertEqual(read.probe, ["status"])
        self.assertEqual(write.set, ["enable=1"])
        self.assertTrue(write.yes)

    def test_waveform_compare_command_is_parseable(self) -> None:
        args = build_parser().parse_args(["waveform", "compare", "before.json", "after.json"])
        self.assertEqual(args.waveform_cmd, "compare")
        self.assertEqual(args.baseline, "before.json")
        self.assertEqual(args.new, "after.json")

    def test_hardware_session_commands_are_parseable(self) -> None:
        launch = build_parser().parse_args(["hw-session", "launch", "--name", "v80"])
        status = build_parser().parse_args(["hw-session", "status", "--name", "v80"])
        close = build_parser().parse_args(["hw-session", "close", "--name", "v80", "--force"])
        self.assertEqual(launch.hw_session_cmd, "launch")
        self.assertEqual(status.hw_session_cmd, "status")
        self.assertTrue(close.force)

    def test_decoupled_ila_lifecycle_commands_are_parseable(self) -> None:
        arm = build_parser().parse_args(
            ["ila", "arm", "--ila", "ila0", "--samples", "256", "--windows", "2"]
        )
        status = build_parser().parse_args(["ila", "status", "--ila", "ila0"])
        wait = build_parser().parse_args(["ila", "wait", "--ila", "ila0", "--timeout", "9"])
        upload = build_parser().parse_args(
            ["ila", "upload", "--ila", "ila0", "--csv", "capture.csv"]
        )

        self.assertEqual(arm.ila_cmd, "arm")
        self.assertEqual(arm.windows, 2)
        self.assertEqual(status.ila_cmd, "status")
        self.assertEqual(wait.timeout, 9)
        self.assertEqual(upload.output, "capture.csv")
        self.assertEqual(upload.format, "csv")

    def test_host_command_capture_is_parseable(self) -> None:
        args = build_parser().parse_args(
            [
                "ila",
                "with-capture",
                "--ila",
                "ila0",
                "--out",
                "capture.vcd",
                "--format",
                "vcd",
                "--host-timeout",
                "12",
                "--exec",
                "--",
                "./workload",
                "--case",
                "smoke",
            ]
        )
        self.assertEqual(args.ila_cmd, "with-capture")
        self.assertEqual(args.format, "vcd")
        self.assertEqual(args.host_timeout, 12)
        self.assertEqual(args.exec_command, ["--", "./workload", "--case", "smoke"])

    def test_multi_ila_commands_are_parseable(self) -> None:
        arm = build_parser().parse_args(
            [
                "ila",
                "group-arm",
                "--ila",
                "ila0",
                "--ila",
                "ila1",
                "--source-ila",
                "ila0",
                "--trigger",
                "state",
                "==",
                "3",
            ]
        )
        upload = build_parser().parse_args(
            [
                "ila",
                "group-upload",
                "--ila",
                "ila0",
                "--ila",
                "ila1",
                "--out-dir",
                "captures",
                "--format",
                "vcd",
            ]
        )
        self.assertEqual(arm.ila, ["ila0", "ila1"])
        self.assertEqual(arm.source_ila, "ila0")
        self.assertEqual(upload.out_dir, "captures")
        self.assertEqual(upload.format, "vcd")

    def test_advanced_trigger_options_are_parseable(self) -> None:
        arm = build_parser().parse_args(
            [
                "ila",
                "arm",
                "--ila",
                "ila0",
                "--tsm",
                "trigger.tsm",
                "--capture-value",
                "valid",
                "==",
                "1",
                "--capture-condition",
                "or",
                "--trig-in",
                "trigger_or_trig_in",
                "--trig-out",
                "trigger_only",
            ]
        )
        compile_trigger = build_parser().parse_args(
            ["ila", "compile-trigger", "--ila", "ila0", "--tsm", "trigger.tsm"]
        )

        self.assertEqual(arm.tsm, "trigger.tsm")
        self.assertEqual(arm.capture_value, [["valid", "==", "1"]])
        self.assertEqual(arm.capture_condition, "or")
        self.assertEqual(arm.trig_in, "trigger_or_trig_in")
        self.assertEqual(arm.trig_out, "trigger_only")
        self.assertEqual(compile_trigger.ila_cmd, "compile-trigger")

    def test_chipscopy_capture_options_are_parseable(self) -> None:
        args = build_parser().parse_args(
            [
                "capture",
                "--ila",
                "ila0",
                "--csv",
                "capture.csv",
                "--samples",
                "256",
                "--windows",
                "4",
                "--trigger-position",
                "32",
                "--trigger",
                "state",
                ">=",
                "3",
                "--trigger",
                "valid",
                "==",
                "1",
            ]
        )

        self.assertEqual(args.samples, 256)
        self.assertEqual(args.windows, 4)
        self.assertEqual(args.trigger_position, 32)
        self.assertEqual(args.trigger, [["state", ">=", "3"], ["valid", "==", "1"]])

    def test_reports_utilization_help_documents_report_aliases(self) -> None:
        parser = build_parser()
        reports_parser = parser._subparsers._group_actions[0].choices["reports"]  # noqa: SLF001
        utilization_parser = reports_parser._subparsers._group_actions[0].choices["utilization"]  # noqa: SLF001
        help_text = utilization_parser.format_help()

        self.assertIn("--report shell", help_text)
        self.assertIn("reports/shell_utilization.rpt", help_text)
        self.assertIn("--report user", help_text)
        self.assertIn("reports/config_0/user_synthed_c0_0.rpt", help_text)


if __name__ == "__main__":
    unittest.main()
