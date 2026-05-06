from __future__ import annotations

from enum import Enum
from typing import Protocol, TypedDict


class Capability(str, Enum):
    ILA_BASIC_CAPTURE = "ila.basic_capture"
    PROGRAM = "program"
    TARGETS = "targets"


class TargetInfo(TypedDict):
    target: str
    part: str


class TargetsResult(TypedDict):
    targets: list[TargetInfo]


class ProbeInfo(TypedDict):
    name: str
    width: int


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


class DebugBackend(Protocol):
    name: str

    def list_targets(self, part_hint: str | None, timeout: int = 120) -> TargetsResult:
        ...

    def program(self, bit: str, ltx: str | None, part_hint: str, timeout: int = 300) -> ProgramResult:
        ...

    def list_ilas(self, part_hint: str, timeout: int = 180) -> ListIlasResult:
        ...

    def capture(
        self,
        part_hint: str,
        ila_name: str,
        csv_path: str,
        samples: int,
        timeout: int = 120,
    ) -> CaptureResult:
        ...

    def capabilities(self) -> set[Capability]:
        ...
