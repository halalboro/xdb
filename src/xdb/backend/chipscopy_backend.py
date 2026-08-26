from __future__ import annotations

import hashlib
import importlib
from dataclasses import fields, is_dataclass
from enum import Enum
import os
from pathlib import Path
from typing import Any, cast

from xdb.errors import XdbError
from xdb.backend.base import (
    Capability,
    CaptureResult,
    DebugProvenance,
    IlaArmResult,
    IlaStatusResult,
    InstrumentsResult,
    ListIlasResult,
    ProbeTrigger,
    ProgramResult,
    TargetsResult,
)


class ChipScoPyBackend:
    name = "chipscopy"

    def capabilities(self) -> set[Capability]:
        return {
            Capability.TARGETS,
            Capability.PROGRAM,
            Capability.ILA_LIST,
            Capability.ILA_CONTROL,
            Capability.ILA_BASIC_CAPTURE,
            Capability.ILA_BASIC_TRIGGER,
            Capability.ILA_CAPTURE_POSITION,
            Capability.ILA_MULTI_WINDOW_CAPTURE,
            Capability.INSTRUMENTS_LIST,
        }

    def list_targets(self, part_hint: str | None, timeout: int = 120) -> TargetsResult:
        del timeout  # not currently plumbed through ChipScoPy APIs
        session = self._create_session(require_cs=False)
        try:
            devices = self._get_versal_devices(session)
            out: dict[str, Any] = {
                "targets": [
                    {
                        "target": self._target_name(d),
                        "part": str(getattr(d, "part_name", "")),
                    }
                    for d in devices
                ]
            }
            if part_hint:
                ph = part_hint.lower()
                out["targets"] = [t for t in out["targets"] if ph in str(t.get("part", "")).lower()]
            out["provenance"] = self._provenance(require_cs=False)
            return cast(TargetsResult, out)
        finally:
            self._delete_session(session)

    def program(
        self, bit: str, ltx: str | None, part_hint: str, timeout: int = 300
    ) -> ProgramResult:
        del ltx, timeout  # program uses bit/pdi; ltx used for core discovery later
        session = self._create_session(require_cs=False)
        try:
            dev = self._select_device(session, part_hint)
            dev.program(bit)
            target = self._target_name(dev)
            part = str(getattr(dev, "part_name", ""))
            return {
                "ok": True,
                "target": target,
                "part": part,
                "bitstream": bit,
                "bitstream_sha256": self._sha256_file(bit),
                "ltx": None,
                "provenance": self._provenance(
                    require_cs=False,
                    selected_target=target,
                    selected_part=part,
                ),
            }
        finally:
            self._delete_session(session)

    def list_ilas(
        self,
        part_hint: str,
        timeout: int = 180,
        *,
        ltx: str | None = None,
    ) -> ListIlasResult:
        del timeout
        session = self._create_session(require_cs=True)
        try:
            dev = self._select_device(session, part_hint)
            resolved_ltx = self._ltx_from_env(ltx)
            if resolved_ltx:
                dev.discover_and_setup_cores(ltx_file=resolved_ltx)
            else:
                dev.discover_and_setup_cores()

            ilas_out = []
            for ila in dev.ila_cores:
                probes = []
                for p in ila.probes.values():
                    bit_width = getattr(p, "bit_width", None)
                    probes.append(
                        {"name": p.name, "width": None if bit_width is None else int(bit_width)}
                    )
                ilas_out.append({"name": ila.name, "probes": probes})

            target = self._target_name(dev)
            part = str(getattr(dev, "part_name", ""))
            return cast(
                ListIlasResult,
                {
                    "target": target,
                    "part": part,
                    "ilas": ilas_out,
                    "provenance": self._provenance(
                        require_cs=True,
                        selected_target=target,
                        selected_part=part,
                    ),
                },
            )
        finally:
            self._delete_session(session)

    def list_instruments(self, part_hint: str, timeout: int = 180) -> InstrumentsResult:
        ilas = self.list_ilas(part_hint, timeout=timeout)
        instruments = [
            {
                "type": "ila",
                "name": ila.get("name", ""),
                "capabilities": [
                    Capability.ILA_LIST.value,
                    Capability.ILA_BASIC_CAPTURE.value,
                    Capability.ILA_BASIC_TRIGGER.value,
                    Capability.ILA_CAPTURE_POSITION.value,
                    Capability.ILA_MULTI_WINDOW_CAPTURE.value,
                ],
            }
            for ila in ilas.get("ilas", [])
        ]
        return cast(
            InstrumentsResult,
            {
                "target": ilas.get("target", ""),
                "part": ilas.get("part", ""),
                "instruments": instruments,
                "provenance": ilas.get("provenance", self._provenance(require_cs=True)),
            },
        )

    def arm_ila(
        self,
        part_hint: str,
        ila_name: str,
        samples: int,
        timeout: int = 120,
        *,
        ltx: str | None = None,
        windows: int = 1,
        trigger_position: int | None = None,
        triggers: list[ProbeTrigger] | None = None,
    ) -> IlaArmResult:
        del timeout
        session = self._create_session(require_cs=True)
        try:
            dev, ila = self._select_ila(session, part_hint, ila_name, ltx)
            effective_position, normalized_triggers = self._arm_capture(
                ila, samples, windows, trigger_position, triggers
            )
            ila.refresh_status()
            target = self._target_name(dev)
            part = str(getattr(dev, "part_name", ""))
            return {
                "target": target,
                "part": part,
                "ila": ila_name,
                "status": self._status_dict(ila.status),
                "samples": samples,
                "windows": windows,
                "trigger_position": effective_position,
                "triggers": normalized_triggers,
                "provenance": self._provenance(
                    require_cs=True, selected_target=target, selected_part=part
                ),
            }
        finally:
            self._delete_session(session)

    def ila_status(
        self,
        part_hint: str,
        ila_name: str,
        timeout: int = 120,
        *,
        ltx: str | None = None,
    ) -> IlaStatusResult:
        del timeout
        session = self._create_session(require_cs=True)
        try:
            dev, ila = self._select_ila(session, part_hint, ila_name, ltx)
            ila.refresh_status()
            return self._ila_status_result(dev, ila, ila_name)
        finally:
            self._delete_session(session)

    def wait_ila(
        self,
        part_hint: str,
        ila_name: str,
        timeout: int = 120,
        *,
        ltx: str | None = None,
    ) -> IlaStatusResult:
        session = self._create_session(require_cs=True)
        try:
            dev, ila = self._select_ila(session, part_hint, ila_name, ltx)
            status = ila.wait_till_done(max_wait_minutes=max(timeout, 1) / 60.0)
            return self._ila_status_result(dev, ila, ila_name, status=status)
        finally:
            self._delete_session(session)

    def upload_ila(
        self,
        part_hint: str,
        ila_name: str,
        csv_path: str,
        timeout: int = 120,
        *,
        ltx: str | None = None,
    ) -> CaptureResult:
        del timeout
        session = self._create_session(require_cs=True)
        try:
            dev, ila = self._select_ila(session, part_hint, ila_name, ltx)
            if not ila.upload() or ila.waveform is None:
                raise XdbError("ILA has no completed waveform to upload")
            out_path = str(Path(csv_path))
            ila.waveform.export_waveform("CSV", out_path)
            window_size = int(getattr(ila.waveform, "window_size", 0))
            window_count = int(ila.waveform.get_window_count())
            trigger_positions = list(getattr(ila.waveform, "trigger_position", []))
            trigger_position = int(trigger_positions[0]) if trigger_positions else 0
            return self._capture_result(
                dev,
                ila_name,
                out_path,
                window_size,
                window_count,
                trigger_position,
                [],
            )
        finally:
            self._delete_session(session)

    def capture(
        self,
        part_hint: str,
        ila_name: str,
        csv_path: str,
        samples: int,
        timeout: int = 120,
        *,
        ltx: str | None = None,
        windows: int = 1,
        trigger_position: int | None = None,
        triggers: list[ProbeTrigger] | None = None,
    ) -> CaptureResult:
        session = self._create_session(require_cs=True)
        try:
            dev, ila = self._select_ila(session, part_hint, ila_name, ltx)
            effective_position, normalized_triggers = self._arm_capture(
                ila, samples, windows, trigger_position, triggers
            )
            ila.wait_till_done(max_wait_minutes=max(timeout, 1) / 60.0)
            if not ila.upload() or ila.waveform is None:
                raise XdbError("failed to upload ILA data")
            out_path = str(Path(csv_path))
            ila.waveform.export_waveform("CSV", out_path)
            return self._capture_result(
                dev,
                ila_name,
                out_path,
                samples,
                windows,
                effective_position,
                normalized_triggers,
            )
        finally:
            self._delete_session(session)

    def _select_ila(self, session, part_hint: str, ila_name: str, ltx: str | None):
        dev = self._select_device(session, part_hint)
        resolved_ltx = self._ltx_from_env(ltx)
        if resolved_ltx:
            dev.discover_and_setup_cores(ltx_file=resolved_ltx)
        else:
            dev.discover_and_setup_cores()
        ila = dev.ila_cores.get(name=ila_name)
        if not ila:
            raise XdbError(f"ILA not found: {ila_name}")
        return dev, ila

    @staticmethod
    def _arm_capture(
        ila,
        samples: int,
        windows: int,
        trigger_position: int | None,
        triggers: list[ProbeTrigger] | None,
    ) -> tuple[int, list[ProbeTrigger]]:
        if samples <= 0 or samples & (samples - 1):
            raise XdbError("samples per window must be a positive power of two")
        if windows <= 0:
            raise XdbError("window count must be positive")
        effective_position = samples // 2 if trigger_position is None else trigger_position
        if not 0 <= effective_position < samples:
            raise XdbError(f"trigger position must be between 0 and {samples - 1}")
        data_depth = int(getattr(getattr(ila, "static_info", None), "data_depth", 0))
        if data_depth and samples * windows > data_depth:
            raise XdbError(
                f"capture requests {samples * windows} samples but ILA depth is {data_depth}"
            )
        normalized_triggers = list(triggers or [])
        ila.reset_probes()
        if normalized_triggers:
            trigger_values: dict[str, list[str | int]] = {}
            for trigger in normalized_triggers:
                trigger_values.setdefault(trigger["probe"], []).extend(
                    [trigger["operator"], trigger["value"]]
                )
            for probe, values in trigger_values.items():
                ila.set_probe_trigger_value(probe, values)
            ila.run_basic_trigger(
                trigger_position=effective_position,
                window_count=windows,
                window_size=samples,
            )
        else:
            ila.run_trigger_immediately(
                trigger_position=effective_position,
                window_count=windows,
                window_size=samples,
            )
        return effective_position, normalized_triggers

    def _ila_status_result(self, dev, ila, ila_name: str, *, status=None) -> IlaStatusResult:
        target = self._target_name(dev)
        part = str(getattr(dev, "part_name", ""))
        return {
            "target": target,
            "part": part,
            "ila": ila_name,
            "status": self._status_dict(ila.status if status is None else status),
            "provenance": self._provenance(
                require_cs=True, selected_target=target, selected_part=part
            ),
        }

    def _capture_result(
        self,
        dev,
        ila_name: str,
        csv_path: str,
        samples: int,
        windows: int,
        trigger_position: int,
        triggers: list[ProbeTrigger],
    ) -> CaptureResult:
        target = self._target_name(dev)
        part = str(getattr(dev, "part_name", ""))
        return {
            "ok": True,
            "target": target,
            "part": part,
            "ila": ila_name,
            "csv": csv_path,
            "samples": samples,
            "windows": windows,
            "total_samples": samples * windows,
            "trigger_position": trigger_position,
            "triggers": triggers,
            "provenance": self._provenance(
                require_cs=True, selected_target=target, selected_part=part
            ),
        }

    @classmethod
    def _status_dict(cls, value) -> dict[str, object]:
        normalized = cls._normalize_value(value)
        if not isinstance(normalized, dict):
            raise XdbError(f"unexpected ILA status type: {type(value).__name__}")
        return normalized

    @classmethod
    def _normalize_value(cls, value):
        if isinstance(value, Enum):
            return value.name.lower()
        if is_dataclass(value) and not isinstance(value, type):
            return {
                field.name: cls._normalize_value(getattr(value, field.name))
                for field in fields(value)
            }
        if isinstance(value, dict):
            return {str(key): cls._normalize_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._normalize_value(item) for item in value]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if hasattr(value, "__dict__"):
            return {
                str(key): cls._normalize_value(item)
                for key, item in vars(value).items()
                if not str(key).startswith("_")
            }
        return str(value)

    def _create_session(self, require_cs: bool):
        chipscopy = self._chipscopy_imports()
        provenance = self._provenance(require_cs=require_cs)
        if require_cs:
            return chipscopy["create_session"](
                hw_server_url=provenance["hw_server_url"],
                cs_server_url=provenance["cs_server_url"],
            )
        return chipscopy["create_session"](hw_server_url=provenance["hw_server_url"])

    def _provenance(
        self,
        *,
        require_cs: bool,
        selected_target: str | None = None,
        selected_part: str | None = None,
    ) -> DebugProvenance:
        out: DebugProvenance = {
            "backend": self.name,
            "device_family": "versal",
            "hw_server_url": os.environ.get("HW_SERVER_URL", "TCP:localhost:3121"),
            "cs_server_url": (
                os.environ.get("CS_SERVER_URL", "TCP:localhost:3042") if require_cs else None
            ),
        }
        if selected_target is not None:
            out["selected_target"] = selected_target
        if selected_part is not None:
            out["selected_part"] = selected_part
        return out

    @staticmethod
    def _delete_session(session) -> None:
        try:
            chipscopy = importlib.import_module("chipscopy")
            delete_session = getattr(chipscopy, "delete_session")
            delete_session(session)
        except Exception:
            pass

    @staticmethod
    def _chipscopy_imports() -> dict[str, Any]:
        try:
            chipscopy = importlib.import_module("chipscopy")
            create_session = getattr(chipscopy, "create_session")
        except Exception as e:
            raise XdbError(
                "chipscopy backend requested but chipscopy is not available. "
                "Install xdb with the 'versal' extra."
            ) from e
        return {"create_session": create_session}

    @staticmethod
    def _ltx_from_env(override: str | None = None) -> str | None:
        ltx = override or os.environ.get("FPGA_LTX")
        if ltx and os.path.isfile(ltx):
            return ltx
        return None

    @staticmethod
    def _get_versal_devices(session):
        devices = [
            d for d in session.devices if str(getattr(d, "family_name", "")).lower() == "versal"
        ]
        return devices

    def _select_device(self, session, part_hint: str):
        part_hint_l = part_hint.lower()
        versal_devices = self._get_versal_devices(session)
        candidates = [
            d for d in versal_devices if part_hint_l in str(getattr(d, "part_name", "")).lower()
        ]

        if not versal_devices:
            raise XdbError("no Versal devices found via chipscopy")
        if not candidates:
            raise XdbError(f"no Versal target matching part hint {part_hint}")

        target_hint = (os.environ.get("FPGA_JTAG_TARGET") or "").strip()
        if target_hint:
            target_matches = [d for d in candidates if self._matches_target_hint(d, target_hint)]
            if len(target_matches) == 1:
                return target_matches[0]
            if not target_matches:
                raise XdbError(
                    f"no Versal target matching FPGA_JTAG_TARGET {target_hint} "
                    f"and part hint {part_hint}"
                )
            raise XdbError(
                f"ambiguous Versal target: FPGA_JTAG_TARGET {target_hint} and part hint "
                f"{part_hint} match {len(target_matches)} devices"
            )

        if len(candidates) != 1:
            raise XdbError(
                f"ambiguous Versal target: part hint {part_hint} matches {len(candidates)} devices; "
                "set FPGA_JTAG_TARGET"
            )
        return candidates[0]

    @classmethod
    def _matches_target_hint(cls, device, target_hint: str) -> bool:
        hint = target_hint.lower()
        values = [cls._target_name(device), str(device)]
        try:
            values.extend(str(value) for value in device.to_dict().values())
        except Exception:
            pass
        return any(hint in value.lower() for value in values)

    @staticmethod
    def _sha256_file(path: str) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _target_name(device) -> str:
        try:
            d = device.to_dict()
            cable_name = str(d.get("cable_name") or "")
            part = str(d.get("part") or getattr(device, "part_name", ""))
            dna = str(d.get("dna") or "")
            if cable_name and dna:
                return f"{cable_name}:{dna}"
            if cable_name and part:
                return f"{cable_name}:{part}"
        except Exception:
            pass
        return str(device)
