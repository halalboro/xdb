from __future__ import annotations

import hashlib
import importlib
import json
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
    IlaTriggerCompileResult,
    IlaTriggerConfig,
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

    def __init__(self, persistent_session=None) -> None:
        self._persistent_session = persistent_session

    def capabilities(self) -> set[Capability]:
        return {
            Capability.TARGETS,
            Capability.PROGRAM,
            Capability.ILA_LIST,
            Capability.ILA_CONTROL,
            Capability.ILA_BASIC_CAPTURE,
            Capability.ILA_BASIC_TRIGGER,
            Capability.ILA_ADVANCED_TRIGGER,
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
                    Capability.ILA_ADVANCED_TRIGGER.value,
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
        advanced_trigger: IlaTriggerConfig | None = None,
    ) -> IlaArmResult:
        del timeout
        session = self._create_session(require_cs=True)
        try:
            dev, ila = self._select_ila(session, part_hint, ila_name, ltx)
            effective_position, normalized_triggers = self._arm_capture(
                ila, samples, windows, trigger_position, triggers, advanced_trigger
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

    def compile_ila_trigger(
        self,
        part_hint: str,
        ila_name: str,
        tsm_path: str,
        timeout: int = 120,
        *,
        ltx: str | None = None,
    ) -> IlaTriggerCompileResult:
        del timeout
        session = self._create_session(require_cs=True)
        try:
            dev, ila = self._select_ila(session, part_hint, ila_name, ltx)
            error_count, messages = ila.run_advanced_trigger(tsm_path, compile_only=True)
            target = self._target_name(dev)
            part = str(getattr(dev, "part_name", ""))
            return {
                "target": target,
                "part": part,
                "ila": ila_name,
                "tsm_path": tsm_path,
                "error_count": int(error_count),
                "messages": str(messages),
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
        output_path: str,
        timeout: int = 120,
        *,
        ltx: str | None = None,
        export_format: str = "CSV",
        probe_names: list[str] | None = None,
        start_window: int = 0,
        window_count: int | None = None,
        start_sample: int = 0,
        sample_count: int | None = None,
        include_gap: bool = False,
    ) -> CaptureResult:
        del timeout
        session = self._create_session(require_cs=True)
        try:
            dev, ila = self._select_ila(session, part_hint, ila_name, ltx)
            if not ila.upload() or ila.waveform is None:
                raise XdbError("ILA has no completed waveform to upload")
            out_path = str(Path(output_path).resolve())
            normalized_format = export_format.upper()
            if normalized_format not in {"CSV", "VCD", "CITF"}:
                raise XdbError(f"unsupported waveform export format: {export_format}")
            if normalized_format == "CITF" and (
                probe_names
                or start_window != 0
                or window_count is not None
                or start_sample != 0
                or sample_count is not None
                or include_gap
            ):
                raise XdbError("CITF export requires the complete unfiltered waveform")
            ila.waveform.export_waveform(
                normalized_format,
                out_path,
                probe_names=probe_names,
                start_window_idx=start_window,
                window_count=window_count,
                start_sample_idx=start_sample,
                sample_count=sample_count,
                include_gap=include_gap,
            )
            window_size = int(getattr(ila.waveform, "window_size", 0))
            captured_windows = int(ila.waveform.get_window_count())
            trigger_positions = list(getattr(ila.waveform, "trigger_position", []))
            trigger_position = int(trigger_positions[0]) if trigger_positions else 0
            result = self._capture_result(
                dev,
                ila_name,
                out_path,
                window_size,
                captured_windows,
                trigger_position,
                [],
                export_format=normalized_format,
            )
            manifest_path = out_path + ".json"
            output_sha256 = self._sha256_file(out_path)
            Path(manifest_path).write_text(
                json.dumps(
                    {
                        "schema": "xdb.ila-waveform/v1",
                        "output": out_path,
                        "output_sha256": output_sha256,
                        "export_format": normalized_format,
                        "selection": {
                            "probe_names": probe_names,
                            "start_window": start_window,
                            "window_count": window_count,
                            "start_sample": start_sample,
                            "sample_count": sample_count,
                            "include_gap": include_gap,
                        },
                        "capture": result,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            result["manifest"] = manifest_path
            result["output_sha256"] = output_sha256
            return result
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

    def _arm_capture(
        self,
        ila,
        samples: int,
        windows: int,
        trigger_position: int | None,
        triggers: list[ProbeTrigger] | None,
        advanced_trigger: IlaTriggerConfig | None = None,
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
        config = advanced_trigger
        tsm_path = config.get("tsm_path") if config else None
        if tsm_path and normalized_triggers:
            raise XdbError("TSM and basic probe triggers are mutually exclusive")
        ila.reset_probes()
        self._set_probe_values(ila, normalized_triggers, capture=False)
        capture_values = list(config.get("capture_values", [])) if config else []
        self._set_probe_values(ila, capture_values, capture=True)

        if config:
            enums = self._chipscopy_trigger_enums()
            trig_in = enums["ILATrigInMode"][str(config.get("trig_in", "disabled")).upper()]
            trig_out = enums["ILATrigOutMode"][str(config.get("trig_out", "disabled")).upper()]
            capture_name = str(
                config.get("capture_condition", "and" if capture_values else "always")
            )
            capture_condition = enums["ILACaptureCondition"][capture_name.upper()]
            common = {
                "trigger_position": effective_position,
                "window_count": windows,
                "window_size": samples,
                "capture_condition": capture_condition,
                "trig_in": trig_in,
                "trig_out": trig_out,
            }
            if tsm_path:
                ila.run_advanced_trigger(tsm_path, **common)
            else:
                trigger_condition = enums["ILATriggerCondition"][
                    str(config.get("trigger_condition", "and")).upper()
                ]
                ila.run_basic_trigger(
                    trigger_condition=trigger_condition,
                    **common,
                )
        elif normalized_triggers:
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

    @staticmethod
    def _set_probe_values(ila, values: list[ProbeTrigger], *, capture: bool) -> None:
        grouped: dict[str, list[str | int]] = {}
        for comparison in values:
            grouped.setdefault(comparison["probe"], []).extend(
                [comparison["operator"], comparison["value"]]
            )
        setter = ila.set_probe_capture_value if capture else ila.set_probe_trigger_value
        for probe, comparisons in grouped.items():
            setter(probe, comparisons)

    @staticmethod
    def _chipscopy_trigger_enums() -> dict[str, Any]:
        try:
            module = importlib.import_module("chipscopy.api.ila")
            return {
                name: getattr(module, name)
                for name in (
                    "ILACaptureCondition",
                    "ILATriggerCondition",
                    "ILATrigInMode",
                    "ILATrigOutMode",
                )
            }
        except Exception as error:
            raise XdbError("ChipScoPy ILA trigger enums are unavailable") from error

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
        output_path: str,
        samples: int,
        windows: int,
        trigger_position: int,
        triggers: list[ProbeTrigger],
        *,
        export_format: str = "CSV",
    ) -> CaptureResult:
        target = self._target_name(dev)
        part = str(getattr(dev, "part_name", ""))
        result: CaptureResult = {
            "ok": True,
            "target": target,
            "part": part,
            "ila": ila_name,
            "output": output_path,
            "export_format": export_format,
            "samples": samples,
            "windows": windows,
            "total_samples": samples * windows,
            "trigger_position": trigger_position,
            "triggers": triggers,
            "provenance": self._provenance(
                require_cs=True, selected_target=target, selected_part=part
            ),
        }
        if export_format == "CSV":
            result["csv"] = output_path
        return result

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
        if self._persistent_session is not None:
            return self._persistent_session
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

    def _delete_session(self, session) -> None:
        if self._persistent_session is session:
            return
        try:
            chipscopy = importlib.import_module("chipscopy")
            delete_session = getattr(chipscopy, "delete_session")
            delete_session(session)
        except Exception:
            pass

    def close(self) -> None:
        if self._persistent_session is None:
            return
        session = self._persistent_session
        self._persistent_session = None
        try:
            chipscopy = importlib.import_module("chipscopy")
            getattr(chipscopy, "delete_session")(session)
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
