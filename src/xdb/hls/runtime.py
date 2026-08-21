from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from xdb.config import config_path_value
from xdb.errors import XdbError


MANIFEST_NAME = "xdb-hls-csim.json"
RUNTIME_KIND = "hls-csim"
SCHEMA_VERSION = 1
STAGE_STAMP = ".xdb-hls-stage.json"
CONTROL_DIR = ".xdb-hls"
RESULT_SCHEMA = "xdb-hls-csim-result-v1"
_ENV_PACKAGE = "XDB_HLS_PACKAGE_RUNTIME"
_ENV_WORKSPACE = "XDB_HLS_WORKSPACE"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_RESERVED_ENV = {
    "HOME",
    "TMPDIR",
    "XDB_HLS_PACKAGE_RUNTIME",
    "XDB_HLS_WORKSPACE",
    "XDB_HLS_PROJECT",
    "XDB_HLS_TOP",
    "XDB_HLS_CASE",
}


@dataclass(frozen=True)
class Entrypoint:
    path: str
    args: tuple[str, ...]


@dataclass(frozen=True)
class HlsCase:
    name: str
    args: tuple[str, ...]
    fixtures: tuple[str, ...]
    expected_exit_code: int | None
    success_marker: str | None


@dataclass(frozen=True)
class ToolSpec:
    family: str
    version: str
    executable: str
    version_args: tuple[str, ...]
    version_regex: str


@dataclass(frozen=True)
class EnvironmentSpec:
    passed: tuple[str, ...]
    injected: Mapping[str, str]


@dataclass(frozen=True)
class ArtifactSpec:
    path: str
    required: bool
    max_bytes: int


@dataclass(frozen=True)
class ProvenanceSpec:
    source_revision: str
    source_sha256: str
    configuration_sha256: str


@dataclass(frozen=True)
class HlsManifest:
    path: Path
    root: Path
    schema_version: int
    runtime_kind: str
    project: str
    top: str
    prepare: Entrypoint
    run: Entrypoint
    cases: tuple[HlsCase, ...]
    default_case: str
    default_timeout_seconds: float
    expected_exit_code: int
    success_marker: str | None
    tool: ToolSpec
    environment: EnvironmentSpec
    provenance: ProvenanceSpec
    compile_flags: tuple[str, ...]
    artifacts: tuple[ArtifactSpec, ...]

    def case_map(self) -> dict[str, HlsCase]:
        return {case.name: case for case in self.cases}


@dataclass(frozen=True)
class HlsRuntime:
    manifest: HlsManifest
    package_root: Path
    package_fingerprint: str
    workspace: Path
    staged: bool
    workspace_reused: bool
    needs_stage: bool

    @property
    def control_dir(self) -> Path:
        return self.workspace / CONTROL_DIR

    @property
    def runs_dir(self) -> Path:
        return self.control_dir / "runs"

    @property
    def last_result_path(self) -> Path:
        return self.control_dir / "last-result.json"

    @property
    def active_path(self) -> Path:
        return self.control_dir / "active.json"


def _require_object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise XdbError(f"{context} must be an object")
    return value


def _require_exact_fields(
    data: Mapping[str, Any],
    *,
    context: str,
    required: Iterable[str],
    optional: Iterable[str] = (),
) -> None:
    required_set = set(required)
    allowed = required_set | set(optional)
    missing = sorted(required_set - set(data))
    unknown = sorted(set(data) - allowed)
    if missing:
        raise XdbError(f"{context} is missing required field(s): {', '.join(missing)}")
    if unknown:
        raise XdbError(f"{context} contains unsupported field(s): {', '.join(unknown)}")


def _require_string(value: Any, context: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise XdbError(f"{context} must be a string")
    if not allow_empty and not value.strip():
        raise XdbError(f"{context} must not be empty")
    if any(ord(char) < 0x20 and char not in "\t" for char in value):
        raise XdbError(f"{context} contains a control character")
    return value


def _require_string_list(value: Any, context: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise XdbError(f"{context} must be an array of strings")
    result: list[str] = []
    for index, item in enumerate(value):
        result.append(_require_string(item, f"{context}[{index}]", allow_empty=True))
    return tuple(result)


def _require_int(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise XdbError(f"{context} must be an integer")
    return value


def _require_number(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise XdbError(f"{context} must be a number")
    return float(value)


def _validate_name(value: str, context: str) -> str:
    if not _NAME_RE.fullmatch(value):
        raise XdbError(
            f"{context} must start with an alphanumeric character and contain only "
            "letters, digits, '.', '_', or '-'"
        )
    return value


def _validate_timeout(value: float, context: str) -> float:
    if not 0 < value <= 86400:
        raise XdbError(f"{context} must be > 0 and <= 86400 seconds")
    return value


def _validate_exit_code(value: int, context: str) -> int:
    if not 0 <= value <= 255:
        raise XdbError(f"{context} must be between 0 and 255")
    return value


def _validate_marker(value: str | None, context: str) -> str | None:
    if value is None:
        return None
    marker = _require_string(value, context)
    if len(marker.encode("utf-8")) > 4096:
        raise XdbError(f"{context} must be at most 4096 UTF-8 bytes")
    return marker


def _relative_path(value: Any, context: str) -> str:
    text = _require_string(value, context)
    path = Path(text)
    if path.is_absolute() or text.startswith(("/", "\\")):
        raise XdbError(f"{context} must be package-relative: {text}")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise XdbError(f"{context} contains an invalid or traversing path component: {text}")
    return path.as_posix()


def _resolve_declared_path(root: Path, relative: str, context: str, *, must_exist: bool) -> Path:
    candidate = root / relative
    try:
        resolved = candidate.resolve(strict=must_exist)
    except OSError as error:
        raise XdbError(f"{context} does not exist: {candidate}") from error
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise XdbError(f"{context} escapes the package root: {relative}") from error
    return resolved


def _parse_entrypoint(data: Any, context: str) -> Entrypoint:
    item = _require_object(data, context)
    _require_exact_fields(item, context=context, required=("path",), optional=("args",))
    return Entrypoint(
        path=_relative_path(item["path"], f"{context}.path"),
        args=_require_string_list(item.get("args", []), f"{context}.args"),
    )


def _parse_case(data: Any, index: int) -> HlsCase:
    context = f"manifest.cases[{index}]"
    item = _require_object(data, context)
    _require_exact_fields(
        item,
        context=context,
        required=("name",),
        optional=("args", "fixtures", "expected_exit_code", "success_marker"),
    )
    name = _validate_name(_require_string(item["name"], f"{context}.name"), f"{context}.name")
    fixtures_value = item.get("fixtures", [])
    if not isinstance(fixtures_value, list):
        raise XdbError(f"{context}.fixtures must be an array of package-relative paths")
    fixtures = tuple(
        _relative_path(value, f"{context}.fixtures[{fixture_index}]")
        for fixture_index, value in enumerate(fixtures_value)
    )
    expected = item.get("expected_exit_code")
    expected_exit_code = (
        None
        if expected is None
        else _validate_exit_code(_require_int(expected, f"{context}.expected_exit_code"), context)
    )
    return HlsCase(
        name=name,
        args=_require_string_list(item.get("args", []), f"{context}.args"),
        fixtures=fixtures,
        expected_exit_code=expected_exit_code,
        success_marker=_validate_marker(item.get("success_marker"), f"{context}.success_marker"),
    )


def _parse_tool(data: Any) -> ToolSpec:
    context = "manifest.tool"
    item = _require_object(data, context)
    _require_exact_fields(
        item,
        context=context,
        required=("family", "version", "executable", "version_args", "version_regex"),
    )
    family = _validate_name(
        _require_string(item["family"], f"{context}.family"), f"{context}.family"
    )
    executable = _require_string(item["executable"], f"{context}.executable")
    if Path(executable).name != executable or not _NAME_RE.fullmatch(executable):
        raise XdbError(f"{context}.executable must be an executable name resolved from PATH")
    version_regex = _require_string(item["version_regex"], f"{context}.version_regex")
    try:
        compiled = re.compile(version_regex)
    except re.error as error:
        raise XdbError(f"{context}.version_regex is invalid: {error}") from error
    if compiled.groups != 1:
        raise XdbError(f"{context}.version_regex must contain exactly one capture group")
    return ToolSpec(
        family=family,
        version=_require_string(item["version"], f"{context}.version"),
        executable=executable,
        version_args=_require_string_list(item["version_args"], f"{context}.version_args"),
        version_regex=version_regex,
    )


def _parse_environment(data: Any) -> EnvironmentSpec:
    context = "manifest.environment"
    item = _require_object(data, context)
    _require_exact_fields(item, context=context, required=("pass", "set"))
    passed = _require_string_list(item["pass"], f"{context}.pass")
    if len(set(passed)) != len(passed):
        raise XdbError(f"{context}.pass contains duplicate names")
    injected_data = _require_object(item["set"], f"{context}.set")
    injected: dict[str, str] = {}
    for key, value in injected_data.items():
        if not isinstance(key, str) or not _ENV_RE.fullmatch(key):
            raise XdbError(f"{context}.set contains an invalid environment name: {key!r}")
        injected[key] = _require_string(value, f"{context}.set.{key}", allow_empty=True)
    for key in [*passed, *injected]:
        if not _ENV_RE.fullmatch(key):
            raise XdbError(f"{context} contains an invalid environment name: {key!r}")
        if key in _RESERVED_ENV:
            raise XdbError(f"{context} may not override xdb-managed environment variable {key}")
    if "PATH" not in passed and "PATH" not in injected:
        raise XdbError(f"{context} must explicitly pass or set PATH")
    return EnvironmentSpec(passed=passed, injected=injected)


def _parse_provenance(data: Any) -> ProvenanceSpec:
    context = "manifest.provenance"
    item = _require_object(data, context)
    _require_exact_fields(
        item,
        context=context,
        required=("source_revision", "source_sha256", "configuration_sha256"),
    )
    source_revision = _require_string(item["source_revision"], f"{context}.source_revision")
    source_sha256 = _require_string(item["source_sha256"], f"{context}.source_sha256")
    configuration_sha256 = _require_string(
        item["configuration_sha256"], f"{context}.configuration_sha256"
    )
    if not _HASH_RE.fullmatch(source_sha256):
        raise XdbError(f"{context}.source_sha256 must be a lowercase SHA-256 hex digest")
    if not _HASH_RE.fullmatch(configuration_sha256):
        raise XdbError(f"{context}.configuration_sha256 must be a lowercase SHA-256 hex digest")
    return ProvenanceSpec(
        source_revision=source_revision,
        source_sha256=source_sha256,
        configuration_sha256=configuration_sha256,
    )


def _parse_artifact(data: Any, index: int) -> ArtifactSpec:
    context = f"manifest.artifacts[{index}]"
    item = _require_object(data, context)
    _require_exact_fields(
        item,
        context=context,
        required=("path", "required", "max_bytes"),
    )
    required = item["required"]
    if not isinstance(required, bool):
        raise XdbError(f"{context}.required must be a boolean")
    max_bytes = _require_int(item["max_bytes"], f"{context}.max_bytes")
    if not 1 <= max_bytes <= 16 * 1024 * 1024:
        raise XdbError(f"{context}.max_bytes must be between 1 and 16777216")
    path = _relative_path(item["path"], f"{context}.path")
    if Path(path).parts[0] in {CONTROL_DIR, STAGE_STAMP}:
        raise XdbError(f"{context}.path uses an xdb-reserved runtime path: {path}")
    return ArtifactSpec(path=path, required=required, max_bytes=max_bytes)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, json.JSONDecodeError) as error:
        raise XdbError(f"failed to read HLS C-simulation manifest {path}: {error}") from error
    return _require_object(data, f"HLS C-simulation manifest {path}")


def discover_hls_manifest(package: str | Path) -> Path:
    path = Path(package).expanduser().resolve()
    if path.is_file():
        if path.name != MANIFEST_NAME:
            raise XdbError(f"HLS C-simulation manifest must be named {MANIFEST_NAME}: {path}")
        return path
    if not path.is_dir():
        raise XdbError(f"HLS C-simulation package does not exist: {path}")
    candidates = [path / MANIFEST_NAME, path / "project" / "hls" / MANIFEST_NAME]
    found: list[Path] = []
    for candidate in candidates:
        if not candidate.is_file():
            continue
        resolved = candidate.resolve()
        try:
            resolved.relative_to(path)
        except ValueError as error:
            raise XdbError(
                f"HLS C-simulation manifest escapes the package root: {candidate}"
            ) from error
        found.append(resolved)
    if not found:
        raise XdbError(
            f"HLS C-simulation manifest not found under {path}; expected {MANIFEST_NAME} "
            f"or project/hls/{MANIFEST_NAME}"
        )
    return found[0]


def load_hls_manifest(package: str | Path) -> HlsManifest:
    manifest_path = discover_hls_manifest(package)
    root = manifest_path.parent.resolve()
    data = _load_json(manifest_path)
    _require_exact_fields(
        data,
        context="manifest",
        required=(
            "schema_version",
            "runtime_kind",
            "project",
            "top",
            "prepare",
            "run",
            "cases",
            "default_case",
            "default_timeout_seconds",
            "expected_exit_code",
            "tool",
            "environment",
            "provenance",
            "compile_flags",
            "artifacts",
        ),
        optional=("success_marker",),
    )
    schema_version = _require_int(data["schema_version"], "manifest.schema_version")
    if schema_version != SCHEMA_VERSION:
        raise XdbError(
            f"unsupported HLS C-simulation manifest schema version {schema_version}; "
            f"expected {SCHEMA_VERSION}"
        )
    runtime_kind = _require_string(data["runtime_kind"], "manifest.runtime_kind")
    if runtime_kind != RUNTIME_KIND:
        raise XdbError(f"unsupported HLS runtime kind {runtime_kind!r}; expected {RUNTIME_KIND!r}")
    cases_value = data["cases"]
    if not isinstance(cases_value, list) or not cases_value:
        raise XdbError("manifest.cases must be a non-empty array")
    cases = tuple(_parse_case(value, index) for index, value in enumerate(cases_value))
    names = [case.name for case in cases]
    if len(set(names)) != len(names):
        duplicates = sorted(name for name in set(names) if names.count(name) > 1)
        raise XdbError(f"manifest.cases contains duplicate case name(s): {', '.join(duplicates)}")
    default_case = _validate_name(
        _require_string(data["default_case"], "manifest.default_case"), "manifest.default_case"
    )
    if default_case not in names:
        raise XdbError(f"manifest.default_case names an unknown case: {default_case}")
    manifest = HlsManifest(
        path=manifest_path,
        root=root,
        schema_version=schema_version,
        runtime_kind=runtime_kind,
        project=_validate_name(
            _require_string(data["project"], "manifest.project"), "manifest.project"
        ),
        top=_require_string(data["top"], "manifest.top"),
        prepare=_parse_entrypoint(data["prepare"], "manifest.prepare"),
        run=_parse_entrypoint(data["run"], "manifest.run"),
        cases=cases,
        default_case=default_case,
        default_timeout_seconds=_validate_timeout(
            _require_number(data["default_timeout_seconds"], "manifest.default_timeout_seconds"),
            "manifest.default_timeout_seconds",
        ),
        expected_exit_code=_validate_exit_code(
            _require_int(data["expected_exit_code"], "manifest.expected_exit_code"),
            "manifest.expected_exit_code",
        ),
        success_marker=_validate_marker(data.get("success_marker"), "manifest.success_marker"),
        tool=_parse_tool(data["tool"]),
        environment=_parse_environment(data["environment"]),
        provenance=_parse_provenance(data["provenance"]),
        compile_flags=_require_string_list(data["compile_flags"], "manifest.compile_flags"),
        artifacts=tuple(
            _parse_artifact(value, index) for index, value in enumerate(data["artifacts"])
        ),
    )
    _validate_package_paths(manifest)
    return manifest


def _validate_package_paths(manifest: HlsManifest) -> None:
    root = manifest.root
    for reserved in (CONTROL_DIR, STAGE_STAMP):
        if (root / reserved).exists():
            raise XdbError(f"HLS C-simulation package may not contain reserved path {reserved}")
    for entry_name, entry in (("prepare", manifest.prepare), ("run", manifest.run)):
        path = _resolve_declared_path(
            root, entry.path, f"manifest.{entry_name}.path", must_exist=True
        )
        if not path.is_file():
            raise XdbError(f"manifest.{entry_name}.path is not a file: {path}")
        if not os.access(path, os.X_OK):
            raise XdbError(f"manifest.{entry_name}.path is not executable: {path}")
    for case in manifest.cases:
        for fixture in case.fixtures:
            _resolve_declared_path(
                root,
                fixture,
                f"manifest case {case.name!r} fixture",
                must_exist=True,
            )
    artifact_paths = [artifact.path for artifact in manifest.artifacts]
    if len(set(artifact_paths)) != len(artifact_paths):
        raise XdbError("manifest.artifacts contains duplicate paths")
    for artifact in manifest.artifacts:
        _resolve_declared_path(
            root,
            artifact.path,
            f"manifest artifact {artifact.path!r}",
            must_exist=False,
        )
    for path in root.rglob("*"):
        if not path.is_symlink():
            continue
        if path.is_dir():
            raise XdbError(f"package directory symlinks are not supported: {path}")
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as error:
            raise XdbError(f"package symlink escapes the runtime root: {path}") from error


def package_fingerprint(root: str | Path) -> str:
    package_root = Path(root).expanduser().resolve()
    if not package_root.is_dir():
        raise XdbError(f"HLS C-simulation package root does not exist: {package_root}")
    digest = hashlib.sha256()
    for path in sorted(
        package_root.rglob("*"), key=lambda item: item.relative_to(package_root).as_posix()
    ):
        relative = path.relative_to(package_root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            digest.update(b"symlink\0")
            digest.update(os.readlink(path).encode("utf-8"))
        elif path.is_dir():
            digest.update(b"dir\0")
        elif path.is_file():
            digest.update(b"file\0")
            digest.update(b"x" if info.st_mode & 0o111 else b"-")
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        else:
            raise XdbError(f"unsupported package filesystem entry: {path}")
        digest.update(b"\0")
    return digest.hexdigest()


def _repo_root(path: Path) -> Path:
    resolved = path.resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / ".git").exists():
            return candidate
    return resolved


def _xdb_root(anchor: Path) -> Path:
    configured = os.environ.get("XDB_ROOT")
    if configured:
        value = Path(configured).expanduser()
        return (anchor / value).resolve() if not value.is_absolute() else value.resolve()
    return (anchor / ".xdb").resolve()


def resolve_hls_package_arg(package: str | None) -> str:
    value = package or os.environ.get(_ENV_PACKAGE) or config_path_value("hls", "package_runtime")
    if not value:
        raise XdbError(
            f"missing HLS C-simulation package: pass a package path or set {_ENV_PACKAGE}"
        )
    return value


def default_hls_workspace(manifest: HlsManifest, fingerprint: str, cwd: Path | None = None) -> Path:
    anchor = _repo_root((cwd or Path.cwd()).resolve())
    return _xdb_root(anchor) / "hls" / "workspaces" / f"{manifest.project}-{fingerprint[:12]}"


def resolve_hls_workspace_arg(
    workspace: str | None,
    manifest: HlsManifest,
    fingerprint: str,
) -> Path:
    value = workspace or os.environ.get(_ENV_WORKSPACE) or config_path_value("hls", "workspace")
    if value:
        return Path(value).expanduser().resolve()
    return default_hls_workspace(manifest, fingerprint)


def read_stage_stamp(workspace: str | Path) -> dict[str, Any] | None:
    path = Path(workspace).expanduser().resolve() / STAGE_STAMP
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def is_hls_stage_stamp(stamp: Mapping[str, Any] | None) -> bool:
    return bool(
        stamp
        and stamp.get("schema_version") == SCHEMA_VERSION
        and stamp.get("runtime_kind") == RUNTIME_KIND
    )


def _stage_is_fresh(
    workspace: Path,
    *,
    manifest: HlsManifest,
    fingerprint: str,
) -> bool:
    stamp = read_stage_stamp(workspace)
    if not is_hls_stage_stamp(stamp):
        return False
    assert stamp is not None
    manifest_relative = manifest.path.relative_to(manifest.root).as_posix()
    return (
        stamp.get("schema_version") == SCHEMA_VERSION
        and stamp.get("runtime_kind") == RUNTIME_KIND
        and stamp.get("source_root") == str(manifest.root)
        and stamp.get("source_fingerprint") == fingerprint
        and stamp.get("manifest") == manifest_relative
        and (workspace / manifest_relative).is_file()
    )


def _make_writable(root: Path) -> None:
    if not root.exists():
        return
    for path in (root, *root.rglob("*")):
        if path.is_symlink():
            continue
        try:
            mode = path.stat().st_mode
            os.chmod(path, mode | (0o700 if path.is_dir() else 0o600))
        except OSError:
            continue


def remove_hls_workspace(workspace: str | Path) -> bool:
    path = Path(workspace).expanduser().resolve()
    if not path.exists():
        return False
    if not path.is_dir():
        raise XdbError(f"HLS workspace path is not a directory: {path}")
    if path == Path(path.anchor) or path == Path.home().resolve():
        raise XdbError(f"refusing unsafe HLS workspace removal: {path}")
    if any(path.iterdir()) and not is_hls_stage_stamp(read_stage_stamp(path)):
        raise XdbError(f"refusing to replace non-XDB directory selected as HLS workspace: {path}")
    _make_writable(path)
    shutil.rmtree(path)
    return True


def stage_hls_runtime(runtime: HlsRuntime, *, force: bool = False) -> HlsRuntime:
    if runtime.workspace_reused and not force:
        return runtime
    source_root = runtime.package_root
    workspace = runtime.workspace
    try:
        workspace.relative_to(source_root)
        raise XdbError(f"HLS workspace may not be inside the immutable package: {workspace}")
    except ValueError:
        pass
    try:
        source_root.relative_to(workspace)
        raise XdbError(f"HLS workspace may not contain the immutable package: {workspace}")
    except ValueError:
        pass

    workspace.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{workspace.name}.", dir=workspace.parent))
    try:
        shutil.copytree(
            source_root,
            temporary,
            dirs_exist_ok=True,
            symlinks=False,
            copy_function=shutil.copy2,
        )
        _make_writable(temporary)
        manifest_relative = runtime.manifest.path.relative_to(source_root).as_posix()
        stamp = {
            "schema_version": SCHEMA_VERSION,
            "runtime_kind": RUNTIME_KIND,
            "source_root": str(source_root),
            "source_fingerprint": runtime.package_fingerprint,
            "manifest": manifest_relative,
        }
        (temporary / STAGE_STAMP).write_text(
            json.dumps(stamp, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if workspace.exists():
            remove_hls_workspace(workspace)
        os.replace(temporary, workspace)
    except Exception:
        _make_writable(temporary)
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return HlsRuntime(
        manifest=runtime.manifest,
        package_root=runtime.package_root,
        package_fingerprint=runtime.package_fingerprint,
        workspace=runtime.workspace,
        staged=True,
        workspace_reused=False,
        needs_stage=False,
    )


def resolve_hls_runtime(
    package: str | None,
    *,
    workspace: str | None = None,
    stage: bool = False,
    force_restage: bool = False,
) -> HlsRuntime:
    package_value = resolve_hls_package_arg(package)
    manifest = load_hls_manifest(package_value)
    fingerprint = package_fingerprint(manifest.root)
    workspace_path = resolve_hls_workspace_arg(workspace, manifest, fingerprint)
    fresh = _stage_is_fresh(workspace_path, manifest=manifest, fingerprint=fingerprint)
    runtime = HlsRuntime(
        manifest=manifest,
        package_root=manifest.root,
        package_fingerprint=fingerprint,
        workspace=workspace_path,
        staged=False,
        workspace_reused=fresh,
        needs_stage=not fresh,
    )
    if stage:
        runtime = stage_hls_runtime(runtime, force=force_restage)
    return runtime


def select_hls_cases(
    manifest: HlsManifest,
    *,
    case_name: str | None,
    all_cases: bool,
) -> tuple[HlsCase, ...]:
    if case_name and all_cases:
        raise XdbError("--case and --all are mutually exclusive")
    cases = manifest.case_map()
    if all_cases:
        return tuple(sorted(manifest.cases, key=lambda item: item.name))
    selected_name = case_name or manifest.default_case
    try:
        return (cases[selected_name],)
    except KeyError as error:
        available = ", ".join(sorted(cases))
        raise XdbError(
            f"unknown HLS C-simulation case {selected_name!r}; available: {available}"
        ) from error


def manifest_summary(manifest: HlsManifest) -> dict[str, Any]:
    return {
        "schema_version": manifest.schema_version,
        "runtime_kind": manifest.runtime_kind,
        "project": manifest.project,
        "top": manifest.top,
        "default_case": manifest.default_case,
        "cases": [case.name for case in sorted(manifest.cases, key=lambda item: item.name)],
        "default_timeout_seconds": manifest.default_timeout_seconds,
        "expected_exit_code": manifest.expected_exit_code,
        "success_marker": manifest.success_marker,
        "prepare": {"path": manifest.prepare.path, "args": list(manifest.prepare.args)},
        "run": {"path": manifest.run.path, "args": list(manifest.run.args)},
        "tool": {
            "family": manifest.tool.family,
            "version": manifest.tool.version,
            "executable": manifest.tool.executable,
            "version_args": list(manifest.tool.version_args),
            "version_regex": manifest.tool.version_regex,
        },
        "environment": {
            "pass": list(manifest.environment.passed),
            "set": dict(sorted(manifest.environment.injected.items())),
        },
        "provenance": {
            "source_revision": manifest.provenance.source_revision,
            "source_sha256": manifest.provenance.source_sha256,
            "configuration_sha256": manifest.provenance.configuration_sha256,
        },
        "compile_flags": list(manifest.compile_flags),
        "artifacts": [
            {
                "path": artifact.path,
                "required": artifact.required,
                "max_bytes": artifact.max_bytes,
            }
            for artifact in manifest.artifacts
        ],
    }
