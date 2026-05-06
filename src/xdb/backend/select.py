from __future__ import annotations

import os

from ..errors import XdbError
from .base import DebugBackend
from .chipscopy_backend import ChipScoPyBackend
from .vivado import VivadoBackend


def select_backend(name: str | None = None) -> DebugBackend:
    backend_name = (name or os.environ.get("XDB_BACKEND") or "auto").strip().lower()

    if backend_name == "vivado":
        return VivadoBackend()
    if backend_name == "chipscopy":
        return ChipScoPyBackend()
    if backend_name == "auto":
        family = (os.environ.get("XDB_DEVICE_FAMILY") or "").strip().lower()
        if family == "versal":
            return ChipScoPyBackend()
        return VivadoBackend()

    raise XdbError(
        f"unsupported backend: {backend_name} (supported: auto, vivado, chipscopy)"
    )
