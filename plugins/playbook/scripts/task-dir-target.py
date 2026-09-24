#!/usr/bin/env python3
"""Does a shell command name a task directory that is, or may be, inside the project?

task-gate-hook's "don't create task directories manually" guard (Guard 2) calls
this only after its own trigger regex has matched, to decide whether the match
is a real task directory or a fixture elsewhere (task 080, S1d — the 073 flag
C15: `<tmp>/.agent/tasks/001-x` in a temp dir was refused).

Usage:   printf '%s' <command> | PB_PROJECT=<project root> python3 task-dir-target.py
Exit 0   every `.agent[/<lane>]/tasks/` token is an absolute path outside the project
Exit 1   some token is, or may be, inside it — the hook blocks
Other    (a crash) — the hook blocks too; this script can only NARROW the guard

Only a SIMPLE command is judged at all: its first word is `mkdir`, it has no
shell operator (`; && || | & ( ) < >`), and it contains no `$`, backtick, glob,
brace or newline anywhere — the filesystem is read BEFORE the command runs, so an
earlier step (`ln -s …;`, `cd … &&`, `$(…)`) could repoint what was judged.
Within it, only a literal absolute path is ever judged "outside"; a relative
path, `~`, or a command shlex cannot split all count as "may be inside" — the
guard's old answer. Tokens are matched after collapsing `..` and `//`. On Windows,
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

_TASK_DIR = re.compile(r"\.agent(/[^/]+)?/tasks(/|$)")
# case-folded twin: `.AGENT/TASKS` is the live tree on a case-insensitive disk
_TASK_DIR_ANYCASE = re.compile(_TASK_DIR.pattern, re.I)
# a word the shell still rewrites at run time: a variable, a backtick, or a glob
# (kept strict as in round 1, although a glob cannot create a new path)
_UNRESOLVED = re.compile(r"\$[A-Za-z_{(0-9]|`|[?*\[]")
_NAME_HINT = re.compile(r"\.ag|tasks", re.I)
_MSYS_DRIVE = re.compile(r"^/([A-Za-z])(/|$)")
_WIN_DRIVE = re.compile(r"^[A-Za-z]:/")
_SHELL_EXPANDS = set("$`*?[{")
_OPERATORS = set(";&|()<>")


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


def _collapsed(token: str) -> str:
    """`..` and `//` collapsed, so `.agent/x/../tasks` and `.agent//tasks` are seen."""
    return posixpath.normpath(token.replace("\\", "/"))


_ANSI_C_QUOTE = re.compile(r"\$'((?:[^'\\]|\\.)*)'", re.S)
_BRACE_CAP = 512          # more spellings than this → judged strictly (blocks)


def _decode_ansi_c(command: str) -> "str | None":
    """Replace every `$'…'` with what bash makes of it (`$'\\x73'` → `s`).
    None when the decoder is unavailable — the caller then fails closed."""
    if "$'" not in command:
        return command
    try:
        from command_guard import _ansi_c_decode
    except Exception:
        return None
    return _ANSI_C_QUOTE.sub(lambda m: _ansi_c_decode(m.group(1)), command)


def _brace_expand(text: str) -> "list[str] | None":
    """Every spelling bash's brace expansion can produce: comma lists (nested)
    and `{a..z}` / `{1..9}` sequences. None past _BRACE_CAP (fail closed)."""
    out, work = [], [text]
    while work:
        t = work.pop()
        m = None
        depth, start = 0, -1
        for k, ch in enumerate(t):                # expand the FIRST complete,
            if ch == "{":                         # expandable top-level group
                if depth == 0:
                    start = k
                depth += 1
            elif ch == "}" and depth:
                depth -= 1
                if depth == 0 and _expandable(t[start + 1:k]):
                    m = (start, k)
                    break
        if m is None:
            out.append(t)
        else:
            s, e = m
            head, inner, tail = t[:s], t[s + 1:e], t[e + 1:]
            seq = _SEQ.fullmatch(inner)
            if seq and "," not in _top_level(inner):
                a, b = seq.group(1), seq.group(2)
                if a.lstrip("-").isdigit() and b.lstrip("-").isdigit():
                    lo, hi = sorted((int(a), int(b)))
                    if hi - lo > _BRACE_CAP:
                        return None
                    alts = [str(v) for v in range(lo, hi + 1)]
                elif len(a) == 1 and len(b) == 1:
                    lo, hi = sorted((ord(a), ord(b)))
                    alts = [chr(v) for v in range(lo, hi + 1)]
                else:
                    alts = [inner]
            else:
                alts = _split_top_level(inner)
            work.extend(head + alt + tail for alt in alts)
        if len(out) + len(work) > _BRACE_CAP:
            return None
    return out


_SEQ = re.compile(r"(-?\w+)\.\.(-?\w+)(?:\.\.-?\d+)?")


def _expandable(inner: str) -> bool:
    """A `{…}` bash expands: a top-level comma list or an `a..b` sequence
    (`{x}` and `{}` stay literal)."""
    return "," in _top_level(inner) or bool(_SEQ.fullmatch(inner))


def _top_level(inner: str) -> str:
    """`inner` with nested `{…}` groups removed (their commas are not ours)."""
    depth, keep = 0, []
    for ch in inner:
        if ch == "{":
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
        elif depth == 0:
            keep.append(ch)
    return "".join(keep)


def _split_top_level(inner: str) -> "list[str]":
    parts, depth, cur = [], 0, []
    for ch in inner:
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
            continue
        if ch == "{":
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
        cur.append(ch)
    parts.append("".join(cur))
    return parts


def _spellings(command: str) -> "list[str] | None":
    """What the shell can turn this command's words into, loosely: ANSI-C
    decoded, quotes/backslashes/`$` removed, braces expanded. None = cannot
    tell (decoder missing, expansion too large) → the caller fails closed."""
    decoded = _decode_ansi_c(command)
    if decoded is None:
        return None
    stripped = re.sub(r"[\"'\\\\$]", "", decoded)
    # Too many spellings to enumerate: judge the text as written rather than
    # refuse it (post-cap fix, impl panel round 3 — failing closed here refused
    # `touch f{1..1000}` once any `{` reached the helper). The bound: a task
    # dir hidden inside a brace list that large is not seen.
    expanded = _brace_expand(stripped)
    return expanded if expanded is not None else [stripped]


def runs_mkdir(command: str) -> bool:
    """Does any spelling contain the word `mkdir`? (task 085 round 2, T2: the
    hook's own trigger also fires on `$'…'`, which it cannot decode.)"""
    sp = _spellings(command)
    if sp is None:
        return True
    return any(re.search(r"(^|[^\w])mkdir($|[^\w])", s) for s in sp)


def names_a_task_dir(command: str) -> bool:
    """Could this command, once the shell dequotes/unescapes/expands it, name a
    task directory at all? (task 085 G2 + round 2 T2). `.ag'ent/tasks'`,
    `.ag\\ent`, `ta''sks`, `ta$'\\x73'ks`, `ta{sk,zz}s` and `ta{r..t}ks` all read
    as what the shell creates; a glob needs no help (it only matches paths that
    already exist, so it cannot create a new task dir). Loose on purpose: a
    match only sends the command to the strict path below."""
    sp = _spellings(command)
    if sp is None:
        return True
    # Round 3 (U1, U7): a WORD of some spelling must be a task-dir path — the
    # real pattern after `..`/`//` collapse, case-folded (a case-insensitive disk
    # maps `.AGENT/TASKS` onto the live tree). Two loose substrings blocked
    # `.agentic/tasks` and `.agent-stuff/my-tasks`.
    if any(_TASK_DIR_ANYCASE.search(_collapsed(w)) for s in sp for w in s.split()):
        return True
    # Round 3 (U2): `$` was stripped above, so a variable can bridge the name
    # (`.$D/tasks`, `.agent/$T`). A word holding an unresolved variable or a
    # backtick that also SHOWS part of the name is judged strictly (blocks);
    # one with no visible hint (`"$D/build"`, `$A/$B`) stays allowed — the
    # owner's call on unknown variables (2026-09-23).
    decoded = _decode_ansi_c(command) or command
    return any(_UNRESOLVED.search(w) and _NAME_HINT.search(w) for w in decoded.split())


def may_be_inside(command: str, project: str) -> bool:
    if not runs_mkdir(command):
        return False                              # the `$'…'` trigger: no mkdir at all
    if not names_a_task_dir(command):
        return False                              # an ordinary mkdir (task 085 G2)
    # Only a SIMPLE command is ever judged (task 080 round 2). The judgment reads
    # the filesystem BEFORE the command runs, so any earlier step — `ln -s …;`,
    # `cd … &&`, a `$(…)` — could repoint the path it judged. Expansion anywhere,
    # or a newline, likewise means shlex does not see what the shell will run.
    if "\n" in command or "\r" in command or _SHELL_EXPANDS & set(command):
        return True
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return True
    if not tokens or posixpath.basename(tokens[0].replace("\\", "/")) != "mkdir":
        return True
    if any(set(t) <= _OPERATORS for t in tokens):
        return True
    targets = [t for t in tokens if _TASK_DIR_ANYCASE.search(_collapsed(t))]
    if not targets:
        # The hook's trigger matched text shlex does not return as one token
        # (e.g. spread across quoting) — cannot judge, keep the guard.
        return True
    for t in targets:
        if t.startswith("~") or not _is_absolute(t):
            return True
        if _lexically_inside(t, project) or _physically_inside(t, project):
            return True
    return False


def main() -> int:
    # The command arrives on STDIN, never in the environment: Git Bash (MSYS)
    # rewrites an env value that starts with `/` into a Windows path before a
    # native python sees it (`/bin/mkdir …` became `C:/Program Files/…`) — CI
    # windows lane, task 080. PB_PROJECT may be rewritten; _canon accepts both.
    command = sys.stdin.read()
    project = os.environ.get("PB_PROJECT", "")
    if not command or not project:
        return 1
    return 1 if may_be_inside(command, project) else 0


if __name__ == "__main__":
    sys.exit(main())
