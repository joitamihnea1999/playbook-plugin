#!/usr/bin/env python3
"""task-status.py [--fields] <task.md> — read a task's state the way the CLI reads it.

The enforcing hooks' ONE source of truth for task.md state (V7 / P-F, task 043;
gates + F3, task 055). The stop-hook used to mirror `tasks.core._extract_status`
in awk and count gates with grep — both fence-blind, so a `## Status`/`blocked`
pair or a `- [ ] Freehand…` line quoted inside a code fence (a documentation
example, or a decoy) released unchecked gates; task-gate-hook's F3 done-check
had the same awk. Re-implementing the shared CommonMark fence engine in awk/grep
(bash 3.2 / gawk / mawk / BSD grep) would leave two implementations that drift;
instead the hooks call this script, which imports the SAME readers the lifecycle
uses, so Python and the enforcing hooks cannot disagree about the same file.

  task-status.py <task.md>           one line: the status (`pending`, `blocked`,
                                     `done (…)`, `unknown` when no live `## Status`)
  task-status.py --fields <task.md>  three lines: status, live unchecked gate
                                     count, first live unchecked gate text (empty
                                     when none) — `tasks.core._live_gate_state`,
                                     one spawn for everything the stop-hook needs

Exits 0 on success. Any failure (missing file, import error) exits non-zero with a
one-line diagnostic on stderr; the hooks treat that as UNREADABLE state and fail
CLOSED (open gates enforced, no Freehand release) — loud, never a guess.

Stdlib-only; the plugin root (this file's parent's parent) is put on sys.path.
"""
from __future__ import annotations

import sys
from pathlib import Path


def main(argv: "list[str]") -> int:
    fields = False
    args = argv[1:]
    if args and args[0] == "--fields":
        fields = True
        args = args[1:]
    if len(args) != 1:
        print("usage: task-status.py [--fields] <task.md>", file=sys.stderr)
        return 2
    task_file = Path(args[0])
    if not task_file.is_file():
        print(f"task-status: not a file: {task_file}", file=sys.stderr)
        return 1
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    try:
        from tasks.core import _extract_status, _live_gate_state
    except Exception as exc:  # noqa: BLE001 — any import failure is "unreadable"
        print(f"task-status: cannot import tasks.core: {exc}", file=sys.stderr)
        return 1
    status = _extract_status(task_file)
    if status == "error":
        print("task-status: could not read the task file", file=sys.stderr)
        return 1
    out = [status]
    if fields:
        try:
            lines = task_file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            print(f"task-status: could not read the task file: {exc}", file=sys.stderr)
            return 1
        unchecked, _total, first = _live_gate_state(lines)
        # The gate text is ONE task.md line, so it cannot carry a newline; a
        # stray CR (a CRLF file read via splitlines never keeps one) is dropped
        # so the third field is exactly one line for the hook's `read`.
        out += [str(unchecked), first.replace("\r", "")]
    # Bytes, `\n` only: a text-mode print() on native Windows emits CRLF and bash
    # command substitution keeps the CR, so `blocked\r` would miss the hook's exact
    # match (round-5 panel). The hook also strips one trailing CR — belt + braces.
    sys.stdout.buffer.write(("\n".join(out) + "\n").encode("utf-8", "replace"))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
