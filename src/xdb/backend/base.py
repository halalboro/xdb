from __future__ import annotations

from enum import Enum
from typing import Protocol, TypedDict


class Capability(str, Enum):
    TARGETS = "targets"
    PROGRAM = "program"
    ILA_LIST = "ila.list"
    ILA_CONTROL = "ila.control"
    ILA_BASIC_CAPTURE = "ila.basic_capture"
    ILA_BASIC_TRIGGER = "ila.basic_trigger"
    ILA_ADVANCED_TRIGGER = "ila.advanced_trigger"
    ILA_CAPTURE_POSITION = "ila.capture_position"
    ILA_MULTI_WINDOW_CAPTURE = "ila.multi_window_capture"
    ILA_MULTI_CORE = "ila.multi_core"
    VIO_LIST = "vio.list"
    VIO_READ = "vio.read"
    VIO_WRITE = "vio.write"
    CORE_INVENTORY = "core.inventory"
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


class IlaStatusResult(ProvenanceResult):
    target: str
    part: str
    ila: str
    status: dict[str, object]


class IlaArmResult(IlaStatusResult):
    samples: int
    windows: int
    trigger_position: int
    triggers: list[ProbeTrigger]


class IlaTriggerConfig(TypedDict, total=False):
    trigger_condition: str
    capture_condition: str
    capture_values: list[ProbeTrigger]
    tsm_path: str
    trig_in: str
    trig_out: str


class IlaTriggerCompileResult(ProvenanceResult):
    target: str
    part: str
    ila: str
    tsm_path: str
    error_count: int
    messages: str


class CaptureExportResult(TypedDict, total=False):
    csv: str
    output: str
    export_format: str
    manifest: str
    output_sha256: str


class CaptureResult(ProvenanceResult, CaptureExportResult):
    ok: bool
    target: str
    part: str
    ila: str
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
    ) -> IlaArmResult: ...

    def compile_ila_trigger(
        self,
        part_hint: str,
        ila_name: str,
        tsm_path: str,
        timeout: int = 120,
        *,
        ltx: str | None = None,
    ) -> IlaTriggerCompileResult: ...

    def ila_status(
        self,
        part_hint: str,
        ila_name: str,
        timeout: int = 120,
        *,
        ltx: str | None = None,
    ) -> IlaStatusResult: ...

    def wait_ila(
        self,
        part_hint: str,
        ila_name: str,
        timeout: int = 120,
        *,
        ltx: str | None = None,
    ) -> IlaStatusResult: ...

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
    ) -> CaptureResult: ...

    def arm_ila_group(
        self,
        part_hint: str,
        ila_names: list[str],
        samples: int,
        timeout: int = 120,
        *,
        ltx: str | None = None,
        windows: int = 1,
        trigger_position: int | None = None,
        triggers: list[ProbeTrigger] | None = None,
        source_ila: str | None = None,
    ) -> dict[str, object]: ...

    def ila_group_status(
        self,
        part_hint: str,
        ila_names: list[str],
        timeout: int = 120,
        *,
        ltx: str | None = None,
    ) -> dict[str, object]: ...

    def wait_ila_group(
        self,
        part_hint: str,
        ila_names: list[str],
        timeout: int = 120,
        *,
        ltx: str | None = None,
    ) -> dict[str, object]: ...

    def upload_ila_group(
        self,
        part_hint: str,
        ila_names: list[str],
        output_dir: str,
        timeout: int = 120,
        *,
        ltx: str | None = None,
        export_format: str = "CSV",
    ) -> dict[str, object]: ...

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

    def list_vios(
        self, part_hint: str, timeout: int = 180, *, ltx: str | None = None
    ) -> dict[str, object]: ...

    def read_vio(
        self,
        part_hint: str,
        vio_name: str,
        probes: list[str] | None = None,
        timeout: int = 120,
        *,
        ltx: str | None = None,
    ) -> dict[str, object]: ...

    def write_vio(
        self,
        part_hint: str,
        vio_name: str,
        values: dict[str, int],
        timeout: int = 120,
        *,
        ltx: str | None = None,
    ) -> dict[str, object]: ...

    def core_inventory(
        self, part_hint: str, timeout: int = 180, *, ltx: str | None = None
    ) -> dict[str, object]: ...

    def list_instruments(self, part_hint: str, timeout: int = 180) -> InstrumentsResult: ...

    def capabilities(self) -> set[Capability]: ...
