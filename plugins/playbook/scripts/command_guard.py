#!/usr/bin/env python3
"""command_guard — the destructive/outward-command interlock (the missing
deterministic safety layer).

The sandbox contains filesystem blast radius; the code-edit gate stops untracked
code; the close contract catches under-leveling at close. The remaining hole is a
DANGEROUS or IRREVERSIBLE shell command running before any of those can help —
`rm -rf /`, `git push --force`, `curl | sh`, a DB `DROP`. This classifies the
Bash command a PreToolUse hook is about to run and BLOCKS the unambiguous
high-blast ones until they are explicitly acknowledged.

Design for "safe without new problems":
  * CONSERVATIVE — only high-confidence, unambiguous patterns, matched at a
    command position (so `echo "rm -rf /"` / `grep "DROP TABLE"` do NOT trip it),
    with narrow scope (a relative `rm -rf ./build` is fine; only dangerous
    targets flag; `--force-with-lease` is allowed, only `--force`/`-f` flags).
  * FAIL-OPEN on any internal error (a broken guard must never wedge a session);
    FAIL-CLOSED on a match (block until acknowledged).
  * ACKNOWLEDGE path: `PLAYBOOK_ALLOW_DANGEROUS=1` lets a human-confirmed command
    through; config `command_guard: false` disables the guard; config
    `dangerous_commands: [regex,...]` adds project-specific patterns.

The threat model is the AGENT'S MISTAKE (running something dangerous it didn't
weigh), not an adversary — so a deliberate ack is enough; the point is that no
dangerous command runs by accident.

`classify_command` is a pure function, spec'd by the fixtures in
tests/test_command_guard.py — a decision fixture set (dangerous MUST block, safe
lookalikes MUST allow). Stdlib only. Dev/enforcement path.
"""
from __future__ import annotations

import json
import os
import re
import sys

# Statement separators: each becomes its own command position. Single `|` too,
# so `foo | rm -rf /` still sees `rm` at a command position.
_SEP = re.compile(r"&&|\|\||[;\n|]")
# Prefixes that delegate to the command that follows (the real command is next).
_PREFIX = re.compile(r"^(sudo|env|nohup|time|command|builtin|exec|then|do|else)\b"
                     r"|^\w+=\S*")  # also strip a leading VAR=value assignment


def _strip_prefixes(seg: str) -> str:
    s = seg.strip()
    while True:
        m = _PREFIX.match(s)
        if not m:
            return s
        s = s[m.end():].strip()


def _rm_is_dangerous(seg: str) -> bool:
    """`rm` recursive+force against a DANGEROUS target. A relative subdir
    (`./build`, `node_modules`) is NOT dangerous; `/`, `~`, `$HOME`, `*`, `..`,
    or any absolute path is."""
    if not re.match(r"rm\b", seg):
        return False
    toks = seg.split()
    flags = "".join(t[1:] for t in toks if t.startswith("-") and not t.startswith("--"))
    longs = [t for t in toks if t.startswith("--")]
    recursive = "r" in flags or "R" in flags or "--recursive" in longs
    force = "f" in flags or "--force" in longs
    if not (recursive and force):
        return False
    targets = [t for t in toks[1:] if not t.startswith("-")]
    for t in targets:
        if (t in ("/", "/*", "~", "..") or t.startswith("/") or t.startswith("~")
                or t.startswith("$HOME") or t.startswith("$") and "HOME" in t
                or "*" in t or t.startswith("..")):
            return True
    return False


def _segment_checks(seg: str):
    """Command-position checks on one prefix-stripped segment → (name, why) or None."""
    if _rm_is_dangerous(seg):
        return ("rm-rf-dangerous-target", "recursive force-delete of a dangerous path")
    if re.match(r"git\s+push\b", seg) and re.search(r"(?:^|\s)(--force|-f)\b", seg) \
            and "--force-with-lease" not in seg:
        return ("git-push-force", "force-push overwrites remote history irreversibly")
    if re.match(r"git\s+reset\b", seg) and re.search(r"(?:^|\s)--hard\b", seg):
        return ("git-reset-hard", "discards uncommitted work irrecoverably")
    if re.match(r"git\s+clean\b", seg) and re.search(r"-[a-zA-Z]*f", seg) \
            and re.search(r"-[a-zA-Z]*d", seg):
        return ("git-clean-force", "deletes untracked files/dirs irrecoverably")
    if re.match(r"dd\b", seg) and re.search(r"of=/dev/", seg):
        return ("dd-to-device", "writes raw to a device — destroys it")
    if re.match(r"mkfs", seg):
        return ("mkfs", "formats a filesystem — destroys its contents")
    if re.search(r">\s*/dev/(sd|nvme|disk|hd)", seg):
        return ("redirect-to-device", "overwrites a raw device")
    return None


# Whole-command patterns (context spans segments): pipe-to-shell, and SQL that is
# clearly issued to a DB client (so `grep "DROP TABLE"` is NOT flagged).
_WHOLE = [
    ("pipe-to-shell",
     re.compile(r"\b(curl|wget|fetch)\b[^|]*\|\s*(sudo\s+)?(sh|bash|zsh|ksh|python3?|perl|ruby)\b", re.I),
     "piping a downloaded script straight into a shell runs unreviewed remote code"),
    ("sql-destructive",
     re.compile(r"\b(psql|mysql|mariadb|sqlite3?|mongo(?:sh)?|clickhouse|cockroach)\b"
                r".*\b(drop\s+(database|table|schema)|truncate\b|delete\s+from)\b", re.I),
     "a DB drop/truncate/delete issued to a client is irreversible"),
]


_SHELL_C = re.compile(r"^(?:sh|bash|zsh|ksh|dash)\b[^;]*?\s-[a-z]*c\s+(.+)$")


def _unwrap_shell_c(seg: str) -> "str | None":
    """`bash -lc "rm -rf /"` → `rm -rf /`. Codex wraps exec in `bash -lc <script>`,
    which would otherwise hide the real command behind the interpreter token."""
    m = _SHELL_C.match(seg.strip())
    if not m:
        return None
    inner = m.group(1).strip()
    if len(inner) >= 2 and inner[0] in "\"'" and inner[-1] == inner[0]:
        inner = inner[1:-1]
    return inner


def classify_command(command, extra_patterns=None, _depth=0):
    """Return ("block", name, why) or ("allow", None, None). Pure + deterministic.

    `command` may be a str, or a list of argv tokens (Codex `exec_command`), in
    which case the joined form AND each element are checked."""
    if isinstance(command, (list, tuple)):
        for part in list(command) + [" ".join(str(p) for p in command)]:
            v = classify_command(part, extra_patterns, _depth)
            if v[0] == "block":
                return v
        return ("allow", None, None)
    if not command or not str(command).strip():
        return ("allow", None, None)
    command = str(command)
    for seg in _SEP.split(command):
        stripped = _strip_prefixes(seg)
        hit = _segment_checks(stripped)
        if hit:
            return ("block", hit[0], hit[1])
        inner = _unwrap_shell_c(stripped)
        if inner and _depth < 3:                       # unwrap `bash -lc "<script>"`
            v = classify_command(inner, extra_patterns, _depth + 1)
            if v[0] == "block":
                return v
    for name, rx, why in _WHOLE:
        if rx.search(command):
            return ("block", name, why)
    for pat in (extra_patterns or []):
        try:
            if re.search(pat, command, re.I):
                return ("block", "project-dangerous", f"matches project pattern {pat!r}")
        except re.error:
            continue
    return ("allow", None, None)


# ── hook body ─────────────────────────────────────────────────────────────────

def _find_root():
    d = os.getcwd()
    while True:
        if os.path.isdir(os.path.join(d, ".agent", "tasks")):
            return d
        if os.path.isdir(os.path.join(d, ".agent")):
            for sub in os.listdir(os.path.join(d, ".agent")):
                if os.path.isdir(os.path.join(d, ".agent", sub, "tasks")):
                    return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _load_cfg(root):
    if not root:
        return {}
    try:
        with open(os.path.join(root, ".agent", "config.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def main() -> int:
    # FAIL-OPEN: any failure to read/parse must allow (never wedge a session).
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    # Bash/Shell/run_terminal_command = Claude + grok (post-normalize);
    # exec_command = Codex's shell tool.
    if payload.get("tool_name") not in ("Bash", "Shell", "run_terminal_command", "exec_command"):
        return 0
    ti = payload.get("tool_input") or {}
    command = ti.get("command", ti.get("cmd", ""))     # codex exec may use either
    if not isinstance(command, (str, list)):
        return 0

    root = _find_root()
    cfg = _load_cfg(root)
    if cfg.get("command_guard") is False:
        return 0
    if os.environ.get("PLAYBOOK_ALLOW_DANGEROUS"):     # human-acknowledged
        return 0

    extra = cfg.get("dangerous_commands")
    extra = extra if isinstance(extra, list) else []
    try:
        verdict, name, why = classify_command(command, extra)
    except Exception:
        return 0                                        # fail-open on any bug
    if verdict != "block":
        return 0

    shown = command if isinstance(command, str) else " ".join(str(p) for p in command)
    sys.stderr.write(
        f"BLOCKED — destructive/irreversible command ({name}): {why}.\n"
        f"  command: {shown.strip()[:200]}\n"
        "  If this is intended: confirm with the user, and run it inside a task\n"
        "  classified `## Risk: irreversible` with a rollback plan. To proceed on\n"
        "  a one-off you've confirmed, re-run with PLAYBOOK_ALLOW_DANGEROUS=1.\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
