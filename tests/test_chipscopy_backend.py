from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import types
import unittest
from enum import Enum
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xdb.backend.chipscopy_backend import ChipScoPyBackend
from xdb.errors import XdbError


class FakeTriggerCondition(Enum):
    AND = 1
    OR = 2
    NAND = 3
    NOR = 4


class FakeCaptureCondition(Enum):
    ALWAYS = 0
    AND = 1
    OR = 2
    NAND = 3
    NOR = 4


class FakeTrigInMode(Enum):
    DISABLED = 0
    TRIG_IN_ONLY = 1
    TRIGGER_OR_TRIG_IN = 2


class FakeTrigOutMode(Enum):
    DISABLED = 0
    TRIGGER_ONLY = 1
    TRIG_IN_ONLY = 2
    TRIGGER_OR_TRIG_IN = 3


FAKE_TRIGGER_ENUMS = {
    "ILATriggerCondition": FakeTriggerCondition,
    "ILACaptureCondition": FakeCaptureCondition,
    "ILATrigInMode": FakeTrigInMode,
    "ILATrigOutMode": FakeTrigOutMode,
}


class FakeProbe:
    def __init__(self, name: str, width: int) -> None:
        self.name = name
        self.bit_width = width


class FakeWaveform:
    window_size = 256
    trigger_position = [32, 32]

    def __init__(self) -> None:
        self.last_export: tuple[str, str, dict[str, object]] | None = None

    def get_window_count(self) -> int:
        return len(self.trigger_position)

    def export_waveform(self, output_format: str, path: str, **kwargs: object) -> None:
        self.last_export = (output_format, path, kwargs)
        if output_format not in {"CSV", "VCD", "CITF"}:
            raise AssertionError(output_format)
        Path(path).write_text(f"{output_format} waveform\n", encoding="utf-8")


class FakeIla:
    def __init__(self, name: str) -> None:
        self.name = name
        self.static_info = types.SimpleNamespace(data_depth=4096)
        self.probes = {"probe0": FakeProbe("probe0", 32)}
        self.waveform = FakeWaveform()
        self.trigger_args: dict[str, int] | None = None
        self.basic_trigger_args: dict[str, int] | None = None
        self.trigger_values: dict[str, list[str | int]] = {}
        self.capture_values: dict[str, list[str | int]] = {}
        self.advanced_trigger_args: tuple[str, dict[str, object]] | None = None
        self.wait_minutes: float | None = None
        self.status = types.SimpleNamespace(
            capture_state="idle",
            is_armed=False,
            is_full=False,
            samples_captured=0,
            windows_captured=0,
        )

    def reset_probes(self) -> None:
        pass

    def run_trigger_immediately(self, **kwargs: int) -> None:
        self.trigger_args = kwargs
        self.status = types.SimpleNamespace(
            capture_state="pre_trigger",
            is_armed=True,
            is_full=False,
            samples_captured=0,
            windows_captured=0,
        )

    def set_probe_trigger_value(self, probe: str, values: list[str | int]) -> None:
        self.trigger_values[probe] = values

    def set_probe_capture_value(self, probe: str, values: list[str | int]) -> None:
        self.capture_values[probe] = values

    def run_basic_trigger(self, **kwargs: object) -> None:
        self.basic_trigger_args = kwargs
        self.status = types.SimpleNamespace(
            capture_state="pre_trigger",
            is_armed=True,
            is_full=False,
            samples_captured=0,
            windows_captured=0,
        )

    def refresh_status(self) -> None:
        pass

    def run_advanced_trigger(self, tsm_path: str, *, compile_only: bool = False, **kwargs: object):
        self.advanced_trigger_args = (tsm_path, kwargs)
        if compile_only:
            return 0, ""
        self.status = types.SimpleNamespace(
            capture_state="pre_trigger",
            is_armed=True,
            is_full=False,
            samples_captured=0,
            windows_captured=0,
        )
        return 0, ""

    def wait_till_done(self, *, max_wait_minutes: float):
        self.wait_minutes = max_wait_minutes
        self.status = types.SimpleNamespace(
            capture_state="idle",
            is_armed=False,
            is_full=True,
            samples_captured=256,
            windows_captured=2,
        )
        return self.status

    def upload(self) -> bool:
        return True


class FakeIlaCollection:
    def __init__(self, ilas: list[FakeIla]) -> None:
        self._ilas = ilas

    def __iter__(self):
        return iter(self._ilas)

    def get(self, *, name: str):
        return next((ila for ila in self._ilas if ila.name == name), None)


class FakeDevice:
    family_name = "versal"

    def __init__(self, part: str, serial: str) -> None:
        self.part_name = part
        self.serial = serial
        self.ila_cores = FakeIlaCollection([FakeIla("ila0")])
        self.programmed: str | None = None
        self.discovered_ltx: str | None = None

    def __str__(self) -> str:
        return f"device:{self.serial}"

    def to_dict(self) -> dict[str, str]:
        return {
            "cable_name": "rose-cable",
            "part": self.part_name,
            "dna": self.serial,
            "serial": self.serial,
        }

    def program(self, path: str) -> None:
        self.programmed = path

    def discover_and_setup_cores(self, ltx_file: str | None = None) -> None:
        self.discovered_ltx = ltx_file


class FakeSession:
    def __init__(self, devices: list[FakeDevice]) -> None:
        self.devices = devices


class ChipScoPyBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.device = FakeDevice("xcv80-lsva4737-2MHP-e-S", "XFL1EZVSAG4SA")
        self.session = FakeSession([self.device])
        self.created: list[dict[str, str]] = []
        self.deleted: list[FakeSession] = []

        module = types.ModuleType("chipscopy")

        def create_session(**kwargs: str) -> FakeSession:
            self.created.append(kwargs)
            return self.session

        def delete_session(session: FakeSession) -> None:
            self.deleted.append(session)

        module.create_session = create_session  # type: ignore[attr-defined]
        module.delete_session = delete_session  # type: ignore[attr-defined]
        self.module_patch = patch.dict(sys.modules, {"chipscopy": module})
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)

    def test_persistent_backend_reuses_session_until_explicit_close(self) -> None:
        backend = ChipScoPyBackend(persistent_session=self.session)
        with patch.dict(os.environ, {}, clear=True):
            first = backend.list_targets("xcv80")
            second = backend.list_targets("xcv80")
            backend.close()

        self.assertEqual(len(first["targets"]), 1)
        self.assertEqual(len(second["targets"]), 1)
        self.assertEqual(self.created, [])
        self.assertEqual(self.deleted, [self.session])

    def test_targets_records_server_backend_and_closes_session(self) -> None:
        with patch.dict(
            os.environ,
            {"HW_SERVER_URL": "TCP:rose:3122", "CS_SERVER_URL": "TCP:rose:3042"},
            clear=True,
        ):
            result = ChipScoPyBackend().list_targets("xcv80")

        self.assertEqual(self.created, [{"hw_server_url": "TCP:rose:3122"}])
        self.assertEqual(self.deleted, [self.session])
        self.assertEqual(result["targets"][0]["part"], self.device.part_name)
        self.assertEqual(result["provenance"]["backend"], "chipscopy")
        self.assertEqual(result["provenance"]["device_family"], "versal")
        self.assertEqual(result["provenance"]["hw_server_url"], "TCP:rose:3122")
        self.assertIsNone(result["provenance"]["cs_server_url"])

    def test_program_needs_no_ltx_and_hashes_programmed_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pdi = Path(td) / "design.pdi"
            pdi.write_bytes(b"versal-pdi")
            with patch.dict(
                os.environ,
                {
                    "HW_SERVER_URL": "TCP:rose:3122",
                    "FPGA_JTAG_TARGET": self.device.serial,
                },
                clear=True,
            ):
                result = ChipScoPyBackend().program(str(pdi), None, "xcv80")

        self.assertEqual(self.device.programmed, str(pdi))
        self.assertEqual(result["ltx"], None)
        self.assertEqual(result["bitstream_sha256"], hashlib.sha256(b"versal-pdi").hexdigest())
        self.assertEqual(result["provenance"]["selected_part"], self.device.part_name)
        self.assertEqual(result["provenance"]["selected_target"], result["target"])
        self.assertEqual(self.deleted, [self.session])

    def test_ambiguous_part_match_fails_closed_and_closes_session(self) -> None:
        self.session.devices.append(FakeDevice(self.device.part_name, "SECOND-V80"))
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(XdbError, "ambiguous Versal target"):
                ChipScoPyBackend().program("unused.pdi", None, "xcv80")
        self.assertEqual(self.deleted, [self.session])

    def test_explicit_jtag_target_disambiguates_matching_parts(self) -> None:
        second = FakeDevice(self.device.part_name, "SECOND-V80")
        self.session.devices.append(second)
        with tempfile.TemporaryDirectory() as td:
            pdi = Path(td) / "design.pdi"
            pdi.write_bytes(b"pdi")
            with patch.dict(os.environ, {"FPGA_JTAG_TARGET": second.serial}, clear=True):
                ChipScoPyBackend().program(str(pdi), None, "xcv80")
        self.assertIsNone(self.device.programmed)
        self.assertEqual(second.programmed, str(pdi))

    def test_ila_listing_uses_cs_server_and_explicit_ltx(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ltx = Path(td) / "debug.ltx"
            ltx.write_text("probes", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "HW_SERVER_URL": "TCP:rose:3122",
                    "CS_SERVER_URL": "TCP:rose:3042",
                },
                clear=True,
            ):
                result = ChipScoPyBackend().list_ilas("xcv80", ltx=str(ltx))

        self.assertEqual(
            self.created,
            [{"hw_server_url": "TCP:rose:3122", "cs_server_url": "TCP:rose:3042"}],
        )
        self.assertEqual(self.device.discovered_ltx, str(ltx))
        self.assertEqual(
            result["ilas"], [{"name": "ila0", "probes": [{"name": "probe0", "width": 32}]}]
        )
        self.assertEqual(result["provenance"]["cs_server_url"], "TCP:rose:3042")
        self.assertEqual(self.deleted, [self.session])

    def test_decoupled_ila_lifecycle_arms_checks_waits_and_uploads(self) -> None:
        backend = ChipScoPyBackend()
        with tempfile.TemporaryDirectory() as td:
            csv = Path(td) / "uploaded.csv"
            with patch.dict(os.environ, {}, clear=True):
                armed = backend.arm_ila("xcv80", "ila0", 256, windows=2, trigger_position=32)
                status = backend.ila_status("xcv80", "ila0")
                waited = backend.wait_ila("xcv80", "ila0", timeout=60)
                uploaded = backend.upload_ila("xcv80", "ila0", str(csv))
            self.assertTrue(csv.is_file())

        self.assertTrue(armed["status"]["is_armed"])
        self.assertEqual(armed["windows"], 2)
        self.assertTrue(status["status"]["is_armed"])
        self.assertTrue(waited["status"]["is_full"])
        self.assertEqual(uploaded["samples"], 256)
        self.assertEqual(uploaded["windows"], 2)
        self.assertEqual(uploaded["total_samples"], 512)
        self.assertEqual(uploaded["trigger_position"], 32)
        self.assertEqual(len(self.created), 4)
        self.assertEqual(self.deleted, [self.session] * 4)

    def test_waveform_upload_exports_selected_windows_and_writes_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "capture.vcd"
            with patch.dict(os.environ, {}, clear=True):
                result = ChipScoPyBackend().upload_ila(
                    "xcv80",
                    "ila0",
                    str(output),
                    export_format="VCD",
                    probe_names=["state"],
                    start_window=1,
                    window_count=1,
                    start_sample=4,
                    sample_count=16,
                    include_gap=True,
                )
            manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))

        waveform = next(iter(self.device.ila_cores)).waveform
        assert waveform is not None and waveform.last_export is not None
        self.assertEqual(waveform.last_export[0], "VCD")
        self.assertEqual(waveform.last_export[2]["probe_names"], ["state"])
        self.assertEqual(waveform.last_export[2]["start_window_idx"], 1)
        self.assertEqual(waveform.last_export[2]["sample_count"], 16)
        self.assertEqual(result["export_format"], "VCD")
        self.assertNotIn("csv", result)
        self.assertEqual(manifest["schema"], "xdb.ila-waveform/v1")
        self.assertEqual(manifest["output_sha256"], result["output_sha256"])
        self.assertEqual(manifest["selection"]["include_gap"], True)

    def test_capture_exports_csv_and_closes_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            csv = Path(td) / "capture.csv"
            with patch.dict(os.environ, {}, clear=True):
                result = ChipScoPyBackend().capture("xcv80", "ila0", str(csv), 1024, timeout=120)
            self.assertTrue(csv.is_file())

        ila = next(iter(self.device.ila_cores))
        self.assertEqual(
            ila.trigger_args, {"trigger_position": 512, "window_count": 1, "window_size": 1024}
        )
        self.assertEqual(ila.wait_minutes, 2.0)
        self.assertEqual(result["samples"], 1024)
        self.assertEqual(result["windows"], 1)
        self.assertEqual(result["total_samples"], 1024)
        self.assertEqual(result["trigger_position"], 512)
        self.assertEqual(result["triggers"], [])
        self.assertEqual(self.deleted, [self.session])

    def test_advanced_trigger_arms_tsm_with_capture_qualifiers_and_trigger_io(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tsm = Path(td) / "trigger.tsm"
            tsm.write_text("state s0:\n  trigger;\n", encoding="utf-8")
            with (
                patch.dict(os.environ, {}, clear=True),
                patch.object(
                    ChipScoPyBackend,
                    "_chipscopy_trigger_enums",
                    return_value=FAKE_TRIGGER_ENUMS,
                ),
            ):
                result = ChipScoPyBackend().arm_ila(
                    "xcv80",
                    "ila0",
                    256,
                    advanced_trigger={
                        "tsm_path": str(tsm),
                        "capture_condition": "or",
                        "capture_values": [{"probe": "valid", "operator": "==", "value": 1}],
                        "trig_in": "trigger_or_trig_in",
                        "trig_out": "trigger_only",
                    },
                )

        ila = next(iter(self.device.ila_cores))
        self.assertEqual(ila.capture_values["valid"], ["==", 1])
        self.assertIsNotNone(ila.advanced_trigger_args)
        assert ila.advanced_trigger_args is not None
        self.assertEqual(ila.advanced_trigger_args[0], str(tsm))
        kwargs = ila.advanced_trigger_args[1]
        self.assertEqual(kwargs["capture_condition"], FakeCaptureCondition.OR)
        self.assertEqual(kwargs["trig_in"], FakeTrigInMode.TRIGGER_OR_TRIG_IN)
        self.assertEqual(kwargs["trig_out"], FakeTrigOutMode.TRIGGER_ONLY)
        self.assertTrue(result["status"]["is_armed"])

    def test_trigger_state_machine_can_be_compiled_without_arming(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tsm = Path(td) / "trigger.tsm"
            tsm.write_text("state s0:\n  trigger;\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                result = ChipScoPyBackend().compile_ila_trigger("xcv80", "ila0", str(tsm))
        self.assertEqual(result["error_count"], 0)
        self.assertEqual(result["messages"], "")
        ila = next(iter(self.device.ila_cores))
        self.assertEqual(ila.advanced_trigger_args, (str(tsm), {}))

    def test_capture_rejects_request_larger_than_ila_depth(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(XdbError, "ILA depth is 4096"):
                    ChipScoPyBackend().capture(
                        "xcv80", "ila0", str(Path(td) / "capture.csv"), 2048, windows=4
                    )
        self.assertEqual(self.deleted, [self.session])

    def test_triggered_multi_window_capture_groups_probe_comparisons(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            csv = Path(td) / "capture.csv"
            with patch.dict(os.environ, {}, clear=True):
                result = ChipScoPyBackend().capture(
                    "xcv80",
                    "ila0",
                    str(csv),
                    256,
                    windows=4,
                    trigger_position=32,
                    triggers=[
                        {"probe": "state", "operator": "==", "value": 3},
                        {"probe": "state", "operator": "<=", "value": 10},
                        {"probe": "valid", "operator": "==", "value": "1"},
                    ],
                )

        ila = next(iter(self.device.ila_cores))
        self.assertIsNone(ila.trigger_args)
        self.assertEqual(
            ila.basic_trigger_args,
            {"trigger_position": 32, "window_count": 4, "window_size": 256},
        )
        self.assertEqual(ila.trigger_values["state"], ["==", 3, "<=", 10])
        self.assertEqual(ila.trigger_values["valid"], ["==", "1"])
        self.assertEqual(result["windows"], 4)
        self.assertEqual(result["total_samples"], 1024)
        self.assertEqual(result["trigger_position"], 32)
        self.assertEqual(len(result["triggers"]), 3)
        self.assertEqual(self.deleted, [self.session])


if __name__ == "__main__":
    unittest.main()
