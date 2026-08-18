#!/usr/bin/env python3
"""bwrap bind ORDER is load-bearing (spike finding, 2026-08-13).

Later binds stack over earlier ones. The original profile bound the project
read-only FIRST and /tmp read-write SECOND — so a project living under /tmp was
silently re-exposed writable in judge mode (empirically demonstrated: a judge-
profile shell created a file inside a /tmp-resident "read-only" project).

Invariants:
  * every broad rw mount (/tmp, write log, home subpaths) precedes the project
    bind, so the project bind governs overlap;
  * .git's ro-bind comes after the project bind (stays ro even in rw mode);
  * extra_rw comes last (a judge workspace inside a ro project stays writable);
  * live negative control (skipped without bwrap): a /tmp-resident project is
    actually write-BLOCKED under the fixed profile, while /tmp itself and
    subprocess exec still work — the facts judge-execution (L1) rests on.

Run: python3 tests/test_sandbox_bind_order.py
"""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "plugins/playbook"))
from provider.sandbox import build_bwrap_argv  # noqa: E402


def bind_index(argv, path, kinds=("--bind", "--ro-bind")):
    """Index of the FIRST bind whose source is exactly `path`."""
    for i in range(len(argv) - 2):
        if argv[i] in kinds and argv[i + 1] == path:
            return i
    return -1


class BindOrder(unittest.TestCase):
    def _argv(self, proj, writable=False, extra_rw=None):
        return build_bwrap_argv(proj, Path(proj) / ".git", ["true"],
                                extra_rw, project_writable=writable)

    def test_broad_rw_mounts_precede_project_bind(self):
        proj = str(Path(tempfile.mkdtemp()).resolve())  # tmp-resident on purpose
        argv = self._argv(proj)
        self.assertLess(bind_index(argv, "/tmp"), bind_index(argv, proj),
                        "/tmp rw bind must come BEFORE the project bind, or a "
                        "/tmp-resident project is re-exposed writable")

    def test_project_is_ro_bound_in_judge_mode(self):
        proj = str(Path(tempfile.mkdtemp()).resolve())
        argv = self._argv(proj, writable=False)
        i = bind_index(argv, proj)
        self.assertEqual(argv[i], "--ro-bind")

    def test_git_ro_bind_after_project(self):
        proj = str(Path(tempfile.mkdtemp()).resolve())
        argv = self._argv(proj, writable=True)
        self.assertLess(bind_index(argv, proj),
                        bind_index(argv, str(Path(proj) / ".git"), kinds=("--ro-bind",)),
                        ".git must stay read-only even when the project is writable")

    def test_extra_rw_after_project(self):
        proj = Path(tempfile.mkdtemp()).resolve()
        ws = proj / "workspace"
        argv = self._argv(str(proj), writable=False, extra_rw=[str(ws)])
        self.assertLess(bind_index(argv, str(proj)), bind_index(argv, str(ws)),
                        "the judge workspace must stay writable inside a ro project")


@unittest.skipUnless(shutil.which("bwrap"), "bwrap not available")
class LiveNegativeControl(unittest.TestCase):
    """The spike, pinned: a /tmp-resident project must be write-blocked, while
    exec and /tmp writes (the L1 facts) keep working."""

    def test_tmp_resident_project_is_blocked_but_exec_and_tmp_work(self):
        proj = Path(tempfile.mkdtemp(prefix="sbx-order-")).resolve()
        (proj / "code.py").write_text("x = 1\n", encoding="utf-8")
        script = (
            'echo "exec:$(python3 -c \'print(40+2)\')"; '
            'echo t > /tmp/sbx-order-probe && echo "tmp:OK"; '
            '(echo hack > pwned.txt 2>/dev/null && echo "repo:ALLOWED") || echo "repo:BLOCKED"'
        )
        argv = build_bwrap_argv(proj, None, ["bash", "-c", script], None,
                                project_writable=False)
        r = subprocess.run(argv, cwd=str(proj), capture_output=True, text=True, timeout=60)
        self.assertIn("exec:42", r.stdout)
        self.assertIn("tmp:OK", r.stdout)
        self.assertIn("repo:BLOCKED", r.stdout,
                      "a /tmp-resident project must be read-only to judges")
        self.assertFalse((proj / "pwned.txt").exists(), "no mutation may reach the host")


if __name__ == "__main__":
    unittest.main()
