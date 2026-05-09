from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xdb.sim.client import doctor_session
from xdb.sim.session_store import session_paths, write_meta


class DoctorTests(unittest.TestCase):
    def test_doctor_reports_missing_session_without_throwing(self) -> None:
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
                        "XDB_SIM_CACHE_DIR": str(tmp_path / "cache"),
                        "XDB_SIM_PACKAGE_RUNTIME": "",
                        "XDB_SIM_WORKSPACE": "",
                    },
                    clear=False,
                ):
                    result = doctor_session(None)
            finally:
                os.chdir(old_cwd)

        self.assertFalse(result["ok"])
        checks = {check["name"]: check for check in result["checks"]}
        self.assertFalse(checks["session_metadata"]["ok"])
        self.assertIn("run: xdb sim launch", result["suggestions"])

    def test_doctor_reports_corrupt_metadata_and_suggests_force_close(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            (repo / ".git").mkdir()
            old_cwd = Path.cwd()
            try:
                os.chdir(repo)
                with patch.dict(os.environ, {"XDB_SIM_CACHE_DIR": str(tmp_path / "cache")}, clear=False):
                    paths = session_paths("unit")
                    paths.session_dir.mkdir(parents=True)
                    paths.meta_path.write_text("", encoding="utf-8")
                    result = doctor_session("unit")
            finally:
                os.chdir(old_cwd)

        self.assertFalse(result["ok"])
        checks = {check["name"]: check for check in result["checks"]}
        self.assertFalse(checks["session_metadata"]["ok"])
        self.assertIn("run: xdb sim close --force", result["suggestions"])

    def test_doctor_reports_dead_cached_pid_and_missing_socket(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            (repo / ".git").mkdir()
            old_cwd = Path.cwd()
            try:
                os.chdir(repo)
                with patch.dict(os.environ, {"XDB_SIM_CACHE_DIR": str(tmp_path / "cache")}, clear=False):
                    paths = session_paths("unit")
                    write_meta(
                        paths,
                        {
                            "pid": 999999999,
                            "socket_path": str(paths.socket_path),
                            "state": "ready",
                        },
                    )
                    result = doctor_session("unit")
            finally:
                os.chdir(old_cwd)

        self.assertFalse(result["ok"])
        checks = {check["name"]: check for check in result["checks"]}
        self.assertFalse(checks["daemon_pid_alive"]["ok"])
        self.assertFalse(checks["control_socket_exists"]["ok"])
        self.assertIn("run: xdb sim close --force", result["suggestions"])


if __name__ == "__main__":
    unittest.main()
