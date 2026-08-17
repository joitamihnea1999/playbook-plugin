"""`tasks compact <N>` — the mechanical half of the sanctioned compaction ritual.

task.md IS the execution trace and grows monotonically; once it outgrows the
review budget a judge reads it through a keyhole (see `audit.check_task_bloat`).
The sticker's remedy is to move OLD review-round narrative VERBATIM to
`task-archive.md`, leaving a pointer — "moving history is not deleting it". That
was hand cut-and-paste: tedious and risky enough that it rarely happened, so
task files bloated and reviews degraded.

Contract — judgment stays with the agent, the move stays safe:
  * the AGENT decides what is cold by wrapping each block in
    `<!-- archive:start -->` … `<!-- archive:end -->`;
  * this command appends every marked block VERBATIM to `task-archive.md`
    (same dir) under a dated header and replaces it in task.md with a one-line
    pointer.

It fails LOUD rather than amputate: an unmatched marker, or a block that
contains a gate checkbox, a `<!-- pin -->`, or a protected level-2 section
heading (Intent/Why/Design/Work Plan/Parked/Status/Risk/References), aborts the
whole run with a diagnostic and writes nothing. So a mismarked region can never
silently cost you a gate or the Intent.

Leaf imports only (core/shared); never a command module.
"""
from __future__ import annotations

import datetime
import re
import sys
from pathlib import Path

from tasks.core import resolve_agent_dir
from tasks.shared import find_project_root

_START = "<!-- archive:start -->"
_END = "<!-- archive:end -->"
_GATE_RE = re.compile(r"^\s*- \[[ xX]\]")
_PROTECTED_HEADING_RE = re.compile(
    r"^##\s+(Intent|Why|Design|Work Plan|Parked|Status|Risk|References)\b", re.IGNORECASE)


def _find_task_md(project_path: Path, task_num: str) -> "Path | None":
    tasks_dir = resolve_agent_dir(project_path) / "tasks"
    if not tasks_dir.exists():
        return None
    matches = sorted(tasks_dir.glob(f"{task_num}-*/task.md"))
    return matches[0] if matches else None


def _blocks(lines: "list[str]") -> "tuple[list[tuple[int, int]], str | None]":
    """Marker-pair spans as (start_index, end_index) INCLUSIVE of both markers,
    or (…, error) when the markers do not nest cleanly. Nesting is not allowed —
    a second start before an end, an end with no open start, or an unclosed start
    are all refused, because guessing the intent is exactly how a wrong region
    gets moved."""
    spans: "list[tuple[int, int]]" = []
    open_at = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s == _START:
            if open_at is not None:
                return spans, f"nested {_START} at line {i + 1} (previous opened at line {open_at + 1})"
            open_at = i
        elif s == _END:
            if open_at is None:
                return spans, f"{_END} at line {i + 1} with no matching {_START}"
            spans.append((open_at, i))
            open_at = None
    if open_at is not None:
        return spans, f"unclosed {_START} at line {open_at + 1}"
    return spans, None


def _validate(block_lines: "list[str]") -> "str | None":
    """None when a block is safe to move, else why it is not."""
    for ln in block_lines:
        if _GATE_RE.match(ln):
            return f"contains a gate checkbox ({ln.strip()[:60]!r})"
        if "<!-- pin -->" in ln:
            return "contains a <!-- pin --> (must survive trims — never archive it)"
        if _PROTECTED_HEADING_RE.match(ln):
            return f"contains a protected section heading ({ln.strip()[:60]!r})"
    return None


def cmd_compact(cmd_args) -> None:
    dry_run = "--dry-run" in cmd_args
    positional = [a for a in cmd_args if not a.startswith("-")]
    if not positional:
        print("'compact' requires a task number (e.g. `tasks compact 12`). "
              "Wrap cold review narrative in "
              f"{_START} … {_END} first.", file=sys.stderr)
        sys.exit(1)

    project_path = find_project_root()
    task_num = positional[0].zfill(3)
    task_md = _find_task_md(project_path, task_num)
    if task_md is None:
        print(f"Error: no task {task_num} found under .agent/tasks/.", file=sys.stderr)
        sys.exit(1)

    text = task_md.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    spans, err = _blocks(lines)
    if err:
        print(f"Error: archive markers do not nest cleanly — {err}. "
              "Nothing moved.", file=sys.stderr)
        sys.exit(1)
    if not spans:
        print(f"Nothing to compact in {task_md.parent.name}/task.md — "
              f"wrap cold narrative in {_START} … {_END} first.")
        return

    # Validate every block BEFORE touching anything (all-or-nothing).
    for start, end in spans:
        inner = lines[start + 1:end]
        why = _validate(inner)
        if why:
            print(f"Error: refusing to compact the block at lines {start + 1}-{end + 1}: "
                  f"it {why}. Move only review-round narrative — never gates, "
                  "Intent/Design/Parked, or pinned content. Nothing moved.",
                  file=sys.stderr)
            sys.exit(1)

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    moved_blocks = []
    total_lines = 0
    # Rebuild task.md, replacing each span (markers included) with a pointer.
    out: "list[str]" = []
    cursor = 0
    for start, end in spans:
        out.extend(lines[cursor:start])
        inner = lines[start + 1:end]
        total_lines += len(inner)
        moved_blocks.append("".join(inner))
        pointer = f"> _[compacted {len(inner)} lines → task-archive.md ({stamp})]_\n"
        out.append(pointer)
        cursor = end + 1
    out.extend(lines[cursor:])
    new_task_text = "".join(out)

    archive_path = task_md.parent / "task-archive.md"
    archive_add = "".join(
        f"## Compacted {stamp}\n\n{blk.rstrip()}\n\n---\n\n" for blk in moved_blocks)

    if dry_run:
        print(f"[dry-run] {len(spans)} block(s), {total_lines} line(s) would move "
              f"from {task_md.parent.name}/task.md → task-archive.md.")
        return

    # Append to archive first; only rewrite task.md if that succeeded, so a
    # failure can never delete the trace without preserving it.
    with archive_path.open("a", encoding="utf-8") as fh:
        if archive_path.stat().st_size == 0:
            fh.write(f"# Task {task_num} — Archived Narrative\n\n"
                     "> Moved verbatim from task.md by `tasks compact`. "
                     "History, not deletion.\n\n")
        fh.write(archive_add)
    task_md.write_text(new_task_text, encoding="utf-8")

    print(f"Compacted {len(spans)} block(s), {total_lines} line(s) → "
          f"{archive_path.parent.name}/task-archive.md. "
          f"task.md is now {len(new_task_text):,} bytes.")
