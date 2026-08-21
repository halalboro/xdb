from __future__ import annotations

import json
import os
import sys
from pathlib import Path


class FakeHlsPackage:
    def __init__(
        self,
        root: Path,
        *,
        cases: list[dict] | None = None,
        prepare_exit: int = 0,
        expected_tool_version: str = "2023.2",
        observed_tool_version: str = "2023.2",
        success_marker: str | None = "HLS_CSIM_PASS",
        artifact_max_bytes: int = 1024 * 1024,
    ):
        self.root = root
        self.package = root / "package"
        self.workspace = root / "workspace"
        self.tool_bin = root / "tool-bin"
        self.package.mkdir(parents=True)
        self.tool_bin.mkdir(parents=True)
        (self.package / "bin").mkdir()
        (self.package / "fixtures").mkdir()
        (self.package / "fixtures" / "input.txt").write_text("fixture\n", encoding="utf-8")
        self._write_executable(
            self.tool_bin / "vitis_hls",
            f'#!{sys.executable}\nprint("vitis_hls v{observed_tool_version}")\n',
        )
        self._write_executable(
            self.package / "bin" / "prepare",
            f"""#!{sys.executable}
from pathlib import Path
print("prepare stdout")
Path("prepare.stderr").write_text("prepared\\n", encoding="utf-8")
raise SystemExit({prepare_exit})
""",
        )
        self._write_executable(
            self.package / "bin" / "run",
            f"#!{sys.executable}\n"
            + """import os
import signal
import subprocess
import sys
from pathlib import Path

case = sys.argv[1]
Path("outputs").mkdir(exist_ok=True)
Path("outputs/run.log").write_text((case + "\\n") * 64, encoding="utf-8")
Path("case-order.log").open("a", encoding="utf-8").write(case + "\\n")
print(f"case={case} fixed={os.environ.get('FIXED', '')}")
if case == "fail":
    print("intentional failure", file=sys.stderr)
    raise SystemExit(3)
if case == "nomarker":
    raise SystemExit(0)
if case == "signal":
    os.kill(os.getpid(), signal.SIGTERM)
if case == "spawn":
    child = subprocess.Popen(["sleep", "60"])
    Path("child.pid").write_text(str(child.pid), encoding="utf-8")
    child.wait()
print("HLS_CSIM_PASS")
""",
        )
        selected_cases = cases or [
            {"name": "pass", "args": ["pass"], "fixtures": ["fixtures/input.txt"]}
        ]
        manifest = {
            "schema_version": 1,
            "runtime_kind": "hls-csim",
            "project": "fake-d3",
            "top": "decoderTop",
            "prepare": {"path": "bin/prepare", "args": []},
            "run": {"path": "bin/run", "args": []},
            "cases": selected_cases,
            "default_case": selected_cases[0]["name"],
            "default_timeout_seconds": 5,
            "expected_exit_code": 0,
            "tool": {
                "family": "vitis_hls",
                "version": expected_tool_version,
                "executable": "vitis_hls",
                "version_args": ["-version"],
                "version_regex": "v([0-9]+\\.[0-9]+)",
            },
            "environment": {"pass": ["PATH"], "set": {"FIXED": "manifest"}},
            "provenance": {
                "source_revision": "0123456789abcdef0123456789abcdef01234567",
                "source_sha256": "a" * 64,
                "configuration_sha256": "b" * 64,
            },
            "compile_flags": ["-DQUEKUF_CODE_DISTANCE=3"],
            "artifacts": [
                {
                    "path": "outputs/run.log",
                    "required": True,
                    "max_bytes": artifact_max_bytes,
                }
            ],
        }
        if success_marker is not None:
            manifest["success_marker"] = success_marker
        self.manifest_path = self.package / "xdb-hls-csim.json"
        self.write_manifest(manifest)

    def _write_executable(self, path: Path, text: str) -> None:
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)

    def read_manifest(self) -> dict:
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def write_manifest(self, manifest: dict) -> None:
        self.manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def environment(self) -> dict[str, str]:
        return {
            "PATH": f"{self.tool_bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "XDB_HLS_PACKAGE_RUNTIME": str(self.package),
            "XDB_HLS_WORKSPACE": str(self.workspace),
        }
