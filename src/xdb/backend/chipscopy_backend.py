from __future__ import annotations

import hashlib
import importlib
import os
from pathlib import Path
from typing import Any, cast

from xdb.errors import XdbError
from xdb.backend.base import (
    Capability,
    CaptureResult,
    DebugProvenance,
    InstrumentsResult,
    ListIlasResult,
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
            Capability.ILA_BASIC_CAPTURE,
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
                "capabilities": [Capability.ILA_LIST.value, Capability.ILA_BASIC_CAPTURE.value],
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

    def capture(
        self,
        part_hint: str,
        ila_name: str,
        csv_path: str,
        samples: int,
        timeout: int = 120,
        *,
        ltx: str | None = None,
    ) -> CaptureResult:
        session = self._create_session(require_cs=True)
        try:
            dev = self._select_device(session, part_hint)
            resolved_ltx = self._ltx_from_env(ltx)
            if resolved_ltx:
                dev.discover_and_setup_cores(ltx_file=resolved_ltx)
            else:
                dev.discover_and_setup_cores()

            ila = dev.ila_cores.get(name=ila_name)
            if not ila:
                raise XdbError(f"ILA not found: {ila_name}")

            ila.reset_probes()
            ila.run_trigger_immediately(
                trigger_position=samples // 2,
                window_count=1,
                window_size=samples,
            )
            max_wait_minutes = max(timeout, 1) / 60.0
            ila.wait_till_done(max_wait_minutes=max_wait_minutes)
            uploaded = ila.upload()
            if not uploaded or ila.waveform is None:
                raise XdbError("failed to upload ILA data")

            out_path = str(Path(csv_path))
            ila.waveform.export_waveform("CSV", out_path)

            target = self._target_name(dev)
            part = str(getattr(dev, "part_name", ""))
            return {
                "ok": True,
                "target": target,
                "part": part,
                "ila": ila_name,
                "csv": out_path,
                "samples": samples,
                "provenance": self._provenance(
                    require_cs=True,
                    selected_target=target,
                    selected_part=part,
                ),
            }
        finally:
            self._delete_session(session)

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
