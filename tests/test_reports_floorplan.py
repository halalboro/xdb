from __future__ import annotations

import io
import json
import os
import stat
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xdb.cli import main
from xdb.errors import XdbError
from xdb.reports.floorplan import (
    _merge_pblock_rectangles,
    _write_svg,
    discover_floorplan_checkpoint,
    generate_floorplan_svg,
    inspect_floorplan_checkpoint,
    parse_floorplan_records,
    render_floorplan_svg,
)


FLOORPLAN_RECORDS = """XDB_FLOORPLAN_BEGIN
META\tschema\txdb-floorplan-records-v1
META\tdesign\tcyt_top
META\tdevice\txcu280-fsvh2892-2L-e
META\ttool_version\t2023.2
SITE\tSLICE_X0Y0\tSLICEL\t10\t10
SITE\tSLICE_X1Y0\tSLICEM\t20\t10
SITE\tRAMB36_X0Y0\tRAMB36\t30\t10
SITE\tDSP48E2_X0Y0\tDSP48E2\t40\t10
SITE\tURAM288_X0Y0\tURAM288\t50\t10
SITE\tGTYE4_CHANNEL_X0Y0\tGTYE4_CHANNEL\t60\t10
SITE\tPCIE40E4_X0Y0\tPCIE40E4\t70\t10
SITE\tODD_SITE_X0Y0\tODD_SITE\t80\t10
OCC\tSLICE_X0Y0\tinst_static\t3
OCC\tSLICE_X0Y0\tinst_shell\t2
OCC\tSLICE_X1Y0\tinst_shell\t4
OCC\tSLICE_X1Y0\ttiny_shared_module\t1
OCC\tRAMB36_X0Y0\tmodule<&quot;\t1
OCC\tMISSING_X0Y0\tinst_shell\t2
PBLOCK\tpblock_user\tSLICE_X0Y0:SLICE_X1Y0 RAMB36_X0Y0:RAMB36_X0Y0
STAT\tprimitive_cells\t15
STAT\tplaced_cells\t13
STAT\tunplaced_cells\t2
STAT\tsites_with_coordinates\t8
STAT\tsites_without_coordinates\t0
STAT\trouting_errors\t0
XDB_FLOORPLAN_END
"""


class FloorplanReportTests(unittest.TestCase):
    def _write(self, root: Path, relative: str, data: str | bytes) -> Path:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            path.write_bytes(data)
        else:
            path.write_text(data, encoding="utf-8")
        return path

    def test_discovery_prefers_packaged_shell_routed_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            expected = self._write(root, "checkpoints/shell_routed.dcp", b"routed")
            self._write(root, "checkpoints/static_routed_locked.dcp", b"static")

            result = discover_floorplan_checkpoint(root)

        self.assertEqual(result, expected)

    def test_discovery_requires_selection_when_fallback_is_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "a/first_routed.dcp", b"a")
            self._write(root, "b/second_routed.dcp", b"b")

            with self.assertRaisesRegex(XdbError, "multiple routed checkpoints"):
                discover_floorplan_checkpoint(root)

    def test_discovery_does_not_treat_unrouted_name_as_routed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "checkpoints/design_unrouted.dcp", b"unrouted")

            with self.assertRaisesRegex(XdbError, "no routed Vivado DCP"):
                discover_floorplan_checkpoint(root)

    def test_parse_records_classifies_resources_and_preserves_placement(self) -> None:
        design = parse_floorplan_records(FLOORPLAN_RECORDS, "shell_routed.dcp")

        self.assertEqual(design.design, "cyt_top")
        self.assertEqual(design.device, "xcu280-fsvh2892-2L-e")
        self.assertEqual(design.sites["SLICE_X0Y0"].resource, "logic")
        self.assertEqual(design.sites["RAMB36_X0Y0"].resource, "bram")
        self.assertEqual(design.sites["DSP48E2_X0Y0"].resource, "dsp")
        self.assertEqual(design.sites["URAM288_X0Y0"].resource, "uram")
        self.assertEqual(design.sites["GTYE4_CHANNEL_X0Y0"].resource, "transceiver")
        self.assertEqual(design.sites["PCIE40E4_X0Y0"].resource, "hard")
        self.assertEqual(design.sites["ODD_SITE_X0Y0"].resource, "other")
        self.assertEqual(design.stats["placed_cells"], 13)
        self.assertEqual(len(design.occupancy), 6)
        self.assertEqual(design.pblocks[0].name, "pblock_user")

    def test_pblock_merging_preserves_l_shaped_holes(self) -> None:
        horizontal = (0.0, 10.0, 20.0, 20.0)
        vertical = (0.0, 0.0, 10.0, 20.0)
        same_band = (20.0, 10.0, 30.0, 20.0)

        merged = _merge_pblock_rectangles([horizontal, vertical, same_band])

        self.assertEqual(len(merged), 2)
        self.assertIn(vertical, merged)
        self.assertIn((0.0, 10.0, 30.0, 20.0), merged)

    def test_malformed_records_fail_with_context(self) -> None:
        records = FLOORPLAN_RECORDS.replace("SITE\tSLICE_X0Y0\tSLICEL\t10\t10", "SITE\tbroken")

        with self.assertRaisesRegex(XdbError, "invalid Vivado floorplan record"):
            parse_floorplan_records(records, "broken.dcp")

    def test_record_schema_and_placement_totals_are_validated(self) -> None:
        missing_schema = FLOORPLAN_RECORDS.replace(
            "META\tschema\txdb-floorplan-records-v1\n",
            "",
        )
        bad_total = FLOORPLAN_RECORDS.replace(
            "STAT\tplaced_cells\t13",
            "STAT\tplaced_cells\t99",
        )
        missing_stat = FLOORPLAN_RECORDS.replace("STAT\trouting_errors\t0\n", "")
        duplicate_metadata = FLOORPLAN_RECORDS.replace(
            "META\tdesign\tcyt_top\n",
            "META\tdesign\tcyt_top\nMETA\tdesign\tother\n",
        )

        with self.assertRaisesRegex(XdbError, "record schema"):
            parse_floorplan_records(missing_schema, "missing-schema.dcp")
        with self.assertRaisesRegex(XdbError, "occupancy accounts for 13 cells"):
            parse_floorplan_records(bad_total, "bad-total.dcp")
        with self.assertRaisesRegex(XdbError, "missing routing_errors"):
            parse_floorplan_records(missing_stat, "missing-stat.dcp")
        with self.assertRaisesRegex(XdbError, "duplicate metadata"):
            parse_floorplan_records(duplicate_metadata, "duplicate-metadata.dcp")

    def test_checkpoint_inspection_uses_vivado_data_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = self._write(Path(tmp), "shell_routed.dcp", b"dcp")

            def fake_vivado(tcl: str, args: list[str], *, timeout: int) -> SimpleNamespace:
                self.assertIn("report_route_status -return_string", tcl)
                self.assertIn("GRID_RANGES", tcl)
                self.assertEqual(args[0], str(checkpoint))
                self.assertEqual(args[2], "2")
                self.assertEqual(timeout, 17)
                Path(args[1]).write_text(FLOORPLAN_RECORDS, encoding="utf-8")
                return SimpleNamespace(stdout="Vivado banner")

            with patch("xdb.reports.floorplan._run_vivado_tcl", side_effect=fake_vivado):
                design = inspect_floorplan_checkpoint(
                    checkpoint,
                    hierarchy_depth=2,
                    timeout=17,
                )

        self.assertEqual(design.design, "cyt_top")
        self.assertEqual(design.stats["primitive_cells"], 15)

    def test_checkpoint_inspection_rejects_routing_errors(self) -> None:
        records = FLOORPLAN_RECORDS.replace(
            "STAT\trouting_errors\t0",
            "STAT\trouting_errors\t3",
        )
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = self._write(Path(tmp), "placed.dcp", b"dcp")

            def fake_vivado(_tcl: str, args: list[str], *, timeout: int) -> SimpleNamespace:
                del timeout
                Path(args[1]).write_text(records, encoding="utf-8")
                return SimpleNamespace(stdout="")

            with patch("xdb.reports.floorplan._run_vivado_tcl", side_effect=fake_vivado):
                with self.assertRaisesRegex(XdbError, "3 routing errors"):
                    inspect_floorplan_checkpoint(checkpoint)

    def test_svg_is_deterministic_valid_and_escapes_hierarchy_names(self) -> None:
        design = parse_floorplan_records(FLOORPLAN_RECORDS, "shell_routed.dcp")

        first, metadata = render_floorplan_svg(
            design,
            title="Placement <overview>",
            hierarchy_depth=1,
            checkpoint_sha256="a" * 64,
        )
        second, second_metadata = render_floorplan_svg(
            design,
            title="Placement <overview>",
            hierarchy_depth=1,
            checkpoint_sha256="a" * 64,
        )

        self.assertEqual(first, second)
        self.assertEqual(metadata, second_metadata)
        root = ET.fromstring(first)
        self.assertTrue(root.tag.endswith("svg"))
        self.assertIn("Placement &lt;overview&gt;", first)
        self.assertNotIn("module<&quot;", first)
        self.assertIn("module&lt;&amp;quot;", first)
        self.assertIn('id="device-resources"', first)
        self.assertIn('id="placed-hierarchies"', first)
        self.assertIn('id="pblocks"', first)
        self.assertEqual(metadata["unmapped_placed_cells"], 2)
        self.assertEqual(metadata["occupied_sites"], 3)
        self.assertEqual(metadata["mixed_hierarchy_sites"], 2)
        static = next(item for item in metadata["groups"] if item["name"] == "inst_static")
        tiny = next(item for item in metadata["groups"] if item["name"] == "tiny_shared_module")
        self.assertEqual(static["occupied_sites"], 1)
        self.assertEqual(tiny["occupied_sites"], 1)
        self.assertIn('data-hierarchy="tiny_shared_module"', first)

    def test_renderer_rejects_xml_control_characters_in_title(self) -> None:
        design = parse_floorplan_records(FLOORPLAN_RECORDS, "shell_routed.dcp")

        with self.assertRaisesRegex(XdbError, "invalid in XML"):
            render_floorplan_svg(
                design,
                title="bad\x01title",
                checkpoint_sha256="a" * 64,
            )

    def test_renderer_bounds_hierarchy_group_count(self) -> None:
        design = parse_floorplan_records(FLOORPLAN_RECORDS, "shell_routed.dcp")

        with self.assertRaisesRegex(XdbError, "produced 4 color groups"):
            render_floorplan_svg(
                design,
                hierarchy_depth=2,
                checkpoint_sha256="a" * 64,
                max_groups=3,
            )

    def test_svg_writer_honors_restrictive_umask(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "floorplan.svg"
            previous_umask = os.umask(0o077)
            try:
                _write_svg(output, "<svg/>", force=False)
            finally:
                os.umask(previous_umask)

            mode = stat.S_IMODE(output.stat().st_mode)

        self.assertEqual(mode, 0o600)

    def test_svg_writer_does_not_overwrite_a_racing_creator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "floorplan.svg"

            def racing_link(_source: str | bytes | Path, destination: str | bytes | Path) -> None:
                Path(destination).write_text("other process", encoding="utf-8")
                raise FileExistsError(destination)

            with patch("xdb.reports.floorplan.os.link", side_effect=racing_link):
                with self.assertRaisesRegex(XdbError, "output already exists"):
                    _write_svg(output, "<svg/>", force=False)

            self.assertEqual(output.read_text(encoding="utf-8"), "other process")
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

    def test_invalid_output_extension_fails_before_vivado_inspection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "checkpoints/shell_routed.dcp", b"dcp")
            with patch(
                "xdb.reports.floorplan.inspect_floorplan_checkpoint",
                side_effect=AssertionError("Vivado inspection started"),
            ):
                with self.assertRaisesRegex(XdbError, "must use the .svg extension"):
                    generate_floorplan_svg(root, output=root / "floorplan.png")

    def test_invalid_output_parent_fails_before_vivado_inspection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "checkpoints/shell_routed.dcp", b"dcp")
            parent_file = self._write(root, "not-a-directory", b"file")
            with patch(
                "xdb.reports.floorplan.inspect_floorplan_checkpoint",
                side_effect=AssertionError("Vivado inspection started"),
            ):
                with self.assertRaisesRegex(XdbError, "parent is not a directory"):
                    generate_floorplan_svg(
                        root,
                        output=parent_file / "floorplan.svg",
                    )

    def test_generate_writes_svg_and_refuses_implicit_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkpoint = self._write(root, "checkpoints/shell_routed.dcp", b"dcp")
            output = root / "figures" / "floorplan.svg"
            design = parse_floorplan_records(FLOORPLAN_RECORDS, checkpoint)

            with patch("xdb.reports.floorplan.inspect_floorplan_checkpoint", return_value=design):
                result = generate_floorplan_svg(root, output=output, hierarchy_depth=1)

            self.assertEqual(result["output"], str(output))
            ET.parse(output)
            with self.assertRaisesRegex(XdbError, "output already exists"):
                generate_floorplan_svg(root, output=output)

    def test_checkpoint_change_during_inspection_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkpoint = self._write(root, "checkpoints/shell_routed.dcp", b"dcp")
            output = root / "floorplan.svg"
            design = parse_floorplan_records(FLOORPLAN_RECORDS, checkpoint)

            with patch(
                "xdb.reports.floorplan.inspect_floorplan_checkpoint",
                return_value=design,
            ):
                with patch(
                    "xdb.reports.floorplan._sha256",
                    side_effect=["before", "after"],
                ):
                    with self.assertRaisesRegex(XdbError, "checkpoint changed"):
                        generate_floorplan_svg(root, output=output)

            self.assertFalse(output.exists())

    def test_cli_json_does_not_select_hardware_backend(self) -> None:
        result = {
            "schema": "xdb-floorplan-render-v1",
            "source": "shell_routed.dcp",
            "output": "floorplan.svg",
        }
        stdout = io.StringIO()
        argv = [
            "xdb",
            "reports",
            "floorplan",
            "result",
            "--out",
            "floorplan.svg",
            "--json",
        ]
        with patch.object(sys, "argv", argv):
            with patch("xdb.cli.generate_floorplan_svg", return_value=result) as generate:
                with patch(
                    "xdb.cli.select_backend", side_effect=AssertionError("backend selected")
                ):
                    with patch("sys.stdout", stdout):
                        main()

        self.assertEqual(json.loads(stdout.getvalue()), result)
        generate.assert_called_once_with(
            "result",
            output="floorplan.svg",
            dcp=None,
            hierarchy_depth=1,
            title=None,
            show_pblocks=True,
            max_groups=32,
            force=False,
            timeout=1800,
        )


if __name__ == "__main__":
    unittest.main()
