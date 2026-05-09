from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Any, cast

from xdb.errors import XdbError
from xdb.backend.base import (
    Capability,
    CaptureResult,
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
            out = {
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
                out["targets"] = [
                    t for t in out["targets"] if ph in str(t.get("part", "")).lower()
                ]
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
            return {
                "ok": True,
                "target": self._target_name(dev),
                "part": str(getattr(dev, "part_name", "")),
            }
        finally:
            self._delete_session(session)

    def list_ilas(self, part_hint: str, timeout: int = 180) -> ListIlasResult:
        del timeout
        session = self._create_session(require_cs=True)
        try:
            dev = self._select_device(session, part_hint)
            ltx = self._ltx_from_env()
            if ltx:
                dev.discover_and_setup_cores(ltx_file=ltx)
            else:
                dev.discover_and_setup_cores()

            ilas_out = []
            for ila in dev.ila_cores:
                probes = []
                for p in ila.probes.values():
                    probes.append({"name": p.name, "width": int(p.bit_width)})
                ilas_out.append({"name": ila.name, "probes": probes})

            return cast(
                ListIlasResult,
                {
                    "target": self._target_name(dev),
                    "part": str(getattr(dev, "part_name", "")),
                    "ilas": ilas_out,
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
            },
        )

    def capture(
        self,
        part_hint: str,
        ila_name: str,
        csv_path: str,
        samples: int,
        timeout: int = 120,
    ) -> CaptureResult:
        session = self._create_session(require_cs=True)
        try:
            dev = self._select_device(session, part_hint)
            ltx = self._ltx_from_env()
            if ltx:
                dev.discover_and_setup_cores(ltx_file=ltx)
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

            return {
                "ok": True,
                "target": self._target_name(dev),
                "part": str(getattr(dev, "part_name", "")),
                "ila": ila_name,
                "csv": out_path,
                "samples": samples,
            }
        finally:
            self._delete_session(session)

    def _create_session(self, require_cs: bool):
        chipscopy = self._chipscopy_imports()
        hw_url = os.environ.get("HW_SERVER_URL", "TCP:localhost:3121")
        if require_cs:
            cs_url = os.environ.get("CS_SERVER_URL", "TCP:localhost:3042")
            return chipscopy["create_session"](hw_server_url=hw_url, cs_server_url=cs_url)
        return chipscopy["create_session"](hw_server_url=hw_url)

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
    def _ltx_from_env() -> str | None:
        ltx = os.environ.get("FPGA_LTX")
        if ltx and os.path.isfile(ltx):
            return ltx
        return None

    @staticmethod
    def _get_versal_devices(session):
        devices = [d for d in session.devices if str(getattr(d, "family_name", "")).lower() == "versal"]
        return devices

    def _select_device(self, session, part_hint: str):
        part_hint_l = part_hint.lower()
        versal_devices = self._get_versal_devices(session)
        for d in versal_devices:
            part = str(getattr(d, "part_name", ""))
            if part_hint_l in part.lower():
                return d

        if not versal_devices:
            raise XdbError("no Versal devices found via chipscopy")
        raise XdbError(f"no Versal target matching part hint {part_hint}")

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
