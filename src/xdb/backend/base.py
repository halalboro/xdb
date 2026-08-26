from __future__ import annotations

from enum import Enum
from typing import Protocol, TypedDict


class Capability(str, Enum):
    TARGETS = "targets"
    PROGRAM = "program"
    ILA_LIST = "ila.list"
    ILA_BASIC_CAPTURE = "ila.basic_capture"
    ILA_BASIC_TRIGGER = "ila.basic_trigger"
    ILA_CAPTURE_POSITION = "ila.capture_position"
    ILA_MULTI_WINDOW_CAPTURE = "ila.multi_window_capture"
    INSTRUMENTS_LIST = "instruments.list"


class TargetInfo(TypedDict):
    target: str
    part: str


class DebugSelection(TypedDict, total=False):
    selected_target: str
    selected_part: str


class DebugProvenance(DebugSelection):
    backend: str
    device_family: str
    hw_server_url: str
    cs_server_url: str | None


class ProvenanceResult(TypedDict, total=False):
    provenance: DebugProvenance


class TargetsResult(ProvenanceResult):
    targets: list[TargetInfo]


class ProbeInfo(TypedDict):
    name: str
    width: int | None


class IlaInfo(TypedDict):
    name: str
    probes: list[ProbeInfo]


class ListIlasResult(ProvenanceResult):
    target: str
    part: str
    ilas: list[IlaInfo]


class ProgramResultExtras(ProvenanceResult, total=False):
    bitstream: str
    bitstream_sha256: str
    ltx: str | None


class ProgramResult(ProgramResultExtras):
    ok: bool
    target: str
    part: str


class ProbeTrigger(TypedDict):
    probe: str
    operator: str
    value: str | int


class CaptureResult(ProvenanceResult):
    ok: bool
    target: str
    part: str
    ila: str
    csv: str
    samples: int
    windows: int
    total_samples: int
    trigger_position: int
    triggers: list[ProbeTrigger]


class InstrumentInfo(TypedDict):
    type: str
    name: str
    capabilities: list[str]


class InstrumentsResult(ProvenanceResult):
    target: str
    part: str
    instruments: list[InstrumentInfo]


class DebugBackend(Protocol):
    name: str

    def list_targets(self, part_hint: str | None, timeout: int = 120) -> TargetsResult: ...

    def program(
        self, bit: str, ltx: str | None, part_hint: str, timeout: int = 300
    ) -> ProgramResult: ...

    def list_ilas(
        self,
        part_hint: str,
        timeout: int = 180,
        *,
        ltx: str | None = None,
    ) -> ListIlasResult: ...

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
    ) -> CaptureResult: ...

    def list_instruments(self, part_hint: str, timeout: int = 180) -> InstrumentsResult: ...

    def capabilities(self) -> set[Capability]: ...
