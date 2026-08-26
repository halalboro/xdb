from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from xdb.errors import XdbError

_SUPPORTED_SCHEMAS = {
    "xdb.ila-waveform/v1",
    "xdb.ila-group/v1",
    "xdb.ila-with-capture/v1",
}
_PATH_KEYS = {"output", "stdout", "stderr", "manifest"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise XdbError(f"invalid hardware-debug manifest: {path}") from error
    if not isinstance(value, dict) or value.get("schema") not in _SUPPORTED_SCHEMAS:
        raise XdbError(f"unsupported hardware-debug manifest: {path}")
    return value


def _referenced_paths(value: object) -> set[Path]:
    found: set[Path] = set()

    def visit(node: object, key: str | None = None) -> None:
        if key in _PATH_KEYS and isinstance(node, str):
            candidate = Path(node).expanduser()
            if not candidate.is_absolute():
                candidate = Path.cwd() / candidate
            if candidate.exists() or candidate.is_symlink():
                found.add(candidate.absolute())
        elif isinstance(node, dict):
            for child_key, child in node.items():
                visit(child, str(child_key))
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return found


def create_hardware_bundle(
    output_dir: str,
    manifest_paths: list[str],
    *,
    max_bytes: int = 64 * 1024 * 1024,
    session_context: dict[str, object] | None = None,
) -> dict[str, object]:
    if not manifest_paths:
        raise XdbError("at least one hardware-debug manifest is required")
    if max_bytes <= 0:
        raise XdbError("bundle byte limit must be > 0")
    output = Path(output_dir).expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        raise XdbError(f"bundle output directory is not empty: {output}")
    artifacts_dir = output / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    sources: list[dict[str, object]] = []
    referenced: set[Path] = set()
    for raw_path in manifest_paths:
        unresolved = Path(raw_path).expanduser()
        if unresolved.is_symlink():
            raise XdbError(f"bundle input must be a regular non-symlink file: {unresolved}")
        path = unresolved.resolve()
        manifest = _load_manifest(path)
        sources.append({"path": str(path), "schema": manifest["schema"]})
        referenced.add(path)
        referenced.update(_referenced_paths(manifest))

    artifacts = []
    total = 0
    for index, source in enumerate(sorted(referenced)):
        if source.is_symlink() or not source.is_file():
            raise XdbError(f"bundle input must be a regular non-symlink file: {source}")
        size = source.stat().st_size
        total += size
        if total > max_bytes:
            raise XdbError(f"hardware-debug bundle exceeds {max_bytes} bytes")
        destination = artifacts_dir / f"{index:03d}-{source.name}"
        shutil.copyfile(source, destination)
        artifacts.append(
            {
                "source": str(source),
                "path": str(destination.relative_to(output)),
                "size": size,
                "sha256": _sha256(destination),
            }
        )

    result: dict[str, object] = {
        "schema": "xdb.hardware-debug-bundle/v1",
        "sources": sources,
        "artifacts": artifacts,
        "total_bytes": total,
        "session": session_context,
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {**result, "output": str(output), "manifest": str(manifest_path)}
