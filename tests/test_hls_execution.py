from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hls_test_support import FakeHlsPackage
from xdb.errors import XdbError
from xdb.hls.bundles import create_hls_bundle
from xdb.hls.diagnostics import hls_doctor, hls_provenance
from xdb.hls.runner import run_hls_sim
from xdb.hls.runtime import resolve_hls_runtime


class HlsExecutionTests(unittest.TestCase):
    def _pid_alive(self, pid: int) -> bool:
        try:
            state = (
                Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].strip()[0]
            )
            if state == "Z":
                return False
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def _wait_pid_gone(self, pid: int, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._pid_alive(pid):
                return True
            time.sleep(0.05)
        return not self._pid_alive(pid)

    def test_named_case_success_captures_logs_result_and_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = FakeHlsPackage(Path(temporary))
            original_manifest = fixture.manifest_path.read_bytes()
            with patch.dict(os.environ, fixture.environment(), clear=False):
                result = run_hls_sim(None, case_name="pass")
                provenance = hls_provenance(None, case_name="pass")

            self.assertEqual(
                list(result),
                [
                    "schema",
                    "xdb_version",
                    "invocation",
                    "ok",
                    "status",
                    "run_id",
                    "started_at",
                    "finished_at",
                    "duration_seconds",
                    "package_runtime",
                    "package_fingerprint",
                    "manifest_path",
                    "workspace",
                    "staged",
                    "workspace_reused",
                    "policy",
                    "selected_cases",
                    "timeout_seconds",
                    "manifest",
                    "tool",
                    "prepare",
                    "cases",
                    "artifacts",
                    "result_path",
                ],
            )
            self.assertEqual(result["schema"], "xdb-hls-csim-result-v1")
            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["selected_cases"], ["pass"])
            self.assertEqual(result["tool"]["observed_version"], "2023.2")
            self.assertEqual(result["prepare"]["status"], "passed")
            self.assertEqual(result["cases"][0]["status"], "passed")
            self.assertTrue(Path(result["cases"][0]["stdout_path"]).is_file())
            self.assertIn("fixed=manifest", Path(result["cases"][0]["stdout_path"]).read_text())
            self.assertTrue(Path(result["result_path"]).is_file())
            self.assertEqual(provenance["package_fingerprint"], result["package_fingerprint"])
            self.assertEqual(provenance["last_result"]["status"], "passed")
            self.assertEqual(provenance["observed_tool_version"], "2023.2")
            self.assertEqual(fixture.manifest_path.read_bytes(), original_manifest)

    def test_all_cases_are_sorted_with_explicit_failure_policy(self) -> None:
        cases = [
            {"name": "zeta", "args": ["pass"], "fixtures": ["fixtures/input.txt"]},
            {"name": "alpha", "args": ["fail"], "fixtures": ["fixtures/input.txt"]},
            {"name": "middle", "args": ["pass"], "fixtures": ["fixtures/input.txt"]},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            fixture = FakeHlsPackage(Path(temporary), cases=cases)
            with patch.dict(os.environ, fixture.environment(), clear=False):
                fail_fast = run_hls_sim(None, all_cases=True)
                continued = run_hls_sim(None, all_cases=True, continue_on_failure=True)

            self.assertFalse(fail_fast["ok"])
            self.assertEqual([case["name"] for case in fail_fast["cases"]], ["alpha"])
            self.assertEqual(fail_fast["cases"][0]["status"], "unexpected_exit")
            self.assertEqual(
                [case["name"] for case in continued["cases"]],
                ["alpha", "middle", "zeta"],
            )
            self.assertEqual(continued["policy"], "continue-on-failure")

    def test_prepare_failure_and_missing_success_marker_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = FakeHlsPackage(Path(temporary), prepare_exit=7)
            with patch.dict(os.environ, fixture.environment(), clear=False):
                result = run_hls_sim(None)
            self.assertEqual(result["status"], "prepare_failed")
            self.assertEqual(result["prepare"]["exit_code"], 7)
            self.assertEqual(result["cases"], [])

        cases = [{"name": "nomarker", "args": ["nomarker"], "fixtures": ["fixtures/input.txt"]}]
        with tempfile.TemporaryDirectory() as temporary:
            fixture = FakeHlsPackage(Path(temporary), cases=cases)
            with patch.dict(os.environ, fixture.environment(), clear=False):
                result = run_hls_sim(None)
            self.assertEqual(result["status"], "missing_success_marker")
            self.assertEqual(result["cases"][0]["exit_code"], 0)
            self.assertFalse(result["cases"][0]["success_marker_found"])

    def test_case_may_override_expected_exit_and_success_marker(self) -> None:
        cases = [
            {
                "name": "expected-failure",
                "args": ["fail"],
                "fixtures": ["fixtures/input.txt"],
                "expected_exit_code": 3,
                "success_marker": "intentional failure",
            }
        ]
        with tempfile.TemporaryDirectory() as temporary:
            fixture = FakeHlsPackage(Path(temporary), cases=cases)
            with patch.dict(os.environ, fixture.environment(), clear=False):
                result = run_hls_sim(None)

            self.assertTrue(result["ok"])
            self.assertEqual(result["cases"][0]["exit_code"], 3)
            self.assertTrue(result["cases"][0]["success_marker_found"])

    def test_wrong_tool_version_fails_before_prepare(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = FakeHlsPackage(Path(temporary), observed_tool_version="2025.1")
            with patch.dict(os.environ, fixture.environment(), clear=False):
                result = run_hls_sim(None)

            self.assertEqual(result["status"], "tool_version_mismatch")
            self.assertEqual(result["tool"]["observed_version"], "2025.1")
            self.assertIsNone(result["prepare"])
            self.assertEqual(result["cases"], [])

    def test_signal_termination_is_recorded_and_fails(self) -> None:
        cases = [{"name": "signal", "args": ["signal"], "fixtures": ["fixtures/input.txt"]}]
        with tempfile.TemporaryDirectory() as temporary:
            fixture = FakeHlsPackage(Path(temporary), cases=cases)
            with patch.dict(os.environ, fixture.environment(), clear=False):
                result = run_hls_sim(None)

            self.assertEqual(result["status"], "unexpected_exit")
            self.assertEqual(result["cases"][0]["termination_signal"], signal.SIGTERM)
            self.assertEqual(result["cases"][0]["exit_code"], -signal.SIGTERM)

    def test_missing_required_artifact_fails_after_successful_case(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = FakeHlsPackage(Path(temporary))
            manifest = fixture.read_manifest()
            manifest["artifacts"][0]["path"] = "outputs/missing.log"
            fixture.write_manifest(manifest)
            with patch.dict(os.environ, fixture.environment(), clear=False):
                result = run_hls_sim(None)

            self.assertEqual(result["status"], "missing_required_artifact")
            self.assertFalse(result["artifacts"][0]["exists"])

    def test_workspace_lock_prevents_concurrent_restage_or_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = FakeHlsPackage(Path(temporary))
            with patch.dict(os.environ, fixture.environment(), clear=False):
                runtime = resolve_hls_runtime(None, stage=False)
                lock_path = runtime.workspace.parent / f".{runtime.workspace.name}.xdb-hls.lock"
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                with lock_path.open("a+", encoding="utf-8") as lock:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    with self.assertRaisesRegex(XdbError, "already in use"):
                        run_hls_sim(None, force_restage=True)

            self.assertFalse(fixture.workspace.exists())

    def test_timeout_terminates_complete_child_process_group(self) -> None:
        cases = [{"name": "spawn", "args": ["spawn"], "fixtures": ["fixtures/input.txt"]}]
        with tempfile.TemporaryDirectory() as temporary:
            fixture = FakeHlsPackage(Path(temporary), cases=cases)
            with patch.dict(os.environ, fixture.environment(), clear=False):
                result = run_hls_sim(None, timeout_seconds=0.3)

            child_pid = int((fixture.workspace / "child.pid").read_text())
            self.assertEqual(result["status"], "timed_out")
            self.assertTrue(result["cases"][0]["timed_out"])
            self.assertTrue(self._wait_pid_gone(child_pid), f"child process {child_pid} survived")
            self.assertTrue(Path(result["cases"][0]["stderr_path"]).is_file())
            with patch.dict(os.environ, fixture.environment(), clear=False):
                doctor = hls_doctor(None, probe_tool=False)
            abnormal = next(
                check for check in doctor["checks"] if check["name"] == "prior_abnormal_termination"
            )
            self.assertFalse(abnormal["ok"])

    def test_cli_failure_returns_nonzero_with_stable_json(self) -> None:
        cases = [{"name": "nomarker", "args": ["nomarker"], "fixtures": ["fixtures/input.txt"]}]
        with tempfile.TemporaryDirectory() as temporary:
            fixture = FakeHlsPackage(Path(temporary), cases=cases)
            environment = os.environ.copy()
            environment.update(fixture.environment())
            environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
            process = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "xdb.cli",
                    "hls",
                    "sim",
                    str(fixture.package),
                    "--workspace",
                    str(fixture.workspace),
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(process.returncode, 1, process.stderr)
            result = json.loads(process.stdout)
            self.assertEqual(result["schema"], "xdb-hls-csim-result-v1")
            self.assertEqual(result["status"], "missing_success_marker")

    def test_interrupt_retains_result_and_terminates_child(self) -> None:
        cases = [{"name": "spawn", "args": ["spawn"], "fixtures": ["fixtures/input.txt"]}]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = FakeHlsPackage(root, cases=cases)
            environment = os.environ.copy()
            environment.update(fixture.environment())
            environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "xdb.cli",
                    "hls",
                    "sim",
                    str(fixture.package),
                    "--workspace",
                    str(fixture.workspace),
                    "--case",
                    "spawn",
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            child_file = fixture.workspace / "child.pid"
            deadline = time.monotonic() + 5
            while (
                not child_file.is_file() and process.poll() is None and time.monotonic() < deadline
            ):
                time.sleep(0.05)
            self.assertTrue(child_file.is_file())
            child_pid = int(child_file.read_text())
            os.kill(process.pid, signal.SIGINT)
            stdout, stderr = process.communicate(timeout=10)

            self.assertEqual(process.returncode, 130, stderr)
            result = json.loads(stdout)
            self.assertEqual(result["status"], "interrupted")
            self.assertTrue(self._wait_pid_gone(child_pid), f"child process {child_pid} survived")
            runtime = resolve_hls_runtime(
                str(fixture.package), workspace=str(fixture.workspace), stage=False
            )
            retained = json.loads(runtime.last_result_path.read_text())
            self.assertEqual(retained["status"], "interrupted")

    def test_doctor_returns_structured_failure_for_malformed_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = FakeHlsPackage(Path(temporary))
            manifest = fixture.read_manifest()
            manifest["runtime_kind"] = "wrong"
            fixture.write_manifest(manifest)
            with patch.dict(os.environ, fixture.environment(), clear=False):
                doctor = hls_doctor(None)

            self.assertFalse(doctor["ok"])
            self.assertEqual(doctor["checks"][0]["name"], "runtime_manifest")
            self.assertIn("runtime kind", doctor["checks"][0]["detail"])

    def test_doctor_reports_tool_mismatch_stale_stage_and_prior_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = FakeHlsPackage(root)
            with patch.dict(os.environ, fixture.environment(), clear=False):
                run_hls_sim(None)
                fixture._write_executable(
                    fixture.tool_bin / "vitis_hls",
                    f"#!{sys.executable}\nprint('vitis_hls v2025.1')\n",
                )
                (fixture.package / "changed.txt").write_text("changed\n", encoding="utf-8")
                runtime = resolve_hls_runtime(None, stage=False)
                runtime.active_path.write_text(
                    json.dumps({"pid": 99999999, "run_id": "stale"}), encoding="utf-8"
                )
                doctor = hls_doctor(None)

            checks = {check["name"]: check for check in doctor["checks"]}
            self.assertFalse(doctor["ok"])
            self.assertFalse(checks["workspace_fresh"]["ok"])
            self.assertFalse(checks["tool_version"]["ok"])
            self.assertFalse(checks["active_run"]["ok"])
            self.assertFalse(checks["active_run"]["data"]["alive"])
            self.assertIn("--restage", "\n".join(doctor["suggestions"]))

    def test_bundle_rejects_runtime_artifact_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = FakeHlsPackage(root)
            bundle = root / "bundle"
            outside = root / "secret.txt"
            outside.write_text("secret\n", encoding="utf-8")
            with patch.dict(os.environ, fixture.environment(), clear=False):
                run_hls_sim(None)
                artifact = fixture.workspace / "outputs" / "run.log"
                artifact.unlink()
                artifact.symlink_to(outside)
                create_hls_bundle(
                    None,
                    workspace=str(fixture.workspace),
                    out=str(bundle),
                )

            manifest = json.loads((bundle / "manifest.json").read_text())
            escaped = next(
                item for item in manifest["omitted"] if item.get("path") == "outputs/run.log"
            )
            self.assertIn("escapes", escaped["reason"])
            self.assertNotIn("secret", "\n".join(manifest["files"]))

    def test_bundle_is_bounded_and_contains_deterministic_evidence_set(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = FakeHlsPackage(root, artifact_max_bytes=20)
            bundle = root / "bundle"
            second_bundle = root / "bundle-second"
            with patch.dict(os.environ, fixture.environment(), clear=False):
                run_hls_sim(None)
                result = create_hls_bundle(
                    None,
                    workspace=str(fixture.workspace),
                    out=str(bundle),
                    max_bytes=1024 * 1024,
                )
                create_hls_bundle(
                    None,
                    workspace=str(fixture.workspace),
                    out=str(second_bundle),
                    max_bytes=1024 * 1024,
                )

            files = result["files"]
            self.assertEqual(files, sorted(files))
            self.assertIn("manifest.json", files)
            self.assertIn("runtime/xdb-hls-csim.json", files)
            self.assertIn("result.json", files)
            self.assertIn("doctor.json", files)
            self.assertIn("provenance.json", files)
            self.assertTrue(any(path.startswith("logs/") for path in files))
            bundle_manifest = json.loads((bundle / "manifest.json").read_text())
            artifact_record = next(
                item
                for item in bundle_manifest["copied"]
                if item["source"].endswith("outputs/run.log")
            )
            self.assertTrue(artifact_record["truncated"])
            self.assertEqual(artifact_record["copied_bytes"], 20)
            self.assertFalse(any("fixtures/input.txt" in path for path in files))
            for relative in files:
                self.assertEqual(
                    (bundle / relative).read_bytes(),
                    (second_bundle / relative).read_bytes(),
                    relative,
                )


if __name__ == "__main__":
    unittest.main()
