"""Wire the shell fixture suites into `unittest discover` (§2.3).

Six `*-fixture.sh` suites carry the most safety-critical coverage the Python
suite does NOT — session-GC parity (wrapper-multiuser S18, which
test_session_gc_policy.py explicitly delegates to), wrapper-lane provisioning,
init's bash-log deployment, merge-doctor cross-lane contamination, merge-verify's
exit contract, gate-logging write-failure — and NOTHING executed them (no
`unittest discover`, no CI, no Makefile). That is why the whole C-tier of
verification-report-1.5.9 survived an 852-green suite: the bugs lived in the exact
paths the suite didn't run. This runner shells out to each fixture as a subtest,
so the default `python3 -m unittest discover -s tests` reaches them.

Each fixture's contract: run `bash <fixture>`, exit 0 iff every scenario passed.
A fixture is SKIPPED (not failed) when a binary it needs is absent, so the suite
stays green on a minimal host. `BASH_ENV` is unset for the child: a dogfooding
host propagates the plugin's own bash-log via BASH_ENV, which writes
`.agent/bash_history` into every scratch project and would corrupt fixtures that
assert on `.agent/` contents.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from tests._bashcheck import bash_or_skip
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent

# fixture filename -> extra binaries it needs beyond bash + python3.
_FIXTURES = {
    "gate-logging-failure-fixture.sh": [],
    "init-bash-log-fixture.sh": [],
    "merge-doctor-fixture.sh": ["git"],
    "merge-verify-fixture.sh": ["git"],
    "wrapper-atomicity-fixture.sh": [],
    "wrapper-multiuser-fixture.sh": [],   # zsh is optional (guarded inside)
}

_PER_FIXTURE_TIMEOUT = 300


def _clean_env() -> dict:
    env = dict(os.environ)
    # A dogfooding host's BASH_ENV points at the plugin's bash-log, which writes
    # .agent/bash_history into scratch projects and breaks .agent-content asserts.
    env.pop("BASH_ENV", None)
    # Let each fixture own its session id / role rather than inheriting ours.
    for k in ("PLAYBOOK_SESSION_ID", "PLAYBOOK_ROLE", "PLAYBOOK_EVAL_CONFIG"):
        env.pop(k, None)
    return env


class ShellFixtures(unittest.TestCase):
    def test_shell_fixtures_pass(self):
        for name, needs in _FIXTURES.items():
            with self.subTest(fixture=name):
                path = _HERE / name
                self.assertTrue(path.exists(), f"missing fixture: {name}")
                missing = [b for b in (["bash", "python3"] + needs)
                           if shutil.which(b) is None]
                if missing:
                    self.skipTest(f"{name}: missing binary(ies): {missing}")
                try:
                    # Run in a FRESH tempdir, not the repo root. The repo is
                    # itself a dogfooded playbook project (real .agent/ lanes,
                    # CLAUDE.md, MIND_MAP.md), and a fixture that walks up from
                    # cwd or asserts on ".agent contents of a fresh clone" reads
                    # that live state and reports spurious failures — the exact
                    # cause of the wrapper-multiuser 271/2-vs-273/0 split between
                    # this runner (was cwd=repo root) and scripts/verify (already
                    # isolates in a TemporaryDirectory). Every fixture locates its
                    # own inputs via an absolute $0-derived path, so a neutral cwd
                    # is safe and matches the canonical gate.
                    with tempfile.TemporaryDirectory() as td:
                        r = subprocess.run(
                            [bash_or_skip(), str(path)],
                            cwd=td, env=_clean_env(),
                            capture_output=True, text=True,
                            timeout=_PER_FIXTURE_TIMEOUT,
                        )
                except subprocess.TimeoutExpired:
                    self.fail(f"{name}: timed out after {_PER_FIXTURE_TIMEOUT}s")
                if r.returncode != 0:
                    lines = (r.stdout + r.stderr).splitlines()
                    # Surface EVERY failing assertion, not just the tail: a
                    # fixture can print 250+ PASS lines and the two FAILs that
                    # matter scroll off a 25-line tail (the wrapper-multiuser
                    # blind spot in CI run 32454916957).
                    fails = [ln for ln in lines if "FAIL" in ln]
                    detail = "\n".join(fails) if fails else ""
                    detail += "\n--- last 25 lines ---\n" + "\n".join(lines[-25:])
                    self.fail(f"{name} failed (rc={r.returncode}):\n{detail}")


if __name__ == "__main__":
    unittest.main()
