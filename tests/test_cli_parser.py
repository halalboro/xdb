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

    def test_reports_util_alias_is_parseable_but_hidden_from_help_usage(self) -> None:
        parser = build_parser()
        reports = parser.parse_args(["reports", "util", "build-output"])
        canonical = parser.parse_args(["reports", "utilization", "build-output"])
        reports_help = parser._subparsers._group_actions[0].choices["reports"].format_help()  # noqa: SLF001

        self.assertEqual(reports.cmd, "reports")
        self.assertEqual(reports.reports_cmd, "util")
        self.assertEqual(canonical.reports_cmd, "utilization")
        self.assertIn("{utilization,compare}", reports_help)
        self.assertNotIn("{utilization,util", reports_help)

    def test_reports_compare_command_is_parseable(self) -> None:
        args = build_parser().parse_args([
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
        ])

        self.assertEqual(args.cmd, "reports")
        self.assertEqual(args.reports_cmd, "compare")
        self.assertEqual(args.old, "old-build")
        self.assertEqual(args.new, ["new-build-a", "new-build-b"])
        self.assertEqual(args.report, "shell")
        self.assertEqual(args.new_name, ["a", "b"])

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
