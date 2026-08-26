#!/usr/bin/env python3
"""A first-class BLOCKED state (upstream issue #08).

A task can reach a checkpoint whose next gate is the owner's decision, not the
agent's work. With no honest state for that, the agent's options were all lies:
check the gate anyway, `work done` (false), or misuse `freehand`. This adds a
`blocked` state so the pause is self-documenting.

Invariants (from the write-up):
  * the Stop hook exits 0 on a blocked task with unchecked gates, FIRST attempt
    (stop_hook_active:false — the retry valve would mask a broken implementation);
  * blocking checks/adds/reorders NO gate — gate lines byte-identical before/after;
  * `tasks list`/`status` show BLOCKED, counted separately from pending and done;
  * `_find_active_task` skips a blocked task;
  * `tasks work <N>` clears it and the Stop hook blocks again afterwards;
  * the reason cannot break the gate parsers — a reason containing `- [ ]`, a `## `
    heading, or backticks must not become a phantom gate/section (see #09).

Run: python3 tests/test_blocked_state.py
"""
import os
import subprocess
from tests._bashcheck import bash_or_skip
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN = _HERE.parent / "plugins/playbook"
SCRIPTS = PLUGIN / "scripts"
sys.path.insert(0, str(PLUGIN))
from tasks.core import (  # noqa: E402
    _extract_status, _find_active_task, _gate_counts, _is_blocked,
    resume_blocked_task, set_task_blocked,
)

SID = "pid-blocked-test"

TASK = """# {n} - Decide

## Status
pending

## Work Plan
- [x] G1: measure current p99
- [ ] G2: if p99 > 200ms rewrite the index; if under, cancel
"""


class SetBlockedPure(unittest.TestCase):
    def _task(self, body=TASK.format(n="012")):
        d = Path(tempfile.mkdtemp())
        tf = d / "task.md"
        tf.write_text(body, encoding="utf-8")
        return tf

    def test_sets_status_and_records_reason(self):
        tf = self._task()
        set_task_blocked(tf, "waiting on owner: rewrite or cancel?")
        self.assertTrue(_is_blocked(tf))
        text = tf.read_text(encoding="utf-8")
        self.assertIn("## Blocked", text)
        self.assertIn("waiting on owner", text)

    def test_blocking_does_not_touch_gates(self):
        tf = self._task()
        before = _gate_counts(tf.read_text(encoding="utf-8"))
        gate_lines_before = [l for l in tf.read_text().splitlines() if l.lstrip().startswith("- [")]
        set_task_blocked(tf, "pause")
        after = _gate_counts(tf.read_text(encoding="utf-8"))
        gate_lines_after = [l for l in tf.read_text().splitlines() if l.lstrip().startswith("- [")]
        self.assertEqual(before, after)
        self.assertEqual(gate_lines_before, gate_lines_after)

    def test_hostile_reason_cannot_forge_a_gate_or_heading(self):
        tf = self._task()
        before = _gate_counts(tf.read_text(encoding="utf-8"))
        set_task_blocked(tf, "- [ ] fake gate\n## Fake Heading\n`weird`")
        after = _gate_counts(tf.read_text(encoding="utf-8"))
        self.assertEqual(before, after, "a reason must never mint a phantom gate (#09)")
        # The reason still round-trips as readable text.
        self.assertIn("fake gate", tf.read_text(encoding="utf-8"))

    def test_reblock_is_idempotent(self):
        tf = self._task()
        set_task_blocked(tf, "first reason")
        set_task_blocked(tf, "second reason")
        text = tf.read_text(encoding="utf-8")
        self.assertEqual(text.count("## Blocked"), 1)
        self.assertIn("second reason", text)
        self.assertNotIn("first reason", text)

    def test_resume_flips_status_and_stamps(self):
        tf = self._task()
        set_task_blocked(tf, "pause")
        resume_blocked_task(tf)
        self.assertEqual(_extract_status(tf), "in_progress")
        self.assertIn("Resumed", tf.read_text(encoding="utf-8"))

    def test_find_active_task_skips_blocked(self):
        proj = Path(tempfile.mkdtemp())
        td = proj / ".agent" / "tasks" / "012-decide"
        td.mkdir(parents=True)
        tf = td / "task.md"
        tf.write_text(TASK.format(n="012"), encoding="utf-8")
        self.assertIsNotNone(_find_active_task(proj))   # active while pending
        set_task_blocked(tf, "pause")
        self.assertIsNone(_find_active_task(proj), "a blocked task must not be active")


class SetBlockedFenceAware(unittest.TestCase):
    """P1 (parked by the 1.5.39 panel): the block-state WRITERS must locate the
    `## Blocked` section fence-aware, exactly as the handoff writer/readers already
    do (core._iter_nonfenced). A task.md that quotes a fenced `## Blocked` example
    (documentation of the ritual) must not have that example — or the real record —
    deleted or mis-spliced. write_handoff was made fence-safe in Session C; this
    covers the writers it CALLS (set_task_blocked / resume_blocked_task)."""

    def _core(self):
        import tasks.core as core
        return core

    def _task(self, body):
        d = Path(tempfile.mkdtemp())
        tf = d / "task.md"
        tf.write_text(body, encoding="utf-8")
        return tf

    # A fenced `## Blocked` example sits BEFORE the real content it must not eat.
    DECOY = (
        "# T\n\n## Status\npending\n\n"
        "## Docs\nFor reference, the blocked format looks like:\n"
        "```\n## Blocked\n> example reason  (since 2000-01-01T00:00)\n```\n\n"
        "## Work Plan\n- [x] G1: done\n- [ ] G2: real gate\n"
    )

    def test_set_blocked_ignores_fenced_decoy(self):
        core = self._core()
        tf = self._task(self.DECOY)
        core.set_task_blocked(tf, "REALPAUSE waiting on owner")
        text = tf.read_text(encoding="utf-8")
        # The fenced example is untouched: both fences survive (balanced), and its
        # body is byte-intact — the writer never reached into the fence.
        self.assertEqual(text.count("```"), 2,
                         "fence-blind delete stranded an unclosed fence")
        self.assertIn("> example reason  (since 2000-01-01T00:00)", text,
                      "the fenced example body was deleted")
        # The real block reason is live and readable by the fence-aware reader —
        # on the buggy writer the fresh section lands inside the broken fence and
        # the reader (correctly) sees nothing live.
        self.assertEqual(core._extract_block_reason(tf), "REALPAUSE waiting on owner")
        # The real Work Plan H2 and its gate survive.
        self.assertIn("## Work Plan", text)
        self.assertIn("- [ ] G2: real gate", text)

    def test_resume_ignores_fenced_decoy(self):
        core = self._core()
        tf = self._task(
            "# T\n\n## Status\nblocked\n\n"
            "## Docs\n```\n## Blocked\n> example\n```\n\n"
            "## Blocked\n> real reason  (since 2000-01-01T00:00)\n")
        core.resume_blocked_task(tf)
        text = tf.read_text(encoding="utf-8")
        # Exactly ONE resume stamp — on the LIVE blocked section, never the fenced
        # example (the fence-blind writer stamps both).
        self.assertEqual(text.count("> Resumed"), 1,
                         "resume stamped the fenced example too")
        self.assertEqual(core._extract_status(tf), "in_progress")
        # The fenced example is byte-intact.
        self.assertIn("```\n## Blocked\n> example\n```", text)

    def test_set_blocked_ignores_trailing_content_fence_closer(self):
        # Panel (codex-sol/#1, codex-terra/#1): `_iter_nonfenced` must apply the
        # CommonMark closer rule — a ```lang line inside a fence is CONTENT, not a
        # closer (only a whitespace-only run closes). Otherwise the fence "closes"
        # early and the `## Blocked` after it reads as live → mis-splice. This
        # aligns the block path with the stricter `_closed_fence_line_indices`.
        core = self._core()
        tf = self._task(
            "# T\n\n## Status\npending\n\n"
            "## Docs\n```\nexample code\n```text\n## Blocked\n> decoy reason\n```\n\n"
            "## Work Plan\n- [ ] G1: real gate\n")
        core.set_task_blocked(tf, "REALPAUSE")
        text = tf.read_text(encoding="utf-8")
        # The whole fenced example is preserved and balanced (2 opener/closer runs).
        self.assertEqual(text.count("```"), 3)
        self.assertIn("> decoy reason", text)
        self.assertIn("- [ ] G1: real gate", text)
        self.assertEqual(core._extract_block_reason(tf), "REALPAUSE")

    def test_set_blocked_ignores_indented_fence_markers(self):
        # Panel (opus/sonnet/codex, converging): a >=4-space-indented ``` is an
        # indented code block, not a fence closer (CommonMark ^ {0,3}). A real
        # fence must not be "closed" by an indented marker, exposing an interior
        # `## Blocked` as live — `_iter_nonfenced` must share the ≤3-space rule
        # with `_closed_fence_line_indices`.
        core = self._core()
        tf = self._task(
            "# T\n\n## Status\npending\n\n"
            "## Docs\n```\nexample\n    ```\n## Blocked\n> decoy\n```\n\n"
            "## Work Plan\n- [ ] G1: real gate\n")
        core.set_task_blocked(tf, "REALPAUSE")
        text = tf.read_text(encoding="utf-8")
        self.assertIn("- [ ] G1: real gate", text)
        self.assertIn("> decoy", text)
        self.assertEqual(core._extract_block_reason(tf), "REALPAUSE")

    def test_resume_stamp_byte_identical_mid_file(self):
        # Panel (opus finding 2): the resume stamp must land byte-identically to
        # the pre-fix code when `## Blocked` is NOT the last section (the changed
        # `lines[:span[1]]` insertion path). Old behavior inserted the stamp right
        # before the next live H2, after the body incl. its trailing blank line.
        import re
        core = self._core()
        tf = self._task(
            "# T\n\n## Status\nblocked\n\n"
            "## Blocked\n> reason  (since 2000-01-01T00:00)\n\n"
            "## Notes\n- keep me\n")
        core.resume_blocked_task(tf)
        norm = re.sub(r"20\d\d-\d\d-\d\dT[0-9:+\-]+", "TS",
                      tf.read_text(encoding="utf-8"))
        self.assertEqual(
            norm,
            "# T\n\n## Status\nin_progress\n\n"
            "## Blocked\n> reason  (since TS)\n\n> Resumed TS\n"
            "## Notes\n- keep me\n")

    def test_normal_block_and_resume_byte_identical(self):
        # Negative control: with NO fenced heading, output must be byte-identical
        # to the pre-fix shape (captured from current behavior; only the ISO
        # timestamps vary). This is what proves the fence-aware rewrite did not
        # perturb the ordinary block/resume path.
        import re
        core = self._core()
        tf = self._task(
            "# T\n\n## Status\npending\n\n## Work Plan\n- [ ] G1\n")

        def norm(s):
            return re.sub(r"20\d\d-\d\d-\d\dT[0-9:+\-]+", "TS", s)

        core.set_task_blocked(tf, "pause here")
        self.assertEqual(
            norm(tf.read_text(encoding="utf-8")),
            "# T\n\n## Status\nblocked\n\n## Work Plan\n- [ ] G1\n\n"
            "## Blocked\n> pause here  (since TS)\n\n")
        core.resume_blocked_task(tf)
        self.assertEqual(
            norm(tf.read_text(encoding="utf-8")),
            "# T\n\n## Status\nin_progress\n\n## Work Plan\n- [ ] G1\n\n"
            "## Blocked\n> pause here  (since TS)\n\n> Resumed TS\n")


class BlockedEndToEnd(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name) / "proj"
        (self.project / ".agent" / "tasks").mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)
        td = self.project / ".agent" / "tasks" / "012-decide"
        td.mkdir(parents=True)
        (td / "task.md").write_text(TASK.format(n="012"), encoding="utf-8")
        self.task_file = td / "task.md"

    def _env(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(PLUGIN)
        env["PLAYBOOK_SESSION_ID"] = SID
        return env

    def run_tasks(self, *args):
        return subprocess.run([sys.executable, "-m", "tasks.cli", *args],
                              cwd=self.project, env=self._env(), capture_output=True, text=True)

    def run_stop_hook(self, stop_active=False):
        payload = '{"stop_hook_active": %s}' % ("true" if stop_active else "false")
        return subprocess.run([bash_or_skip(), str(SCRIPTS / "stop-hook")],
                              input=payload, cwd=self.project, env=self._env(),
                              capture_output=True, text=True)

    def _set_counters(self):
        # High activity so the conversational bypass (writes==0 & tools<5) does NOT
        # fire — otherwise a green exit would not prove the BLOCKED path ran.
        sd = self.project / ".agent" / "sessions" / SID
        sd.mkdir(parents=True, exist_ok=True)
        (sd / "counters").write_text("writes=9\ntools=40\n", encoding="utf-8")

    def test_full_lifecycle(self):
        # activate
        self.assertEqual(self.run_tasks("work", "012").returncode, 0)
        self._set_counters()

        # unchecked gate + real activity → hook blocks
        self.assertEqual(self.run_stop_hook().returncode, 2,
                         "open gates with activity must block")

        # block it
        b = self.run_tasks("blocked", "p99 measured; rewrite or cancel is your call")
        self.assertEqual(b.returncode, 0, b.stderr)
        self.assertEqual(_extract_status(self.task_file), "blocked")

        # THE invariant: hook allows the turn to end, on the FIRST attempt
        self._set_counters()
        self.assertEqual(self.run_stop_hook(stop_active=False).returncode, 0,
                         "a blocked task must let the turn end without a fake checkbox")

        # list + status show BLOCKED, counted separately
        lst = self.run_tasks("list")
        self.assertIn("blocked", lst.stdout)
        self.assertIn("1 blocked", lst.stdout)
        st = self.run_tasks("status")
        self.assertIn("BLOCKED", st.stdout)

        # resume clears it, and the hook blocks again
        self.assertEqual(self.run_tasks("work", "012").returncode, 0)
        self.assertEqual(_extract_status(self.task_file), "in_progress")
        self._set_counters()
        self.assertEqual(self.run_stop_hook().returncode, 2,
                         "after resume, open gates block again")

    def test_blocked_requires_a_reason(self):
        self.assertEqual(self.run_tasks("work", "012").returncode, 0)
        r = self.run_tasks("blocked")
        self.assertEqual(r.returncode, 1)
        self.assertIn("reason", r.stderr.lower())

    def test_duplicate_status_headings_hook_agrees_with_python(self):
        """Parity: core._extract_status reads the line after the LAST `## Status`;
        the stop-hook's awk must apply the same rule, or a stray duplicate heading
        makes Python and the enforcing hook disagree about the same file (the #09
        disease in a new spot)."""
        self.assertEqual(self.run_tasks("work", "012").returncode, 0)

        # Decoy heading FIRST says pending, real LAST says blocked →
        # Python reads blocked, so the hook must allow the turn to end (exit 0).
        self.task_file.write_text(
            "# 012 - Decide\n\n## Status\npending\n\n## Work Plan\n"
            "- [ ] open gate\n\n## Status\nblocked\n\n## Blocked\n> waiting\n",
            encoding="utf-8")
        self.assertEqual(_extract_status(self.task_file), "blocked")
        self._set_counters()
        self.assertEqual(self.run_stop_hook().returncode, 0,
                         "hook must honor the LAST ## Status, as Python does")

        # Reverse: decoy FIRST says blocked, real LAST says pending →
        # Python reads pending, so the hook must still block on the open gate.
        self.task_file.write_text(
            "# 012 - Decide\n\n## Status\nblocked\n\n## Work Plan\n"
            "- [ ] open gate\n\n## Status\npending\n",
            encoding="utf-8")
        self.assertEqual(_extract_status(self.task_file), "pending")
        self._set_counters()
        self.assertEqual(self.run_stop_hook().returncode, 2,
                         "a decoy 'blocked' heading must not release the gate")


if __name__ == "__main__":
    unittest.main()
