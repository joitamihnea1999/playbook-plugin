"""Task 096 (PLAN S10): the Windows failure evidence comes from the FAILING run.

Until 096 the Windows CI job re-ran the whole suite and both shell fixtures in a
second `Full failure detail` step (10.8-13.5 min on every push, green or not), and
what it uploaded was that SECOND run — a flake could pass in it. Now
`scripts/verify` itself appends every child command's complete output to
`$PLAYBOOK_VERIFY_FULL_LOG` when the variable is set, runs unittest at `-v` then,
and `tests/test_shell_fixtures.py` appends each fixture's full transcript (the
unittest-wrapped run is otherwise capped at 40 diagnostic lines). The log must
never change a verdict.

Also 086 W6: `scripts/verify` printed failure detail with `print()` on a cp1252
Windows console and crashed on U+2260 (CI 35970907673), cutting the list short.

Run: python3 -m unittest tests.test_verify_full_log
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import test_shell_fixtures  # noqa: E402

ENV = "PLAYBOOK_VERIFY_FULL_LOG"


def _load_verify():
    path = ROOT / "scripts" / "verify"
    loader = importlib.machinery.SourceFileLoader("verify_full_log_under_test", str(path))
    spec = importlib.util.spec_from_loader("verify_full_log_under_test", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


V = _load_verify()

# 300 lines, a U+2260 among them: more than any reporter cap, and the character
# that crashed the cp1252 console (086).
_CHILD = ("import sys\n"
          "for i in range(300):\n"
          "    sys.stdout.buffer.write(('line %d \\u2260\\n' % i).encode('utf-8'))\n"
          "sys.exit(3)\n")


class RunWritesTheFullLog(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.log = self.td / "full.txt"

    def test_every_line_of_a_failing_child_reaches_the_log(self):
        with mock.patch.dict(os.environ, {ENV: str(self.log)}):
            rc, out = V.run([sys.executable, "-c", _CHILD], cwd=self.td)
        self.assertEqual(rc, 3)
        text = self.log.read_text(encoding="utf-8")
        self.assertIn("rc 3", text)
        self.assertIn("line 0 ≠", text)
        self.assertIn("line 299 ≠", text)            # uncapped
        self.assertEqual(text.count("≠"), 300)
        self.assertEqual(out.count("≠"), 300)          # the check still sees it all

    def test_unset_writes_nothing(self):
        env = {k: v for k, v in os.environ.items() if k != ENV}
        with mock.patch.dict(os.environ, env, clear=True):
            V.run([sys.executable, "-c", "print('x')"], cwd=self.td)
        self.assertEqual(list(self.td.iterdir()), [])

    def test_an_unwritable_log_never_changes_the_result(self):
        bad = self.td / "is-a-dir"
        bad.mkdir()
        with mock.patch.dict(os.environ, {ENV: str(bad)}):
            rc, out = V.run([sys.executable, "-c", _CHILD], cwd=self.td)
        self.assertEqual(rc, 3)
        self.assertEqual(out.count("≠"), 300)

    def test_a_timed_out_child_leaves_its_partial_output_in_the_log(self):
        child = ("import sys, time\n"
                 "print('partial before the hang', flush=True)\n"
                 "time.sleep(60)\n")
        with mock.patch.dict(os.environ, {ENV: str(self.log)}):
            with self.assertRaises(subprocess.TimeoutExpired):
                V.run([sys.executable, "-c", child], cwd=self.td, timeout=3)
        text = self.log.read_text(encoding="utf-8")
        self.assertIn("TIMED OUT", text)
        self.assertIn("partial before the hang", text)


class UnittestVerbosity(unittest.TestCase):
    def _argv(self, env):
        seen = {}

        def fake_run(cmd, cwd=V.ROOT, timeout=900):
            seen["cmd"] = cmd
            return 0, "Ran 1 test\n\nOK\n"

        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(V, "run", side_effect=fake_run):
            V._unittest()
        return seen["cmd"]

    def test_verbose_only_when_the_full_log_is_on(self):
        base = {k: v for k, v in os.environ.items() if k != ENV}
        self.assertIn("-q", self._argv(base))
        self.assertNotIn("-v", self._argv(base))
        on = dict(base, **{ENV: "/nonexistent/full.txt"})
        self.assertIn("-v", self._argv(on))
        self.assertNotIn("-q", self._argv(on))


class ShellFixtureTranscript(unittest.TestCase):
    def test_full_transcript_is_appended_when_the_log_is_on(self):
        log = Path(tempfile.mkdtemp()) / "full.txt"
        lines = [f"  FAIL  S{i} something" for i in range(120)]
        with mock.patch.dict(os.environ, {ENV: str(log)}):
            test_shell_fixtures._full_log("wrapper-multiuser-fixture.sh", 1, "\n".join(lines))
        text = log.read_text(encoding="utf-8")
        self.assertIn("wrapper-multiuser-fixture.sh", text)
        self.assertIn("S0 something", text)
        self.assertIn("S119 something", text)            # past the 40-line block cap

    def test_nothing_when_off_and_never_raises(self):
        env = {k: v for k, v in os.environ.items() if k != ENV}
        with mock.patch.dict(os.environ, env, clear=True):
            test_shell_fixtures._full_log("x.sh", 0, "text")   # no file, no error
        d = Path(tempfile.mkdtemp())
        with mock.patch.dict(os.environ, {ENV: str(d)}):      # a directory: unwritable
            test_shell_fixtures._full_log("x.sh", 1, "text")


class Cp1252Console(unittest.TestCase):
    """086 W6: a FAIL detail line with U+2260 must not crash the report."""

    def test_report_survives_a_cp1252_stdout(self):
        raw = io.BytesIO()
        console = io.TextIOWrapper(raw, encoding="cp1252")

        def fake_checks():
            V.record("fake check", V.FAIL, "a ≠ b", None,
                     ["AssertionError: 1 ≠ 2"])

        with mock.patch.object(V, "run_checks", side_effect=fake_checks), \
                mock.patch.object(V, "results", []), \
                mock.patch.object(sys, "argv", ["verify"]), \
                mock.patch.object(sys, "stdout", console):
            rc = V.main()
            sys.stdout.flush()
        self.assertEqual(rc, 1)
        out = raw.getvalue().decode("utf-8", "replace")
        self.assertIn("1 ≠ 2", out)


if __name__ == "__main__":
    unittest.main()
