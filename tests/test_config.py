from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xdb.config import config_path_value, load_config, set_config_file
from xdb.sim.trace_profiles import get_trace_profile


class ConfigTests(unittest.TestCase):
    def tearDown(self) -> None:
        set_config_file(None)

    def test_loads_toml_config_from_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_file = Path(tmp) / "sim" / "xdb.conf"
            config_file.parent.mkdir()
            config_file.write_text('trace_profile_file = "profiles.json"\n', encoding="utf-8")
            with patch.dict(os.environ, {"XDB_CONFIG_FILE": str(config_file)}, clear=False):
                loaded = load_config()
                profile_path = config_path_value("trace_profile_file")

        self.assertEqual(loaded["source"], str(config_file))
        self.assertEqual(profile_path, str(config_file.parent / "profiles.json"))

    def test_cli_override_takes_precedence_over_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_config = Path(tmp) / "env.toml"
            cli_config = Path(tmp) / "cli.toml"
            env_config.write_text('trace_profile_file = "env.json"\n', encoding="utf-8")
            cli_config.write_text('trace_profile_file = "cli.json"\n', encoding="utf-8")
            with patch.dict(os.environ, {"XDB_CONFIG_FILE": str(env_config)}, clear=False):
                set_config_file(str(cli_config))
                profile_path = config_path_value("trace_profile_file")

        self.assertEqual(profile_path, str(cli_config.parent / "cli.json"))

    def test_trace_profile_file_can_come_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_file = root / "sim" / "xdb.conf"
            profile_file = root / "sim" / "profiles.json"
            config_file.parent.mkdir()
            config_file.write_text('trace_profile_file = "profiles.json"\n', encoding="utf-8")
            profile_file.write_text(
                '{"profiles":{"smoke":{"transactions":true}}}',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"XDB_CONFIG_FILE": str(config_file)}, clear=False):
                result = get_trace_profile("smoke")

        self.assertEqual(result["source"], str(profile_file))
        self.assertTrue(result["config"]["transactions"])


if __name__ == "__main__":
    unittest.main()
