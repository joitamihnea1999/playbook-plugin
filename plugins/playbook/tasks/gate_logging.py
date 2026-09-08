"""Detect silently-stopped gate logging (bug report #4).

state-echo-hook records each gate transition into a lane's `chat_log.md` as a
`**[G<task>:<line>]**` entry, and message submissions as `**[M<n>]**`. On one
machine (cristi's Windows lane) the G-entries stopped at task 064 while tasks
kept running to ~110 with full gate discipline — a silent retro-fidelity loss
(`build_task_windows` in retro.py attributes messages to tasks via G-entries).

This module provides a pure, testable detector used by `tasks doctor` §1e. It
is deliberately conservative (only warns when the log is provably alive and the
gap is real) so it never cries wolf on a fresh or idle lane.
"""
from __future__ import annotations

import re
from pathlib import Path

_G_RE = re.compile(r"^\*\*\[G(\d+):", re.MULTILINE)
_M_RE = re.compile(r"^\*\*\[M\d+\]", re.MULTILINE)
_TASK_DIR_RE = re.compile(r"^(\d{3})-")


def gate_logging_gap(
    chat_log_text: str,
    done_task_numbers: list[int],
    *,
    min_gap: int = 2,
) -> str | None:
    """Return a warning string if gate-logging appears to have silently stopped
    in this lane, else None.

    Signals:
      - G-entries (`**[G<task>:…]**`) mark the last task whose gates were logged.
      - M-entries (`**[M…]**`) prove the chat log is still being written to; with
        none, there's no evidence the lane is active — stay silent (no cry-wolf).
      - `done_task_numbers`: completed tasks in the lane. If ≥min_gap of them are
        NEWER than the last logged gate task, logging stopped while work went on.
    """
    if not _M_RE.search(chat_log_text):
        return None  # log absent / never written — no signal
    if not done_task_numbers:
        return None
    max_done = max(done_task_numbers)
    g_tasks = [int(n) for n in _G_RE.findall(chat_log_text)]
    if not g_tasks:
        # Log is alive and done tasks exist, but NOT a single gate entry —
        # gate logging likely never fired on this machine.
        if len(done_task_numbers) >= min_gap:
            return (f"no gate-logging entries at all, but {len(done_task_numbers)} "
                    f"done task(s) exist (up to {max_done:03d}) and the chat log is "
                    f"active — state-echo gate logging may never have fired here")
        return None
    max_g = max(g_tasks)
    newer_done = [n for n in done_task_numbers if n > max_g]
    if len(newer_done) >= min_gap:
        return (f"gate-logging last recorded task {max_g:03d}, but "
                f"{len(newer_done)} newer done task(s) (up to {max_done:03d}) have "
                f"no gate entries — state-echo gate logging may have stopped")
    return None


def done_task_numbers(tasks_dir: Path) -> list[int]:
    """Numbers of tasks under `tasks_dir` whose `## Status` is `done`."""
    result: list[int] = []
    if not tasks_dir.is_dir():
        return result
    for child in sorted(tasks_dir.iterdir()):
        m = _TASK_DIR_RE.match(child.name)
        if not (child.is_dir() and m):
            continue
        task_md = child / "task.md"
        if not task_md.is_file():
            continue
        if _task_is_done(task_md):
            result.append(int(m.group(1)))
    return result


def _task_is_done(task_md: Path) -> bool:
    """The shared fence-aware status reader (task 043): this module used to carry
    its own fence-blind `## Status` regex with different skip rules, so doctor's
    gate-logging-gap check could count a fenced `## Status`/done example as a
    closed task. One reader for every consumer."""
    from tasks.core import _is_done
    return _is_done(task_md)   # ONE predicate (case-sensitive, like the writer's token)
