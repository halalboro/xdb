from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xdb.cli import main
from xdb.errors import XdbError
from xdb.reports.utilization import (
    discover_utilization_report,
    format_utilization_comparison,
    format_utilization_csv,
    format_utilization_delta_comparison,
    format_utilization_delta_csv,
    format_utilization_table,
    parse_utilization_report,
    utilization_compare_data,
)


REPORT_TEXT = """| Tool Version : Vivado v.2025.1 (lin64) Build 6140274 Wed May 21 22:58:25 MDT 2025
| Date         : Wed May  6 16:13:13 2026
| Design       : cyt_top
| Device       : xcv80-lsva4737-2MHP-e-S
| Design State : Physopt postRoute

1. Netlist Logic
----------------
+----------------------------+--------+-------+------------+-----------+-------+
|          Site Type         |  Used  | Fixed | Prohibited | Available | Util% |
+----------------------------+--------+-------+------------+-----------+-------+
| Registers                  | 233693 | 34745 |          0 |   5148416 |  4.54 |
| CLB LUTs                   | 116420 | 22905 |          0 |   2574208 |  4.52 |
|   LUT as Logic             |  99622 | 18725 |          0 |   2574208 |  3.87 |
+----------------------------+--------+-------+------------+-----------+-------+

3. BLOCKRAM
-----------
+--------------------------+------+-------+------------+-----------+-------+
|         Site Type        | Used | Fixed | Prohibited | Available | Util% |
+--------------------------+------+-------+------------+-----------+-------+
| Block RAM Tile           |   87 |     0 |          0 |      3741 |  2.33 |
| URAM                     |    0 |     0 |          0 |      1925 |  0.00 |
+--------------------------+------+-------+------------+-----------+-------+

4. ARITHMETIC
-------------
+--------------------+------+-------+------------+-----------+-------+
|      Site Type     | Used | Fixed | Prohibited | Available | Util% |
+--------------------+------+-------+------------+-----------+-------+
| DSP Slices         |    0 |     0 |          0 |     10848 |  0.00 |
+--------------------+------+-------+------------+-----------+-------+
"""


class UtilizationReportTests(unittest.TestCase):
    def _write_report(self, root: Path, text: str = REPORT_TEXT) -> Path:
        report = root / "reports" / "shell_utilization.rpt"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(text, encoding="utf-8")
        return report

    def test_parse_standard_vivado_utilization_snippet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = self._write_report(Path(tmp))
            parsed = parse_utilization_report(report)

        self.assertEqual(
            parsed["tool_version"],
            "Vivado v.2025.1 (lin64) Build 6140274 Wed May 21 22:58:25 MDT 2025",
        )
        self.assertEqual(parsed["design"], "cyt_top")
        self.assertEqual(parsed["device"], "xcv80-lsva4737-2MHP-e-S")
        self.assertEqual(parsed["design_state"], "Physopt postRoute")
        resources = parsed["resources"]
        self.assertEqual(resources["clb_luts"]["used"], 116420)
        self.assertEqual(resources["clb_luts"]["available"], 2574208)
        self.assertEqual(resources["clb_luts"]["util_percent"], 4.52)
        self.assertEqual(resources["registers"]["fixed"], 34745)
        self.assertEqual(resources["block_ram_tile"]["used"], 87)
        self.assertEqual(resources["uram"]["available"], 1925)
        self.assertEqual(resources["dsp_slices"]["util_percent"], 0.0)

    def test_directory_discovery_finds_shell_utilization_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = self._write_report(root)
            discovered = discover_utilization_report(root)

        self.assertEqual(discovered, report)

    def test_report_user_alias_resolves_known_user_synthesis_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user_report = root / "reports" / "config_0" / "user_synthed_c0_0.rpt"
            user_report.parent.mkdir(parents=True)
            user_report.write_text(REPORT_TEXT, encoding="utf-8")
            discovered = discover_utilization_report(root, report="user")

        self.assertEqual(discovered, user_report)

    def test_missing_report_produces_xdb_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(XdbError, "no Vivado utilization report"):
                discover_utilization_report(Path(tmp))

    def test_json_serializable_output_includes_numeric_types(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parsed = parse_utilization_report(self._write_report(Path(tmp)))
            round_tripped = json.loads(json.dumps(parsed))

        self.assertIsInstance(round_tripped["resources"]["clb_luts"]["used"], int)
        self.assertIsInstance(round_tripped["resources"]["clb_luts"]["util_percent"], float)

    def test_human_table_output_contains_summary_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parsed = parse_utilization_report(self._write_report(Path(tmp)))
            text = format_utilization_table(parsed)

        self.assertIn("Resource", text)
        self.assertIn("CLB LUTs", text)
        self.assertIn("116420", text)
        self.assertIn("Block RAM Tile", text)

    def test_comparison_output_includes_one_row_per_input_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = root / "a"
            b = root / "b"
            parsed_a = parse_utilization_report(self._write_report(a))
            parsed_b = parse_utilization_report(
                self._write_report(b, REPORT_TEXT.replace("116420", "120000", 1))
            )
            text = format_utilization_comparison([parsed_a, parsed_b], names=["d3", "d7"])

        self.assertIn("d3", text)
        self.assertIn("d7", text)
        self.assertIn("116420 4.52%", text)
        self.assertIn("120000 4.52%", text)

    def test_delta_comparison_output_includes_absolute_and_relative_deltas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = parse_utilization_report(self._write_report(root / "d13"))
            new = parse_utilization_report(
                self._write_report(root / "d17", REPORT_TEXT.replace("116420", "232840", 1))
            )
            data = utilization_compare_data(
                old, new, old_name="d13", new_name="d17", resources=["clb_luts"]
            )
            text = format_utilization_delta_comparison(data)

        self.assertIn("baseline: d13", text)
        self.assertIn("new:      d17", text)
        self.assertIn("+116420", text)
        self.assertIn("+100.00%", text)
        self.assertIn("Δ Util", text)

    def test_delta_comparison_csv_output_has_expected_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = parse_utilization_report(self._write_report(root / "d13"))
            new = parse_utilization_report(
                self._write_report(root / "d17", REPORT_TEXT.replace("116420", "232840", 1))
            )
            data = utilization_compare_data(old, new, resources=["clb_luts"])
            text = format_utilization_delta_csv(data)

        self.assertEqual(
            text.splitlines()[0],
            "resource,old_used,new_used,delta_used,delta_used_percent,old_util_percent,new_util_percent,delta_util_percent_points",
        )
        self.assertIn("clb_luts,116420,232840,116420,100.0,4.52,4.52,0.0", text)

    def test_csv_output_has_expected_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parsed = parse_utilization_report(self._write_report(Path(tmp)))
            text = format_utilization_csv([parsed], names=["d7"], resources=["clb_luts"])

        self.assertEqual(
            text.splitlines(),
            [
                "build,resource,used,available,util_percent",
                "d7,clb_luts,116420,2574208,4.52",
            ],
        )

    def test_cli_reports_utilization_json_does_not_select_hardware_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = self._write_report(Path(tmp))
            stdout = io.StringIO()
            with patch.object(
                sys, "argv", ["xdb", "reports", "utilization", "--json", str(report)]
            ):
                with patch("sys.stdout", stdout):
                    main()
            result = json.loads(stdout.getvalue())

        self.assertEqual(result["resources"]["registers"]["used"], 233693)

    def test_cli_reports_compare_prints_deltas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = self._write_report(root / "d13")
            new = self._write_report(root / "d17", REPORT_TEXT.replace("116420", "232840", 1))
            stdout = io.StringIO()
            with patch.object(
                sys,
                "argv",
                [
                    "xdb",
                    "reports",
                    "compare",
                    "--old-name",
                    "d13",
                    "--new-name",
                    "d17",
                    str(old),
                    str(new),
                ],
            ):
                with patch("sys.stdout", stdout):
                    main()

        self.assertIn("baseline: d13", stdout.getvalue())
        self.assertIn("+116420", stdout.getvalue())

    def test_cli_reports_compare_supports_multiple_new_builds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = self._write_report(root / "d13")
            new_a = self._write_report(root / "d15", REPORT_TEXT.replace("116420", "120000", 1))
            new_b = self._write_report(root / "d17", REPORT_TEXT.replace("116420", "232840", 1))
            stdout = io.StringIO()
            with patch.object(
                sys,
                "argv",
                [
                    "xdb",
                    "reports",
                    "compare",
                    "--old-name",
                    "d13",
                    "--new-name",
                    "d15",
                    "--new-name",
                    "d17",
                    str(old),
                    str(new_a),
                    str(new_b),
                ],
            ):
                with patch("sys.stdout", stdout):
                    main()

        text = stdout.getvalue()
        self.assertIn("new:      d15", text)
        self.assertIn("new:      d17", text)
        self.assertIn("+3580", text)
        self.assertIn("+116420", text)

    def test_cli_top_level_util_supports_comparison_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = self._write_report(root / "a")
            b = self._write_report(root / "b")
            stdout = io.StringIO()
            with patch.object(
                sys,
                "argv",
                ["xdb", "util", "--name", "d3", "--name", "d7", str(a), str(b)],
            ):
                with patch("sys.stdout", stdout):
                    main()

        self.assertIn("d3", stdout.getvalue())
        self.assertIn("d7", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
