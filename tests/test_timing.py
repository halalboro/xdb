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
from xdb.timing.analysis import (
    compare_triage,
    discover_checkpoint,
    group_clock_pairs,
    group_timing_paths,
    parse_critical_warnings_text,
    parse_drc_text,
    parse_timing_paths_text,
    parse_timing_summary_text,
)


TIMING_SUMMARY = """| Tool Version      : Vivado v.2025.1 (lin64)
| Design            : cyt_top
| Device            : xcv80-lsva4737
| Design State      : Physopt postRoute

------------------------------------------------------------------------------------------------
| Design Timing Summary
| ---------------------
------------------------------------------------------------------------------------------------

    WNS(ns)      TNS(ns)  TNS Failing Endpoints  TNS Total Endpoints      WHS(ns)      THS(ns)  THS Failing Endpoints  THS Total Endpoints     WPWS(ns)     TPWS(ns)  TPWS Failing Endpoints  TPWS Total Endpoints
    -------      -------  ---------------------  -------------------      -------      -------  ---------------------  -------------------     --------     --------  ----------------------  --------------------
     -0.789    -1981.162                   3939               437814        0.008        0.000                      0               437433        0.346        0.000                       0                172310

Timing constraints are not met.

------------------------------------------------------------------------------------------------
| Clock Summary
| -------------
------------------------------------------------------------------------------------------------

Clock                  Waveform(ns)         Period(ns)      Frequency(MHz)
-----                  ------------         ----------      --------------
clkout1_primitive      {0.000 2.000}        4.000           250.000
  generated_clk        {0.000 1.000}        2.000           500.000

------------------------------------------------------------------------------------------------
| Intra Clock Table
| -----------------
------------------------------------------------------------------------------------------------

Clock                  WNS(ns)      TNS(ns)  TNS Failing Endpoints  TNS Total Endpoints      WHS(ns)      THS(ns)  THS Failing Endpoints  THS Total Endpoints     WPWS(ns)     TPWS(ns)  TPWS Failing Endpoints  TPWS Total Endpoints
-----                  -------      -------  ---------------------  -------------------      -------      -------  ---------------------  -------------------     --------     --------  ----------------------  --------------------
clkout1_primitive_1     -0.789    -1981.162                   3939               208832        0.008        0.000                      0               208832        1.394        0.000                       0                 81735

------------------------------------------------------------------------------------------------
| Inter Clock Table
| -----------------
------------------------------------------------------------------------------------------------

From Clock           To Clock                 WNS(ns)      TNS(ns)  TNS Failing Endpoints  TNS Total Endpoints      WHS(ns)      THS(ns)  THS Failing Endpoints  THS Total Endpoints
----------           --------                 -------      -------  ---------------------  -------------------      -------      -------  ---------------------  -------------------
clk_a                clk_b                     -0.125       -1.000                      2                  100        0.010        0.000                      0                  100

------------------------------------------------------------------------------------------------
| Timing Details
| --------------
------------------------------------------------------------------------------------------------

Max Delay Paths
--------------------------------------------------------------------------------------
Slack (VIOLATED) :        -0.789ns  (required time - arrival time)
  Source:                 inst_shell/inst_dynamic/inst_user_wrapper_0/helios_core/foo_reg/C
                            (rising edge-triggered cell FDSE clocked by clkout1_primitive_1  {rise@0.000ns fall@2.000ns period=4.000ns})
  Destination:            inst_shell/inst_dynamic/inst_user_wrapper_0/helios_core/bar_reg/CE
                            (rising edge-triggered cell FDRE clocked by clkout1_primitive_1  {rise@0.000ns fall@2.000ns period=4.000ns})
  Path Group:             clkout1_primitive_1
  Path Type:              Setup (Max at Slow Process Corner)
"""

DRC_REPORT = """Report DRC

            Checks found: 3
+------------+----------+------------------------+--------+
| Rule       | Severity | Description            | Checks |
+------------+----------+------------------------+--------+
| AVALXA-268 | Warning  | CLK_DOM_COM_WF         | 2      |
| RTSTAT-10  | Critical Warning | No routable loads | 1    |
+------------+----------+------------------------+--------+
"""

LOG_TEXT = """INFO: harmless
CRITICAL WARNING: [Route 35-586] BUFG route-thru used for routing clock net inst/a/clk_out1
WARNING: [Route 35-39] The design did not meet timing requirements.
WARNING: debug hub core has no connected clocks
"""


class TimingTests(unittest.TestCase):
    def test_parse_timing_summary_report_snippet(self) -> None:
        parsed = parse_timing_summary_text(TIMING_SUMMARY, source="summary.rpt")

        self.assertEqual(parsed["metadata"]["design"], "cyt_top")
        self.assertEqual(parsed["summary"]["wns"], -0.789)
        self.assertEqual(parsed["summary"]["tns_failing_endpoints"], 3939)
        self.assertFalse(parsed["summary"]["timing_met"])
        self.assertEqual(parsed["clocks"][0]["name"], "clkout1_primitive")
        self.assertEqual(parsed["failing_clock_pairs"][0]["from_clock"], "clkout1_primitive_1")
        self.assertEqual(
            parsed["paths"][0]["source"],
            "inst_shell/inst_dynamic/inst_user_wrapper_0/helios_core/foo_reg/C",
        )

    def test_parse_paths_and_group_by_hierarchy_and_clock_pair(self) -> None:
        paths = parse_timing_paths_text(TIMING_SUMMARY)
        grouped = group_timing_paths(paths, depth=3)
        clock_pairs = group_clock_pairs(paths)

        self.assertEqual(grouped["failing_path_count"], 1)
        self.assertEqual(
            grouped["by_common_ancestor"][0]["name"], "inst_shell/inst_dynamic/inst_user_wrapper_0"
        )
        self.assertEqual(grouped["by_bucket"][0]["name"], "user")
        self.assertEqual(clock_pairs[0]["from_clock"], "clkout1_primitive_1")
        self.assertEqual(clock_pairs[0]["count"], 1)

    def test_parse_critical_warnings_prioritizes_clock_and_timing(self) -> None:
        warnings = parse_critical_warnings_text(LOG_TEXT, source="vivado.log")
        categories = [warning["category"] for warning in warnings]

        self.assertIn("route_bufg_route_through", categories)
        self.assertIn("timing_not_met", categories)
        self.assertEqual(warnings[0]["severity"], "high")

    def test_parse_drc_summary_table(self) -> None:
        parsed = parse_drc_text(DRC_REPORT, source="drc.rpt")

        self.assertEqual(parsed["checks_found"], 3)
        self.assertEqual(parsed["by_severity"]["Warning"], 2)
        self.assertEqual(parsed["rules"][1]["rule"], "RTSTAT-10")

    def test_discover_checkpoint_prefers_shell_routed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dcp = root / "checkpoints" / "shell_routed.dcp"
            dcp.parent.mkdir()
            dcp.write_text("", encoding="utf-8")

            self.assertEqual(discover_checkpoint(root), dcp)

    def test_cli_timing_summary_json_uses_reports_without_hardware_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reports = root / "reports"
            reports.mkdir()
            (reports / "shell_timing_summary.rpt").write_text(TIMING_SUMMARY, encoding="utf-8")
            stdout = io.StringIO()
            with patch.object(sys, "argv", ["xdb", "timing", "summary", "--json", str(root)]):
                with patch("sys.stdout", stdout):
                    main()

        result = json.loads(stdout.getvalue())
        self.assertEqual(result["summary"]["wns"], -0.789)

    def test_compare_triage_reports_summary_deltas_and_added_warnings(self) -> None:
        old = {
            "source": "old",
            "summary": {"wns": 0.1, "tns": 0.0, "tns_failing_endpoints": 0},
            "clock_pairs": [],
            "hierarchy": {"by_common_ancestor": []},
            "critical_warnings": [],
        }
        new = {
            "source": "new",
            "summary": {"wns": -0.2, "tns": -1.0, "tns_failing_endpoints": 4},
            "clock_pairs": [{"from_clock": "a", "to_clock": "b"}],
            "hierarchy": {"by_common_ancestor": [{"name": "inst_shell/inst_dynamic", "count": 4}]},
            "critical_warnings": [{"category": "timing_not_met"}],
        }

        compared = compare_triage(old, new, old_name="good", new_name="bad")

        self.assertAlmostEqual(compared["summary_delta"]["wns"]["delta"], -0.3)
        self.assertEqual(compared["clock_pairs"]["added"], ["a->b"])
        self.assertEqual(compared["critical_warnings"]["added"], ["timing_not_met"])


if __name__ == "__main__":
    unittest.main()
