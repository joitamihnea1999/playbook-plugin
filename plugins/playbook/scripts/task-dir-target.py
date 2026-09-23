#!/usr/bin/env python3
"""Does a shell command name a task directory that is, or may be, inside the project?

task-gate-hook's "don't create task directories manually" guard (Guard 2) calls
this only after its own trigger regex has matched, to decide whether the match
is a real task directory or a fixture elsewhere (task 080, S1d — the 073 flag
C15: `<tmp>/.agent/tasks/001-x` in a temp dir was refused).

Usage:   PB_CMD=<command> PB_PROJECT=<project root> python3 task-dir-target.py
Exit 0   every `.agent[/<lane>]/tasks/` token is an absolute path outside the project
Exit 1   some token is, or may be, inside it — the hook blocks
Other    (a crash) — the hook blocks too; this script can only NARROW the guard

Only a literal absolute path is ever judged "outside". A relative path, `~`, a
`$VAR`, a backtick, or a command shlex cannot split all count as "may be
inside" — the guard's old answer. Inside-ness is judged lexically (after `..`
collapsing) AND through the filesystem (`samefile` on every existing ancestor),
so a symlink into the project or a case variant on a case-insensitive disk is
still inside.
"""
from __future__ import annotations

import os
import posixpath
import re
import shlex
import sys

_TASK_DIR = re.compile(r"\.agent(/[^/]+)?/tasks/")
_MSYS_DRIVE = re.compile(r"^/([A-Za-z])(/|$)")
_WIN_DRIVE = re.compile(r"^[A-Za-z]:/")


def _canon(path: str) -> str:
    """Lexical form: forward slashes, `..` collapsed; on Windows the Git-Bash
    `/c/…` spelling becomes `c:/…` and case is folded (the disk ignores it)."""
    p = path.replace("\\", "/")
    if os.name == "nt":
        m = _MSYS_DRIVE.match(p)
        if m:
            p = f"{m.group(1)}:/{p[3:]}"
        p = p.lower()
    return posixpath.normpath(p)


def _is_absolute(token: str) -> bool:
    t = token.replace("\\", "/")
    return t.startswith("/") or bool(_WIN_DRIVE.match(t))


def _lexically_inside(path: str, project: str) -> bool:
    p, root = _canon(path), _canon(project)
    return p == root or p.startswith(root.rstrip("/") + "/")


def _physically_inside(path: str, project: str) -> bool:
    fs_path = _canon(path) if os.name == "nt" else path
    fs_proj = _canon(project) if os.name == "nt" else project
    probe = fs_path
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return False
        probe = parent
    while True:
        try:
            if os.path.samefile(probe, fs_proj):
                return True
        except OSError:
            pass
        parent = os.path.dirname(probe)
        if parent == probe:
            return False
        probe = parent


def may_be_inside(command: str, project: str) -> bool:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return True
    targets = [t for t in tokens if _TASK_DIR.search(t.replace("\\", "/"))]
    if not targets:
        # The hook's trigger matched text shlex does not return as one token
        # (e.g. spread across quoting) — cannot judge, keep the guard.
        return True
    for t in targets:
        if "$" in t or "`" in t or t.startswith("~") or not _is_absolute(t):
            return True
        if _lexically_inside(t, project) or _physically_inside(t, project):
            return True
    return False


def main() -> int:
    command = os.environ.get("PB_CMD", "")
    project = os.environ.get("PB_PROJECT", "")
    if not command or not project:
        return 1
    return 1 if may_be_inside(command, project) else 0


if __name__ == "__main__":
    sys.exit(main())
