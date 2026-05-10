from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from xdb.sim.types import SessionMeta


def config_matches(
    meta: SessionMeta,
    launch_spec: Mapping[str, object],
    simset: str,
    mode: str,
    top: str,
) -> bool:
    return (
        str(meta.get("launch_kind") or "") == "runtime"
        and str(meta.get("package_runtime") or "") == str(launch_spec.get("package_runtime") or "")
        and str(meta.get("simset") or "") == simset
        and str(meta.get("mode") or "") == mode
        and str(meta.get("top") or "") == top
    )


def launch_spec_summary(launch_spec: Mapping[str, object]) -> dict[str, Any]:
    return {
        "launch_kind": launch_spec.get("launch_kind"),
        "package_runtime": launch_spec.get("package_runtime"),
        "runtime_root": launch_spec.get("runtime_root"),
        "workspace": launch_spec.get("workspace"),
        "work_dir": launch_spec.get("work_dir"),
        "compile_script": launch_spec.get("compile_script"),
        "elaborate_script": launch_spec.get("elaborate_script"),
        "simulate_script": launch_spec.get("simulate_script"),
        "staged": launch_spec.get("staged"),
        "workspace_reused": launch_spec.get("workspace_reused"),
        "needs_stage": launch_spec.get("needs_stage"),
    }
