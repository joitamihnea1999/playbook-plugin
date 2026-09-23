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
`$VAR`, a backtick, a glob or brace the shell would expand, or a command shlex
cannot split all count as "may be inside" — the guard's old answer. On Windows,
only a drive-letter or Git-Bash `/c/…` spelling can be judged: any other
leading-`/` path is an MSYS mount (`/tmp`) that Python cannot resolve.

A token is inside when ANY of these says so (so no single view can let it out):
its `..`-collapsed spelling under the project's spelling or its realpath; the
realpath of the raw token (kernel `..` semantics) or of the collapsed one under
the project's realpath (symlinks into any subdirectory, `/var`→`/private/var`);
or `samefile` between the project and an existing ancestor of either realpath
(case variants on a case-insensitive disk).
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
_SHELL_EXPANDS = set("$`*?[{")


def _canon(path: str) -> str:
    """Lexical form: forward slashes and `..` collapsed FIRST; then, on Windows,
    the Git-Bash `/c/…` spelling becomes `c:/…` and case is folded."""
    p = posixpath.normpath(path.replace("\\", "/"))
    if os.name == "nt":
        m = _MSYS_DRIVE.match(p)
        if m:
            p = f"{m.group(1)}:/{p[3:]}"
        p = p.lower()
    return p


def _is_absolute(token: str) -> bool:
    t = token.replace("\\", "/")
    if os.name == "nt":
        return bool(_WIN_DRIVE.match(t) or _MSYS_DRIVE.match(t))
    return t.startswith("/")


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _lexically_inside(path: str, project: str) -> bool:
    return _under(_canon(path), _canon(project))


def _fs(path: str) -> str:
    """A spelling the OS can open (Windows needs `c:/…`, not `/c/…`)."""
    return _canon(path) if os.name == "nt" else path


def _existing_ancestor(path: str) -> str | None:
    probe = path
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return None
        probe = parent
    return probe


def _physically_inside(path: str, project: str) -> bool:
    proj_real = _canon(os.path.realpath(_fs(project)))
    candidates = {os.path.realpath(_fs(path)),
                  os.path.realpath(_fs(posixpath.normpath(path.replace("\\", "/"))))}
    for cand in candidates:
        if _under(_canon(cand), proj_real):
            return True
        probe = _existing_ancestor(cand)
        while probe is not None:
            try:
                if os.path.samefile(probe, _fs(project)):
                    return True
            except OSError:
                pass
            parent = os.path.dirname(probe)
            probe = None if parent == probe else parent
    return False


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
        if _SHELL_EXPANDS & set(t) or t.startswith("~") or not _is_absolute(t):
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
