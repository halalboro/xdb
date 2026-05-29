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
from xdb.vivado_ip import format_vivado_ip_info, parse_xci, vivado_ip_info


JSON_XCI = """{
  "schema": "xilinx.com:schema:json_instance:1.0",
  "ip_inst": {
    "xci_name": "axis_register_slice_meta_8",
    "component_reference": "xilinx.com:ip:axis_register_slice:1.1",
    "ip_revision": "27",
    "gen_directory": "../../gen/ip/axis_register_slice_meta_8",
    "parameters": {
      "component_parameters": {
        "TDATA_NUM_BYTES": [ { "value": "1", "value_src": "user", "resolve_type": "user", "format": "long" } ],
        "HAS_TLAST": [ { "value": "0", "resolve_type": "user", "format": "long" } ],
        "Component_Name": [ { "value": "axis_register_slice_meta_8", "resolve_type": "user" } ]
      },
      "project_parameters": {
        "DEVICE": [ { "value": "xcu280" } ],
        "PACKAGE": [ { "value": "fsvh2892" } ],
        "SPEEDGRADE": [ { "value": "-2L" } ]
      },
      "runtime_parameters": {
        "SWVERSION": [ { "value": "2022.2" } ]
      }
    },
    "boundary": {
      "ports": {
        "aclk": [ { "direction": "in" } ],
        "s_axis_tdata": [ { "direction": "in", "size_left": "7", "size_right": "0" } ],
        "m_axis_tdata": [ { "direction": "out", "size_left": "7", "size_right": "0" } ]
      },
      "interfaces": {
        "S_AXIS": { "vlnv": "xilinx.com:interface:axis:1.0", "mode": "slave" }
      }
    }
  }
}
"""

XML_XCI = """<?xml version="1.0"?>
<spirit:component xmlns:spirit="http://www.spiritconsortium.org/XMLSchema/SPIRIT/1685-2009">
  <spirit:vendor>xilinx.com</spirit:vendor>
  <spirit:library>ip</spirit:library>
  <spirit:name>fifo_generator</spirit:name>
  <spirit:version>13.2</spirit:version>
  <spirit:configurableElementValues>
    <spirit:configurableElementValue spirit:referenceId="PARAM_VALUE.Component_Name">fifo_0</spirit:configurableElementValue>
    <spirit:configurableElementValue spirit:referenceId="PARAM_VALUE.Input_Depth">1024</spirit:configurableElementValue>
  </spirit:configurableElementValues>
  <spirit:model>
    <spirit:ports>
      <spirit:port><spirit:name>clk</spirit:name><spirit:wire><spirit:direction>in</spirit:direction></spirit:wire></spirit:port>
    </spirit:ports>
  </spirit:model>
</spirit:component>
"""


class VivadoIpTests(unittest.TestCase):
    def test_parse_json_xci_metadata_parameters_and_ports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "axis_register_slice_meta_8.xci"
            path.write_text(JSON_XCI, encoding="utf-8")
            parsed = parse_xci(path)

        self.assertEqual(parsed["name"], "axis_register_slice_meta_8")
        self.assertEqual(parsed["vlnv"], "xilinx.com:ip:axis_register_slice:1.1")
        self.assertEqual(parsed["part"], "xcu280-fsvh2892-2L")
        self.assertEqual(parsed["sw_version"], "2022.2")
        self.assertEqual(parsed["parameters"]["component"]["TDATA_NUM_BYTES"]["value"], "1")
        self.assertEqual(parsed["ports"][2]["width"], 8)
        self.assertEqual(parsed["interfaces"][0]["name"], "S_AXIS")

    def test_parse_xml_xci_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fifo_0.xci"
            path.write_text(XML_XCI, encoding="utf-8")
            parsed = parse_xci(path)

        self.assertEqual(parsed["name"], "fifo_0")
        self.assertEqual(parsed["vlnv"], "xilinx.com:ip:fifo_generator:13.2")
        self.assertEqual(parsed["parameters"]["component"]["Input_Depth"]["value"], "1024")
        self.assertEqual(parsed["ports"][0]["name"], "clk")

    def test_vivado_ip_info_filters_parameters_by_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ip.xci"
            path.write_text(JSON_XCI, encoding="utf-8")
            info = vivado_ip_info(path, param_patterns=["*TLAST"])

        params = info["ips"][0]["parameters"]
        self.assertIn("HAS_TLAST", params["component"])
        self.assertNotIn("TDATA_NUM_BYTES", params["component"])

    def test_vivado_ip_info_discovers_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a" / "ip_a").mkdir(parents=True)
            (root / "b").mkdir()
            (root / "a" / "ip_a" / "ip_a.xci").write_text(JSON_XCI, encoding="utf-8")
            (root / "b" / "fifo_0.xci").write_text(XML_XCI, encoding="utf-8")
            info = vivado_ip_info(root)

        self.assertEqual(info["count"], 2)
        self.assertEqual([ip["name"] for ip in info["ips"]], ["axis_register_slice_meta_8", "fifo_0"])

    def test_format_vivado_ip_info_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ip.xci"
            path.write_text(JSON_XCI, encoding="utf-8")
            text = format_vivado_ip_info(vivado_ip_info(path))

        self.assertIn("ip: axis_register_slice_meta_8", text)
        self.assertIn("vlnv: xilinx.com:ip:axis_register_slice:1.1", text)
        self.assertIn("TDATA_NUM_BYTES: 1", text)
        self.assertIn("ports (3):", text)
        self.assertIn("s_axis_tdata", text)
        self.assertIn("interfaces (1):", text)

    def test_cli_ip_info_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ip.xci"
            path.write_text(JSON_XCI, encoding="utf-8")
            stdout = io.StringIO()
            with patch.object(sys, "argv", ["xdb", "vivado", "ip-info", "--json", str(path)]):
                with patch("sys.stdout", stdout):
                    main()

        result = json.loads(stdout.getvalue())
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["ips"][0]["name"], "axis_register_slice_meta_8")


if __name__ == "__main__":
    unittest.main()
