from __future__ import annotations

from enum import Enum
from typing import Protocol, TypedDict


class Capability(str, Enum):
    TARGETS = "targets"
    PROGRAM = "program"
    ILA_LIST = "ila.list"
    ILA_BASIC_CAPTURE = "ila.basic_capture"
    INSTRUMENTS_LIST = "instruments.list"


class TargetInfo(TypedDict):
    target: str
    part: str


class TargetsResult(TypedDict):
    targets: list[TargetInfo]


class ProbeInfo(TypedDict):
    name: str
    width: int | None


class IlaInfo(TypedDict):
    name: str
    probes: list[ProbeInfo]


class ListIlasResult(TypedDict):
    target: str
    part: str
    ilas: list[IlaInfo]


class ProgramResult(TypedDict):
    ok: bool
    target: str
    part: str


class CaptureResult(TypedDict):
    ok: bool
    target: str
    part: str
    ila: str
    csv: str
    samples: int


class InstrumentInfo(TypedDict):
    type: str
    name: str
    capabilities: list[str]


class InstrumentsResult(TypedDict):
    target: str
    part: str
    instruments: list[InstrumentInfo]


class DebugBackend(Protocol):
    name: str

    def list_targets(self, part_hint: str | None, timeout: int = 120) -> TargetsResult:
        ...

    def program(self, bit: str, ltx: str | None, part_hint: str, timeout: int = 300) -> ProgramResult:
        ...

    def list_ilas(
        self,
        part_hint: str,
        timeout: int = 180,
        *,
        ltx: str | None = None,
    ) -> ListIlasResult:
        ...

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
        ...

    def list_instruments(self, part_hint: str, timeout: int = 180) -> InstrumentsResult:
        ...

    def capabilities(self) -> set[Capability]:
        ...
