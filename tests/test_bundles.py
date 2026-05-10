from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xdb.sim.bundles import create_sim_bundle, resolve_bundle_dir
from xdb.sim.session_store import session_paths, write_meta


class BundleTests(unittest.TestCase):
    def test_resolve_relative_bundle_dir_under_xdb_root(self) -> None:
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
                    bundle_dir = resolve_bundle_dir("unit", "fail-001")
            finally:
                os.chdir(old_cwd)

        self.assertEqual(bundle_dir, tmp_path / "xdb-root" / "artifacts" / "bundles" / "fail-001")

    def test_create_bundle_writes_core_artifacts_and_logs(self) -> None:
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
                    write_meta(paths, {"pid": 999999999, "socket_path": str(paths.socket_path)})
                    paths.daemon_log_path.write_text("daemon log\n", encoding="utf-8")
                    paths.vivado_log_path.write_text("vivado log\n", encoding="utf-8")
                    result = create_sim_bundle(
                        "unit",
                        out="fail-001",
                        doctor={"ok": False},
                        provenance={"session": "unit"},
                        trace_result={
                            "action": {
                                "result": {
                                    "kind": "exec",
                                    "stdout": "host out\n",
                                    "stderr": "host err\n",
                                }
                            }
                        },
                    )
                bundle_dir = Path(result["bundle_dir"])
                self.assertTrue((bundle_dir / "manifest.json").is_file())
                self.assertTrue((bundle_dir / "doctor.json").is_file())
                self.assertTrue((bundle_dir / "provenance.json").is_file())
                self.assertTrue((bundle_dir / "metadata.json").is_file())
                self.assertTrue((bundle_dir / "trace.json").is_file())
                self.assertEqual((bundle_dir / "logs" / "daemon.log").read_text(encoding="utf-8"), "daemon log\n")
                self.assertEqual((bundle_dir / "logs" / "vivado.log").read_text(encoding="utf-8"), "vivado log\n")
                self.assertEqual((bundle_dir / "host" / "stdout.txt").read_text(encoding="utf-8"), "host out\n")
                manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
                self.assertTrue(manifest["contains_trace"])
                self.assertIn("trace.json", manifest["files"])
            finally:
                os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main()
