#!/usr/bin/env python3
"""task-status.py <task.md> — print a task's status value, the way the CLI reads it.

The stop-hook's ONE source of truth for `## Status` (V7 / P-F, task 043). It used
to mirror `tasks.core._extract_status` in awk — fence-blind, so a `## Status` /
`blocked` pair quoted inside a code fence (a documentation example, or a decoy)
released unchecked gates. Re-implementing the shared CommonMark fence engine in
awk (bash 3.2 / gawk / mawk) would leave two implementations that drift; instead
the hook calls this script, which imports the SAME reader the lifecycle uses, so
Python and the enforcing hook cannot disagree about the same file.

Prints the status (`pending`, `blocked`, `done (…)`, `unknown` when no live
`## Status` exists) and exits 0. Any failure (missing file, import error) exits
non-zero with a one-line diagnostic on stderr; the hook treats that as an
UNREADABLE status and falls through to the gate count (fail CLOSED, loud).

Stdlib-only; the plugin root (this file's parent's parent) is put on sys.path.
"""
from __future__ import annotations

import sys
from pathlib import Path


def main(argv: "list[str]") -> int:
    if len(argv) != 2:
        print("usage: task-status.py <task.md>", file=sys.stderr)
        return 2
    task_file = Path(argv[1])
    if not task_file.is_file():
        print(f"task-status: not a file: {task_file}", file=sys.stderr)
        return 1
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    try:
        from tasks.core import _extract_status
    except Exception as exc:  # noqa: BLE001 — any import failure is "unreadable"
        print(f"task-status: cannot import tasks.core: {exc}", file=sys.stderr)
        return 1
    status = _extract_status(task_file)
    if status == "error":
        print("task-status: could not read the task file", file=sys.stderr)
        return 1
    # Bytes, `\n` only: a text-mode print() on native Windows emits CRLF and bash
    # command substitution keeps the CR, so `blocked\r` would miss the hook's exact
    # match (round-5 panel). The hook also strips one trailing CR — belt + braces.
    sys.stdout.buffer.write((status + "\n").encode("utf-8", "replace"))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
