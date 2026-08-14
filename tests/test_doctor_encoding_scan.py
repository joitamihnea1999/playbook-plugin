"""Doctor check #7 (write_text/read_text carry encoding=) after the 1.5.9 split.

The check used to scan [cli.py, core.py] resolved via sys.modules[__name__] —
correct while every command arm lived in cli.py. The split moved the arms
across the tasks package, so the check now globs every *.py in the package
dir: same output today (all modules scan clean — the ONE declared semantic
edit of design-1.5.9.md §5), but the coverage no longer silently shrinks to
whichever file hosts the doctor code.

Two pins: the real package PASSES, and — the doctrine's negative control: an
instrument must prove it can report failure — a package copy with a planted
unencoded call FAILS with a count.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PLAYBOOK = _HERE.parent / "plugins" / "playbook"

_CHECK_LINE = "encoding: write_text/read_text have encoding="


def _run_doctor(pythonpath: Path) -> str:
    with tempfile.TemporaryDirectory() as t:
        proj = Path(t)
        (proj / ".agent" / "tasks").mkdir(parents=True)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(pythonpath)
        env["PLAYBOOK_SESSION_ID"] = "pid-999999997"
        r = subprocess.run(
            [sys.executable, "-m", "tasks.cli", "doctor"],
            cwd=proj, env=env, capture_output=True, text=True, timeout=120,
        )
        return r.stdout + r.stderr


class DoctorEncodingScan(unittest.TestCase):
    def test_real_package_scans_clean(self):
        out = _run_doctor(_PLAYBOOK)
        line = next((l for l in out.splitlines() if _CHECK_LINE in l), None)
        self.assertIsNotNone(line, out)
        self.assertIn("[PASS]", line)
        self.assertIn("all encoded", line)

    def test_planted_unencoded_call_fails_the_check(self):
        with tempfile.TemporaryDirectory() as t:
            pkgroot = Path(t) / "pkgroot"
            shutil.copytree(_PLAYBOOK / "tasks", pkgroot / "tasks",
                            ignore=shutil.ignore_patterns("__pycache__"))
            # provider/ is needed importable for doctor's advisory imports to
            # degrade gracefully rather than change the check under test.
            shutil.copytree(_PLAYBOOK / "provider", pkgroot / "provider",
                            ignore=shutil.ignore_patterns("__pycache__"))
            planted = pkgroot / "tasks" / "zz_planted.py"
            planted.write_text(
                "from pathlib import Path\n\n\n"
                "def bad(p: Path) -> str:\n"
                "    return p.read_text()\n",
                encoding="utf-8",
            )
            out = _run_doctor(pkgroot)
        line = next((l for l in out.splitlines() if _CHECK_LINE in l), None)
        self.assertIsNotNone(line, out)
        self.assertIn("[FAIL]", line)
        self.assertIn("1 unencoded call", line)


if __name__ == "__main__":
    unittest.main()
