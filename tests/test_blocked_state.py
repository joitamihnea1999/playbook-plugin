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

    def test_set_blocked_fails_closed_on_unclosed_fence(self):
        # Panel round-3 (codex-sol/codex-terra): a destructive section writer must
        # fail CLOSED on an UNCLOSED fence — treat the remainder as fenced and
        # never DELETE a `## Blocked` decoy after an unclosed opener. (A fail-open
        # scanner deletes it — the regression this guards against.) On this
        # malformed input the fresh block appended at EOF lands inside the trailing
        # unclosed fence, so the safety property is non-deletion (+ the reason is
        # still written), not readability — a documented malformed-input corner.
        core = self._core()
        before = self._task(
            "# T\n\n## Status\npending\n\n"
            "## Docs\n```\n## Blocked\n> decoy\n> keep this quoted line\n")
        core.set_task_blocked(before, "REALPAUSE")
        text = before.read_text(encoding="utf-8")
        self.assertIn("> decoy", text, "unclosed-fence decoy must not be deleted")
        self.assertIn("> keep this quoted line", text)
        self.assertIn("REALPAUSE", text, "the block reason is still recorded")
        self.assertEqual(core._extract_status(before), "blocked")

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


class StatusFenceAware(unittest.TestCase):
    """V7 / P-F (task 043, split from 039): `## Status` is the ENFORCEMENT status —
    the stop-hook releases the turn when it reads `blocked`, `_is_done` drives
    reopen/list. Both the reader (`_extract_status`) and the single writer
    (`_set_status`) must select the LAST *live* `## Status` through the shared
    strict scanner: a `## Status` quoted inside a code fence (documentation of
    the ritual, or a decoy) is content, not the status. The reader is fail
    CLOSED: an UNCLOSED trailing fence must not turn its interior into a live
    `blocked` (that is exactly the release bypass), so the remainder stays fenced.
    """

    def _core(self):
        import tasks.core as core
        return core

    def _task(self, body):
        d = Path(tempfile.mkdtemp())
        tf = d / "task.md"
        tf.write_text(body, encoding="utf-8")
        return tf

    LIVE_PENDING_FENCED_BLOCKED = (
        "# T\n\n## Status\npending\n\n"
        "## Docs\nThe blocked ritual looks like:\n"
        "```\n## Status\nblocked\n```\n\n"
        "## Work Plan\n- [ ] G1: real gate\n"
    )

    def test_reader_ignores_closed_fenced_status_decoy(self):
        core = self._core()
        tf = self._task(self.LIVE_PENDING_FENCED_BLOCKED)
        self.assertEqual(core._extract_status(tf), "pending",
                         "a fenced `## Status` example must not be read as the status")
        self.assertFalse(core._is_blocked(tf))

    def test_reader_ignores_tilde_fenced_status_decoy(self):
        core = self._core()
        tf = self._task(self.LIVE_PENDING_FENCED_BLOCKED.replace("```", "~~~"))
        self.assertEqual(core._extract_status(tf), "pending")

    def test_reader_fails_closed_on_unclosed_trailing_fence(self):
        # The bypass shape: append an opener that is never closed, then a
        # `## Status`/blocked pair. A fail-OPEN reader would treat it as live and
        # the stop-hook would release the open gate.
        core = self._core()
        tf = self._task("# T\n\n## Status\npending\n\n## Work Plan\n- [ ] G1\n\n"
                        "## Notes\n```\n## Status\nblocked\n")
        self.assertEqual(core._extract_status(tf), "pending")
        self.assertFalse(core._is_blocked(tf))

    def test_reader_nbsp_closer_does_not_close_the_fence(self):
        # V6 shape: a ```+NBSP line is NOT a closer, so the decoy stays fenced.
        core = self._core()
        tf = self._task("# T\n\n## Status\npending\n\n## Docs\n```\n## Status\n"
                        "blocked\n```\u00a0\n\n## Work Plan\n- [ ] G1\n")
        self.assertEqual(core._extract_status(tf), "pending")

    def test_reader_accepts_atx_variants_and_rejects_indented(self):
        core = self._core()
        for heading in ("## Status", "##\tStatus", "## Status ##", "  ## Status"):
            tf = self._task(f"# T\n\n{heading}\nblocked\n\n## Work Plan\n- [ ] G1\n")
            self.assertEqual(core._extract_status(tf), "blocked", heading)
        # >=4-column indent is an indented code block / not an ATX heading.
        tf = self._task("# T\n\n## Status\npending\n\n    ## Status\n    blocked\n")
        self.assertEqual(core._extract_status(tf), "pending")

    def test_reader_last_live_status_still_wins(self):
        # Control: the LAST-wins rule (parity with the stop-hook) is unchanged
        # among LIVE headings.
        core = self._core()
        tf = self._task("# T\n\n## Status\npending\n\n## Work Plan\n- [ ] G1\n\n"
                        "## Status\nblocked\n")
        self.assertEqual(core._extract_status(tf), "blocked")

    def test_writer_targets_live_status_not_fenced_decoy(self):
        core = self._core()
        tf = self._task(self.LIVE_PENDING_FENCED_BLOCKED)
        self.assertTrue(core._set_status(tf, "in_progress"))
        text = tf.read_text(encoding="utf-8")
        self.assertIn("## Status\nin_progress\n", text, "the LIVE status must be rewritten")
        self.assertIn("```\n## Status\nblocked\n```", text,
                      "the fenced example must stay byte-intact")
        self.assertEqual(core._extract_status(tf), "in_progress")

    def test_writer_returns_false_when_no_live_status(self):
        core = self._core()
        body = "# T\n\n```\n## Status\npending\n```\n\n## Work Plan\n- [ ] G1\n"
        tf = self._task(body)
        self.assertFalse(core._set_status(tf, "done"))
        self.assertEqual(tf.read_text(encoding="utf-8"), body, "nothing may be written")

    def test_writer_round_trip_byte_identical_on_a_normal_file(self):
        # Negative control: the ordinary task.md shape is unchanged by the routing.
        core = self._core()
        tf = self._task(TASK.format(n="012"))
        core._set_status(tf, "blocked")
        self.assertEqual(tf.read_text(encoding="utf-8"),
                         TASK.format(n="012").replace("## Status\npending", "## Status\nblocked"))

    def test_set_task_blocked_refuses_when_status_cannot_land(self):
        # The `set_task_blocked` non-fenced check (opus #2): if the status cannot be
        # written live, status and reason would diverge (a `## Blocked` section with
        # no `blocked` status) — refuse and write NOTHING.
        core = self._core()
        body = "# T\n\n```\n## Status\npending\n```\n\n## Work Plan\n- [ ] G1\n"
        tf = self._task(body)
        with self.assertRaises(ValueError):
            core.set_task_blocked(tf, "REALPAUSE")
        self.assertEqual(tf.read_text(encoding="utf-8"), body,
                         "a refused block must leave task.md byte-identical")

    def test_set_task_blocked_verifies_status_and_reason_agree(self):
        core = self._core()
        tf = self._task(self.LIVE_PENDING_FENCED_BLOCKED)
        core.set_task_blocked(tf, "REALPAUSE")
        self.assertEqual(core._extract_status(tf), "blocked")
        self.assertEqual(core._extract_block_reason(tf), "REALPAUSE")
        self.assertIn("```\n## Status\nblocked\n```", tf.read_text(encoding="utf-8"))

    def test_status_value_line_must_be_a_value_not_a_heading(self):
        # Panel round 1 (codex-sol/codex-terra, convergent): `## Status` directly
        # followed by another live H2 has NO value; reading `## Risk` as the status
        # is nonsense and WRITING over it would destroy the Risk section.
        core = self._core()
        body = "# T\n\n## Status\n## Risk\nassertive\n\n## Work Plan\n- [ ] G1\n"
        tf = self._task(body)
        self.assertEqual(core._extract_status(tf), "unknown")
        self.assertFalse(core._set_status(tf, "blocked"))
        with self.assertRaises(ValueError):
            core.set_task_blocked(tf, "REALPAUSE")
        self.assertEqual(tf.read_text(encoding="utf-8"), body,
                         "a heading must never be overwritten as a status value")

    def test_status_value_line_must_not_be_a_fence_opener(self):
        core = self._core()
        body = "# T\n\n## Status\n```\npending\n```\n\n## Work Plan\n- [ ] G1\n"
        tf = self._task(body)
        self.assertEqual(core._extract_status(tf), "unknown")
        self.assertFalse(core._set_status(tf, "done"))
        self.assertEqual(tf.read_text(encoding="utf-8"), body)

    def test_last_live_heading_wins_even_when_malformed_no_fallback(self):
        # Round-2 panel (opus/codex convergent): a value-less LAST `## Status` must
        # not fall back to an EARLIER valid pair — that re-opened the release
        # bypass (earlier `blocked`, trailing malformed pair → read blocked).
        core = self._core()
        body = ("# T\n\n## Status\nblocked\n\n## Work Plan\n- [ ] G1\n\n"
                "## Status\n## Risk\nassertive\n")
        tf = self._task(body)
        self.assertEqual(core._extract_status(tf), "unknown")
        self.assertFalse(core._is_blocked(tf))
        self.assertFalse(core._set_status(tf, "done"))
        self.assertEqual(tf.read_text(encoding="utf-8"), body)

    def test_value_line_must_be_status_shaped(self):
        # Round-2 panel (codex-sol Critical): `## Status\n- [ ] GATE` let a block
        # overwrite the gate with `blocked` — a structural line is never a value.
        core = self._core()
        for value in ("- [ ] CRITICAL GATE", "- [x] done gate", "> quoted", "",
                      "<!-- pin -->", "### entry", "* bullet"):
            body = f"# T\n\n## Status\n{value}\n\n## Work Plan\n- [ ] G1\n"
            tf = self._task(body)
            self.assertEqual(core._extract_status(tf), "unknown", repr(value))
            self.assertFalse(core._set_status(tf, "blocked"), repr(value))
            with self.assertRaises(ValueError):
                core.set_task_blocked(tf, "REALPAUSE")
            self.assertEqual(tf.read_text(encoding="utf-8"), body, repr(value))
        # Every real status shape stays readable.
        for value in ("pending", "in_progress", "blocked", "done", "done (2026-01-01)",
                      "in progress", "Done"):
            tf = self._task(f"# T\n\n## Status\n{value}\n\n## Work Plan\n- [ ] G1\n")
            self.assertEqual(core._extract_status(tf), value, repr(value))

    def test_resume_refuses_when_status_cannot_land(self):
        # Round-2 panel (sonnet): resume must not stamp `Resumed` when the status
        # flip did not land — same land-together rule as block.
        core = self._core()
        body = ("# T\n\n```\n## Status\nblocked\n```\n\n## Work Plan\n- [ ] G1\n\n"
                "## Blocked\n> waiting  (since 2000-01-01T00:00)\n")
        tf = self._task(body)
        with self.assertRaises(ValueError):
            core.resume_blocked_task(tf)
        self.assertEqual(tf.read_text(encoding="utf-8"), body)

    def test_indented_value_is_code_not_a_status(self):
        # Round-3 panel (codex convergent): `## Status\n    blocked` / `\tblocked`
        # is Markdown code, not a value — reader unknown, writer refuses.
        core = self._core()
        for value in ("    blocked", "\tblocked"):
            body = f"# T\n\n## Status\n{value}\n\n## Work Plan\n- [ ] G1\n"
            tf = self._task(body)
            self.assertEqual(core._extract_status(tf), "unknown", repr(value))
            self.assertFalse(core._is_blocked(tf), repr(value))
            self.assertFalse(core._set_status(tf, "done"), repr(value))
            self.assertEqual(tf.read_text(encoding="utf-8"), body, repr(value))

    def test_blocked_is_an_exact_token_not_a_prefix(self):
        core = self._core()
        for value in ("blockedness", "blocked and done", "Blocked"):
            tf = self._task(f"# T\n\n## Status\n{value}\n\n## Work Plan\n- [ ] G1\n")
            self.assertEqual(core._extract_status(tf), value)
            self.assertFalse(core._is_blocked(tf), repr(value))
        tf = self._task("# T\n\n## Status\nblocked\n\n## Work Plan\n- [ ] G1\n")
        self.assertTrue(core._is_blocked(tf))

    def test_blank_lines_between_heading_and_value_are_skipped(self):
        # Round-5 panel (opus): `## Status\n\npending` must read AND write the value
        # line (an older hand-formatted file must not become uncloseable).
        core = self._core()
        body = "# T\n\n## Status\n\npending\n\n## Work Plan\n- [ ] G1\n"
        tf = self._task(body)
        self.assertEqual(core._extract_status(tf), "pending")
        self.assertTrue(core._set_status(tf, "blocked"))
        # Round-6 panel (grok): the WRITER collapses the blanks so the value sits
        # directly under the heading — task-gate-hook's F3 awk reads the immediate
        # next line, and a blank there would authorize a stale done-pointer.
        self.assertEqual(tf.read_text(encoding="utf-8"),
                         body.replace("## Status\n\npending", "## Status\nblocked"))
        # Blank then another section: still no value.
        tf2 = self._task("# T\n\n## Status\n\n## Risk\nassertive\n")
        self.assertEqual(core._extract_status(tf2), "unknown")

    def test_task_status_script_emits_lf_only_bytes(self):
        # Round-5 panel (codex): on Windows a text-mode print() emits CRLF and bash
        # keeps the CR, breaking the hook's exact match. The script must write
        # `\n`-only bytes on every platform.
        tf = self._task("# T\n\n## Status\nblocked\n\n## Work Plan\n- [ ] G1\n")
        r = subprocess.run([sys.executable, str(SCRIPTS / "task-status.py"), str(tf)],
                           capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, b"blocked\n")

    @staticmethod
    def _f3_awk_program():
        """task-gate-hook's F3 `## Status` awk, EXTRACTED from the live hook source
        (round-7 panel: a hard-coded copy would keep passing against a fossil)."""
        import re
        src = (SCRIPTS / "task-gate-hook").read_text(encoding="utf-8")
        m = re.search(r"STATUS=\$\(awk '(.*?)'\s*\"\$RESOLVED\"", src, re.S)
        assert m, "could not locate the F3 status awk in scripts/task-gate-hook"
        return m.group(1)

    def _f3_reads(self, tf):
        import shutil
        awk = shutil.which("awk")
        if awk is None:
            self.skipTest("awk not available")
        r = subprocess.run([awk, self._f3_awk_program(), str(tf)], capture_output=True, text=True)
        return "".join(r.stdout.split())   # the hook pipes through tr -d '[:space:]'

    def test_f3_awk_is_extracted_from_the_hook_not_copied(self):
        prog = self._f3_awk_program()
        self.assertIn("## Status", prog)
        self.assertIn("flag", prog)
        # A control read through the extracted program on the canonical layout.
        tf = self._task(TASK.format(n="012"))
        self.assertEqual(self._f3_reads(tf), "pending")

    def test_written_status_layout_agrees_with_the_f3_awk(self):
        # Every file playbook WRITES must read the same in Python and in the
        # fence-blind F3 awk: blank lines after the heading are collapsed and the
        # heading itself is canonicalized to `## Status` on write (round-6/7 panels:
        # `## Status\n\ndone`, `## Status ##\ndone`, `##\tStatus\ndone` all read `""`
        # in F3 → a stale done-pointer would authorize edits).
        core = self._core()
        bodies = [
            "# T\n\n## Status\n\npending\n\n## Work Plan\n- [ ] G1\n",
            "# T\n\n## Status\n\n\n\npending\n\n## Work Plan\n- [ ] G1\n",
            "# T\n\n## Status ##\npending\n\n## Work Plan\n- [ ] G1\n",
            "# T\n\n##\tStatus\npending\n\n## Work Plan\n- [ ] G1\n",
            "# T\n\n  ## Status\n  pending\n\n## Work Plan\n- [ ] G1\n",
            TASK.format(n="012"),
        ]
        for body in bodies:
            tf = self._task(body)
            self.assertTrue(core._set_status(tf, "done"), repr(body))
            self.assertEqual(core._extract_status(tf), "done", repr(body))
            self.assertEqual(self._f3_reads(tf), "done", repr(body))
            self.assertIn("\n## Status\ndone\n", tf.read_text(encoding="utf-8"), repr(body))
        # set_task_blocked DIRECTLY on the raw bodies (the `tasks blocked` path never
        # calls _set_status first — sonnet, round 7).
        for body in bodies:
            tf = self._task(body)
            core.set_task_blocked(tf, "pause")
            self.assertEqual(core._extract_status(tf), "blocked", repr(body))
            self.assertEqual(self._f3_reads(tf), "blocked", repr(body))
            self.assertIn("\n## Status\nblocked\n", tf.read_text(encoding="utf-8"), repr(body))

    def test_value_with_one_to_three_leading_spaces_is_text(self):
        # Round-7 panel (codex): 1-3 leading spaces are ordinary Markdown text (the
        # pre-043 `.strip()` reader accepted them); only a tab / >=4 columns is code.
        core = self._core()
        for value in (" pending", "  pending", "   pending"):
            tf = self._task(f"# T\n\n## Status\n{value}\n\n## Work Plan\n- [ ] G1\n")
            self.assertEqual(core._extract_status(tf), "pending", repr(value))
            self.assertTrue(core._set_status(tf, "done"), repr(value))
            self.assertIn("## Status\ndone\n", tf.read_text(encoding="utf-8"))   # canonical

    def test_lifecycle_reopen_targets_live_status(self):
        # lifecycle's reopen path (`tasks work <N>` on a done task) must use the
        # same fence-aware writer, not a hand-rolled fence-blind loop.
        import re
        src = (PLUGIN / "tasks" / "lifecycle.py").read_text(encoding="utf-8")
        self.assertNotRegex(src, re.compile(r'line\.strip\(\) == "## Status"'),
                            "lifecycle must route status writes through core._set_status")

    def test_retro_status_reader_is_the_shared_one(self):
        import tasks.retro as retro
        lines = self.LIVE_PENDING_FENCED_BLOCKED.splitlines()
        self.assertEqual(retro._extract_status(lines), "pending")


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


    def test_fenced_status_decoy_does_not_release_the_stop_gate(self):
        """V7 / P-F (task 043): the stop-hook reads the SAME fence-aware status as
        Python (scripts/task-status.py → core._extract_status). A `## Status` /
        `blocked` pair quoted in a code fence after the live `pending` — a
        closed fence, a `~~~` fence, or an UNCLOSED trailing fence — must NOT let
        the turn end with an open gate. The fence-blind awk released it."""
        self.assertEqual(self.run_tasks("work", "012").returncode, 0)
        shapes = {
            "closed": ("# 012 - Decide\n\n## Status\npending\n\n## Work Plan\n"
                       "- [ ] open gate\n\n## Docs\n```\n## Status\nblocked\n```\n"),
            "tilde": ("# 012 - Decide\n\n## Status\npending\n\n## Work Plan\n"
                      "- [ ] open gate\n\n## Docs\n~~~\n## Status\nblocked\n~~~\n"),
            "unclosed": ("# 012 - Decide\n\n## Status\npending\n\n## Work Plan\n"
                         "- [ ] open gate\n\n## Docs\n```\n## Status\nblocked\n"),
        }
        for name, body in shapes.items():
            self.task_file.write_text(body, encoding="utf-8")
            self.assertEqual(_extract_status(self.task_file), "pending", name)
            self._set_counters()
            r = self.run_stop_hook()
            self.assertEqual(r.returncode, 2,
                             f"{name}: a fenced `## Status`/blocked decoy released the gate: {r.stderr}")
        # Control: the same file with a REAL trailing blocked status is released,
        # and the hook agrees with Python (the LAST live heading wins).
        self.task_file.write_text(
            "# 012 - Decide\n\n## Status\npending\n\n## Work Plan\n- [ ] open gate\n\n"
            "## Docs\n```\n## Status\npending\n```\n\n## Status\nblocked\n\n## Blocked\n> waiting\n",
            encoding="utf-8")
        self.assertEqual(_extract_status(self.task_file), "blocked")
        self._set_counters()
        self.assertEqual(self.run_stop_hook().returncode, 0,
                         "a real live blocked status after a fenced example must still release")

    def test_status_read_fails_closed_without_python(self):
        """When the python status reader cannot run, the hook must not guess: the
        status is unreadable, so the gate count runs (fail CLOSED, loud) — the
        same policy task-gate-hook applies to a missing python3."""
        self.assertEqual(self.run_tasks("work", "012").returncode, 0)
        self.task_file.write_text(
            "# 012 - Decide\n\n## Status\nblocked\n\n## Work Plan\n- [ ] open gate\n",
            encoding="utf-8")
        self._set_counters()
        shim = Path(tempfile.mkdtemp())
        (shim / "python3").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        os.chmod(shim / "python3", 0o755)
        env = self._env()
        env["PATH"] = str(shim) + os.pathsep + env.get("PATH", "")
        payload = '{"stop_hook_active": false}'
        r = subprocess.run([bash_or_skip(), str(SCRIPTS / "stop-hook")], input=payload,
                           cwd=self.project, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 2,
                         f"unreadable status must fail CLOSED (gate count runs): {r.stderr}")
        self.assertIn("status", r.stderr.lower(), "the fallback must be loud")


    FENCED_ONLY_STATUS = ("# 012 - Decide\n\n```\n## Status\npending\n```\n\n"
                          "## Work Plan\n- [ ] open gate\n")

    def test_blocked_command_refuses_cleanly_when_status_cannot_land(self):
        # Panel round 1 (grok/codex convergent): the ValueError must surface as a
        # refusal (stderr + exit 1), never a traceback, and the file stays intact.
        self.assertEqual(self.run_tasks("work", "012").returncode, 0)
        self.task_file.write_text(self.FENCED_ONLY_STATUS, encoding="utf-8")
        r = self.run_tasks("blocked", "waiting on owner")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("## Status", r.stderr)
        self.assertEqual(self.task_file.read_text(encoding="utf-8"), self.FENCED_ONLY_STATUS)

    def test_handoff_preflights_before_writing_anything(self):
        self.assertEqual(self.run_tasks("work", "012").returncode, 0)
        self.task_file.write_text(self.FENCED_ONLY_STATUS, encoding="utf-8")
        r = self.run_tasks("handoff")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(self.task_file.read_text(encoding="utf-8"), self.FENCED_ONLY_STATUS,
                         "a refused handoff must write NOTHING — no ## Handoff, no status")

    def test_work_done_refuses_instead_of_half_closing(self):
        # Panel round 1 (opus/codex convergent): a close whose `done` cannot land
        # must not write a receipt, clear the session and print "done".
        self.assertEqual(self.run_tasks("work", "012").returncode, 0)
        body = ("# 012 - Decide\n\n```\n## Status\npending\n```\n\n"
                "## Risk\nreversible\n\n## Work Plan\n- [x] gate\n")
        self.task_file.write_text(body, encoding="utf-8")
        r = self.run_tasks("work", "done")
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("Verification Receipt", self.task_file.read_text(encoding="utf-8"))
        self.assertEqual(self.task_file.read_text(encoding="utf-8"), body)
        pointer = self.project / ".agent" / "sessions" / SID / "current_state"
        self.assertTrue(pointer.exists(), "a refused close must keep the session pointer")

    def test_reopen_targets_live_status_not_fenced_example(self):
        # Panel round 1 (grok): prove the reopen path end-to-end, not by grep.
        body = ("# 012 - Decide\n\n## Status\ndone\n\n## Docs\n```\n## Status\npending\n```\n\n"
                "## Work Plan\n- [ ] open gate\n")
        self.task_file.write_text(body, encoding="utf-8")
        r = self.run_tasks("work", "012")
        self.assertEqual(r.returncode, 0, r.stderr)
        text = self.task_file.read_text(encoding="utf-8")
        self.assertIn("## Status\nin_progress\n", text)
        self.assertIn("```\n## Status\npending\n```", text, "the fenced example must stay intact")


    def test_malformed_trailing_status_does_not_fall_back_and_release(self):
        # Round-2 panel: earlier live `blocked` + trailing value-less `## Status`.
        # Reader → unknown; the hook must BLOCK the open gate (fail closed).
        self.assertEqual(self.run_tasks("work", "012").returncode, 0)
        self.task_file.write_text(
            "# 012 - Decide\n\n## Status\nblocked\n\n## Work Plan\n- [ ] open gate\n\n"
            "## Status\n## Risk\nreversible\n", encoding="utf-8")
        self.assertEqual(_extract_status(self.task_file), "unknown")
        self._set_counters()
        r = self.run_stop_hook()
        self.assertEqual(r.returncode, 2, f"fallback to an earlier blocked released the gate: {r.stderr}")


    def test_hook_blocked_match_is_exact(self):
        # Round-3 panel: `blockedness` must not release the gate in the hook either.
        self.assertEqual(self.run_tasks("work", "012").returncode, 0)
        self.task_file.write_text(
            "# 012 - Decide\n\n## Status\nblockedness\n\n## Work Plan\n- [ ] open gate\n",
            encoding="utf-8")
        self._set_counters()
        self.assertEqual(self.run_stop_hook().returncode, 2, "a blocked* prefix released the gate")


    def test_work_done_force_is_not_an_escape_from_the_status_preflight(self):
        # Round-4 panel (opus): --force overrides verify/review/freshness, never the
        # status preflight — a close whose `done` cannot land is refused even forced.
        self.assertEqual(self.run_tasks("work", "012").returncode, 0)
        body = ("# 012 - Decide\n\n```\n## Status\npending\n```\n\n"
                "## Risk\nreversible\n\n## Work Plan\n- [x] gate\n")
        self.task_file.write_text(body, encoding="utf-8")
        r = self.run_tasks("work", "done", "--force", "--reason", "testing the preflight")
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.task_file.read_text(encoding="utf-8"), body)


    def test_hook_tolerates_a_crlf_status_from_python(self):
        # Round-5 panel (codex): a python3 whose stdout is CRLF-translated (native
        # Windows) yields `blocked\r`; the hook must strip the CR, not miss the match.
        self.assertEqual(self.run_tasks("work", "012").returncode, 0)
        self.task_file.write_text(
            "# 012 - Decide\n\n## Status\nblocked\n\n## Work Plan\n- [ ] open gate\n",
            encoding="utf-8")
        self._set_counters()
        shim = Path(tempfile.mkdtemp())
        # The shim answers the STATUS read with CRLF and lets every other python3
        # call (the stop_hook_active JSON parse) fall through to the real one.
        real = sys.executable.replace("\\", "/")
        (shim / "python3").write_text(
            "#!/bin/sh\ncase \"$1\" in *task-status.py) printf 'blocked\\r\\n'; exit 0;; esac\n"
            f"exec \"{real}\" \"$@\"\n", encoding="utf-8")
        os.chmod(shim / "python3", 0o755)
        env = self._env()
        env["PATH"] = str(shim) + os.pathsep + env.get("PATH", "")
        r = subprocess.run([bash_or_skip(), str(SCRIPTS / "stop-hook")],
                           input='{"stop_hook_active": false}', cwd=self.project, env=env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, f"a CR after `blocked` must not defeat the release: {r.stderr}")


if __name__ == "__main__":
    unittest.main()
