"""The dispatch pin — every top-level command stays reachable through the real
entry point (`python3 -m tasks.cli <cmd>`), and behaves like ITSELF.

This is the guard the 1.5.9 cli.py split rests on (design-1.5.9.md §8 peel 0):
as command arms move out of cli.py one module at a time, a peel that drops a
dispatch branch, typos a module name, breaks an import inside a moved arm, or
wires a command to the WRONG arm must fail HERE, loudly — not surface months
later on a real project. Two halves, deliberately redundant:

  1. Source parity: the `COMMANDS` tuple in cli.py must equal the set of
     literals in the dispatch chain (an arm deleted from the chain while the
     tuple still advertises it — or vice versa — is a red test).
  2. Live smoke with a per-command baseline oracle: every command in
     `COMMANDS` is invoked with no arguments in its own fresh scratch project
     through a real subprocess, and must reproduce the exit code and a
     distinctive first-line MARKER recorded from the pre-split behavior
     (2026-08-14, 1.5.8 + judge-F1 fix). The marker is what catches
     cross-wiring (`blocked` answering with `parked`'s output); the
     no-`Traceback` assertion is what catches a moved arm whose new module
     fails to import (the judge's F5 — `Unknown command:` alone would miss
     both). Arms exiting non-zero with their own usage errors is their normal,
     pinned behavior.

The `models` row relaxes its exit code: bare `tasks models` runs a pin check
whose verdict depends on which judge CLIs this machine has — the marker still
pins that the RIGHT arm answered.
"""
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PLAYBOOK = _HERE.parent / "plugins" / "playbook"
sys.path.insert(0, str(_PLAYBOOK))

from tasks.cli import COMMANDS  # noqa: E402
from tasks.template import usage_text  # noqa: E402

_CLI_SRC = _PLAYBOOK / "tasks" / "cli.py"

# Commands intentionally folded into another command's usage entry rather than
# given their own line:
#   ls    — documented as the `list [--pending]` alias.
#   judge — the low-level runner; usage steers users to plan-review/impl-review.
# Everything else must earn its own line (see UsageTextCoverage).
_USAGE_ALIASES = {"ls", "judge"}

# Matches only the top-level dispatch lines (`if cmd == "…"` / `elif cmd in (…)`);
# `review_cmd ==` inside an arm does not start with `if cmd`/`elif cmd`.
_DISPATCH_RE = re.compile(r'^\s*(?:el)?if cmd (?:==|in)\s*(.+?):')
_TOKEN_RE = re.compile(r'"([a-z-]+)"')

# The baseline: (exit_code_or_None, marker). Recorded from a bare run of each
# command in an empty scratch project (`.agent/tasks/` present, nothing else).
# None = don't pin the exit code (machine-dependent). A marker is a substring
# of stdout+stderr distinctive enough that no OTHER arm's bare output contains
# it — that distinctness is what makes miswiring detectable.
_BASELINE = {
    "work": (1, "'work' requires a task number"),
    "new": (1, "'new' requires a type and a name"),
    "init": (0, "Initializing project:"),
    "bootstrap": (0, "=== CLI REFERENCE ==="),
    "list": (0, "No tasks found"),
    "ls": (0, "No tasks found"),
    "panel-review": (1, "'panel-review' requires a task number or --prompt"),
    "models": (None, "Judge pin verdicts"),
    "plan-review": (1, "'plan-review' requires a task number"),
    "impl-review": (1, "'impl-review' requires a task number"),
    "judge": (1, "'judge' requires a task number"),
    "context": (1, "'context' requires a task number"),
    "intent": (1, "'intent' requires a task number"),
    "timeline": (1, "No .agent/bash_history found."),
    "tagger": (1, "No .agent/chat_log.md found."),
    "tag": (1, "No .agent/chat_log.md found."),
    "retro": (1, "No tasks found in window."),
    "status": (0, "No tasks found"),
    "audit": (0, "Running pre-panel audit..."),
    "blocked": (1, "a reason is required"),
    "handoff": (1, "No active task to hand off"),
    "parked": (0, "No open parked items."),
    "freehand": (0, "creating freehand session"),
    # I5: doctor exits non-zero when a check FAILs. The bare scratch project has
    # no CLAUDE.md/MIND_MAP.md, so doctor deterministically reports failures → 1.
    "doctor": (1, "tasks doctor"),
    "environment": (0, "Environment recommendations (advisory"),
    "detect-verify": (0, "Detected verify command"),
    "merge-doctor": (2, "Usage: tasks merge-doctor"),
    "mindmap-sync": (1, "MIND_MAP.md not found"),
    "log": (1, ".agent/chat_log.md not found"),
    "prepare-merge": (1, "could not compute merge base"),
    "compact": (1, "'compact' requires a task number"),
    "recall": (1, "'recall' requires a node id or keyword"),
}


def _dispatch_tokens() -> set[str]:
    tokens: set[str] = set()
    for line in _CLI_SRC.read_text(encoding="utf-8").splitlines():
        m = _DISPATCH_RE.match(line)
        if m:
            tokens.update(_TOKEN_RE.findall(m.group(1)))
    return tokens


class DispatchSourceParity(unittest.TestCase):
    def test_commands_tuple_matches_dispatch_chain(self):
        self.assertEqual(
            set(COMMANDS), _dispatch_tokens(),
            "tasks/cli.py COMMANDS and the dispatch if/elif chain disagree — "
            "an arm was added or removed on one side only.",
        )

    def test_baseline_covers_every_command(self):
        self.assertEqual(set(COMMANDS), set(_BASELINE))

    def test_commands_tuple_is_not_trivially_empty(self):
        # Negative control on the parser itself: an over-strict regex that
        # matched nothing would make the parity test vacuously green.
        self.assertGreaterEqual(len(_dispatch_tokens()), 20)


class DispatchLiveSmoke(unittest.TestCase):
    """Run every command bare in a throwaway project; assert its own baseline."""

    def _run_bare(self, cmd: str) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as t:
            proj = Path(t)
            (proj / ".agent" / "tasks").mkdir(parents=True)
            env = os.environ.copy()
            env["PYTHONPATH"] = str(_PLAYBOOK)
            # The dispatch smoke verifies routing, not live provider/model
            # availability. In a developer install, inheriting real
            # claude/codex/grok binaries makes bare `tasks models` perform live
            # probes and can exceed this test's 120s timeout. BASH_ENV also
            # installs a per-command DEBUG trap in every child shell. Keep the
            # smoke hermetic while retaining ordinary system tools.
            env.pop("BASH_ENV", None)
            if os.name != "nt":
                env["PATH"] = "/usr/bin:/bin"
            # Pin the session id so arms that write session state stay inside
            # the scratch dir deterministically.
            env["PLAYBOOK_SESSION_ID"] = "pid-999999999"
            return subprocess.run(
                [sys.executable, "-m", "tasks.cli", cmd],
                cwd=proj, env=env, capture_output=True, text=True, timeout=120,
            )

    def test_every_command_reaches_its_own_arm(self):
        for cmd in COMMANDS:
            with self.subTest(command=cmd):
                r = self._run_bare(cmd)
                combined = r.stdout + r.stderr
                self.assertNotIn(
                    "Unknown command:", combined,
                    f"`tasks {cmd}` fell through to the dispatcher's rejection "
                    f"branch — its arm is orphaned.\n--- output ---\n{combined}")
                self.assertNotIn(
                    "Traceback", combined,
                    f"`tasks {cmd}` crashed — a moved arm's module or import "
                    f"is broken.\n--- output ---\n{combined}")
                want_rc, marker = _BASELINE[cmd]
                self.assertIn(
                    marker, combined,
                    f"`tasks {cmd}` did not produce its own baseline output — "
                    f"wired to the wrong arm?\n--- output ---\n{combined}")
                if want_rc is not None:
                    self.assertEqual(
                        r.returncode, want_rc,
                        f"`tasks {cmd}` exit code drifted from baseline."
                        f"\n--- output ---\n{combined}")

    def test_rejection_branch_still_rejects(self):
        # Negative control: the smoke assertions mean nothing if the
        # dispatcher stopped printing the rejection for unknown commands.
        r = self._run_bare("no-such-command")
        self.assertIn("Unknown command:", r.stdout + r.stderr)
        self.assertNotEqual(r.returncode, 0)


class UsageTextCoverage(unittest.TestCase):
    """`tasks --help` must document every dispatchable command as its own entry,
    so help can never silently under-document a shipped command again (the drift
    this test was born from: intent/timeline/tagger/tag/merge-doctor/mindmap-sync
    all dispatched but absent from usage_text).

    A command counts as documented only when some usage line, stripped of leading
    whitespace, STARTS with the token at a word boundary — not a bare substring.
    Substring matching would be fooled: the arg placeholder `[intent]` would
    satisfy the `intent` command, and `judge` inside `Multi-model judge panel`
    would satisfy `judge`.
    """

    _CMD_RES = {c: re.compile(r"^" + re.escape(c) + r"(?![\w-])") for c in COMMANDS}

    def _documented(self, tok: str) -> bool:
        pat = self._CMD_RES[tok]
        return any(pat.match(ln.strip()) for ln in usage_text().splitlines())

    def test_every_command_documented_in_usage(self):
        missing = sorted(
            c for c in COMMANDS
            if c not in _USAGE_ALIASES and not self._documented(c))
        self.assertEqual(
            missing, [],
            "tasks --help (template.usage_text) does not document these commands "
            f"as their own entry: {missing}. Add a one-line entry per command — "
            "or, if one is intentionally folded into another's entry, add it to "
            "_USAGE_ALIASES with a comment saying why.",
        )

    def test_usage_aliases_are_real_commands(self):
        # Guard the exemption set itself: every alias exempted above must be a
        # real dispatch command, else the exemption is quietly masking a typo.
        self.assertLessEqual(
            _USAGE_ALIASES, set(COMMANDS),
            f"_USAGE_ALIASES names non-commands: {_USAGE_ALIASES - set(COMMANDS)}")


if __name__ == "__main__":
    unittest.main()
