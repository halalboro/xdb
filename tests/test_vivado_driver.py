from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xdb.sim.vivado_driver import VivadoSimDriver


class VivadoSimDriverRuntimeLaunchTests(unittest.TestCase):
    def _write_fake_xsim(self, bin_dir: Path, tool_name: str) -> None:
        bin_dir.mkdir(parents=True)
        executable = bin_dir / "xsim"
        executable.write_text(
            f"""#!{sys.executable}
import json
import os
import re
import sys
from pathlib import Path

capture = {{
    "tool": {tool_name!r},
    "version": os.environ.get("FAKE_XSIM_VERSION", ""),
    "library_path": os.environ.get("LD_LIBRARY_PATH", ""),
    "path": os.environ.get("PATH", ""),
    "arguments": sys.argv[1:],
}}
Path(os.environ["FAKE_XSIM_CAPTURE_FILE"]).write_text(
    json.dumps(capture),
    encoding="utf-8",
)
for line in sys.stdin:
    match = re.match(r'^set __xdb_request_id "([^"]+)"', line)
    if match:
        request_id = match.group(1)
        print(f"__XDB_BEGIN__ {{request_id}}", flush=True)
        print(json.dumps({{"ok": True, "time": "0 ns"}}), flush=True)
        print(f"__XDB_END__ {{request_id}}", flush=True)
    if line.strip() == "exit":
        break
""",
            encoding="utf-8",
        )
        executable.chmod(0o755)

    def test_runtime_launch_uses_script_environment_arguments_and_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work_dir = root / "runtime workspace"
            work_dir.mkdir()
            ambient_bin = root / "ambient toolchain" / "bin"
            packaged_bin = root / "packaged toolchain" / "bin"
            ambient_lib = root / "ambient toolchain" / "lib"
            packaged_lib = root / "packaged toolchain" / "lib"
            ambient_lib.mkdir(parents=True)
            packaged_lib.mkdir(parents=True)
            self._write_fake_xsim(ambient_bin, "ambient")
            self._write_fake_xsim(packaged_bin, "packaged")

            capture_file = root / "captured launch.json"
            compile_script = work_dir / "compile.sh"
            elaborate_script = work_dir / "elaborate.sh"
            simulate_script = work_dir / "simulate generated.sh"
            compile_script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            elaborate_script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            simulate_script.write_text(
                f"""#!/bin/bash -f
set -Eeuo pipefail
export FAKE_XSIM_VERSION='packaged-version'
export LD_LIBRARY_PATH={shlex.quote(str(packaged_lib))}:"${{LD_LIBRARY_PATH:-}}"
export PATH={shlex.quote(str(packaged_bin))}:"$PATH"
snapshot_name='work.tb top'
extra_argument='VALUE=a b'
xsim "$snapshot_name" -key '{{Behavioral:sim_1:Functional:tb top}}' -tclbatch 'commands with spaces.tcl' -log 'simulate output.log' --define "$extra_argument"
""",
                encoding="utf-8",
            )

            original_path = os.environ.get("PATH", "")
            parent_path = str(ambient_bin)
            if original_path:
                parent_path += os.pathsep + original_path
            launch_env = {
                "PATH": parent_path,
                "LD_LIBRARY_PATH": str(ambient_lib),
                "FAKE_XSIM_VERSION": "ambient-version",
                "FAKE_XSIM_CAPTURE_FILE": str(capture_file),
            }
            driver = VivadoSimDriver(
                project="",
                simset="sim_1",
                mode="behavioral",
                top="tb_top",
                vivado_log_path=str(work_dir / "vivado.log"),
                work_dir=str(work_dir),
                compile_script=str(compile_script),
                elaborate_script=str(elaborate_script),
                simulate_script=str(simulate_script),
            )
            process = None
            with patch.dict(os.environ, launch_env, clear=False):
                try:
                    driver.start(timeout=5)
                    process = driver.proc
                    self.assertIsNotNone(process)
                    self.assertEqual(driver.status()["time"], "0 ns")
                finally:
                    driver.shutdown()

            self.assertIsNotNone(process)
            if process is not None:
                self.assertIsNotNone(process.poll())
            self.assertIsNone(driver.proc)

            captured = json.loads(capture_file.read_text(encoding="utf-8"))
            self.assertEqual(captured["tool"], "packaged")
            self.assertEqual(captured["version"], "packaged-version")
            self.assertEqual(
                captured["library_path"],
                f"{packaged_lib}{os.pathsep}{ambient_lib}",
            )
            self.assertEqual(captured["path"], f"{packaged_bin}{os.pathsep}{parent_path}")
            self.assertEqual(
                captured["arguments"],
                [
                    "work.tb top",
                    "-key",
                    "{Behavioral:sim_1:Functional:tb top}",
                    "-log",
                    "simulate output.log",
                    "--define",
                    "VALUE=a b",
                ],
            )

    def test_runtime_launcher_rejects_tclbatch_without_script_argument(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            simulate_script = work_dir / "simulate.sh"
            simulate_script.write_text(
                "#!/bin/bash -f\nset -Eeuo pipefail\nxsim work.tb_top -tclbatch\n",
                encoding="utf-8",
            )
            driver = VivadoSimDriver(
                project="",
                simset="sim_1",
                mode="behavioral",
                top="tb_top",
                vivado_log_path=str(work_dir / "vivado.log"),
                work_dir=str(work_dir),
                simulate_script=str(simulate_script),
            )

            completed = subprocess.run(
                driver._runtime_simulate_command(),
                cwd=work_dir,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(completed.returncode, 2)
            self.assertIn("-tclbatch is missing its script argument", completed.stderr)


if __name__ == "__main__":
    unittest.main()
