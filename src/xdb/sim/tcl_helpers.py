from __future__ import annotations

from importlib.resources import files


def _tcl_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("$", "\\$")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )
    return f'"{escaped}"'



def _tcl_list(values: list[str]) -> str:
    if not values:
        return "[list]"
    return "[list " + " ".join(_tcl_string(v) for v in values) + "]"



def load_tcl_library() -> str:
    path = files("xdb.sim").joinpath("tcl/xdb_api.tcl")
    return path.read_text(encoding="utf-8")
