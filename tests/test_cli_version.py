#!/usr/bin/env python3
"""Task 073 (C6): `tasks --version` prints the plugin version (was "Unknown command")."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN = _HERE.parent / "plugins/playbook"


class CliVersion(unittest.TestCase):
    def test_version_flag_prints_plugin_json_version(self):
        want = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
        env = dict(os.environ, PYTHONPATH=str(PLUGIN))
        for flag in ("--version", "version", "-V"):
            r = subprocess.run([sys.executable, "-m", "tasks.cli", flag], env=env,
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), want, flag)


if __name__ == "__main__":
    unittest.main()
