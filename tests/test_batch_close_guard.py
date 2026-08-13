#!/usr/bin/env python3
"""Guard 0.5 v2 — annotated batch-close (F1), blind-judge-reviewed design.

The old guard hard-blocked 3+ newly-checked gates per write and warned at 2 —
6/6 field journals called out the tax (one logical step forced into split
writes, sharpest at review triage). The redesign keeps the anti-backfill
invariant and releases the tax:

  * delta ≤ 1: exactly the old behavior (silent) — singles stay free;
  * a batch (2–5 newly-checked gates) is allowed ONLY when every newly-checked
    line extends its unchecked original by ≥ 8 unicode non-whitespace chars
    (its own outcome note; a pointer note like "— see Round 2 Result" counts);
  * a bare or sub-floor batch blocks (v1 only WARNED at 2 — tightened);
  * 6+ blocks even fully annotated (ceiling: fabricating notes for the whole
    plan in one write is the checkbox-theater scenario);
  * a checked line with no unchecked counterpart (born-checked) blocks the
    batch — gates are added OPEN, then closed;
  * already-checked lines carried through the edit are IGNORED (not
    born-checked), and unchecking lines cannot launder the batch size — all
    tiers key on the newly-checked count, not the raw x-delta;
  * two batch-closing writes with NO tool call between them block the second
    (the two-writes-of-5 end-of-task pattern, killed mechanically — the
    judge's condition on the design's PASS).

Pairing (judge Finding 1): checked-in-new matching checked-in-old (multiset)
= carried; else prefix-paired to an unchecked-in-old original (longest wins,
empty originals excluded) = newly-checked; else born-checked.

Covers the pure helper (scripts/gate-batch-check.py) and the REAL hook path
(bash task-gate-hook subprocess).

Run: python3 -m unittest tests.test_batch_close_guard
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN = _HERE.parent / "plugins/playbook"
HOOK = PLUGIN / "scripts" / "task-gate-hook"
HELPER = PLUGIN / "scripts" / "gate-batch-check.py"

SESSION = "pid-batch-test"

G = ["- [ ] G1: run the suite",
     "- [ ] G2: update the mind map",
     "- [ ] G3: write the journal",
     "- [ ] G4: commit the work",
     "- [ ] G5: check the docs",
     "- [ ] G6: close the loop"]


def checked(line: str, note: str = "") -> str:
    return line.replace("- [ ]", "- [x]", 1) + note


class ProjectFixture:
    def __init__(self, gates: "list[str]" = G):
        self.proj = Path(tempfile.mkdtemp()) / "proj"
        task_dir = self.proj / ".agent" / "tasks" / "001-thing"
        task_dir.mkdir(parents=True)
        self.task_file = task_dir / "task.md"
        self.task_file.write_text(
            "# 001 - Thing\n\n## Status\npending\n\n## Work Plan\n"
            + "\n".join(gates) + "\n", encoding="utf-8")
        sess = self.proj / ".agent" / "sessions" / SESSION
        sess.mkdir(parents=True)
        (sess / "current_state").write_text("001\n", encoding="utf-8")
        self.session_dir = sess

    def set_tools_counter(self, n: int):
        (self.session_dir / "counters").write_text(
            f"tools={n}\nwrites=0\n", encoding="utf-8")

    def run_hook(self, payload: dict) -> subprocess.CompletedProcess:
        env = dict(os.environ, PLAYBOOK_SESSION_ID=SESSION)
        env.pop("PLAYBOOK_ROLE", None)
        return subprocess.run(["bash", str(HOOK)],
                              input=json.dumps(payload).encode(),
                              cwd=self.proj, env=env, capture_output=True,
                              timeout=60)

    def edit_payload(self, old: "list[str]", new: "list[str]",
                     replace_all: bool = False) -> dict:
        return {"hook_event_name": "PreToolUse", "tool_name": "Edit",
                "tool_input": {"file_path": str(self.task_file),
                               "old_string": "\n".join(old),
                               "new_string": "\n".join(new),
                               "replace_all": replace_all}}

    def write_payload(self, content: str) -> dict:
        return {"hook_event_name": "PreToolUse", "tool_name": "Write",
                "tool_input": {"file_path": str(self.task_file),
                               "content": content}}


NOTE = " — 283 tests green, receipts stamped"     # comfortably above floor
PTR = " — see Round 2 Result"                      # the pointer idiom


class BatchAllowances(unittest.TestCase):
    def test_annotated_3_batch_allowed(self):
        f = ProjectFixture()
        p = f.edit_payload(G[:3], [checked(g, NOTE) for g in G[:3]])
        r = f.run_hook(p)
        self.assertEqual(r.returncode, 0, r.stderr.decode())

    def test_pointer_idiom_accepted(self):
        f = ProjectFixture()
        p = f.edit_payload(G[:2], [checked(g, PTR) for g in G[:2]])
        r = f.run_hook(p)
        self.assertEqual(r.returncode, 0, r.stderr.decode())

    def test_judge_accept_case_arrow_283_green(self):
        f = ProjectFixture()
        p = f.edit_payload(G[:2], [checked(g, " → 283 green") for g in G[:2]])
        r = f.run_hook(p)
        self.assertEqual(r.returncode, 0, r.stderr.decode())

    def test_single_bare_close_stays_silent(self):
        f = ProjectFixture()
        p = f.edit_payload(G[:1], [checked(G[0])])
        r = f.run_hook(p)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertEqual(r.stderr.decode().strip(), "")

    def test_carried_checked_lines_are_not_counted(self):
        # Judge Finding 1 regression: an edit spanning previously-closed gates
        # must not read them as born-checked.
        f = ProjectFixture(gates=[checked(G[0], NOTE), *G[1:3]])
        old = [checked(G[0], NOTE), G[1]]
        new = [checked(G[0], NOTE), checked(G[1], NOTE)]
        r = f.run_hook(f.edit_payload(old, new))
        self.assertEqual(r.returncode, 0, r.stderr.decode())

    def test_duplicate_gate_text_multiset_pairing(self):
        dup = "- [ ] Checkpoint: converging or scattering?"
        f = ProjectFixture(gates=[dup, dup, G[0]])
        old = [dup, dup]
        new = [checked(dup, NOTE), checked(dup, PTR)]
        r = f.run_hook(f.edit_payload(old, new))
        self.assertEqual(r.returncode, 0, r.stderr.decode())

    def test_annotation_only_edit_never_counted(self):
        f = ProjectFixture(gates=[checked(G[0]), *G[1:]])
        old = [checked(G[0])]
        new = [checked(G[0], NOTE)]
        r = f.run_hook(f.edit_payload(old, new))
        self.assertEqual(r.returncode, 0, r.stderr.decode())


class BatchBlocks(unittest.TestCase):
    def test_bare_3_batch_blocked(self):
        # THE negative control: the fabricated bare batch stays forbidden.
        f = ProjectFixture()
        r = f.run_hook(f.edit_payload(G[:3], [checked(g) for g in G[:3]]))
        self.assertEqual(r.returncode, 2, r.stderr.decode())
        self.assertIn(b"outcome note", r.stderr)

    def test_bare_2_batch_blocked_tightened_from_warn(self):
        f = ProjectFixture()
        r = f.run_hook(f.edit_payload(G[:2], [checked(g) for g in G[:2]]))
        self.assertEqual(r.returncode, 2,
                         "v1 only warned at 2 bare closes; v2 must block")

    def test_subfloor_notes_blocked(self):
        # Judge reject case: "— done" is 5 non-ws chars, under the floor of 8.
        f = ProjectFixture()
        r = f.run_hook(f.edit_payload(
            G[:3], [checked(g, " — done") for g in G[:3]]))
        self.assertEqual(r.returncode, 2, r.stderr.decode())

    def test_annotated_6_batch_blocked_by_ceiling(self):
        f = ProjectFixture()
        r = f.run_hook(f.edit_payload(
            G[:6], [checked(g, NOTE) for g in G[:6]]))
        self.assertEqual(r.returncode, 2, r.stderr.decode())
        self.assertIn(b"smaller", r.stderr)

    def test_born_checked_line_blocks_the_batch(self):
        f = ProjectFixture()
        new = [checked(G[0], NOTE),
               "- [x] G-new: minted already closed — with a long note"]
        r = f.run_hook(f.edit_payload(G[:1], new))
        self.assertEqual(r.returncode, 2, r.stderr.decode())
        self.assertIn(b"born-checked", r.stderr.lower())

    def test_uncheck_cannot_launder_batch_size(self):
        # Uncheck 2 + check 3 bare in one write: raw x-delta is 1, but the
        # newly-checked count is 3 — must block as a bare batch.
        f = ProjectFixture(gates=[checked(G[0]), checked(G[1]), *G[2:5]])
        old = [checked(G[0]), checked(G[1]), *G[2:5]]
        new = [G[0], G[1],
               checked(G[2]), checked(G[3]), checked(G[4])]
        r = f.run_hook(f.edit_payload(old, new))
        self.assertEqual(r.returncode, 2,
                         "unchecking laundered the batch size:\n"
                         + r.stderr.decode())

    def test_replace_all_multiplication_counted(self):
        dup = "- [ ] Checkpoint: same text twice"
        f = ProjectFixture(gates=[dup, dup, *G])
        p = f.edit_payload([dup], [checked(dup)], replace_all=True)
        r = f.run_hook(p)
        self.assertEqual(r.returncode, 2,
                         "replace_all closing 2 occurrences bare must block")


class ConsecutiveBatchGuard(unittest.TestCase):
    def test_second_batch_with_no_intervening_work_blocked(self):
        f = ProjectFixture()
        f.set_tools_counter(10)
        r = f.run_hook(f.edit_payload(G[:2], [checked(g, NOTE) for g in G[:2]]))
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        # The edit itself bumps tools by 1 (PostToolUse); nothing else ran.
        f.set_tools_counter(11)
        r = f.run_hook(f.edit_payload(G[2:4], [checked(g, NOTE) for g in G[2:4]]))
        self.assertEqual(r.returncode, 2,
                         "back-to-back batch closes with no work between "
                         "must block:\n" + r.stderr.decode())
        self.assertIn(b"no ", r.stderr.lower())

    def test_second_batch_after_real_work_allowed(self):
        f = ProjectFixture()
        f.set_tools_counter(10)
        r = f.run_hook(f.edit_payload(G[:2], [checked(g, NOTE) for g in G[:2]]))
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        f.set_tools_counter(14)  # the edit + three real tool calls since
        r = f.run_hook(f.edit_payload(G[2:4], [checked(g, NOTE) for g in G[2:4]]))
        self.assertEqual(r.returncode, 0, r.stderr.decode())


class WritePathAndScope(unittest.TestCase):
    def test_write_tool_annotated_batch_allowed_bare_blocked(self):
        f = ProjectFixture()
        base = f.task_file.read_text(encoding="utf-8")
        annotated = base
        for g in G[:3]:
            annotated = annotated.replace(g, checked(g, NOTE))
        r = f.run_hook(f.write_payload(annotated))
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        bare = base
        for g in G[:3]:
            bare = bare.replace(g, checked(g))
        r = f.run_hook(f.write_payload(bare))
        self.assertEqual(r.returncode, 2, r.stderr.decode())

    def test_other_files_not_guarded(self):
        f = ProjectFixture()
        p = {"hook_event_name": "PreToolUse", "tool_name": "Edit",
             "tool_input": {"file_path": str(f.proj / "notes.md"),
                            "old_string": "- [ ] a\n- [ ] b\n- [ ] c",
                            "new_string": "- [x] a\n- [x] b\n- [x] c"}}
        r = f.run_hook(p)
        self.assertEqual(r.returncode, 0, r.stderr.decode())


if __name__ == "__main__":
    unittest.main()
