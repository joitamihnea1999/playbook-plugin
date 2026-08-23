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

from tasks.atomic import atomic_write
from tasks.core import resolve_agent_dir
from tasks.shared import find_project_root

_START = "<!-- archive:start -->"
_END = "<!-- archive:end -->"
_FENCE = "```"


def _atomic_write(path: Path, text: str) -> None:
    """Write `text` to `path` all-or-nothing WITHOUT newline translation, so a
    CRLF task.md is preserved byte-for-byte and a crash mid-write can never
    leave a truncated trace. Thin wrapper over the package primitive; the
    `newline=""` is what keeps a CRLF file from being rewritten LF-only."""
    atomic_write(path, text, newline="")
_GATE_RE = re.compile(r"^\s*- \[[ xX]\]")
_PROTECTED_HEADING_RE = re.compile(
    r"^##\s+(Intent|Why|Design|Work Plan|Parked|Status|Risk|References"
    r"|Verification Receipt|Pre-Panel Audit)\b", re.IGNORECASE)
# A single close's receipt ENTRY (`### <ts> · risk <r> · commit <sha>`) must not
# be archivable even when its `## Verification Receipt` heading stays outside the
# block: the verify-contract drift sweep baselines off these entries, and moving
# one to task-archive.md (under `## Compacted`) would launder a weakening (T5,
# round-8 panel).
_RECEIPT_ENTRY_RE = re.compile(r"^\s*###\s+.+·\s*risk\s+\S+\s*·\s*commit\b")


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
    gets moved. Markers inside a ``` code fence are ignored — a task that quotes
    the ritual in an example must not have its example moved."""
    spans: "list[tuple[int, int]]" = []
    open_at = None
    infence = False
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith(_FENCE):
            infence = not infence
            continue
        if infence:
            continue
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
        if _RECEIPT_ENTRY_RE.match(ln):
            return f"contains a verification-receipt entry ({ln.strip()[:60]!r})"
    return None


def _protected_section_spans(lines: "list[str]") -> "list[tuple[int, int]]":
    """Inclusive (start, end) line ranges of every `## Verification Receipt` /
    `## Pre-Panel Audit` SECTION (heading through the line before the next
    top-level `## `). Matched on the STRIPPED line, exactly like the audit
    reader, so an indented ` ## Verification Receipt` is covered too (panel
    round-9 codex). A block overlapping any of these must be refused: protecting
    only the heading/entry LINES missed a block that wraps just the `- [PASS]`
    command bullets, stranding them in the archive and emptying the drift
    baseline (panel round-9 grok)."""
    from tasks.core import _closed_fence_line_indices
    fenced = _closed_fence_line_indices(lines)   # ONE shared CommonMark scanner
    protected = ("## Verification Receipt", "## Pre-Panel Audit")
    spans: "list[tuple[int, int]]" = []
    i, n = 0, len(lines)
    while i < n:
        if i not in fenced and lines[i].strip() in protected:
            # The section ends at the next REAL (non-fenced) top-level heading. A
            # `## ` line inside a code fence (a decoy example) or otherwise must
            # NOT terminate the span early, or a block wrapping the command
            # bullets below it would escape the overlap check (panel round-11
            # sonnet, Critical) — same fence rule the reader and writer use.
            j = i + 1
            while j < n and not (j not in fenced and lines[j].strip().startswith("## ")):
                j += 1
            spans.append((i, j - 1))
            i = j
        else:
            i += 1
    return spans


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

    # newline="" preserves the file's real line endings (read_text would fold
    # CRLF→LF and silently rewrite every line of a Windows task.md).
    with task_md.open(encoding="utf-8", errors="replace", newline="") as _fh:
        text = _fh.read()
    nl = "\r\n" if "\r\n" in text else "\n"
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
    protected_spans = _protected_section_spans(lines)
    for start, end in spans:
        inner = lines[start + 1:end]
        why = _validate(inner)
        if not why:
            # Refuse a block whose moved span (start+1 .. end-1) overlaps ANY
            # protected section, even if no protected LINE is literally inside it
            # (e.g. a block wrapping only the receipt's `- [PASS]` bullets).
            for ps, pe in protected_spans:
                if start + 1 <= pe and end - 1 >= ps:
                    why = "overlaps a protected section (Verification Receipt / Pre-Panel Audit)"
                    break
        if why:
            print(f"Error: refusing to compact the block at lines {start + 1}-{end + 1}: "
                  f"it {why}. Move only review-round narrative — never gates, "
                  "Intent/Design/Parked, receipts, or pinned content. Nothing moved.",
                  file=sys.stderr)
            sys.exit(1)

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    moved_blocks = []
    total_lines = 0
    # Rebuild task.md, replacing each NON-EMPTY span (markers included) with a
    # pointer. An empty block (start immediately followed by end) has nothing to
    # move — leave it untouched rather than writing a hollow archive entry.
    out: "list[str]" = []
    cursor = 0
    for start, end in spans:
        out.extend(lines[cursor:start])
        inner = lines[start + 1:end]
        if not inner:
            out.extend(lines[start:end + 1])   # keep the empty marker pair as-is
            cursor = end + 1
            continue
        total_lines += len(inner)
        moved_blocks.append("".join(inner))
        out.append(f"> _[compacted {len(inner)} lines → task-archive.md ({stamp})]_{nl}")
        cursor = end + 1
    out.extend(lines[cursor:])
    new_task_text = "".join(out)

    if not moved_blocks:
        print(f"Nothing to compact in {task_md.parent.name}/task.md — "
              f"the {_START} … {_END} block(s) are empty.")
        return

    archive_path = task_md.parent / "task-archive.md"
    archive_add = "".join(
        f"## Compacted {stamp}{nl}{nl}{blk.rstrip()}{nl}{nl}---{nl}{nl}"
        for blk in moved_blocks)

    if dry_run:
        print(f"[dry-run] {len(moved_blocks)} block(s), {total_lines} line(s) would move "
              f"from {task_md.parent.name}/task.md → task-archive.md.")
        return

    # A MOVE must be all-or-nothing. Append to the archive first, remembering its
    # prior size; then write task.md ATOMICALLY. If that write fails, roll the
    # archive back to its prior size so the block is neither lost nor
    # double-archived on a retry — and report cleanly instead of a traceback.
    existed = archive_path.exists()
    pre_size = archive_path.stat().st_size if existed else 0
    header = ("" if (existed and pre_size) else
              f"# Task {task_num} — Archived Narrative{nl}{nl}"
              f"> Moved verbatim from task.md by `tasks compact`. "
              f"History, not deletion.{nl}{nl}")
    try:
        with archive_path.open("a", encoding="utf-8", newline="") as fh:
            fh.write(header + archive_add)
        _atomic_write(task_md, new_task_text)
    except OSError as e:
        try:
            if existed:
                with open(archive_path, "rb+") as fh:
                    fh.truncate(pre_size)
            else:
                archive_path.unlink()
        except OSError:
            pass
        print(f"Error: could not write task.md ({e}). Archive rolled back — "
              "nothing moved.", file=sys.stderr)
        sys.exit(1)

    print(f"Compacted {len(moved_blocks)} block(s), {total_lines} line(s) → "
          f"{archive_path.parent.name}/task-archive.md. "
          f"task.md is now {len(new_task_text.encode('utf-8')):,} bytes.")
