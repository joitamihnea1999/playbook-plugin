#!/usr/bin/env python3
"""Step 1 (scaffold) tests for the dev-only judge benchmark harness `bench/`.

The harness is dev tooling at the repo root (like `arena/`), never shipped in
`plugins/playbook/`. These tests ride `scripts/verify` via `tests/` discovery
and import `bench/` by path from the repo root — no production module is
touched. Hermetic: temp dirs only, no provider CLI is ever invoked.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_ENTRY = _ROOT / "bench" / "judgebench.py"


def _run(*args, cwd=None):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([sys.executable, str(_ENTRY), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", cwd=cwd or str(_ROOT), env=env,
                          timeout=120)


class ScaffoldTests(unittest.TestCase):
    def test_entrypoint_exists_and_is_dev_only(self):
        self.assertTrue(_ENTRY.is_file(), "bench/judgebench.py missing")
        self.assertTrue((_ROOT / "bench" / "README.md").is_file())
        self.assertTrue((_ROOT / "bench" / "lib" / "__init__.py").is_file())
        # Never shipped: nothing of the bench may live under the plugin tree.
        self.assertFalse((_ROOT / "plugins" / "playbook" / "bench").exists())

    def test_runs_dir_is_gitignored(self):
        gi = (_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("bench/runs/", [ln.strip() for ln in gi])

    def test_top_level_help_exits_zero(self):
        p = _run("--help")
        self.assertEqual(p.returncode, 0, p.stderr)
        for sub in ("corpus", "run", "adjudicate", "report"):
            self.assertIn(sub, p.stdout)

    def test_every_subcommand_help_exits_zero(self):
        for argv in (["corpus", "--help"], ["corpus", "validate", "--help"],
                     ["corpus", "show", "--help"], ["run", "--help"],
                     ["adjudicate", "--help"], ["report", "--help"]):
            p = _run(*argv)
            self.assertEqual(p.returncode, 0, f"{argv}: {p.stderr}")

    def test_no_args_is_usage_error_exit_2(self):
        p = _run()
        self.assertEqual(p.returncode, 2)

    def test_unknown_subcommand_exit_2(self):
        p = _run("frobnicate")
        self.assertEqual(p.returncode, 2)

    def test_corpus_validate_on_empty_corpus_dir(self):
        with tempfile.TemporaryDirectory() as td:
            p = _run("corpus", "validate", "--corpus", td)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("0 cases", p.stdout)

    def test_corpus_validate_on_missing_dir_is_exit_2(self):
        with tempfile.TemporaryDirectory() as td:
            p = _run("corpus", "validate", "--corpus", str(Path(td) / "nope"))
            self.assertEqual(p.returncode, 2)

    def test_run_refuses_without_fake_or_live(self):
        # Real providers are opt-in only: absence of --fake requires --live.
        with tempfile.TemporaryDirectory() as td:
            p = _run("run", "--cases", "all", "--candidates", "a,b",
                     "--run-id", "x", "--corpus", td, "--runs-dir", td)
            self.assertEqual(p.returncode, 2)
            self.assertIn("--fake", p.stderr + p.stdout)
            self.assertIn("--live", p.stderr + p.stdout)


if __name__ == "__main__":
    unittest.main()
