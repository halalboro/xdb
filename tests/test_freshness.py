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
from xdb.sim.session_store import resolve_launch_spec


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
