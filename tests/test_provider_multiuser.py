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
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN = REPO_ROOT / "plugins" / "playbook"
SCRIPTS = PLUGIN / "scripts"

sys.path.insert(0, str(PLUGIN))

from provider.paths import (  # noqa: E402
    InvalidUserMarkerError,
    find_project_root,
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

    def test_absent_marker_is_root_for_all_three(self):
        p = make_project(self.tmp / "legacy", "legacy")
        expected = str(p / ".agent")
        self.assertEqual(str(resolve_agent_dir(p)), expected)
        self.assertEqual(self._python_core_resolve(p), (0, expected))
        self.assertEqual(self._bash_resolve(p), (0, expected))


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
            [str(SCRIPTS / "tasks"), "work", "done", "--force"],
            cwd=str(project), env=cli_env, capture_output=True, text=True,
        )
        self.assertEqual(done.returncode, 0, done.stderr or done.stdout)
        denied = subprocess.run(
            [sys.executable, str(SCRIPTS / "codex-apply-patch-hook")],
            input=json.dumps(payload), capture_output=True, text=True,
            cwd=str(project), env=hook_env,
        )
        self.assertEqual(denied.returncode, 2, "hook allowed an edit with no active task")


if __name__ == "__main__":
    unittest.main()
