from __future__ import annotations

import os

from xdb.errors import XdbError
from xdb.backend.base import DebugBackend
from xdb.backend.chipscopy_backend import ChipScoPyBackend
from xdb.backend.vivado import VivadoBackend


def select_backend(name: str | None = None) -> DebugBackend:
    backend_name = (name or os.environ.get("XDB_BACKEND") or "auto").strip().lower()
    if backend_name in {"auto", "chipscopy"}:
        from xdb.hw_session import persistent_backend_from_env

        persistent = persistent_backend_from_env()
        if persistent is not None:
            return persistent

    if backend_name == "vivado":
        return VivadoBackend()
    if backend_name == "chipscopy":
        return ChipScoPyBackend()
    if backend_name == "auto":
        family = (os.environ.get("XDB_DEVICE_FAMILY") or "").strip().lower()
        if family == "versal":
            return ChipScoPyBackend()
        return VivadoBackend()

    raise XdbError(f"unsupported backend: {backend_name} (supported: auto, vivado, chipscopy)")
