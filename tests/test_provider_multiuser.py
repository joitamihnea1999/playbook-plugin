"""Per-user lane resolution across the provider layer (task 022).

Playbook namespaces runtime state under `.agent/<user>/` when
`.agent/current_user` is present. Before this task the provider layer read and
wrote the ROOT `.agent/` unconditionally, so on a multi-user repo the tasks CLI
wrote `.agent/alice/sessions/<id>/current_state` while the Codex hooks looked in
`.agent/sessions/<id>/` — gate enforcement saw "no active task" no matter what.

Four groups:
  1. provider.paths       — the resolver itself, both layouts + every reject.
  2. cross-implementation — paths.py, tasks.core and gate-echo-lib.sh must agree
                            on the SAME vector table, or the copies drift.
  3. adapter / codex_hooks— every rewired path lands in the lane, and a bad
                            marker produces the documented per-surface behavior
                            (including the real hook subprocess's exit code).
  4. split-brain E2E      — wrapper provisions, CLI writes, hook reads: one lane.
  5. shell logger lanes   — the bundled bash logger, executed for real: it must
                            never elect the shared root when ownership is unknown
                            (PB-LANE-RESOLUTION), and must keep writing a
                            legitimate root lane.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN = REPO_ROOT / "plugins" / "playbook"
SCRIPTS = PLUGIN / "scripts"
BASH_LOG = SCRIPTS / "bash-log.sh"
ZSH_LOG = SCRIPTS / "bash-log.zsh"

sys.path.insert(0, str(PLUGIN))

from provider.paths import (  # noqa: E402
    InvalidUserMarkerError,
    find_project_root,
    lanes_without_marker,
    resolve_agent_dir,
    validate_username,
)

# ── Shared vector table ──────────────────────────────────────────────────────
# The same marker contents run through all three implementations. Adding a case
# here automatically holds paths.py, tasks/core.py and gate-echo-lib.sh to it.
VALID_MARKERS = ["alice", "bob-2", "u_1", "Team.Lead", "x", "9lives"]
INVALID_MARKERS = ["", ".", "..", "../evil", "a/b", "-dash", "_under", "has space", ".hidden"]

# Raw file BYTES, not stripped content. The impl panel showed the vectors above
# could never expose the real divergence: every one is a single line ending in
# "\n", which is exactly where all the implementations already agreed. These are
# the shapes where they did NOT — line endings, line count, and a missing final
# newline. (name, bytes, expected_lane_or_None)
RAW_MARKERS = [
    ("lf",           "alice\n",          "alice"),
    ("crlf",         "alice\r\n",        "alice"),  # Windows is supported
    ("no_trailing",  "alice",            "alice"),  # `read` returns 1 but assigns
    ("padded",       "  alice  \n",      "alice"),
    ("blank_second", "alice\n\n",        "alice"),  # trailing blank line is fine
    ("two_lines",    "alice\nbob\n",     None),     # ambiguous → reject
    ("smuggled",     "alice\n../evil\n", None),     # must NOT resolve to `alice`
    ("crlf_two",     "alice\r\nbob\r\n", None),
]


# The no-marker shapes: the `lanes_without_marker` list each one produces, and
# the answer the bundled Bash logger must give for it. The logger decides on two
# filesystem facts only — the marker, and whether root `.agent/tasks/` is a
# directory — so it writes the root on exactly the two shapes that have root
# `tasks/` and skips every other one. That makes it deliberately STRICTER than
# the three reference implementations, never looser; TestResolverParity asserts
# both halves.
#
# (name, builder, lanes_expected, logger_expected)
NO_MARKER_SHAPES = [
    ("bare",        lambda a: None,                                        [], "skip"),
    ("config_only", lambda a: (a / "config.json").write_text("{}\n"),      [], "skip"),
    ("sessions",    lambda a: (a / "sessions").mkdir(),                    [], "skip"),
    ("child_no_tasks", lambda a: (a / "alice").mkdir(),                    [], "skip"),
    ("root_tasks",  lambda a: (a / "tasks").mkdir(),                       [], "root"),
    ("one_lane",    lambda a: (a / "alice" / "tasks").mkdir(parents=True),  ["alice"], "skip"),
    ("two_lanes",   lambda a: [(a / n / "tasks").mkdir(parents=True)
                               for n in ("alice", "bob")],                 ["alice", "bob"], "skip"),
    ("lane_plus_junk", lambda a: [(a / "alice" / "tasks").mkdir(parents=True),
                                  (a / "sessions").mkdir(),
                                  (a / "config.json").write_text("{}\n")],  ["alice"], "skip"),
    ("mixed",       lambda a: [(a / "tasks").mkdir(),
                               (a / "alice" / "tasks").mkdir(parents=True)], [], "root"),
]


def make_project(root: Path, layout: str, marker: str | None = None) -> Path:
    """Create a scratch playbook project. layout: legacy | multiuser | mixed."""
    root.mkdir(parents=True, exist_ok=True)
    if layout in ("legacy", "mixed"):
        (root / ".agent" / "tasks").mkdir(parents=True, exist_ok=True)
    if layout in ("multiuser", "mixed"):
        (root / ".agent" / "alice" / "tasks").mkdir(parents=True, exist_ok=True)
    if marker is not None:
        (root / ".agent").mkdir(parents=True, exist_ok=True)
        (root / ".agent" / "current_user").write_text(marker + "\n", encoding="utf-8")
    return root


def make_task(agent_dir: Path, number: int, name: str = "demo") -> Path:
    task_dir = agent_dir / "tasks" / f"{number:03d}-{name}"
    task_dir.mkdir(parents=True, exist_ok=True)
    task_md = task_dir / "task.md"
    task_md.write_text("# task\n\n- [ ] do the thing\n", encoding="utf-8")
    return task_md


def activate(agent_dir: Path, session_id: str, number: int) -> None:
    """Write current_state the way `tasks work <N>` really does: zero-padded.

    codex_hooks._find_task_file prefix-matches "<state>-" against the task dir
    name, so an unpadded "7" would not find "007-demo" — using the CLI's own
    format keeps these tests honest about the real contract.
    """
    session = agent_dir / "sessions" / session_id
    session.mkdir(parents=True, exist_ok=True)
    (session / "current_state").write_text(f"{number:03d}", encoding="utf-8")


class TempProjectCase(unittest.TestCase):
    def setUp(self) -> None:
        # resolve(): on macOS mkdtemp hands back /var/... while the resolvers
        # return the /private/var/... realpath, so unresolved expectations fail
        # for a reason that has nothing to do with lanes.
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


# ── 1. The resolver ──────────────────────────────────────────────────────────


class TestResolveAgentDir(TempProjectCase):
    def test_legacy_layout_resolves_root(self):
        p = make_project(self.tmp / "legacy", "legacy")
        self.assertEqual(resolve_agent_dir(p), p / ".agent")

    def test_marker_resolves_lane(self):
        p = make_project(self.tmp / "mu", "multiuser", marker="alice")
        self.assertEqual(resolve_agent_dir(p), p / ".agent" / "alice")

    def test_marker_is_whitespace_stripped(self):
        p = make_project(self.tmp / "ws", "multiuser")
        (p / ".agent" / "current_user").write_text("  alice \n\n", encoding="utf-8")
        self.assertEqual(resolve_agent_dir(p), p / ".agent" / "alice")

    def test_every_invalid_marker_raises(self):
        for bad in INVALID_MARKERS:
            with self.subTest(marker=bad):
                p = make_project(self.tmp / f"bad{abs(hash(bad))}", "legacy", marker=bad)
                with self.assertRaises(InvalidUserMarkerError):
                    resolve_agent_dir(p)

    def test_invalid_marker_error_is_a_valueerror_not_systemexit(self):
        # A4: the codex hooks catch Exception, not BaseException. A SystemExit
        # here would bypass their per-event fail-open/fail-closed policy.
        p = make_project(self.tmp / "vt", "legacy", marker="../evil")
        with self.assertRaises(ValueError):
            resolve_agent_dir(p)
        try:
            resolve_agent_dir(p)
        except Exception as exc:  # noqa: BLE001 - that's the point of the test
            self.assertNotIsInstance(exc, SystemExit)

    def test_unreadable_marker_raises_rather_than_degrading(self):
        # Task 021's I2 lesson: a present-but-unreadable marker must not be
        # reported as "legacy layout" — that is how state splits in two.
        if os.geteuid() == 0:
            self.skipTest("root can read a 0000 file")
        p = make_project(self.tmp / "noperm", "multiuser", marker="alice")
        marker = p / ".agent" / "current_user"
        marker.chmod(0o000)
        self.addCleanup(marker.chmod, 0o644)
        with self.assertRaises(InvalidUserMarkerError):
            resolve_agent_dir(p)

    def test_missing_agent_dir_is_legacy(self):
        p = self.tmp / "empty"
        p.mkdir()
        self.assertEqual(resolve_agent_dir(p), p / ".agent")


class TestFindProjectRoot(TempProjectCase):
    def test_finds_legacy_root(self):
        p = make_project(self.tmp / "legacy", "legacy")
        self.assertEqual(find_project_root(p), p)

    def test_finds_multiuser_root_without_marker(self):
        # The fresh-clone shape: lanes present, marker absent (it is gitignored).
        p = make_project(self.tmp / "mu", "multiuser")
        self.assertEqual(find_project_root(p), p)

    def test_finds_root_from_deep_subdirectory(self):
        p = make_project(self.tmp / "mu2", "multiuser", marker="alice")
        deep = p / "src" / "a" / "b"
        deep.mkdir(parents=True)
        self.assertEqual(find_project_root(deep), p)

    def test_returns_none_outside_a_project(self):
        plain = self.tmp / "plain"
        (plain / "sub").mkdir(parents=True)
        # tmp itself has no .agent, and mktemp dirs live under /tmp or /var.
        self.assertIsNone(find_project_root(plain / "sub"))


# ── 2. Cross-implementation agreement ────────────────────────────────────────


class TestResolverParity(TempProjectCase):
    """paths.py, tasks/core.py and gate-echo-lib.sh must classify identically.

    Three hand-written copies of one validation contract is exactly the drift
    class this task is fixing; this table is the guard that keeps them honest.
    """

    def _bash_resolve(self, project: Path):
        """Run gate-echo-lib.sh's resolve_agent_dir. Returns (rc, stdout)."""
        script = (
            f'source "{SCRIPTS}/gate-echo-lib.sh"\n'
            f'resolve_agent_dir "{project}"\n'
        )
        proc = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True
        )
        return proc.returncode, proc.stdout.strip()

    def _python_core_resolve(self, project: Path):
        """Run tasks/core.py's resolve_agent_dir out-of-process (it may exit)."""
        code = (
            "import sys\n"
            "from pathlib import Path\n"
            "from tasks.core import resolve_agent_dir\n"
            "print(resolve_agent_dir(Path(sys.argv[1])))\n"
        )
        env = {**os.environ, "PYTHONPATH": str(PLUGIN)}
        proc = subprocess.run(
            [sys.executable, "-c", code, str(project)],
            capture_output=True, text=True, env=env,
        )
        return proc.returncode, proc.stdout.strip()

    def _bash_log_resolve(self, project: Path, expected_lane: str | None):
        """Run bash-log.sh against the project; return (rc, resolved_lane_name_or_None)."""
        if expected_lane:
            (project / ".agent" / expected_lane).mkdir(parents=True, exist_ok=True)
        for h in project.glob(".agent/**/bash_history"):
            h.unlink()
        if (project / ".agent" / "bash_history").exists():
            (project / ".agent" / "bash_history").unlink()

        script = (
            f'BASH_COMMAND="my_test_command"\n'
            f'source "{SCRIPTS}/bash-log.sh"\n'
            f'_cpb_log_cmd\n'
        )
        proc = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, cwd=str(project)
        )
        if proc.returncode != 0:
            return proc.returncode, None

        histories = list(project.glob(".agent/**/bash_history"))
        if not histories:
            return 0, None
        if len(histories) > 1:
            raise ValueError(f"Multiple histories written: {histories}")

        rel = histories[0].relative_to(project / ".agent")
        if rel.name == "bash_history" and len(rel.parts) == 1:
            return 0, ""
        return 0, rel.parts[0]

    def _shell_lanes_without_marker(self, project: Path):
        """gate-echo-lib.sh's lanes_without_marker, as a sorted list."""
        proc = subprocess.run(
            ["bash", "-c",
             f'source "{SCRIPTS}/gate-echo-lib.sh"\nlanes_without_marker "{project}"\n'],
            capture_output=True, text=True,
        )
        return proc.returncode, sorted(x for x in proc.stdout.split() if x)

    def _logger_target(self, project: Path):
        """Run the bundled bash logger for real; return "root", a lane name, or None.

        `BASH_ENV` is stripped so a dogfooding host's INSTALLED logger cannot be
        sourced into the probe shell and log a second time.
        """
        env = {k: v for k, v in os.environ.items()
               if k not in ("BASH_ENV", "PLAYBOOK_SESSION_ID", "PLAYBOOK_ROLE")}
        subprocess.run(
            ["bash", "--noprofile", "--norc", "-c",
             'source "$1"\necho parity_probe >/dev/null\n', "_",
             str(SCRIPTS / "bash-log.sh")],
            cwd=str(project), env=env, capture_output=True, text=True,
        )
        written = sorted((project / ".agent").rglob("bash_history"))
        if not written:
            return None
        if len(written) > 1:
            raise ValueError(f"logger wrote more than one history: {written}")
        rel = written[0].relative_to(project / ".agent")
        return "root" if len(rel.parts) == 1 else rel.parts[0]

    def test_logger_is_never_looser_than_lanes_without_marker(self):
        """The bundled Bash logger against the three reference implementations.

        It agrees with them wherever root `.agent/tasks/` exists — the root is a
        real lane there, and refusing it would kill logging for every legacy and
        mixed project. On every OTHER marker-absent shape it is deliberately
        stricter: it skips where `resolve_agent_dir` still answers the root. The
        cost is a missing forensic log on a project that has no `tasks/` yet, and
        it heals the moment anything creates one.

        The direction is what matters, so it is asserted rather than left to the
        table: the logger may never write the root where a reference reports that
        a per-user lane exists. That is the Critical fresh-clone contamination.
        """
        for name, build, expected, logger_expected in NO_MARKER_SHAPES:
            with self.subTest(shape=name):
                project = self.tmp / f"nm_{name}"
                agent = project / ".agent"
                agent.mkdir(parents=True)
                build(agent)
                self.assertFalse((agent / "current_user").exists(),
                                 "these shapes are the MARKER-ABSENT half")

                self.assertEqual(sorted(lanes_without_marker(project)), expected,
                                 "provider.paths")

                rc, shell_lanes = self._shell_lanes_without_marker(project)
                self.assertEqual(rc, 0, "gate-echo-lib.sh exited non-zero")
                self.assertEqual(shell_lanes, expected, "gate-echo-lib.sh")

                rc, core_lanes = self._python_core_lanes(project)
                self.assertEqual(rc, 0, f"tasks.core exited {rc}")
                self.assertEqual(core_lanes, expected, "tasks.core")

                target = self._logger_target(project)
                self.assertEqual(
                    target, None if logger_expected == "skip" else "root",
                    f"the logger's answer for {name} is decided by the marker "
                    f"and root .agent/tasks/ alone — wrote {target!r}")
                if target is not None:
                    self.assertEqual(
                        expected, [],
                        f"the logger elected the root on {name}, where the "
                        f"reference implementations report lanes {expected}")

    def _python_core_lanes(self, project: Path):
        """tasks/core.py's lanes_without_marker, out-of-process, as a sorted list."""
        code = (
            "import sys, json\n"
            "from pathlib import Path\n"
            "from tasks.core import lanes_without_marker\n"
            "print(json.dumps(sorted(lanes_without_marker(Path(sys.argv[1])))))\n"
        )
        env = {**os.environ, "PYTHONPATH": str(PLUGIN)}
        proc = subprocess.run(
            [sys.executable, "-c", code, str(project)],
            capture_output=True, text=True, env=env,
        )
        if proc.returncode != 0:
            return proc.returncode, None
        return 0, json.loads(proc.stdout)

    def test_dot_named_lane_is_a_known_shell_python_divergence(self):
        """A dot-named lane still splits gate-echo-lib.sh from the Python copies.

        `.agent/.hidden/tasks/` with no marker: `provider/paths.py` and
        `tasks/core.py` report `['.hidden']`, `gate-echo-lib.sh` reports `[]`
        because it globs and globbing skips dotfiles. That divergence predates
        1.5.34 and is Phase 4's to reconcile; it is pinned so a reconciliation
        has to update this test deliberately.

        The logger is no longer part of it. It consults no child of `.agent/` at
        all, so this is just another marker-absent shape with no root `tasks/`
        and it skips, like every other one.
        """
        project = self.tmp / "dot_lane"
        (project / ".agent" / ".hidden" / "tasks").mkdir(parents=True)
        self.assertFalse((project / ".agent" / "current_user").exists())

        self.assertEqual(sorted(lanes_without_marker(project)), [".hidden"],
                         "provider.paths stopped counting dot-named lanes")
        rc, core_lanes = self._python_core_lanes(project)
        self.assertEqual((rc, core_lanes), (0, [".hidden"]),
                         "tasks.core stopped counting dot-named lanes")

        rc, shell_lanes = self._shell_lanes_without_marker(project)
        self.assertEqual((rc, shell_lanes), (0, []),
                         "gate-echo-lib.sh started counting dot-named lanes — if "
                         "this is the Phase 4 reconciliation, the logger must move "
                         "with it and this test must be rewritten, not deleted")

        self.assertIsNone(
            self._logger_target(project),
            "a marker-absent shape with no root .agent/tasks/ must skip, "
            "whatever the children of .agent/ are named",
        )

    def test_valid_markers_agree_across_all_three(self):
        for name in VALID_MARKERS:
            with self.subTest(marker=name):
                p = make_project(self.tmp / f"ok{name.replace('.', '_')}", "legacy", marker=name)
                expected = str(p / ".agent" / name)

                self.assertEqual(str(resolve_agent_dir(p)), expected, "provider.paths")

                rc, out = self._python_core_resolve(p)
                self.assertEqual(rc, 0, f"tasks.core exited {rc}")
                self.assertEqual(out, expected, "tasks.core")

                rc, out = self._bash_resolve(p)
                self.assertEqual(rc, 0, f"gate-echo-lib exited {rc}")
                self.assertEqual(out, expected, "gate-echo-lib.sh")

                rc, resolved_lane = self._bash_log_resolve(p, name)
                self.assertEqual(rc, 0, f"bash-log.sh exited {rc}")
                self.assertEqual(resolved_lane, name, "bash-log.sh")

    def test_invalid_markers_rejected_by_all_three(self):
        for bad in INVALID_MARKERS:
            with self.subTest(marker=bad):
                p = make_project(self.tmp / f"no{abs(hash(bad))}", "legacy", marker=bad)

                with self.assertRaises(InvalidUserMarkerError):
                    resolve_agent_dir(p)

                rc, _ = self._python_core_resolve(p)
                self.assertNotEqual(rc, 0, "tasks.core accepted an invalid marker")

                rc, _ = self._bash_resolve(p)
                self.assertNotEqual(rc, 0, "gate-echo-lib.sh accepted an invalid marker")

                rc, resolved_lane = self._bash_log_resolve(p, None)
                self.assertEqual(rc, 0, f"bash-log.sh exited {rc}")
                self.assertIsNone(resolved_lane, "bash-log.sh accepted an invalid marker")

    def _write_raw(self, project: Path, data: str) -> None:
        (project / ".agent").mkdir(parents=True, exist_ok=True)
        # newline="" so the exact bytes reach disk — Python would otherwise
        # translate "\r\n" and the CRLF vectors would silently become LF.
        with open(project / ".agent" / "current_user", "w", newline="") as fh:
            fh.write(data)

    def _shell_lane(self, project: Path, snippet: str):
        """Run a shell resolver snippet; returns (rc, stdout)."""
        proc = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True)
        return proc.returncode, proc.stdout.strip()

    def test_raw_marker_shapes_agree_across_every_implementation(self):
        """Line endings / line count / final newline — where the copies diverged.

        Before this, `alice\\n../evil` resolved to lane `alice` in the shell
        readers while Python rejected it, and a CRLF marker was accepted by
        Python but silently rejected in shell (disabling logging and nudges).
        """
        for name, data, expected in RAW_MARKERS:
            with self.subTest(marker=name):
                p = make_project(self.tmp / f"raw_{name}", "legacy")
                self._write_raw(p, data)
                want = str(p / ".agent" / expected) if expected else None

                # 1. provider.paths (in-process)
                if want:
                    self.assertEqual(str(resolve_agent_dir(p)), want, "provider.paths")
                else:
                    with self.assertRaises(InvalidUserMarkerError, msg="provider.paths"):
                        resolve_agent_dir(p)

                # 2. tasks.core (subprocess — it may SystemExit)
                rc, out = self._python_core_resolve(p)
                if want:
                    self.assertEqual((rc, out), (0, want), "tasks.core")
                else:
                    self.assertNotEqual(rc, 0, "tasks.core accepted it")

                # 3. gate-echo-lib.sh
                rc, out = self._bash_resolve(p)
                if want:
                    self.assertEqual((rc, out), (0, want), "gate-echo-lib.sh")
                else:
                    self.assertNotEqual(rc, 0, "gate-echo-lib.sh accepted it")

                # 4. monitor-nudge.sh's inline copy — it can't source the lib,
                #    so assert it via observable behavior: a nudge sitting in
                #    the lane is delivered only if it resolved the same lane.
                nudge_dir = p / ".agent" / (expected or "alice") / "monitor"
                nudge_dir.mkdir(parents=True, exist_ok=True)
                (nudge_dir / "nudge.md").write_text(f"nudge-{name}\n", encoding="utf-8")
                proc = subprocess.run(
                    ["bash", str(REPO_ROOT / "plugins" / "playbook" / "hooks" / "monitor-nudge.sh")],
                    input='{"hook_event_name":"PostToolUse"}',
                    capture_output=True, text=True, cwd=str(p),
                    env={k: v for k, v in os.environ.items() if k != "PLAYBOOK_ROLE"},
                )
                if want:
                    self.assertIn(f"nudge-{name}", proc.stdout, "monitor-nudge.sh")
                else:
                    self.assertNotIn(f"nudge-{name}", proc.stdout,
                                     "monitor-nudge.sh delivered from a rejected marker")

                # 5. bash-log.sh's inline copy
                rc, resolved_lane = self._bash_log_resolve(p, expected)
                self.assertEqual(rc, 0, f"bash-log.sh exited {rc}")
                if expected:
                    self.assertEqual(resolved_lane, expected, "bash-log.sh")
                else:
                    self.assertIsNone(resolved_lane, "bash-log.sh accepted a bad raw marker")

    def test_absent_marker_is_root_for_all_three(self):
        p = make_project(self.tmp / "legacy", "legacy")
        expected = str(p / ".agent")
        self.assertEqual(str(resolve_agent_dir(p)), expected)
        self.assertEqual(self._python_core_resolve(p), (0, expected))
        self.assertEqual(self._bash_resolve(p), (0, expected))
        rc, resolved_lane = self._bash_log_resolve(p, "")
        self.assertEqual(rc, 0)
        self.assertEqual(resolved_lane, "")


# ── 3. Rewired provider surfaces ─────────────────────────────────────────────


class TestCodexHooksPaths(TempProjectCase):
    def setUp(self) -> None:
        super().setUp()
        from provider import codex_hooks
        self.ch = codex_hooks

    def test_state_and_task_paths_follow_the_lane(self):
        p = make_project(self.tmp / "mu", "multiuser", marker="alice")
        lane = p / ".agent" / "alice"
        make_task(lane, 7)
        activate(lane, "pid-1", 7)

        self.assertEqual(
            self.ch.current_state_file(p, "pid-1"),
            lane / "sessions" / "pid-1" / "current_state",
        )
        self.assertTrue(self.ch.has_active_task(p, "pid-1"))
        self.assertEqual(self.ch._chat_log_path(p), lane / "chat_log.md")
        self.assertEqual(self.ch._chat_counter_path(p), lane / "chat_log_counter")
        self.assertEqual(
            self.ch._session_counter_path(p, "pid-1"), lane / "sessions" / "pid-1" / "counters"
        )

    def test_legacy_layout_unchanged(self):
        p = make_project(self.tmp / "legacy", "legacy")
        root = p / ".agent"
        make_task(root, 3)
        activate(root, "pid-9", 3)

        self.assertEqual(
            self.ch.current_state_file(p, "pid-9"), root / "sessions" / "pid-9" / "current_state"
        )
        self.assertTrue(self.ch.has_active_task(p, "pid-9"))
        self.assertEqual(self.ch._chat_log_path(p), root / "chat_log.md")

    def test_root_state_is_invisible_from_the_lane(self):
        """The actual field bug, pinned: state written to the wrong lane must
        NOT satisfy the gate."""
        p = make_project(self.tmp / "split", "multiuser", marker="alice")
        lane = p / ".agent" / "alice"
        make_task(lane, 5)
        # Old (buggy) wrapper behavior: provision + activate at the ROOT.
        activate(p / ".agent", "pid-3", 5)
        self.assertFalse(self.ch.has_active_task(p, "pid-3"))
        # Same state in the lane: visible.
        activate(lane, "pid-3", 5)
        self.assertTrue(self.ch.has_active_task(p, "pid-3"))

    def test_agent_dir_writable_tests_the_lane(self):
        p = make_project(self.tmp / "wr", "multiuser", marker="alice")
        self.assertTrue(self.ch._agent_dir_writable(p))
        if os.geteuid() == 0:
            self.skipTest("root ignores mode bits")
        lane = p / ".agent" / "alice"
        lane.chmod(0o500)
        self.addCleanup(lane.chmod, 0o755)
        # Root .agent stays writable; only the lane is not — the distinction the
        # old root-only check could not make.
        self.assertTrue(os.access(p / ".agent", os.W_OK))
        self.assertFalse(self.ch._agent_dir_writable(p))

    def test_bad_marker_propagates_for_the_hook_policy(self):
        p = make_project(self.tmp / "bad", "legacy", marker="a/b")
        with self.assertRaises(InvalidUserMarkerError):
            self.ch.current_state_file(p, "pid-1")


class TestAdapterPaths(TempProjectCase):
    def _adapter(self, project_root: Path, session_id: str = "pid-1"):
        from provider.adapters.claude import ClaudeAdapter
        return ClaudeAdapter(session_id=session_id, project_root=project_root)

    def test_session_facts_read_from_the_lane(self):
        p = make_project(self.tmp / "mu", "multiuser", marker="alice")
        lane = p / ".agent" / "alice"
        task_md = make_task(lane, 12)
        activate(lane, "pid-1", 12)

        facts = self._adapter(p)._load_session_facts()
        self.assertEqual(facts.active_task_number, 12)
        self.assertEqual(facts.active_task_path, task_md)

    def test_session_facts_legacy_unchanged(self):
        p = make_project(self.tmp / "legacy", "legacy")
        root = p / ".agent"
        task_md = make_task(root, 4)
        activate(root, "pid-1", 4)

        facts = self._adapter(p)._load_session_facts()
        self.assertEqual(facts.active_task_number, 4)
        self.assertEqual(facts.active_task_path, task_md)

    def test_chat_log_offset_round_trips_in_the_lane(self):
        p = make_project(self.tmp / "mu", "multiuser", marker="alice")
        lane = p / ".agent" / "alice"
        (lane / "sessions" / "pid-1").mkdir(parents=True)

        adapter = self._adapter(p)
        adapter.save_chat_log_offset(4096)
        self.assertTrue((lane / "sessions" / "pid-1" / "chat_log_offset").exists())
        self.assertFalse((p / ".agent" / "sessions").exists())
        self.assertEqual(adapter._load_chat_log_offset(), 4096)

    def test_bad_marker_reports_no_active_task_never_root(self):
        # D6. An earlier version degraded to the root lane here and a test
        # pinned that as correct; the impl panel showed it was a real defect,
        # so this test now asserts the opposite.
        p = make_project(self.tmp / "bad", "legacy", marker="a/b")
        root = p / ".agent"
        make_task(root, 2)
        activate(root, "pid-1", 2)
        facts = self._adapter(p)._load_session_facts()
        self.assertIsNone(facts.active_task_number, "root state leaked through a bad marker")
        self.assertIsNone(facts.active_task_path)

    def test_bad_marker_cannot_surface_a_stale_root_task_as_active(self):
        # The scenario a judge reproduced: a multi-user repo whose root lane
        # still holds an old task. With a malformed marker the adapter used to
        # answer with that stale root task — so leftover state could satisfy
        # the gate for a session that has no task at all.
        p = make_project(self.tmp / "stale", "multiuser", marker="a/b")
        make_task(p / ".agent" / "alice", 5)
        make_task(p / ".agent", 99)
        activate(p / ".agent", "pid-1", 99)
        facts = self._adapter(p)._load_session_facts()
        self.assertIsNone(facts.active_task_number, "stale root task surfaced as active")

    def test_bad_marker_write_creates_no_root_state(self):
        # save_chat_log_offset goes through the same helper and used to
        # mkdir(parents=True) into the root lane.
        p = make_project(self.tmp / "wbad", "multiuser", marker="a/b")
        self._adapter(p).save_chat_log_offset(4096)
        self.assertFalse(
            (p / ".agent" / "sessions").exists(),
            "write path created root session state under a malformed marker",
        )
        self.assertEqual(self._adapter(p)._load_chat_log_offset(), 0)


class TestCodexHookSubprocess(TempProjectCase):
    """The real hook script, over stdin, asserting the process exit code.

    Contract (codex-apply-patch-hook): 0 = allow, 2 = deny.
    """

    HOOK = SCRIPTS / "codex-apply-patch-hook"

    def _run(self, project: Path, payload: dict, session_id: str = "pid-1"):
        env = {
            **os.environ,
            "PLAYBOOK_PROJECT_ROOT": str(project),
            "PLAYBOOK_SESSION_ID": session_id,
        }
        env.pop("PLAYBOOK_ROLE", None)
        return subprocess.run(
            [sys.executable, str(self.HOOK)],
            input=json.dumps(payload), capture_output=True, text=True,
            cwd=str(project), env=env,
        )

    @staticmethod
    def _patch_payload(path: str = "src/app.py"):
        return {
            "hook_event_name": "PreToolUse",
            "tool_name": "apply_patch",
            "tool_input": {
                "command": f"*** Begin Patch\n*** Update File: {path}\n*** End Patch\n"
            },
        }

    def test_denies_without_task_in_multiuser_repo(self):
        p = make_project(self.tmp / "mu", "multiuser", marker="alice")
        proc = self._run(p, self._patch_payload())
        self.assertEqual(proc.returncode, 2, f"stderr={proc.stderr}")

    def test_allows_with_task_active_in_the_lane(self):
        p = make_project(self.tmp / "mu2", "multiuser", marker="alice")
        lane = p / ".agent" / "alice"
        make_task(lane, 8)
        activate(lane, "pid-1", 8)
        proc = self._run(p, self._patch_payload())
        self.assertEqual(proc.returncode, 0, f"stderr={proc.stderr}")

    def test_task_active_in_the_WRONG_lane_still_denies(self):
        p = make_project(self.tmp / "mu3", "multiuser", marker="alice")
        make_task(p / ".agent" / "alice", 8)
        activate(p / ".agent", "pid-1", 8)   # root lane — the old bug's write
        proc = self._run(p, self._patch_payload())
        self.assertEqual(proc.returncode, 2, f"stderr={proc.stderr}")

    def test_legacy_repo_allows_with_task(self):
        p = make_project(self.tmp / "legacy", "legacy")
        make_task(p / ".agent", 1)
        activate(p / ".agent", "pid-1", 1)
        proc = self._run(p, self._patch_payload())
        self.assertEqual(proc.returncode, 0, f"stderr={proc.stderr}")

    def test_invalid_marker_fails_CLOSED_with_exit_2_not_1(self):
        """A4, the reason paths.py raises ValueError instead of SystemExit.

        SystemExit would escape the hook's `except Exception` and exit 1 — not
        the deny channel, so Codex would not treat it as a block.
        """
        p = make_project(self.tmp / "bad", "multiuser", marker="../evil")
        make_task(p / ".agent" / "alice", 8)
        proc = self._run(p, self._patch_payload())
        self.assertEqual(proc.returncode, 2, f"stderr={proc.stderr}")
        self.assertNotEqual(proc.returncode, 1, "SystemExit leaked past the handler")

    def test_invalid_marker_post_tool_use_fails_OPEN(self):
        p = make_project(self.tmp / "bad2", "multiuser", marker="../evil")
        payload = {**self._patch_payload(), "hook_event_name": "PostToolUse"}
        proc = self._run(p, payload)
        self.assertEqual(proc.returncode, 0, f"stderr={proc.stderr}")


class TestCodexHookFailurePolicy(TempProjectCase):
    """Every codex hook must apply its documented policy to a bad marker.

    `provider/paths.py` raises a catchable `ValueError` specifically so the
    hooks' handlers can decide. Two of the three had no handler at all, so the
    error escaped as a traceback + exit 1 — neither fail-open nor fail-closed.
    """

    def _run(self, hook: str, payload: dict, project: Path):
        env = {
            **os.environ,
            "PLAYBOOK_PROJECT_ROOT": str(project),
            "PLAYBOOK_SESSION_ID": "pid-1",
        }
        env.pop("PLAYBOOK_ROLE", None)
        return subprocess.run(
            [sys.executable, str(SCRIPTS / hook)],
            input=json.dumps(payload), capture_output=True, text=True,
            cwd=str(project), env=env,
        )

    def test_user_prompt_hook_fails_open_on_bad_marker(self):
        p = make_project(self.tmp / "up", "multiuser", marker="../evil")
        proc = self._run(
            "codex-user-prompt-hook",
            {"hook_event_name": "UserPromptSubmit", "prompt": "hello", "turn_id": "t1"},
            p,
        )
        self.assertEqual(proc.returncode, 0, f"stderr={proc.stderr}")
        self.assertNotIn("Traceback", proc.stderr)

    def test_stop_hook_fails_open_on_bad_marker(self):
        p = make_project(self.tmp / "st", "multiuser", marker="../evil")
        proc = self._run("codex-stop-hook", {"hook_event_name": "Stop", "turn_id": "t1"}, p)
        self.assertEqual(proc.returncode, 0, f"stderr={proc.stderr}")
        self.assertNotIn("Traceback", proc.stderr)

    def test_bad_marker_writes_no_root_state_from_either_hook(self):
        p = make_project(self.tmp / "nw", "multiuser", marker="../evil")
        self._run(
            "codex-user-prompt-hook",
            {"hook_event_name": "UserPromptSubmit", "prompt": "hello", "turn_id": "t1"}, p,
        )
        self._run("codex-stop-hook", {"hook_event_name": "Stop", "turn_id": "t1"}, p)
        self.assertFalse((p / ".agent" / "sessions").exists())
        self.assertFalse((p / ".agent" / "chat_log.md").exists())

    def test_fresh_clone_writes_no_root_state_from_either_hook(self):
        # Lanes present, marker gitignored-absent: running `codex` directly
        # (hooks fire from repo-local .codex/hooks.json, not only via the
        # wrapper) must not mint a phantom root lane.
        p = make_project(self.tmp / "fc", "multiuser")
        self._run(
            "codex-user-prompt-hook",
            {"hook_event_name": "UserPromptSubmit", "prompt": "hello", "turn_id": "t1"}, p,
        )
        self._run("codex-stop-hook", {"hook_event_name": "Stop", "turn_id": "t1"}, p)
        self.assertFalse((p / ".agent" / "sessions").exists())
        self.assertFalse((p / ".agent" / "chat_log.md").exists())
        self.assertEqual([c.name for c in (p / ".agent").iterdir()], ["alice"])

    def test_healthy_repo_still_logs_the_prompt(self):
        # Guard against "fixed by never doing anything".
        p = make_project(self.tmp / "ok", "multiuser", marker="alice")
        proc = self._run(
            "codex-user-prompt-hook",
            {"hook_event_name": "UserPromptSubmit", "prompt": "hello there", "turn_id": "t1"}, p,
        )
        self.assertEqual(proc.returncode, 0, f"stderr={proc.stderr}")
        log = p / ".agent" / "alice" / "chat_log.md"
        self.assertTrue(log.exists(), "prompt was not logged in the lane")
        self.assertIn("hello there", log.read_text())


class TestCodexHookRootWalk(TempProjectCase):
    """Fix 3: the hooks' own `_find_project_root()` fallback, env var UNSET.

    Every lane-relative path a hook computes hangs off this walk, so a
    `.agent/tasks`-only version silently returns cwd in a per-user repo and the
    whole hook operates on the wrong tree.
    """

    HOOKS = ["codex-apply-patch-hook", "codex-user-prompt-hook", "codex-stop-hook"]

    def _walk_from(self, hook: str, cwd: Path) -> str:
        """Import the hook as a module and call its _find_project_root()."""
        code = (
            "import importlib.util, importlib.machinery, sys\n"
            "spec = importlib.util.spec_from_loader('h', importlib.machinery.SourceFileLoader('h', sys.argv[1]))\n"
            "m = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(m)\n"
            "m._bootstrap_imports()\n"
            "print(m._find_project_root())\n"
        )
        env = {k: v for k, v in os.environ.items() if k != "PLAYBOOK_PROJECT_ROOT"}
        env.pop("PLAYBOOK_ROLE", None)
        proc = subprocess.run(
            [sys.executable, "-c", code, str(SCRIPTS / hook)],
            cwd=str(cwd), capture_output=True, text=True, env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def test_walk_finds_multiuser_root_from_subdir(self):
        p = make_project(self.tmp / "mu", "multiuser", marker="alice")
        deep = p / "src" / "deep"
        deep.mkdir(parents=True)
        for hook in self.HOOKS:
            with self.subTest(hook=hook):
                self.assertEqual(self._walk_from(hook, deep), str(p))

    def test_walk_finds_multiuser_root_without_marker(self):
        # Fresh-clone shape: lanes exist, marker gitignored away.
        p = make_project(self.tmp / "fresh", "multiuser")
        for hook in self.HOOKS:
            with self.subTest(hook=hook):
                self.assertEqual(self._walk_from(hook, p), str(p))

    def test_walk_still_finds_legacy_root_from_subdir(self):
        p = make_project(self.tmp / "legacy", "legacy")
        deep = p / "a" / "b"
        deep.mkdir(parents=True)
        for hook in self.HOOKS:
            with self.subTest(hook=hook):
                self.assertEqual(self._walk_from(hook, deep), str(p))

    def test_env_var_still_wins(self):
        p = make_project(self.tmp / "mu2", "multiuser", marker="alice")
        code = (
            "import importlib.util, importlib.machinery, sys\n"
            "spec = importlib.util.spec_from_loader('h', importlib.machinery.SourceFileLoader('h', sys.argv[1]))\n"
            "m = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(m)\n"
            "print(m._find_project_root())\n"
        )
        env = {**os.environ, "PLAYBOOK_PROJECT_ROOT": "/explicit/override"}
        env.pop("PLAYBOOK_ROLE", None)
        proc = subprocess.run(
            [sys.executable, "-c", code, str(SCRIPTS / "codex-apply-patch-hook")],
            cwd=str(p), capture_output=True, text=True, env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "/explicit/override")


# ── 4. Split-brain end-to-end ────────────────────────────────────────────────


class TestSplitBrainEndToEnd(TempProjectCase):
    """wrapper provisions → tasks CLI writes → codex hook reads: ONE lane.

    Each leg was independently root-only or lane-aware before this task; only
    running the real three together proves they converged.
    """

    def test_wrapper_cli_and_hook_share_one_lane(self):
        project = make_project(self.tmp / "e2e", "multiuser", marker="alice")
        lane = project / ".agent" / "alice"
        (project / "MIND_MAP.md").write_text("# map\n", encoding="utf-8")

        # 1. A PATH-shimmed codex, so the wrapper reaches its exec and stops.
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "codex").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
        (bin_dir / "codex").chmod(0o755)
        env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}

        wrapper = subprocess.run(
            [str(SCRIPTS / "playbook-codex")],
            cwd=str(project), env=env, capture_output=True, text=True,
        )
        self.assertEqual(wrapper.returncode, 0, wrapper.stderr)

        provisioned = sorted((lane / "sessions").glob("pid-*"))
        self.assertEqual(len(provisioned), 1, "wrapper did not provision in the lane")
        self.assertFalse(
            (project / ".agent" / "sessions").exists(),
            "wrapper created a root-lane session dir",
        )
        session_id = provisioned[0].name

        # 2. The real tasks CLI creates and activates a task in that session.
        cli_env = {**os.environ, "PLAYBOOK_SESSION_ID": session_id, "PYTHONPATH": str(PLUGIN)}
        new = subprocess.run(
            [str(SCRIPTS / "tasks"), "new", "bugfix", "e2e-demo", "demo"],
            cwd=str(project), env=cli_env, capture_output=True, text=True,
        )
        self.assertEqual(new.returncode, 0, new.stderr or new.stdout)
        work = subprocess.run(
            [str(SCRIPTS / "tasks"), "work", "1"],
            cwd=str(project), env=cli_env, capture_output=True, text=True,
        )
        self.assertEqual(work.returncode, 0, work.stderr or work.stdout)

        # The CLI must have written into the SAME dir the wrapper made.
        state = lane / "sessions" / session_id / "current_state"
        self.assertTrue(state.exists(), "tasks CLI wrote to a different lane")

        # 3. The codex hook, reading independently, now sees the active task.
        hook_env = {
            **os.environ,
            "PLAYBOOK_PROJECT_ROOT": str(project),
            "PLAYBOOK_SESSION_ID": session_id,
        }
        hook_env.pop("PLAYBOOK_ROLE", None)
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "apply_patch",
            "tool_input": {
                "command": "*** Begin Patch\n*** Update File: src/app.py\n*** End Patch\n"
            },
        }
        allowed = subprocess.run(
            [sys.executable, str(SCRIPTS / "codex-apply-patch-hook")],
            input=json.dumps(payload), capture_output=True, text=True,
            cwd=str(project), env=hook_env,
        )
        self.assertEqual(allowed.returncode, 0, f"hook denied an active task: {allowed.stderr}")

        # 4. Deactivate → the same hook must deny again (the gate really works).
        # --force: the scratch task's template gates are deliberately unfinished;
        # we are testing lane plumbing, not gate discipline.
        done = subprocess.run(
            [str(SCRIPTS / "tasks"), "work", "done", "--force", "--reason", "plumbing test"],
            cwd=str(project), env=cli_env, capture_output=True, text=True,
        )
        self.assertEqual(done.returncode, 0, done.stderr or done.stdout)
        denied = subprocess.run(
            [sys.executable, str(SCRIPTS / "codex-apply-patch-hook")],
            input=json.dumps(payload), capture_output=True, text=True,
            cwd=str(project), env=hook_env,
        )
        self.assertEqual(denied.returncode, 2, "hook allowed an edit with no active task")


# ── 5. The bundled shell loggers' lane policy (PB-LANE-RESOLUTION) ───────────
#
# The Critical violation the Phase 1 guarantee ledger recorded. On a fresh clone
# of a multi-user repo — `.agent/<user>/` lanes present, the gitignored
# `.agent/current_user` marker absent, no root `.agent/tasks/` — both bundled
# loggers initialised the lane to the ROOT `.agent` and only reassigned it
# inside the `current_user` branch. The marker-absent path therefore fell
# through and appended to the SHARED root `.agent/bash_history`: the cross-user
# contamination the lane model exists to prevent, while the lane's own history
# file was never written at all.
#
# The policy pinned below:
#
#     valid marker                          -> the validated user lane
#     no marker + root `.agent/tasks/`      -> the root IS a legitimate lane
#     no marker + NO per-user lane present  -> the root IS a legitimate lane
#     no marker + a per-user lane present   -> owner unknown; skip logging
#     invalid / unusable marker             -> owner unknown; skip logging
#
# A "per-user lane" is exactly what `provider/paths.py::lanes_without_marker`
# counts: a direct child directory of `.agent/` that itself contains `tasks/`.
# The logger therefore writes the root precisely when `lanes_without_marker` is
# empty and skips precisely when it is not, on every shape a supported surface
# can produce — and that is asserted, not asserted in prose: `TestResolverParity.test_logger_agrees_with_lanes_without_marker`
# runs the same vector table through all three reference implementations and the
# logger. An earlier revision of this fix skipped whenever there was no root
# `.agent/tasks/`, which diverged from all three on four shapes (bare `.agent/`,
# `.agent/config.json` only, `.agent/sessions/` only, a non-lane child) and lost
# history that `tasks retro` and `tasks context` still read from the root.
#
# One shape is deliberately OUTSIDE that table because no single expected value
# can hold all four implementations: a DOT-named lane. See
# `test_dot_named_lane_is_a_known_shell_python_divergence`, which pins the split
# as a fact rather than leaving it to be rediscovered.
#
# The root-lane cases are deliberate ADVERSE CONTROLS, not decoration: a blanket
# "never log to root" fix satisfies every marker-absent assertion here while
# silently killing logging for every legacy, single-user, and mixed-layout
# project. It is the same exemption wrapper scenario S6 defends.
#
# These cases execute the REAL bundled `bash-log.sh` through `bash` and inspect
# filesystem effects. They never rely on an inherited `BASH_ENV`: the host may
# be dogfooding the plugin, in which case the INSTALLED (possibly stale) logger
# would be sourced into every probe shell and log a second time, masking the
# candidate's behavior. Every probe therefore runs with `BASH_ENV` stripped and
# sources the repository copy by explicit path.
#
# zsh is not installed on this host, so `bash-log.zsh` cannot be executed here.
# It is held to the same policy structurally (`ZshLoggerSourceParity`) and its
# live execution is scheduled for the Phase 8 macOS cell — not claimed here.

PROBE = "playbook_lane_probe_command"


class LoggerProbeCase(unittest.TestCase):
    """Runs the bundled bash logger for real and reports what it wrote."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    # ── project shapes ───────────────────────────────────────────────────
    def make_lane_project(self, name: str, *, lanes=(), root_tasks=False,
                     dirs=(), files=(),
                     marker: str | None = None, marker_bytes: str | None = None) -> Path:
        project = self.root / name
        (project / ".agent").mkdir(parents=True)
        if root_tasks:
            (project / ".agent" / "tasks").mkdir()
        for lane in lanes:
            (project / ".agent" / lane / "tasks").mkdir(parents=True)
        for name_ in dirs:
            (project / ".agent" / name_).mkdir(parents=True, exist_ok=True)
        for name_ in files:
            (project / ".agent" / name_).write_text("{}\n", encoding="utf-8")
        if marker is not None:
            marker_bytes = marker + "\n"
        if marker_bytes is not None:
            # newline="" so the exact bytes land on disk (CRLF vectors would
            # otherwise be translated back to LF and stop testing anything).
            with open(project / ".agent" / "current_user", "w", newline="") as fh:
                fh.write(marker_bytes)
        return project

    # ── the probe ────────────────────────────────────────────────────────
    def run_logger(self, project: Path, *, errexit: bool = False,
                   prelude: str = "", epilogue: str = ""):
        """Source the bundled logger in a real bash and run one command.

        Returns the CompletedProcess. `ALIVE` on stdout proves the shell
        survived the DEBUG trap — a logger that skips must skip by returning,
        never by exiting or by tripping the host shell's errexit.

        `prelude` runs BEFORE the logger is sourced, which is where a host
        shell's `shopt`/`set` options come from: this file is sourced via
        BASH_ENV into a shell the logger does not control, so it inherits
        whatever globbing and error options that shell already set.
        `epilogue` runs after the probe, to observe what the logger left behind.
        """
        env = {k: v for k, v in os.environ.items() if k != "BASH_ENV"}
        env.pop("PLAYBOOK_SESSION_ID", None)
        env.pop("PLAYBOOK_ROLE", None)
        script = (
            (prelude + "\n" if prelude else "")
            + ("set -e\n" if errexit else "")
            + 'source "$1"\n'
            + f"echo {PROBE} >/dev/null\n"
            + "echo ALIVE\n"
            + (epilogue + "\n" if epilogue else "")
        )
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-c", script, "_", str(BASH_LOG)],
            cwd=str(project), env=env, text=True, capture_output=True,
        )

    # ── observations ─────────────────────────────────────────────────────
    def histories(self, project: Path) -> list[str]:
        """Every bash_history under the project, as `.agent`-relative paths."""
        agent = project / ".agent"
        return sorted(
            str(p.relative_to(agent))
            for p in agent.rglob("bash_history")
        )

    def lane_decision(self, project: Path) -> str:
        """The logger's answer as one label: 'skip', 'root', or 'lane:<name>'."""
        hist = self.histories(project)
        if not hist:
            return "skip"
        if hist == ["bash_history"]:
            return "root"
        return "lane:" + "+".join(sorted(h.rsplit("/", 1)[0] for h in hist))

    def assert_logged_probe(self, project: Path, rel: str) -> None:
        path = project / ".agent" / rel
        self.assertTrue(path.is_file(), f"{rel} was not written")
        body = path.read_text(encoding="utf-8")
        self.assertIn(PROBE, body, f"{rel} exists but holds no probe line")
        self.assertRegex(body, r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \| AGENT \| ",
                         "history line lost its timestamp/actor framing")


# ── 1. marker absent, lanes present, no root lane: the violation ─────────────

class MarkerAbsentWithLanes(LoggerProbeCase):
    """RED before the fix: the logger wrote the shared root `.agent/bash_history`."""

    def test_marker_absent_with_one_lane_writes_nothing(self):
        project = self.make_lane_project("fresh-clone", lanes=["alice"])
        result = self.run_logger(project)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ALIVE", result.stdout, "the probe shell did not survive")
        self.assertFalse(
            (project / ".agent" / "bash_history").exists(),
            "the logger created the SHARED root .agent/bash_history on a fresh "
            "clone — the cross-user contamination PB-LANE-RESOLUTION records",
        )
        self.assertFalse(
            (project / ".agent" / "alice" / "bash_history").exists(),
            "the logger guessed a lane it had no marker for",
        )
        self.assertEqual(self.histories(project), [],
                         "ownership is unknown here; nothing may be written")

    def test_non_lane_siblings_do_not_cancel_a_real_lane(self):
        """A real lane plus junk: still the fresh-clone shape, still a skip.

        `lanes_without_marker` reports `['alice']` here, and there is no root
        `.agent/tasks/`, so nothing may be written.
        """
        project = self.make_lane_project("lane-plus-junk", lanes=["alice"],
                                          dirs=["sessions"], files=["config.json"])
        result = self.run_logger(project)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.histories(project), [],
                         "a non-lane sibling cancelled a real per-user lane")

    def test_marker_absent_with_several_lanes_picks_none_of_them(self):
        project = self.make_lane_project("many-lanes", lanes=["alice", "bob", "carol"])
        result = self.run_logger(project)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.histories(project), [],
                         "no lane may be selected arbitrarily without a marker")

    def test_marker_absent_leaves_the_lane_directory_untouched(self):
        """Not just the history file: the fresh clone must be byte-identical."""
        project = self.make_lane_project("untouched", lanes=["alice"])
        before = sorted(str(p.relative_to(project)) for p in project.rglob("*"))
        self.run_logger(project)
        after = sorted(str(p.relative_to(project)) for p in project.rglob("*"))
        self.assertEqual(before, after, "the logger created state on a fresh clone")

    def test_marker_absent_under_errexit_does_not_kill_the_shell(self):
        """Skipping must be a `return 0`, never a bare return or an exit.

        This logger runs in a DEBUG trap sourced into every hook shell; a
        non-zero return there kills a `set -e` host (field report 2026-07-21).
        """
        project = self.make_lane_project("errexit", lanes=["alice"])
        result = self.run_logger(project, errexit=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ALIVE", result.stdout)

    def test_marker_absent_deep_subdirectory_still_writes_nothing(self):
        """The walk finds the same `.agent`; the answer must not change."""
        project = self.make_lane_project("deep", lanes=["alice"])
        deep = project / "src" / "pkg" / "mod"
        deep.mkdir(parents=True)
        env = {k: v for k, v in os.environ.items()
               if k not in ("BASH_ENV", "PLAYBOOK_SESSION_ID", "PLAYBOOK_ROLE")}
        result = subprocess.run(
            ["bash", "--noprofile", "--norc", "-c",
             f'source "$1"\necho {PROBE} >/dev/null\necho ALIVE\n', "_", str(BASH_LOG)],
            cwd=str(deep), env=env, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.histories(project), [])


# ── 2. valid marker: the user's lane, and only the user's lane ───────────────

class ValidMarker(LoggerProbeCase):

    def test_valid_marker_writes_the_lane_not_the_root(self):
        project = self.make_lane_project("mu", lanes=["alice"], marker="alice")
        result = self.run_logger(project)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_logged_probe(project, "alice/bash_history")
        self.assertFalse((project / ".agent" / "bash_history").exists(),
                         "wrote the shared root as well as the lane")
        self.assertEqual(self.histories(project), ["alice/bash_history"])

    def test_valid_marker_wins_over_a_root_lane(self):
        """Mixed layout WITH a marker: the marker decides, not the root."""
        project = self.make_lane_project("mixed-marked", lanes=["alice"],
                                    root_tasks=True, marker="alice")
        self.run_logger(project)
        self.assertEqual(self.histories(project), ["alice/bash_history"])

    def test_crlf_marker_still_resolves_the_lane(self):
        """A Windows checkout must not silently disable logging."""
        project = self.make_lane_project("crlf", lanes=["alice"], marker_bytes="alice\r\n")
        self.run_logger(project)
        self.assertEqual(self.histories(project), ["alice/bash_history"])

    def test_marker_without_a_trailing_newline_still_resolves(self):
        project = self.make_lane_project("nonl", lanes=["alice"], marker_bytes="alice")
        self.run_logger(project)
        self.assertEqual(self.histories(project), ["alice/bash_history"])

    def test_valid_marker_whose_lane_dir_is_missing_writes_nothing(self):
        """The named lane does not exist yet: skip, never fall back to root."""
        project = self.make_lane_project("ghost", lanes=["alice"], marker="bob")
        result = self.run_logger(project)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.histories(project), [],
                         "fell back to the root when the marked lane was absent")


# ── 3. invalid / unusable marker: skip, and never fall back to the root ──────

UNUSABLE_MARKER_BYTES = [
    ("empty", ""),
    ("blank_line", "\n"),
    ("dot", ".\n"),
    ("dotdot", "..\n"),
    ("traversal", "../evil\n"),
    ("slash", "a/b\n"),
    ("dash_lead", "-dash\n"),
    ("underscore_lead", "_under\n"),
    ("space", "has space\n"),
    ("hidden", ".hidden\n"),
    ("smuggled_second_line", "alice\n../evil\n"),
    ("two_lines", "alice\nbob\n"),
    ("crlf_two_lines", "alice\r\nbob\r\n"),
    ("at_sign", "alice@evil\n"),
]


class InvalidMarker(LoggerProbeCase):

    def test_invalid_markers_skip_and_never_write_the_root(self):
        for name, raw in UNUSABLE_MARKER_BYTES:
            with self.subTest(marker=name):
                project = self.make_lane_project(f"bad-{name}", lanes=["alice"],
                                            marker_bytes=raw)
                result = self.run_logger(project)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("ALIVE", result.stdout, "shell did not survive")
                self.assertEqual(
                    self.histories(project), [],
                    "an unusable marker must skip logging, not fall back to "
                    "the shared root",
                )

    def test_invalid_marker_does_not_write_the_root_even_with_a_root_lane(self):
        """A root lane exists, but the marker is the authority and it is broken.

        The root-tasks exemption is for the marker-ABSENT case only; a present
        but unusable marker means the owning lane is unknown, and answering the
        root anyway is exactly the contamination this guarantee forbids.
        """
        project = self.make_lane_project("bad-with-root", lanes=["alice"],
                                    root_tasks=True, marker_bytes="../evil\n")
        result = self.run_logger(project)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.histories(project), [])

    def test_unreadable_marker_skips(self):
        project = self.make_lane_project("unreadable", lanes=["alice"], marker="alice")
        marker = project / ".agent" / "current_user"
        marker.chmod(0o000)
        self.addCleanup(marker.chmod, 0o644)
        if os.access(marker, os.R_OK):   # running as root: the mode is advisory
            self.skipTest("cannot make a file unreadable for this user")
        result = self.run_logger(project)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.histories(project), [],
                         "an unreadable marker fell back to a lane or the root")


# ── 4. ADVERSE CONTROL: the legitimate root lane must keep logging ───────────

class LegitimateRootLane(LoggerProbeCase):
    """Rejects a blanket "never log to root" implementation.

    Wrapper scenario S6 defends the same exemption at the wrapper boundary:
    root `.agent/tasks/` means the root IS a lane, and refusing it would break
    every legacy project. The tail of the class pins the other side of that one
    fact: without root `.agent/tasks/` there is no root lane to elect.
    """

    def test_legacy_root_lane_logs_to_the_root(self):
        project = self.make_lane_project("legacy", root_tasks=True)
        result = self.run_logger(project)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_logged_probe(project, "bash_history")
        self.assertEqual(self.histories(project), ["bash_history"])

    def test_mixed_layout_without_a_marker_logs_to_the_root(self):
        """Scenario S6's exact shape: root tasks AND a lane, no marker."""
        project = self.make_lane_project("mixed", lanes=["alice"], root_tasks=True)
        result = self.run_logger(project)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_logged_probe(project, "bash_history")
        self.assertFalse((project / ".agent" / "alice" / "bash_history").exists())

    def test_root_lane_appends_rather_than_truncates(self):
        project = self.make_lane_project("append", root_tasks=True)
        self.run_logger(project)
        self.run_logger(project)
        body = (project / ".agent" / "bash_history").read_text(encoding="utf-8")
        self.assertEqual(body.count(PROBE), 2, "the logger truncated the history")

    def test_bare_agent_directory_writes_nothing(self):
        """`.agent/` with nothing in it: no marker and no root `tasks/`.

        `resolve_agent_dir` answers `<root>/.agent` here, so the logger is
        stricter than it: nothing is written, and nothing is contaminated
        either. It starts logging as soon as `tasks/` exists.
        """
        project = self.make_lane_project("bare")
        result = self.run_logger(project)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.histories(project), [])

    def test_committed_config_only_writes_nothing(self):
        """The reachable shape, and the one the strictness actually costs.

        `.agent/config.json` is documented as committable and git tracks no
        empty `tasks/`, so a clone of a single-user project arrives with exactly
        this. `tasks retro`/`tasks context` read the root file, so until `init`
        creates `tasks/` this project has no shell history — a missing forensic
        log, deliberately preferred over electing a lane nobody owns.
        """
        project = self.make_lane_project("committed-config", files=["config.json"])
        result = self.run_logger(project)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.histories(project), [])

    def test_sessions_dir_only_writes_nothing(self):
        project = self.make_lane_project("sessions-only", dirs=["sessions"])
        result = self.run_logger(project)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.histories(project), [])

    def test_child_directory_without_tasks_writes_nothing(self):
        """`.agent/alice/` with no `alice/tasks/`: not a lane, and not the root.

        The children of `.agent/` are not consulted at all, so this shape is
        indistinguishable from a bare `.agent/` — no root `tasks/`, no write.
        """
        project = self.make_lane_project("child-no-tasks", dirs=["alice"])
        result = self.run_logger(project)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.histories(project), [])

    def test_root_logging_under_errexit_does_not_kill_the_shell(self):
        """The root path must still be a clean `return 0` for a `set -e` host."""
        project = self.make_lane_project("errexit-root", root_tasks=True)
        result = self.run_logger(project, errexit=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ALIVE", result.stdout)
        self.assert_logged_probe(project, "bash_history")


# ── 5. HOST SHELL OPTIONS: the logger does not control the shell it lives in ─

# (label, prelude, errexit) — what a host shell may already have set when
# BASH_ENV sources this logger into it.
HOST_SHELL_OPTIONS = [
    ("default",           "",                   False),
    ("errexit",           "",                   True),
    ("nounset",           "set -u",             False),
    ("pipefail",          "set -o pipefail",    False),
    ("failglob",          "shopt -s failglob",  False),
    ("nullglob",          "shopt -s nullglob",  False),
    ("dotglob",           "shopt -s dotglob",   False),
    ("extglob",           "shopt -s extglob",   False),
    ("failglob+errexit",  "shopt -s failglob",  True),
    ("nullglob+errexit",  "shopt -s nullglob",  True),
    ("dotglob+errexit",   "shopt -s dotglob",   True),
    # Not a shell option but the same class of leak: a non-empty GLOBIGNORE
    # makes bash match dotfiles, exactly as dotglob does.
    ("globignore",        "GLOBIGNORE=x",       False),
    # The three a glob in this file could not survive. Measured on the 1.5.34
    # candidate, which globbed `.agent/`'s children looking for a per-user lane:
    # each of these silently turned a required skip into a write of the SHARED
    # root — with globbing off the pattern stayed literal and matched nothing,
    # and `GLOBIGNORE='*'` discarded every match. They are the regression guard
    # for that glob staying deleted, and they pass trivially without it.
    ("noglob_set_f",      "set -f",             False),
    ("noglob_set_o",      "set -o noglob",      False),
    ("globignore_star",   "GLOBIGNORE='*'",     False),
]


class HostShellOptionMatrix(LoggerProbeCase):
    """Every in-policy shape under every host shell option combination.

    This dimension was entirely untested until a reviewer found that the
    per-user-lane scan — the file's only glob — was evaluated by the HOST
    shell's rules, and the file is sourced into a shell it does not control.
    `shopt -s failglob` raised an error before the scan's `-d` guard could run;
    `set -f`, `set -o noglob` and `GLOBIGNORE='*'` each turned a required skip
    into a write of the shared root. The scan is gone: the decision now rests on
    two `-f`/`-d` tests that no shell option reaches, and this matrix is what
    keeps it that way.

    Four things are asserted for every cell, because three of them were the
    symptoms: exit 0, `ALIVE` on stdout, EMPTY stderr, and the same lane
    decision the shape gets under default options. The decision must be a
    property of the project layout alone — never of the caller's shell options.
    """

    # The marker shapes complete the policy answers; NO_MARKER_SHAPES supplies
    # the other nine with the logger's own answer already in the table.
    # (name, marker, expected) — both carry a real `alice` lane, so the marker
    # is the only thing deciding the answer.
    MARKER_SHAPES = [
        ("valid_marker",   "alice",   "lane:alice"),
        ("invalid_marker", "../evil", "skip"),
    ]

    def _cases(self):
        for name, build, _lanes_expected, logger_expected in NO_MARKER_SHAPES:
            yield name, build, None, logger_expected
        for name, marker, expected in self.MARKER_SHAPES:
            yield name, None, marker, expected

    def _make(self, name, build, marker, suffix):
        if marker is not None:
            return self.make_lane_project(
                f"{name}-{suffix}", lanes=["alice"], marker=marker)
        project = self.make_lane_project(f"{name}-{suffix}")
        build(project / ".agent")
        return project

    def test_every_in_policy_shape_survives_every_host_shell_option(self):
        for shape, build, marker, expected in self._cases():
            for label, prelude, errexit in HOST_SHELL_OPTIONS:
                with self.subTest(shape=shape, host_options=label):
                    project = self._make(shape, build, marker, label)
                    result = self.run_logger(project, prelude=prelude, errexit=errexit)

                    self.assertEqual(
                        result.returncode, 0,
                        f"{shape} under `{label}`: the host shell died "
                        f"(stderr={result.stderr!r})")
                    self.assertIn(
                        "ALIVE", result.stdout,
                        f"{shape} under `{label}`: the host shell never reached "
                        f"the command after the probe")
                    self.assertEqual(
                        result.stderr, "",
                        f"{shape} under `{label}`: the logger wrote to stderr, "
                        f"which hook output feeds back to the agent")
                    self.assertEqual(
                        self.lane_decision(project), expected,
                        f"{shape} under `{label}`: the host shell's options "
                        f"changed the lane decision")

    def test_the_logger_leaves_the_hosts_globbing_options_untouched(self):
        """No residue: the host shell's options must survive the logger.

        The deleted scan had to switch `failglob` off to run at all, and so had
        to switch it back on both exits. Nothing globs here now, so nothing may
        be switched at all — a leak would silently change how every later glob
        in the user's own shell behaves.
        """
        probe = (
            'if shopt -q failglob; then echo "failglob=set"; else echo "failglob=unset"; fi\n'
            'if shopt -q nullglob; then echo "nullglob=set"; else echo "nullglob=unset"; fi\n'
            'if shopt -q dotglob; then echo "dotglob=set"; else echo "dotglob=unset"; fi'
        )
        for shape, lanes, decision in (("bare", [], "skip"),
                                       ("one_lane", ["alice"], "skip")):
            for host_has_failglob in (True, False):
                with self.subTest(shape=shape, failglob_set_by_host=host_has_failglob):
                    project = self.make_lane_project(
                        f"{shape}-residue-{host_has_failglob}", lanes=lanes)
                    result = self.run_logger(
                        project,
                        prelude="shopt -s failglob" if host_has_failglob else "",
                        epilogue=probe)

                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(self.lane_decision(project), decision)
                    want = "set" if host_has_failglob else "unset"
                    self.assertIn(f"failglob={want}", result.stdout,
                                  "the logger changed the host's failglob")
                    self.assertIn("nullglob=unset", result.stdout,
                                  "the logger leaked nullglob into the host shell")
                    self.assertIn("dotglob=unset", result.stdout,
                                  "the logger leaked dotglob into the host shell")

    def test_dotglob_no_longer_moves_the_dot_named_lane_answer(self):
        """`shopt -s dotglob` and a non-empty `GLOBIGNORE` used to move this.

        `.agent/.hidden/tasks/` was visible to the deleted scan under either
        option and invisible without them, so the same project got two different
        answers. With no glob left the shape is just a marker-absent project with
        no root `tasks/`: skip, under all three preludes.
        """
        default = self.make_lane_project("dot-default", lanes=[".hidden"])
        projects = {opt: self.make_lane_project(f"dot-{n}", lanes=[".hidden"])
                    for n, opt in (("dotglob", "shopt -s dotglob"),
                                   ("globignore", "GLOBIGNORE=x"))}

        results = {"": self.run_logger(default)}
        for opt, project in projects.items():
            results[opt] = self.run_logger(project, prelude=opt)

        for opt, result in results.items():
            with self.subTest(host_options=opt or "default"):
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertIn("ALIVE", result.stdout)

        self.assertEqual(self.lane_decision(default), "skip",
                         "no root .agent/tasks/, so there is nothing to write")
        for opt, project in projects.items():
            with self.subTest(host_options=opt):
                self.assertEqual(
                    self.lane_decision(project), "skip",
                    f"`{opt}` moved the answer, so something is reading the "
                    f"children of .agent/ again")


# ── 6. zsh: source-level parity only (zsh is not installed on this host) ─────

class ZshLoggerSourceParity(unittest.TestCase):
    """The zsh logger carries the identical lane policy — checked as SOURCE.

    zsh is unavailable here, so this asserts structure, not behavior. The
    executed-zsh cell is Phase 8 work and is recorded as unverified, not green.
    """

    def setUp(self) -> None:
        self.bash = BASH_LOG.read_text(encoding="utf-8")
        self.zsh = ZSH_LOG.read_text(encoding="utf-8")
        # EVERY assertion in this class runs against CODE, never the raw file.
        # A reviewer found the raw-text form was a false green: these files
        # DOCUMENT their own idioms in comments, so an `assertIn(<idiom>, src)`
        # was satisfied by the comment explaining the idiom and stayed green
        # after the code itself had been changed.
        self.bash_code = "\n".join(self._code_lines(self.bash))
        self.zsh_code = "\n".join(self._code_lines(self.zsh))
        self.code = (("bash", self.bash_code), ("zsh", self.zsh_code))

    def test_both_loggers_gate_the_root_on_root_tasks(self):
        for label, src in self.code:
            with self.subTest(shell=label):
                self.assertRegex(
                    src, r'-d\s+"\$[_A-Za-z]*[Dd]ir/\.agent/tasks"',
                    f"{label} logger lost the root-lane (.agent/tasks) exemption",
                )

    @staticmethod
    def _code_lines(src: str) -> list[str]:
        return [ln.strip() for ln in src.splitlines()
                if ln.strip() and not ln.strip().startswith("#")]

    LANE_INIT = re.compile(r'^(local\s+)?_(cpb_log_)?lane=""$')
    LANE_IS_ROOT = re.compile(
        r'^(local\s+)?_(cpb_log_)?lane="\$[_A-Za-z]*[Dd]ir/\.agent"$')
    MARKER_TEST = re.compile(r'^if \[\[ -f "\$[_A-Za-z]*[Dd]ir/\.agent/current_user" \]\]')
    ROOT_TASKS_GUARD = re.compile(r'^elif \[\[ -d "\$[_A-Za-z]*[Dd]ir/\.agent/tasks" \]\]')

    def test_neither_logger_initialises_the_lane_to_the_bare_root(self):
        """The defect itself: `_lane=<root>` BEFORE the marker is consulted.

        The root is a valid answer — but only as the `.agent/tasks/` branch's
        conclusion, never as the starting value that a marker-absent path can
        fall through to. So this asserts placement, not absence: the lane is
        initialised empty, and every assignment of the bare root sits directly
        under the root-tasks guard, which is now its ONLY legitimate origin.
        """
        for label, src in (("bash", self.bash), ("zsh", self.zsh)):
            with self.subTest(shell=label):
                lines = self._code_lines(src)
                marker_at = [i for i, ln in enumerate(lines) if self.MARKER_TEST.match(ln)]
                self.assertEqual(len(marker_at), 1,
                                 f"{label}: expected exactly one marker test")
                marker_at = marker_at[0]

                init_at = [i for i, ln in enumerate(lines) if self.LANE_INIT.match(ln)]
                self.assertEqual(len(init_at), 1,
                                 f"{label}: lane is not initialised exactly once as unknown")
                self.assertLess(init_at[0], marker_at,
                                f"{label}: lane initialised after the marker test")

                for i, ln in enumerate(lines):
                    if not self.LANE_IS_ROOT.match(ln):
                        continue
                    self.assertGreater(
                        i, marker_at,
                        f"{label} logger still defaults the lane to the shared root")
                    self.assertRegex(
                        lines[i - 1], self.ROOT_TASKS_GUARD,
                        f"{label} logger selects the root somewhere other than "
                        f"the .agent/tasks/ exemption, which is its only "
                        f"legitimate origin",
                    )

    def test_the_zsh_logger_leaves_no_variable_in_the_users_environment(self):
        """`~/.zshenv` is the user's own shell, so every scratch name goes.

        Derived from the code rather than hard-coded, so a new `_cpb_log_*`
        variable cannot be introduced without also being cleaned up, and none
        of the existing ones can be dropped from the `unset` line. Asserted
        against CODE: the raw file mentions these names in its comments.
        """
        lines = self._code_lines(self.zsh)
        unset_lines = [ln for ln in lines if ln.startswith("unset ")]
        self.assertEqual(len(unset_lines), 1,
                         "expected exactly one `unset` line in bash-log.zsh")
        cleaned = set(unset_lines[0].split()[1:])

        introduced = set()
        for ln in lines:
            if ln.startswith("unset "):
                continue
            introduced.update(re.findall(r'(?:^|;\s*)(_cpb_log_\w+)=', ln))
            introduced.update(re.findall(r'^for (_cpb_log_\w+) in ', ln))
            introduced.update(re.findall(r'^\{?\s*read -r (_cpb_log_\w+)', ln))

        self.assertTrue(introduced, "no _cpb_log_* variables found — the "
                                    "derivation itself broke")
        self.assertEqual(
            introduced - cleaned, set(),
            "bash-log.zsh leaves variables behind in the user's environment")
        self.assertEqual(
            cleaned - introduced, set(),
            "bash-log.zsh unsets names it never assigns — stale unset list")

    def test_both_loggers_validate_the_marker_with_the_same_arms(self):
        for arm in (r'""\|"\."\|"\.\."', r'\[a-zA-Z0-9\]\*', r'\*\[!a-zA-Z0-9_\.-\]\*'):
            for label, src in self.code:
                with self.subTest(shell=label, arm=arm):
                    self.assertRegex(src, arm,
                                     f"{label} logger lost a marker validation arm")

    def test_both_loggers_reject_a_second_marker_line(self):
        for label, src in self.code:
            with self.subTest(shell=label):
                self.assertRegex(src, r'read -r _\S*u.*read -r _\S*extra',
                                 f"{label} logger lost the one-line marker contract")

if __name__ == "__main__":
    unittest.main()
