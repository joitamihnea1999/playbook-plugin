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
  * ACKNOWLEDGE path: `PLAYBOOK_ALLOW_DANGEROUS=1` IN THE HOOK'S OWN ENVIRONMENT
    (the operator's shell / harness env, not a command-line prefix) lets a human-confirmed command
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
     # Either order (task 073 round 2: a statement echoed INTO the client from the
     # left never matched before), spanning lines.
     re.compile(r"(?:\b(?:psql|mysql|mariadb|sqlite3?|mongo(?:sh)?|clickhouse|cockroach)\b.*\b(?:drop\s+(?:database|table|schema)|truncate\b|delete\s+from)\b)"
                r"|(?:\b(?:drop\s+(?:database|table|schema)|truncate\b|delete\s+from)\b.*\b(?:psql|mysql|mariadb|sqlite3?|mongo(?:sh)?|clickhouse|cockroach)\b)", re.I | re.S),
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


# Task 073 data-region masking, tightened by the impl panel (round 1):
#  * only a DATA SINK's heredoc is inert — `cat`/`tee` (optionally redirected)
#    with `<<TAG` as the LAST thing on the line; `bash <<EOF`, `sh`, `psql`,
#    `python3 -`, `ssh host` … RUN their body and keep it;
#  * an UNQUOTED heredoc expands `$(…)`, backticks and `${…}` — kept when the
#    body has any;
#  * an unterminated heredoc masks nothing;
#  * echo/printf arguments are masked only when they carry no expansion.
# A redirect target must be a PLAIN PATH — `>(sh)` is a process substitution
# that executes the "data" (impl-panel round 2).
_PATH = r"[^\s()<>|&;`$]+"
_HEREDOC_SINK = re.compile(
    r"^\s*(?:cat|tee)\b[^|;&<>()`$]*(?:>{1,2}\s*" + _PATH + r"\s*)?<<-?\s*(?P<q>['\"]?)(?P<tag>[A-Za-z_][A-Za-z0-9_]*)(?P=q)\s*(?:>{1,2}\s*" + _PATH + r"\s*)?$")
_EXPANSION = re.compile(r"\$\(|`|\$\{|<\(|>\(")
# An echo/printf line is inert only when it is a single simple command redirected
# to a plain file with NO pipe, no expansion and no process substitution anywhere
# on the line — a quote-blind matcher cannot tell `"a | sh"` from ` | sh`, so any
# `|` on the line keeps the text (impl-panel round 2: `echo '… | sh' | bash`).
_DATA_LINE = re.compile(r"^\s*(?:echo|printf)\b[^|<>()`$]*>{1,2}\s*" + _PATH + r"\s*$")


def _strip_data_regions(command):
    """Return the command text with inert DATA removed, for the WHOLE-command
    rules only (the segment rules are line-split and never masked — a heredoc
    body line `rm -rf /` still blocks, conservatively): a `cat`/`tee` heredoc
    body written to a plain file (quoted tag, or no expansion inside) and an
    echo/printf line redirected to a plain file with no pipe/expansion on it.
    Everything that could run stays."""
    lines = str(command).split("\n")
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        m = _HEREDOC_SINK.match(line)
        if not m:
            continue
        tag, quoted = m.group("tag"), bool(m.group("q"))
        j = i
        while j < len(lines) and lines[j].strip() != tag:
            j += 1
        if j >= len(lines):
            continue                                   # unterminated → keep all
        if not quoted and _EXPANSION.search("\n".join(lines[i:j])):
            continue                                   # expansions would run
        i = j + 1                                      # drop body + closing tag

    return "\n".join("echo > file" if _DATA_LINE.match(l) else l for l in out)


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
    # Task 073 (B0): the whole-command patterns must not fire on DATA — a heredoc
    # body or an echo/printf string being written to a file is text about a
    # command, not the command (the documented bound: echoing dangerous text is
    # fine). Segment checks above already ran on the full text.
    whole_text = _strip_data_regions(command)
    for name, rx, why in _WHOLE:
        if rx.search(whole_text):
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


def _load_journal():
    """Load the shared enforcement-journal helper (sibling file). Fail-open:
    None on any error so journalling can never wedge the guard."""
    try:
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pb_journal.py")
        spec = importlib.util.spec_from_file_location("_pb_journal", path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def _normalize_payload(payload):
    """Apply the provider-dialect normalizer in-process.

    The wrapper used to pipe stdin through hook-payload-normalize.py and then
    into this script — two interpreter starts on every shell tool call. Doing it
    here makes the guard one process. FAIL-OPEN on any failure: the caller falls
    back to the raw payload, which is what the pre-normalizer guard read anyway
    (Shell / run_terminal_command are already recognised by name below; the
    normalizer's job here is grok's camelCase toolName/toolInput).
    """
    try:
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "hook-payload-normalize.py")
        spec = importlib.util.spec_from_file_location("_pb_hook_norm", path)
        if spec is None or spec.loader is None:
            return payload
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.normalize(payload)
    except Exception:
        return payload


def _active_task_is_irreversible(root):
    """True iff this session's ACTIVE task (lane/sessions/<sid>/current_state →
    lane/tasks/<N>-*/task.md) carries a live `## Risk` of `irreversible`, read
    with the CLI's fence-aware reader. Any failure → False (the block stands)."""
    try:
        if not root:
            return False
        sid = os.environ.get("PLAYBOOK_SESSION_ID", "").strip()
        if not sid or "/" in sid or "\\" in sid or ".." in sid:
            return False
        j = _load_journal()
        lane = j.resolve_lane_dir(root) if j is not None else None
        lane = str(lane) if lane else os.path.join(root, ".agent")
        with open(os.path.join(lane, "sessions", sid, "current_state"), encoding="utf-8") as fh:
            num = fh.read().strip()
        if not num.isdigit():
            return False
        tasks_dir = os.path.join(lane, "tasks")
        cands = sorted(d for d in os.listdir(tasks_dir) if d.startswith(num + "-"))
        if not cands:
            return False
        task_md = os.path.join(tasks_dir, cands[0], "task.md")
        plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if plugin_dir not in sys.path:
            sys.path.insert(0, plugin_dir)
        from tasks.core import extract_risk, _status_from_lines, _physical_lines   # fence-aware readers
        from pathlib import Path as _P
        _text = _P(task_md).read_text(encoding="utf-8", errors="replace")
        if str(_status_from_lines(_physical_lines(_text))).strip().lower() != "in_progress":
            return False        # a done/blocked/pending task in a stale pointer never acknowledges (round 2)
        return str(extract_risk(_P(task_md))).strip().lower() == "irreversible"
    except Exception:
        return False


def main() -> int:
    # FAIL-OPEN: any failure to read/parse must allow (never wedge a session).
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    payload = _normalize_payload(payload)
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
    if os.environ.get("PLAYBOOK_ALLOW_DANGEROUS", "").strip().lower() in ("1", "true", "yes", "on"):
        return 0                                        # operator-acknowledged (round 2: `=0` is not an ack)

    extra = cfg.get("dangerous_commands")
    extra = extra if isinstance(extra, list) else []
    try:
        verdict, name, why = classify_command(command, extra)
    except Exception:
        return 0                                        # fail-open on any bug
    if verdict != "block":
        return 0

    shown = command if isinstance(command, str) else " ".join(str(p) for p in command)

    if _active_task_is_irreversible(root):
        # The documented in-session acknowledgement (task 073, impl panel: it was
        # a claim without code until now): the ACTIVE task is classified
        # `## Risk: irreversible`, read fence-aware through tasks.core. Logged.
        try:
            j = _load_journal()
            if j is not None:
                j.append(j.resolve_lane_dir(root), "command-guard", "allow",
                         f"ack-irreversible-task:{name or 'dangerous-command'}",
                         session_id=os.environ.get("PLAYBOOK_SESSION_ID", ""),
                         tool=payload.get("tool_name", ""), command=shown)
        except Exception:
            pass
        return 0

    # Enforcement-journal (log-only, best-effort): record the block. Wrapped AND
    # the helper itself swallows errors — journalling can never change the block.
    try:
        j = _load_journal()
        if j is not None:
            j.append(j.resolve_lane_dir(root), "command-guard", "block",
                     name or "dangerous-command",
                     session_id=os.environ.get("PLAYBOOK_SESSION_ID", ""),
                     tool=payload.get("tool_name", ""), command=shown)
    except Exception:
        pass

    sys.stderr.write(block_message(shown, name, why))
    return 2


def block_message(shown, name, why):
    """The block text. Task 073 (B1): the old text said "re-run with
    PLAYBOOK_ALLOW_DANGEROUS=1" — impossible from inside the agent session, the
    hook reads ITS OWN environment (a prefix on the command line cannot set it).
    Say where the acknowledgement actually has to happen."""
    return (
        f"BLOCKED — destructive/irreversible command ({name}): {why}.\n"
        f"  command: {str(shown).strip()[:200]}\n"
        "  If this is intended: confirm with the user and run it inside a task\n"
        "  classified `## Risk: irreversible` with a rollback plan (the interlock\n"
        "  stands down for that task). A one-off acknowledgement is the OPERATOR's:\n"
        "  PLAYBOOK_ALLOW_DANGEROUS=1 must be present in the environment the hook\n"
        "  runs in (the shell that started the agent, or the harness env settings) —\n"
        "  the hook reads its own environment, so a prefix on this command cannot\n"
        "  set it.\n")


if __name__ == "__main__":
    sys.exit(main())
