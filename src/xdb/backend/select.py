from __future__ import annotations

import os

from .base import DebugBackend
from .vivado import VivadoBackend, VivadoError


def select_backend(name: str | None = None) -> DebugBackend:
    backend_name = (name or os.environ.get("XDB_BACKEND") or "vivado").strip().lower()

    if backend_name == "vivado":
        return VivadoBackend()

    raise VivadoError(f"unsupported backend: {backend_name} (supported: vivado)")
