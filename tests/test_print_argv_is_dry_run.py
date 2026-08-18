#!/usr/bin/env python3
"""`--print-argv` is a DRY RUN — it must never execute the agent.

Field finding (1.5.31 audit): `--print-argv` was declared "instead of
executing — inspectable containment", but its check sat AFTER the `--prompt`
branch in provider.sandbox._main. So `sandbox --print-argv --prompt "..."`
silently ignored the dry-run flag and launched a live, billable agent with the
project writable — the exact opposite of the flag's contract, on the path the
`tasks` help text advertises as the primary one (`bin/sandbox --prompt "..."`).

Inspection flags must short-circuit BEFORE any side-effecting branch. This is
the decision fixture for that ordering: every inspection flag is asserted to
exit without running the agent, with `--prompt` present and absent.

Hermetic: a fake `claude` on PATH stands in for the real CLI, so a regression
prints the fake's marker instead of spending money on a real run.

Run: python3 tests/test_print_argv_is_dry_run.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
PLUGIN = _HERE.parent / "plugins/playbook"
sys.path.insert(0, str(PLUGIN))

from provider import sandbox, subagent  # noqa: E402

MARKER = "FAKE_AGENT_WAS_EXECUTED"


class PrintArgvNeverExecutes(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.proj = self.tmp / "proj"
        self.proj.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.proj, check=True)
        # A fake `claude` that announces itself if anything actually runs it.
        self.bindir = self.tmp / "bin"
        self.bindir.mkdir()
        fake = self.bindir / "claude"
        fake.write_text(f"#!/bin/sh\necho {MARKER}\n", encoding="utf-8")
        fake.chmod(0o755)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *flags: str) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if k != "PLAYBOOK_SANDBOXED"}
        env["PYTHONPATH"] = str(PLUGIN)
        # Fake agent first so shutil.which resolves it, real tools still reachable.
        env["PATH"] = f"{self.bindir}:{env.get('PATH', '')}"
        return subprocess.run(
            [sys.executable, "-m", "provider.sandbox",
             "--project-root", str(self.proj), *flags],
            env=env, capture_output=True, text=True, timeout=120)

    def test_print_argv_with_prompt_does_not_execute(self):
        r = self._run("--print-argv", "--agent", "claude", "--prompt", "hello")
        self.assertNotIn(MARKER, r.stdout + r.stderr,
                         "--print-argv with --prompt EXECUTED the agent")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("claude", r.stdout, "argv should name the agent binary")

    def test_print_argv_with_prompt_shows_the_prompt_paths_argv(self):
        """The printed argv must be the one the --prompt path would run,
        not the empty raw-passthrough argv (that would be a lie, not a dry run)."""
        r = self._run("--print-argv", "--agent", "claude", "--prompt", "hello")
        self.assertEqual(r.returncode, 0, r.stderr)
        # headless_argv adds the non-interactive flag; a raw `_wrapped_argv([])`
        # dry run would omit it entirely.
        self.assertIn("-p", r.stdout.split(),
                      f"argv is not the headless --prompt invocation:\n{r.stdout}")

    @unittest.skipUnless(shutil.which("bwrap") or sys.platform == "darwin",
                         "no containment backend installed")
    def test_print_argv_stays_contained(self):
        r = self._run("--print-argv", "--agent", "claude", "--prompt", "hello")
        self.assertTrue(
            "bwrap" in r.stdout or "sandbox-exec" in r.stdout,
            f"dry-run argv is not wrapped in containment:\n{r.stdout}")

    def test_print_argv_without_prompt_still_works(self):
        r = self._run("--print-argv", "--agent", "claude", "--", "echo", "hi")
        self.assertNotIn(MARKER, r.stdout + r.stderr)
        self.assertEqual(r.returncode, 0, r.stderr)

    @unittest.skipUnless(shutil.which("bwrap") or sys.platform == "darwin",
                         "no containment backend installed")
    def test_ro_project_prompt_denies_project_write_but_keeps_rw_exception(self):
        """Exercise the real wrapper, not only the SubagentSpec handoff."""
        allowed = self.proj / "allowed"
        allowed.mkdir()
        fake = self.bindir / "claude"
        fake.write_text(
            "#!/bin/sh\n"
            "printf blocked > \"$PWD/blocked\" 2>/dev/null || :\n"
            "printf allowed > \"$PWD/allowed/wrote\" 2>/dev/null || :\n"
            f"echo {MARKER}\n",
            encoding="utf-8",
        )
        r = self._run(
            "--agent", "claude", "--ro-project", "--rw", str(allowed),
            "--prompt", "hello",
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("blocked", [p.name for p in self.proj.iterdir()],
                         "--ro-project prompt execution wrote the project root")
        self.assertEqual((allowed / "wrote").read_text(encoding="utf-8"), "allowed")

    def test_other_inspection_flags_never_execute(self):
        for flags in (("--list-agents",), ("--list-models",),
                      ("--print-profile",),
                      ("--print-profile", "--prompt", "hello"),
                      ("--list-agents", "--prompt", "hello"),
                      ("--list-models", "--prompt", "hello")):
            with self.subTest(flags=flags):
                r = self._run(*flags)
                self.assertNotIn(MARKER, r.stdout + r.stderr,
                                 f"{flags} EXECUTED the agent")
                self.assertEqual(r.returncode, 0, r.stderr)


class PromptContainmentMatchesTheCliFlags(unittest.TestCase):
    """The inspected wrapper and the executed prompt must enforce one policy."""

    def _capture_spec(self, *extra: str):
        captured = []

        def fake_run(spec, *, project_root):
            captured.append(spec)
            return subagent.SubagentResult(text="stub", returncode=0)

        def fake_stream(spec, *, project_root):
            captured.append(spec)
            return iter(())

        with mock.patch.object(subagent, "run_subagent", side_effect=fake_run), \
                mock.patch.object(subagent, "stream_subagent", side_effect=fake_stream), \
                redirect_stdout(StringIO()):
            rc = sandbox._main([
                "--agent", "grok", "--project-root", str(_HERE.parent),
                "--ro-project", "--rw", "/tmp/one", "--rw", "/tmp/two",
                "--prompt", "inspect", *extra,
            ])
        self.assertEqual(rc, 0)
        self.assertEqual(len(captured), 1)
        return captured[0]

    def test_read_only_and_rw_paths_reach_prompt_runner(self):
        spec = self._capture_spec()
        self.assertEqual(spec.contain, "outdir")
        self.assertEqual(tuple(map(str, spec.extra_rw)), ("/tmp/one", "/tmp/two"))

    def test_read_only_and_rw_paths_reach_streaming_prompt_runner(self):
        spec = self._capture_spec("--stream")
        self.assertEqual(spec.contain, "outdir")
        self.assertEqual(tuple(map(str, spec.extra_rw)), ("/tmp/one", "/tmp/two"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
