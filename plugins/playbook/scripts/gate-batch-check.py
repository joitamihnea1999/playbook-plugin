#!/usr/bin/env python3
"""Guard 0.5 v2 — annotated batch-close check (F1; blind-judge-reviewed).

Called by task-gate-hook on Edit/Write to the ACTIVE task's task.md. Decides
whether the write's newly-checked gates are an allowed batch. stdin: the hook
payload JSON. Exit 0 = allow (stdout empty), exit 2 = block (stdout = the
message the hook relays to stderr). Any other failure = exit 1 and the hook
fails OPEN (a crashed guard must not brick the session).

The rules (design-1.5.5.md, F1 — including the judge's corrections):

  * three-way partition of checked lines in the NEW content (multiset):
    literally present checked in OLD = carried (ignored); else prefix-paired
    to an unchecked OLD original (longest original wins; empty originals
    excluded) = newly-checked, with the extension beyond the original as its
    annotation; else born-checked.
  * n = |newly-checked| (times replace_all occurrences). NEVER the raw
    x-count delta — unchecking in the same write must not launder n.
  * n ≤ 1 → allow silently (singles keep their old freedom).
  * 2 ≤ n ≤ 5 → allow ONLY if no born-checked line and every newly-checked
    line's annotation has ≥ 8 unicode non-whitespace characters ("→ 283
    green" passes; "— done" does not; a pointer like "— see Round 2 Result"
    is the sanctioned idiom when the outcome lives under the gate).
  * n ≥ 6 → block even fully annotated (the whole-plan-in-one-write ceiling).
  * consecutive-batch guard: an allowed batch records the session's `tools`
    counter; the NEXT batch blocks unless at least one other tool call
    happened in between (kills the two-writes-of-5 end-of-task pattern).
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

GATE_RE = re.compile(r"^\s*- \[([ xX])\]\s*(.*)$")
ANNOTATION_FLOOR = 8   # unicode non-whitespace chars (never bash bytes)
BATCH_CEILING = 5

BARE_MSG = """\
BLOCKED: {n} gates closed in one write, but {k} carry no outcome note.
Batch-closing ALREADY-DONE gates requires each line to carry its own outcome
(>= {floor} non-space chars appended to the gate text) — e.g. "→ 283 green",
or a pointer to where the outcome lives: "— see Round 2 Result".
Bare lines:
{lines}
Append each gate's outcome, or close them one at a time as you work."""

CEILING_MSG = """\
BLOCKED: {n} gates closed in one write — more than {ceiling} even with notes.
Close gates in smaller batches as the work actually completes; a whole plan
ticked at once is indistinguishable from backfill."""

BORN_MSG = """\
BLOCKED: this batch mints gate line(s) already checked (born-checked):
{lines}
Gates are added OPEN the moment you discover the work (Standing Orders),
then closed after the work is done — a line born [x] was never observable.
Most common cause: you REWROTE the gate's text while checking it. Keep the
original gate text byte-for-byte and APPEND your outcome after it — e.g.
"- [x] <original text> — 316 green"."""

CONSECUTIVE_MSG = """\
BLOCKED: second batch close with no tool call in between.
A batch attests ALREADY-DONE work; two back-to-back batch writes look like
end-of-task ticking. Do the next piece of work, then close its gates."""


def gate_bodies(text: str) -> "tuple[list[str], list[str]]":
    """(checked_bodies, unchecked_bodies) — line-anchored, whitespace-normal."""
    checked: "list[str]" = []
    unchecked: "list[str]" = []
    for line in text.splitlines():
        m = GATE_RE.match(line)
        if not m:
            continue
        body = m.group(2).strip()
        (checked if m.group(1) in "xX" else unchecked).append(body)
    return checked, unchecked


def partition(old: str, new: str):
    """Return (newly_checked [(body, annotation)], born_checked [body])."""
    new_checked, _ = gate_bodies(new)
    old_checked, old_unchecked = gate_bodies(old)

    carried = Counter(old_checked)
    originals = Counter(o for o in old_unchecked if o)
    newly: "list[tuple[str, str]]" = []
    born: "list[str]" = []
    for body in new_checked:
        if carried[body] > 0:
            carried[body] -= 1
            continue
        candidates = [o for o, c in originals.items() if c > 0 and body.startswith(o)]
        if not candidates:
            born.append(body)
            continue
        orig = max(candidates, key=len)
        originals[orig] -= 1
        newly.append((body, body[len(orig):]))
    return newly, born


def annotation_ok(extension: str) -> bool:
    return len(re.sub(r"\s", "", extension)) >= ANNOTATION_FLOOR


def read_tools_counter(session_dir: Path) -> int:
    try:
        for line in (session_dir / "counters").read_text(encoding="utf-8").splitlines():
            if line.startswith("tools="):
                return int(line.split("=", 1)[1].strip() or 0)
    except (OSError, ValueError):
        pass
    return 0


def main() -> int:
    args = sys.argv[1:]

    def opt(name: str) -> "str | None":
        return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else None

    tool = opt("--tool") or ""
    file_path = opt("--file") or ""
    session_dir = Path(opt("--session-dir") or ".")

    d = json.load(sys.stdin)
    ti = d.get("tool_input", {}) or {}

    if tool == "Edit":
        old = str(ti.get("old_string", ""))
        new = str(ti.get("new_string", ""))
        replace_all = bool(ti.get("replace_all", False))
    elif tool == "Write":
        new = str(ti.get("content", ""))
        replace_all = False
        try:
            old = Path(file_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            old = ""
    else:
        return 0

    newly, born = partition(old, new)

    occurrences = 1
    if replace_all and newly:
        try:
            occurrences = max(Path(file_path).read_text(
                encoding="utf-8", errors="replace").count(str(ti.get("old_string", ""))), 1)
        except OSError:
            occurrences = 1

    n = (len(newly) + len(born)) * occurrences
    if n <= 1:
        return 0

    if born:
        print(BORN_MSG.format(lines="\n".join(f"  - [x] {b}" for b in born)))
        return 2

    if n > BATCH_CEILING:
        print(CEILING_MSG.format(n=n, ceiling=BATCH_CEILING))
        return 2

    bare = [(body, ext) for body, ext in newly if not annotation_ok(ext)]
    if bare:
        print(BARE_MSG.format(
            n=n, k=len(bare) * occurrences, floor=ANNOTATION_FLOOR,
            lines="\n".join(f"  - [x] {b}" for b, _ in bare)))
        return 2

    # Allowed batch — consecutive-batch guard (judge Finding 2).
    marker = session_dir / "last_batch_close"
    tools_now = read_tools_counter(session_dir)
    try:
        recorded = int(marker.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        recorded = None
    if recorded is not None and 0 <= tools_now - recorded <= 1:
        # Only the previous batch edit itself (or nothing) ran since the last
        # allowed batch: end-of-task ticking pattern. Marker NOT updated — a
        # real tool call is the only way forward.
        print(CONSECUTIVE_MSG)
        return 2
    try:
        marker.write_text(str(tools_now), encoding="utf-8")
    except OSError:
        pass  # advisory persistence; never block on it
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Fail OPEN: a crashed guard must not brick the session. The hook
        # treats any exit other than 2 as allow.
        sys.exit(1)
