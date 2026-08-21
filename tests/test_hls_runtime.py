from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hls_test_support import FakeHlsPackage
from xdb.errors import XdbError
from xdb.hls.runtime import (
    discover_hls_manifest,
    load_hls_manifest,
    resolve_hls_runtime,
    select_hls_cases,
)


class HlsRuntimeTests(unittest.TestCase):
    def test_checked_machine_readable_schema_matches_runtime_identity(self) -> None:
        schema_path = (
            Path(__file__).resolve().parents[1] / "schemas" / "xdb-hls-csim-v1.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["schema_version"]["const"], 1)
        self.assertEqual(schema["properties"]["runtime_kind"]["const"], "hls-csim")
        self.assertIn("cases", schema["required"])
        self.assertIn("provenance", schema["required"])

    def test_manifest_discovery_accepts_runtime_manifest_and_package_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = FakeHlsPackage(root / "direct")
            package_output = root / "output"
            nested = package_output / "project" / "hls"
            nested.parent.mkdir(parents=True)
            shutil.copytree(fixture.package, nested)

            self.assertEqual(discover_hls_manifest(nested), nested / "xdb-hls-csim.json")
            self.assertEqual(discover_hls_manifest(package_output), nested / "xdb-hls-csim.json")
            self.assertEqual(
                discover_hls_manifest(nested / "xdb-hls-csim.json"),
                nested / "xdb-hls-csim.json",
            )

    def test_manifest_parsing_and_case_selection_are_strict_and_deterministic(self) -> None:
        cases = [
            {"name": "zeta", "args": ["pass"], "fixtures": ["fixtures/input.txt"]},
            {"name": "alpha", "args": ["pass"], "fixtures": ["fixtures/input.txt"]},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            fixture = FakeHlsPackage(Path(temporary), cases=cases)
            manifest = load_hls_manifest(fixture.package)

        self.assertEqual(manifest.project, "fake-d3")
        self.assertEqual(manifest.tool.version, "2023.2")
        self.assertEqual(
            [case.name for case in select_hls_cases(manifest, case_name=None, all_cases=True)],
            ["alpha", "zeta"],
        )
        self.assertEqual(
            select_hls_cases(manifest, case_name="alpha", all_cases=False)[0].name,
            "alpha",
        )
        with self.assertRaisesRegex(XdbError, "unknown HLS C-simulation case"):
            select_hls_cases(manifest, case_name="missing", all_cases=False)

    def test_manifest_rejects_schema_kind_unknown_fields_and_duplicate_cases(self) -> None:
        mutations = [
            (lambda data: data.update(schema_version=2), "schema version"),
            (lambda data: data.update(runtime_kind="hls-cosim"), "runtime kind"),
            (lambda data: data.update(unknown=True), "unsupported field"),
            (
                lambda data: data["cases"].append(dict(data["cases"][0])),
                "duplicate case",
            ),
        ]
        for mutate, message in mutations:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as temporary:
                fixture = FakeHlsPackage(Path(temporary))
                manifest = fixture.read_manifest()
                mutate(manifest)
                fixture.write_manifest(manifest)
                with self.assertRaisesRegex(XdbError, message):
                    load_hls_manifest(fixture.package)

    def test_manifest_rejects_traversal_missing_fixture_and_non_executable_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = FakeHlsPackage(Path(temporary))
            manifest = fixture.read_manifest()
            manifest["run"]["path"] = "../run"
            fixture.write_manifest(manifest)
            with self.assertRaisesRegex(XdbError, "traversing path component"):
                load_hls_manifest(fixture.package)

        with tempfile.TemporaryDirectory() as temporary:
            fixture = FakeHlsPackage(Path(temporary))
            manifest = fixture.read_manifest()
            manifest["cases"][0]["fixtures"] = ["fixtures/missing.txt"]
            fixture.write_manifest(manifest)
            with self.assertRaisesRegex(XdbError, "does not exist"):
                load_hls_manifest(fixture.package)

        with tempfile.TemporaryDirectory() as temporary:
            fixture = FakeHlsPackage(Path(temporary))
            run = fixture.package / "bin" / "run"
            run.chmod(stat.S_IRUSR | stat.S_IWUSR)
            with self.assertRaisesRegex(XdbError, "not executable"):
                load_hls_manifest(fixture.package)

    def test_manifest_rejects_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = FakeHlsPackage(root)
            outside = root / "outside.txt"
            outside.write_text("outside\n", encoding="utf-8")
            (fixture.package / "escape").symlink_to(outside)

            with self.assertRaisesRegex(XdbError, "symlink escapes"):
                load_hls_manifest(fixture.package)

    def test_staging_is_writable_reused_and_refreshed_by_content_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = FakeHlsPackage(root)
            with patch.dict(os.environ, fixture.environment(), clear=False):
                first = resolve_hls_runtime(None, stage=True)
                second = resolve_hls_runtime(None, stage=True)
                (fixture.package / "new-input.txt").write_text("new\n", encoding="utf-8")
                stale = resolve_hls_runtime(None, stage=False)
                refreshed = resolve_hls_runtime(None, stage=True)

            self.assertTrue(first.staged)
            self.assertTrue(os.access(first.workspace / "bin" / "run", os.W_OK))
            self.assertTrue(second.workspace_reused)
            self.assertFalse(second.staged)
            self.assertTrue(stale.needs_stage)
            self.assertTrue(refreshed.staged)
            self.assertTrue((refreshed.workspace / "new-input.txt").is_file())
            self.assertFalse((fixture.package / ".xdb-hls").exists())

    def test_staging_refuses_to_replace_an_unrelated_workspace_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = FakeHlsPackage(root)
            fixture.workspace.mkdir()
            preserved = fixture.workspace / "user-data.txt"
            preserved.write_text("preserve\n", encoding="utf-8")

            with patch.dict(os.environ, fixture.environment(), clear=False):
                with self.assertRaisesRegex(XdbError, "refusing to replace non-XDB directory"):
                    resolve_hls_runtime(None, stage=True)

            self.assertEqual(preserved.read_text(encoding="utf-8"), "preserve\n")

    def test_config_and_environment_paths_do_not_modify_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = FakeHlsPackage(root)
            config = root / "xdb.toml"
            config.write_text(
                "[hls]\n"
                f'package_runtime = "{fixture.package}"\n'
                f'workspace = "{fixture.workspace}"\n',
                encoding="utf-8",
            )
            before = fixture.manifest_path.read_bytes()
            with patch.dict(os.environ, {"XDB_CONFIG_FILE": str(config)}, clear=False):
                runtime = resolve_hls_runtime(None, stage=True)

            self.assertEqual(runtime.workspace, fixture.workspace)
            self.assertEqual(fixture.manifest_path.read_bytes(), before)
            stamp = json.loads((fixture.workspace / ".xdb-hls-stage.json").read_text())
            self.assertEqual(stamp["source_root"], str(fixture.package))


if __name__ == "__main__":
    unittest.main()
