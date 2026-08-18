#!/usr/bin/env python3
"""First-task chat attribution (F2): the mandate must not be invisible.

Field evidence (StrataDB batches 1 and 4):

  * The message that DEFINES a project — the owner's mandate — predates
    `tasks new 1` / `tasks work 1` by construction. Every attribution window
    opened at the first task's activation, so the one message the plan judge
    most needs ("check the plan against the user's actual words") was
    unattributable. `tasks context 001` → "no attributed messages".
  * On task 010 the judges' intent-check ran blind for a second reason:
    `tasks context` reads only `<!-- TNNN -->` spans, the spans are written
    only by `tasks tag`, and nothing ever runs `tasks tag` — so context was
    blind for EVERY task even though gate entries + bash_history contained
    everything needed to attribute messages (verified against the real
    project: the window fallback returns 3 messages for task 10).

The class fix, at the chokepoints:

  * `build_task_windows` — the earliest-activated task's window opens at the
    epoch, not at its activation (pre-history IS the project seed);
  * `tasks tag` — same rule for the span writer;
  * `tasks context` — falls back to timestamp windows when no spans exist
    (the same fallback `tasks intent`'s chat layer already has), still
    failing loudly when nothing is attributable;
  * `extract_chatlog` — consumes the `(provider/pid)` header suffix instead
    of leaking it into message text (same disease `tasks log` had, fixed in
    1.4.3; the extractor was missed).

Run: python3 -m unittest tests.test_chat_attribution
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN = _HERE.parent / "plugins/playbook"
sys.path.insert(0, str(PLUGIN))

from tasks.retro import build_task_windows, extract_chatlog, _attribute_to_task  # noqa: E402

MANDATE = "Build StrataDB, an embeddable zero-dependency document DB. THE-MANDATE-SENTINEL."

CHAT_LOG = f"""# Project Chat Log

User messages logged with timestamps.

---

**[M001]** [2026-08-12 06:44:12 UTC] `HOST` (claude/pid-111)

{MANDATE}

---

**[G001:29]** [2026-08-12 06:47:59 UTC] `HOST` (2 tool calls)

- [x] Understand: read the brief

---

**[M002]** [2026-08-12 06:50:00 UTC] `HOST` (claude/pid-111)

continue with task one

---

**[M003]** [2026-08-12 07:30:00 UTC] `HOST` (claude/pid-111)

message during task two

---
"""

BASH_HISTORY = """2026-08-12 06:46:00 | bash | .claude/bin/tasks work 1
2026-08-12 07:25:25 | bash | .claude/bin/tasks work 2
"""


def make_project(chat_log: str = CHAT_LOG, bash_history: str | None = BASH_HISTORY) -> Path:
    proj = Path(tempfile.mkdtemp())
    agent = proj / ".agent"
    (agent / "tasks" / "001-first").mkdir(parents=True)
    (agent / "tasks" / "001-first" / "task.md").write_text(
        "# 001 - First\n\n## Status\npending\n", encoding="utf-8")
    (agent / "chat_log.md").write_text(chat_log, encoding="utf-8")
    if bash_history is not None:
        (agent / "bash_history").write_text(bash_history, encoding="utf-8")
    return proj


def run_cli(proj: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PLUGIN)
    return subprocess.run(
        [sys.executable, "-m", "tasks.cli", *args],
        cwd=proj, env=env, capture_output=True, text=True, timeout=60)


class WindowsIncludeTheSeed(unittest.TestCase):
    """`build_task_windows`: pre-history belongs to the earliest-activated task."""

    def _windows(self, proj: Path):
        agent = proj / ".agent"
        bh = agent / "bash_history"
        return build_task_windows(agent / "chat_log.md", bh if bh.exists() else None)

    def test_mandate_before_first_activation_attributes_to_first_task(self):
        proj = make_project()
        windows = self._windows(proj)
        # M001 (06:44:12) predates task 1's activation (06:46:00).
        self.assertEqual(_attribute_to_task("2026-08-12 06:44:12 UTC", windows), 1)

    def test_widening_does_not_leak_into_later_tasks(self):
        proj = make_project()
        windows = self._windows(proj)
        # In-window messages keep their attribution (negative control for the
        # widening: only the FIRST window opens early).
        self.assertEqual(_attribute_to_task("2026-08-12 06:50:00 UTC", windows), 1)
        self.assertEqual(_attribute_to_task("2026-08-12 07:30:00 UTC", windows), 2)

    def test_no_activations_yields_no_windows(self):
        # Negative control: with no activation evidence at all, nothing is
        # invented — an empty dict, not a fabricated window.
        proj = make_project(
            chat_log="# Project Chat Log\n\n---\n\n**[M001]** [2026-08-12 06:44:12 UTC] `HOST`\n\nhello\n\n---\n",
            bash_history=None)
        self.assertEqual(self._windows(proj), {})

    def test_earliest_activated_wins_even_when_numbering_disagrees(self):
        # Task 5 activated before task 2: pre-history belongs to 5 (activation
        # order), not to the lowest number.
        proj = make_project(bash_history=(
            "2026-08-12 06:46:00 | bash | .claude/bin/tasks work 5\n"
            "2026-08-12 07:25:25 | bash | .claude/bin/tasks work 2\n"))
        windows = self._windows(proj)
        self.assertEqual(_attribute_to_task("2026-08-12 06:00:00 UTC", windows), 5)


class ExtractChatlogHeaderSuffix(unittest.TestCase):
    def test_provider_pid_suffix_not_leaked_into_text(self):
        proj = make_project()
        msgs = extract_chatlog(proj / ".agent" / "chat_log.md")
        m1 = next(m for m in msgs if m["id"] == 1)
        self.assertNotIn("claude/pid-111", m1["text"])
        self.assertTrue(m1["text"].startswith("Build StrataDB"), m1["text"][:80])

    def test_legacy_unsuffixed_entries_still_parse(self):
        legacy = ("# Project Chat Log\n\n---\n\n"
                  "**[M001]** [2026-08-12 06:44:12 UTC] `HOST`\n\nplain entry\n\n---\n")
        proj = make_project(chat_log=legacy, bash_history=None)
        msgs = extract_chatlog(proj / ".agent" / "chat_log.md")
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["text"], "plain entry")


class TagCommandCoversTheSeed(unittest.TestCase):
    def test_pre_first_activation_messages_are_tagged_to_first_task(self):
        proj = make_project()
        r = run_cli(proj, "tag")
        self.assertEqual(r.returncode, 0, r.stderr)
        tagged = (proj / ".agent" / "chat_log.md").read_text(encoding="utf-8")
        # The mandate (M001) must sit inside the <!-- T001 --> span.
        t1_open = tagged.index("<!-- T001 -->")
        self.assertGreater(tagged.index("THE-MANDATE-SENTINEL"), t1_open)
        # And the widening must not swallow task 2's messages (control).
        t2_open = tagged.index("<!-- T002 -->")
        self.assertGreater(tagged.index("message during task two"), t2_open)
        self.assertLess(tagged.index("THE-MANDATE-SENTINEL"), t2_open)

    def test_context_returns_the_mandate_after_tag(self):
        proj = make_project()
        run_cli(proj, "tag")
        r = run_cli(proj, "context", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("THE-MANDATE-SENTINEL", r.stdout)


class ContextFallsBackToWindows(unittest.TestCase):
    """`tasks context` must not be blind on an untagged project (task 010)."""

    def test_untagged_project_still_returns_messages(self):
        proj = make_project()  # no `tasks tag` run — the field state
        r = run_cli(proj, "context", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("THE-MANDATE-SENTINEL", r.stdout)
        self.assertIn("continue with task one", r.stdout)
        self.assertNotIn("message during task two", r.stdout)

    def test_untagged_later_task_returns_its_window(self):
        proj = make_project()
        r = run_cli(proj, "context", "2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("message during task two", r.stdout)
        self.assertNotIn("THE-MANDATE-SENTINEL", r.stdout)

    def test_fallback_names_its_source(self):
        # The provenance note goes to stderr; stdout stays pure messages.
        proj = make_project()
        r = run_cli(proj, "context", "1")
        self.assertIn("timestamp window", r.stderr.lower())
        self.assertNotIn("timestamp window", r.stdout.lower())

    def test_truly_unattributable_still_fails_loud(self):
        # Negative control: no gate entries, no bash_history → the old loud
        # failure survives (the fallback must not fabricate attribution).
        proj = make_project(
            chat_log="# Project Chat Log\n\n---\n\n**[M001]** [2026-08-12 06:44:12 UTC] `HOST`\n\nhello\n\n---\n",
            bash_history=None)
        r = run_cli(proj, "context", "1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("No attributed messages", r.stderr)


if __name__ == "__main__":
    unittest.main()
