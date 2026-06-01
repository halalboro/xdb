from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xdb.sim.client import provenance_session, restage_session
from xdb.sim.session_store import cleanup_stale_session, load_meta, resolve_launch_spec, session_paths, write_meta


class FreshnessControlTests(unittest.TestCase):
    def _create_runtime_package(self, root: Path) -> Path:
        package = root / "runtime-package"
        package.mkdir()
        (package / "compile.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        (package / "elaborate.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        (package / "simulate.sh").write_text("#!/usr/bin/env bash\nxsim work.tb_top\n", encoding="utf-8")
        (package / "payload.txt").write_text("hello\n", encoding="utf-8")
        (package / "xdb-runtime.json").write_text(
            json.dumps(
                {
                    "project": "demo",
                    "work_dir": ".",
                    "compile_script": "compile.sh",
                    "elaborate_script": "elaborate.sh",
                    "simulate_script": "simulate.sh",
                }
            ),
            encoding="utf-8",
        )
        return package

    def test_provenance_reports_staged_workspace_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            (repo / ".git").mkdir()
            package = self._create_runtime_package(tmp_path)
            workspace = tmp_path / "workspace"

            env = {
                "XDB_SIM_PACKAGE_RUNTIME": str(package),
                "XDB_SIM_WORKSPACE": str(workspace),
                "XDB_SIM_SIMSET": "sim_1",
                "XDB_SIM_MODE": "behavioral",
                "XDB_SIM_TOP": "tb_top",
            }
            old_cwd = Path.cwd()
            try:
                os.chdir(repo)
                with patch.dict(os.environ, env, clear=False):
                    resolve_launch_spec(stage=True)
                    provenance = provenance_session(None)
            finally:
                os.chdir(old_cwd)

            self.assertEqual(provenance["session"], "default")
            self.assertEqual(provenance["requested"]["top"], "tb_top")
            self.assertFalse(provenance["live_session"]["present"])
            self.assertTrue(provenance["runtime"]["available"])
            self.assertEqual(provenance["runtime"]["package_runtime"], str(package))
            self.assertEqual(provenance["runtime"]["workspace"], str(workspace))
            self.assertTrue(provenance["runtime"]["workspace_exists"])
            self.assertTrue(provenance["runtime"]["workspace_reused"])
            self.assertFalse(provenance["runtime"]["needs_stage"])
            self.assertIsNotNone(provenance["runtime"]["staged_at"])
            self.assertTrue(provenance["runtime"]["stage_source_matches_package"])
            self.assertTrue(provenance["runtime"]["stage_fingerprint_matches_package"])
            self.assertTrue(provenance["comparisons"]["runtime_root_matches_workspace"])

    def test_provenance_uses_session_metadata_after_positional_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            (repo / ".git").mkdir()
            package = self._create_runtime_package(tmp_path)
            workspace = tmp_path / "workspace"

            old_cwd = Path.cwd()
            try:
                os.chdir(repo)
                with patch.dict(
                    os.environ,
                    {
                        "XDB_ROOT": str(tmp_path / "xdb-root"),
                        "XDB_CACHE_ROOT": str(tmp_path / "cache-root"),
                        "XDB_SIM_PACKAGE_RUNTIME": str(package),
                        "XDB_SIM_WORKSPACE": str(workspace),
                    },
                    clear=True,
                ):
                    spec = resolve_launch_spec(stage=True)

                with patch.dict(
                    os.environ,
                    {
                        "XDB_ROOT": str(tmp_path / "xdb-root"),
                        "XDB_CACHE_ROOT": str(tmp_path / "cache-root"),
                    },
                    clear=True,
                ):
                    paths = session_paths("unit")
                    write_meta(
                        paths,
                        {
                            "pid": 0,
                            "launch_kind": "runtime",
                            "project": spec["project"],
                            "simset": "sim_1",
                            "mode": "behavioral",
                            "top": "tb_top",
                            "package_runtime": spec["package_runtime"],
                            "runtime_root": spec["workspace"],
                            "workspace": spec["workspace"],
                            "work_dir": spec["work_dir"],
                            "compile_script": spec["compile_script"],
                            "elaborate_script": spec["elaborate_script"],
                            "simulate_script": spec["simulate_script"],
                            "state": "closed",
                        },
                    )
                    provenance = provenance_session("unit")
            finally:
                os.chdir(old_cwd)

            self.assertTrue(provenance["runtime"]["available"])
            self.assertEqual(provenance["runtime"]["source"], "session_metadata")
            self.assertEqual(provenance["runtime"]["package_runtime"], str(package))
            self.assertEqual(provenance["runtime"]["workspace"], str(workspace))
            self.assertTrue(provenance["runtime"]["workspace_exists"])
            self.assertFalse(provenance["runtime"]["needs_stage"])
            self.assertTrue(provenance["runtime"]["stage_source_matches_package"])
            self.assertTrue(provenance["comparisons"]["runtime_root_matches_workspace"])

    def test_launch_spec_accepts_cli_package_runtime_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            package = self._create_runtime_package(tmp_path)
            workspace = tmp_path / "workspace"

            with patch.dict(
                os.environ,
                {
                    "XDB_SIM_WORKSPACE": str(workspace),
                    "XDB_SIM_SIMSET": "sim_1",
                    "XDB_SIM_MODE": "behavioral",
                    "XDB_SIM_TOP": "tb_top",
                },
                clear=False,
            ):
                spec = resolve_launch_spec(stage=True, package_runtime=str(package))

            self.assertEqual(spec["package_runtime"], str(package))
            self.assertEqual(spec["workspace"], str(workspace))
            self.assertTrue((workspace / "payload.txt").is_file())

    def test_launch_spec_accepts_package_output_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            package_out = tmp_path / "package-out"
            runtime = package_out / "project" / "sim"
            runtime.mkdir(parents=True)
            self._create_runtime_package(tmp_path)
            source_runtime = tmp_path / "runtime-package"
            for child in source_runtime.iterdir():
                target = runtime / child.name
                if child.is_file():
                    target.write_bytes(child.read_bytes())

            workspace = tmp_path / "workspace"
            with patch.dict(os.environ, {"XDB_SIM_WORKSPACE": str(workspace)}, clear=False):
                spec = resolve_launch_spec(stage=True, package_runtime=str(package_out))

            self.assertEqual(spec["package_runtime"], str(runtime.resolve()))
            self.assertTrue((workspace / "payload.txt").is_file())

    def test_session_paths_split_project_outputs_from_socket_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            (repo / ".git").mkdir()
            old_cwd = Path.cwd()
            try:
                os.chdir(repo)
                with patch.dict(
                    os.environ,
                    {
                        "XDB_ROOT": str(tmp_path / "xdb-root"),
                        "XDB_CACHE_ROOT": str(tmp_path / "cache-root"),
                    },
                    clear=False,
                ):
                    paths = session_paths("unit")
            finally:
                os.chdir(old_cwd)

            self.assertEqual(paths.session_dir.parent, tmp_path / "xdb-root" / "sessions")
            self.assertEqual(paths.daemon_log_path.parent, paths.session_dir)
            self.assertEqual(paths.vivado_log_path.parent, paths.session_dir)
            self.assertEqual(paths.socket_path.parent, tmp_path / "cache-root" / "sockets")

    def test_corrupt_session_meta_is_treated_as_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            (repo / ".git").mkdir()
            cache = tmp_path / "cache"
            old_cwd = Path.cwd()
            try:
                os.chdir(repo)
                with patch.dict(os.environ, {"XDB_ROOT": str(tmp_path / "xdb-root"), "XDB_CACHE_ROOT": str(cache)}, clear=False):
                    paths = session_paths(None)
                    paths.session_dir.mkdir(parents=True)
                    paths.meta_path.write_text("", encoding="utf-8")

                    self.assertIsNone(load_meta(paths))
                    cleanup_stale_session(paths)

                    self.assertFalse(paths.session_dir.exists())
            finally:
                os.chdir(old_cwd)

    def test_restage_rebuilds_workspace_without_live_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            (repo / ".git").mkdir()
            package = self._create_runtime_package(tmp_path)
            workspace = tmp_path / "workspace"

            env = {
                "XDB_SIM_PACKAGE_RUNTIME": str(package),
                "XDB_SIM_WORKSPACE": str(workspace),
                "XDB_SIM_SIMSET": "sim_1",
                "XDB_SIM_MODE": "behavioral",
                "XDB_SIM_TOP": "tb_top",
            }
            old_cwd = Path.cwd()
            try:
                os.chdir(repo)
                with patch.dict(os.environ, env, clear=False):
                    resolve_launch_spec(stage=True)
                    (workspace / "stale.txt").write_text("stale\n", encoding="utf-8")
                    result = restage_session(None)
            finally:
                os.chdir(old_cwd)

            self.assertTrue(result["restaged"])
            self.assertTrue(result["workspace_removed"])
            self.assertEqual(result["workspace"], str(workspace))
            self.assertTrue((workspace / "payload.txt").is_file())
            self.assertFalse((workspace / "stale.txt").exists())
            self.assertTrue(result["provenance"]["runtime"]["workspace_exists"])
            self.assertTrue(result["provenance"]["runtime"]["stage_source_matches_package"])


if __name__ == "__main__":
    unittest.main()
