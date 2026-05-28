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
        self.assertIn("{utilization}", reports_help)
        self.assertNotIn("{utilization,util}", reports_help)


if __name__ == "__main__":
    unittest.main()
