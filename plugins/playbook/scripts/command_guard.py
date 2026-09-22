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
  * A COMMAND POSITION IS FOUND THROUGH WRAPPERS AND THEIR OPTIONS (task 077).
    `sudo -u root rm -rf /`, `timeout 5 rm -rf /`, `env -S 'rm -rf /'` and
    `curl … | nice -n 19 bash` all reach the same rules as their bare forms,
    because `_WRAPPERS` + `_walk_prefix` know each wrapper's option arity. Two
    things that walk deliberately does NOT do: it never skips a token that is not
    a known option or a known option's value (scanning past the command is how a
    guard starts blocking `sudo grep -rn "rm -rf /" /etc`), and it treats a
    wrapper's terminal/query mode as non-executing (`sudo --version rm -rf /`
    prints a version and deletes nothing). An unknown option that takes a value
    leaves that value at the head and the segment reads as safe — the guard
    under-blocks there, which is the correct direction to fail for a layer whose
    false positives a user cannot route around.

RECORDED DECISION (task 077, owner-reversible) — a GENERIC pipe into a shell,
`cat evil.sh | sh`, is NOT blocked; the pipe rule stays downloader-specific.
`curl … | sh` is unambiguous because the code is remote and unreviewed; a LOCAL
file may be anything, the guard cannot see inside it, and blocking that shape
trades a real bypass for a routine false positive. The heredoc half is already
covered — segment rules are line-split and never masked, so a heredoc body line
`rm -rf /` blocks on its own. A project that wants the stricter rule turns it on
without a release via `dangerous_commands` (the regex is in docs/configuration.md).

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
import shlex
import sys

# Statement separators: each becomes its own command position. Single `|` too,
# so `foo | rm -rf /` still sees `rm` at a command position.
_SEP = re.compile(r"&&|\|\||[;\n|&]")   # a single `&` backgrounds and separates
# ── the wrapper grammar (task 077) ────────────────────────────────────────────
# A WRAPPER delegates to the command that follows it. Stripping only the bare
# wrapper token left every optioned form unguarded — `sudo rm -rf /` blocked while
# `sudo -u root rm -rf /` ran — because the option token then sat where the
# command should be and every rule here anchors at position 0. So each wrapper
# declares the little grammar it actually has:
#
#   val_short — short options whose value is the NEXT token, but only when the
#               option ends its cluster with nothing attached: `-n 19` takes
#               `19`; `-n19`, `-o0` and `--long=v` carry their own value and take
#               nothing (eating the next token there would swallow the command).
#   val_long  — long options that take a value (`--signal KILL` / `--signal=KILL`).
#   terminal  — query/help modes: the wrapper PRINTS and runs nothing, so the rest
#               of the segment is not a command. `sudo --version rm -rf /` deletes
#               nothing, and a guard that blocked it would be wrong.
#   operands  — leading non-option operands consumed before the command
#               (`timeout 5 …`, `chrt 10 …`). `--` ends OPTIONS, not operands.
#   split     — options whose value IS a command line and must be classified
#               rather than consumed (`env -S 'rm -rf /'`).
#
# Everything unknown is left alone: an unrecognised option that takes a value
# leaves that value at the head and the segment reads as safe. That is the same
# under-blocking the guard already had, and it is the right direction to fail for
# a layer whose false positives a user cannot route around.


def _wspec(val_short="", val_long=(), terminal=(), operands=0,
           split_short="", split_long=()):
    return {
        "val_short": set(val_short),
        "val_long": set(val_long),
        "terminal": set(terminal),
        "operands": operands,
        "split_short": set(split_short),
        "split_long": set(split_long),
    }


_WRAPPERS = {
    # privilege
    "sudo": _wspec(
        val_short="ugUCprtTRDh",
        val_long=("--user", "--group", "--other-user", "--close-from", "--prompt",
                  "--role", "--type", "--chroot", "--chdir", "--host",
                  "--command-timeout"),
        # `-h` is NOT terminal: sudo takes `-h host`, and treating it as a
        # query mode made `sudo -h localhost rm -rf /` read as non-executing
        # (impl panel round 1, grok #1). `--help` alone is the query form.
        terminal=("-V", "--version", "--help", "-l", "--list",
                  "-v", "--validate", "-K", "--remove-timestamp")),
    "doas": _wspec(val_short="uC", val_long=(),
                   terminal=("-L", "-V", "-h", "--help", "--version")),
    # environment / scheduling / buffering
    # `--block-signal`/`--default-signal`/`--ignore-signal` take an OPTIONAL
    # value that is only ever attached with `=`, so modelling them as
    # value-taking swallowed the command (impl panel round 1, sol:high #3).
    "env": _wspec(val_short="uC",
                  val_long=("--unset", "--chdir"),
                  terminal=("--help", "--version"),
                  split_short="S", split_long=("--split-string",)),
    "nice": _wspec(val_short="n", val_long=("--adjustment",),
                   terminal=("--help", "--version")),
    "ionice": _wspec(val_short="cnp", val_long=("--class", "--classdata", "--pid"),
                     terminal=("-h", "--help", "-V", "--version")),
    "chrt": _wspec(val_short="p", operands=1,
                   terminal=("-h", "--help", "-V", "--version")),
    "stdbuf": _wspec(val_short="ioe",
                     val_long=("--input", "--output", "--error"),
                     terminal=("--help", "--version")),
    "timeout": _wspec(val_short="ks", val_long=("--kill-after", "--signal"),
                      operands=1, terminal=("--help", "--version")),
    "setsid": _wspec(terminal=("-h", "--help", "-V", "--version")),
    "nohup": _wspec(terminal=("--help", "--version")),
    "time": _wspec(val_short="fo", val_long=("--format", "--output"),
                   terminal=("-V", "--version", "--help")),
    "xargs": _wspec(val_short="nPIidaEeLsD",
                    val_long=("--max-args", "--max-procs", "--replace",
                              "--delimiter", "--arg-file", "--eof",
                              "--max-chars", "--max-lines"),
                    terminal=("--help", "--version")),
    # shell builtins that delegate
    "command": _wspec(terminal=("-v", "-V")),
    "builtin": _wspec(),
    # `eval` delegates to a STRING, so its remainder is classified, not walked.
    "eval": _wspec(),
    "exec": _wspec(val_short="a", val_long=()),
    # control-flow keywords that can precede a command in a split segment
    "then": _wspec(),
    "do": _wspec(),
    "else": _wspec(),
    "elif": _wspec(),
}

# ── lexing and naming (impl panel round 1) ────────────────────────────────────
# Round 1 shipped a whitespace tokenizer and ONE normalisation site. The panel
# produced 31 vectors that walked through, and every one of them was really two
# defects: quoting is invisible to a whitespace split (`sudo -p 'Password: ' rm
# -rf /` puts `rm` where an option value was expected), and a command can be
# NAMED many ways (`'rm'`, `"/bin/rm"`, `r\m`, `~/rm`, `$HOME/bin/rm`) while the
# rules all anchor on the bare word. So: one lexer, one naming function, used
# everywhere a command position is decided.

def _lex(s):
    """(raw, start, value) per token. Quote- and escape-aware, and FORGIVING —
    an unbalanced quote simply runs to the end rather than raising, because a
    classifier that throws on hostile input fails OPEN."""
    toks = []
    i, n = 0, len(s)
    while i < n:
        while i < n and s[i].isspace():
            i += 1
        if i >= n:
            break
        start = i
        buf = []
        while i < n:
            c = s[i]
            if c.isspace():
                break
            if c == "\\" and i + 1 < n:
                buf.append(s[i + 1])
                i += 2
                continue
            if c in "\"'":
                q = c
                i += 1
                while i < n and s[i] != q:
                    if q == '"' and s[i] == "\\" and i + 1 < n:
                        buf.append(s[i + 1])
                        i += 2
                        continue
                    buf.append(s[i])
                    i += 1
                i += 1                                 # closing quote, or end
                continue
            buf.append(c)
            i += 1
        toks.append((s[start:i], start, "".join(buf)))
    return toks


# A command NAME never contains whitespace. That single rule is what keeps this
# from turning data into commands: `'rm'` is an obfuscated name, `"rm -rf /"` is
# a string, and only the first one is renamed.
_PATH_PREFIX = re.compile(r"^(?:[A-Za-z0-9_.+~$@{}-]*/)+")
_LEAD_GROUP = "({!"
_TRAIL_GROUP = ")};"


def _command_name(value):
    """The bare command a token names, or None when the token is not a name."""
    if not value:
        return None
    v = value.lstrip(_LEAD_GROUP).rstrip(_TRAIL_GROUP)
    if not v or any(ch.isspace() for ch in v):
        return None
    v = _PATH_PREFIX.sub("", v)
    return v or None


# A leading token that groups or negates, rather than naming a command.
_GROUPERS = ("(", "{", "!", "&&", "||")
_ASSIGN = re.compile(r"^\w+=")
_TOKEN = re.compile(r"\S+")
# Termination is guaranteed by every iteration consuming at least one token; this
# is a belt-and-braces ceiling on pathological input, never a scanning budget
# (a cap that stopped the WALK would itself be a bypass — nest N+1 wrappers).
_WALK_CEILING = 10000


def _unquote(s):
    s = s.strip()
    if len(s) >= 2 and s[0] in "\"'" and s[-1] == s[0]:
        return s[1:-1]
    return s


def _walk_prefix(seg):
    """Walk the wrapper/option prefix of one segment.

    Returns `(rest, executes, payloads)`:
      * `rest` — the segment text from the first token that is not part of a
        wrapper prefix, sliced out of the ORIGINAL string so quoting and spacing
        survive for the regex rules;
      * `executes` — False when a terminal/query option means the segment runs
        no command at all;
      * `payloads` — command-line strings found inside option values (`env -S`),
        for the caller to classify recursively.
    """
    s = seg.strip()
    lexed = _lex(s)
    toks = [(value, start) for _raw, start, value in lexed]
    i = 0
    payloads = []
    # Termination needs no ceiling: every path below consumes at least one token,
    # and the token list is finite. A ceiling WOULD be the bypass — nest one more
    # wrapper than the cap and the walk stops short (impl panel round 1, three
    # seats; the round-1 code had one and its test used 500 against a cap of
    # 10 000, so it proved nothing).
    while i < len(toks):
        tok = toks[i][0]
        if tok in _GROUPERS or _ASSIGN.match(tok):
            i += 1                                     # consumes a token → terminates
            if _ASSIGN.match(tok):                     # `x=$(echo hi) rm -rf /`
                depth = tok.count("(") - tok.count(")")
                while depth > 0 and i < len(toks):
                    depth += toks[i][0].count("(") - toks[i][0].count(")")
                    i += 1
            continue
        name = _command_name(tok)
        spec = _WRAPPERS.get(name) if name else None
        if spec is None:
            break                                      # this token is the command
        if name == "eval":                             # delegates to a STRING
            i += 1
            if i < len(toks):
                rest_toks = lexed[i:]
                # one quoted operand → its VALUE is the command line; several
                # tokens → the remainder as written.
                payloads.append(rest_toks[0][2] if len(rest_toks) == 1
                                else s[rest_toks[0][1]:])
            i = len(toks)
            break
        i += 1                                         # the wrapper itself
        end_of_opts = False
        operands = spec["operands"]
        while i < len(toks):
            t = toks[i][0]
            if not end_of_opts and t == "--":
                end_of_opts = True                     # ends OPTIONS, not operands
                i += 1
                continue
            if not end_of_opts and t.startswith("--") and len(t) > 2:
                base = t.split("=", 1)[0]
                if base in spec["terminal"]:
                    return (s, False, payloads)
                if base in spec["split_long"]:
                    if "=" in t:
                        payloads.append(_unquote(s[toks[i][1] + len(base) + 1:]))
                    elif i + 1 < len(toks):
                        payloads.append(_unquote(s[toks[i + 1][1]:]))
                    i = len(toks)
                    continue
                if base in spec["val_long"] and "=" not in t:
                    i += 2
                    continue
                i += 1
                continue
            if not end_of_opts and t.startswith("-") and len(t) > 1:
                if t in spec["terminal"]:
                    return (s, False, payloads)
                letters = t[1:]
                takes_next = False
                for k, ch in enumerate(letters):
                    attached = letters[k + 1:]
                    if ch in spec["split_short"]:
                        if attached:                   # `-S'rm -rf /'`
                            payloads.append(_unquote(s[toks[i][1] + k + 2:]))
                        elif i + 1 < len(toks):        # `-S 'rm -rf /'`
                            payloads.append(_unquote(s[toks[i + 1][1]:]))
                        i = len(toks)
                        takes_next = None              # already advanced
                        break
                    if ch in spec["val_short"]:
                        takes_next = not attached      # `-n 19` yes, `-n19`/`-o0` no
                        break
                if takes_next is None:
                    continue                           # a split option consumed the rest
                i += 1
                if takes_next:
                    i += 1
                continue
            if operands > 0:                           # `timeout 5 …`, `chrt 10 …`
                operands -= 1
                i += 1
                continue
            break                                      # the command starts here
    rest = s[toks[i][1]:] if i < len(toks) else ""
    return (rest, True, payloads)


def _normalize_command_head(seg):
    """`seg` with its command written plainly: quotes, escapes, a leading `(`,
    and a directory prefix removed from the HEAD token only. Everything after it
    is left byte-for-byte, so the rules still see the real arguments."""
    toks = _lex(seg)
    if not toks:
        return seg
    raw, start, value = toks[0]
    name = _command_name(value)
    if name is None or name == raw:
        return seg
    return seg[:start] + name + seg[start + len(raw):]


# `git` is not a wrapper — the VERB carries the meaning — but it has the same
# "options before the verb" shape, and `git -C <dir> push --force` is ordinary
# agent usage (impl panel round 1, opus #2). These are git's global options.
_GIT_VALUE_SHORT = set("Cc")
_GIT_VALUE_LONG = {"--git-dir", "--work-tree", "--namespace", "--exec-path",
                   "--super-prefix", "--config-env"}


def _strip_git_globals(seg):
    toks = _lex(seg)
    if not toks or _command_name(toks[0][2]) != "git":
        return seg
    i = 1
    while i < len(toks):
        t = toks[i][2]
        if t.startswith("--"):
            base = t.split("=", 1)[0]
            if base in _GIT_VALUE_LONG and "=" not in t:
                i += 2
                continue
            if base in _GIT_VALUE_LONG or base in ("--paginate", "--no-pager",
                                                   "--bare", "--literal-pathspecs",
                                                   "--no-replace-objects"):
                i += 1
                continue
            break
        if t.startswith("-") and len(t) > 1:
            letters = t[1:]
            if letters[0] in _GIT_VALUE_SHORT:
                i += 1 if letters[1:] else 2
                continue
            i += 1
            continue
        break
    if i == 1 or i >= len(toks):
        return seg
    return "git " + seg[toks[i][1]:]


def _strip_prefixes(seg):
    """Back-compatible shim: the prefix-stripped text only."""
    return _walk_prefix(seg)[0]


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
     re.compile(r"\b(curl|wget|fetch)\b[^|]*\|\s*(?:(?:sudo|env|command|nice|nohup)(?:\s+-\S+)*\s+)*(sh|bash|zsh|ksh|python3?|perl|ruby)\b", re.I),
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
_DATA_LINE = re.compile(r"^\s*(?:echo|printf)\b[^|;&<>()`$]*>{1,2}\s*" + _PATH + r"\s*$")   # single simple command only (round 3)


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


# The pipe rule had its OWN wrapper list (`sudo|env|command|nice|nohup` with
# flag-only options), so every value-taking form walked past it — `curl … | sudo
# -u root bash` ran while `curl … | sudo bash` blocked. It now walks the same
# `_WRAPPERS` table as the segment rules. The original regex is KEPT: it still
# catches shapes this split does not see (a pipe inside `bash -c '…'`), and two
# independent rules that both block is the right redundancy for this layer.
_DOWNLOADER = re.compile(r"^(?:curl|wget|fetch|aria2c)$")
_INTERPRETERS = {"sh", "bash", "zsh", "ksh", "dash", "ash",
                 "python", "python3", "perl", "ruby", "node"}
_SINGLE_PIPE = re.compile(r"(?<!\|)\|(?!\|)")
_PIPE_WHY = "piping a downloaded script straight into a shell runs unreviewed remote code"


def _pipes_downloader_into_shell(text):
    """True when a downloader's output reaches an interpreter through a pipe,
    however many optioned wrappers sit in between."""
    parts = _SINGLE_PIPE.split(text)
    if len(parts) < 2:
        return False
    seen_downloader = False
    for part in parts:
        # `|&` pipes stdout AND stderr, and a grouped `(bash)` / `{ bash; }` runs
        # the same interpreter — both walked past the first version of this rule.
        part = part.lstrip("&")
        rest, executes, payloads = _walk_prefix(part)
        if not executes:
            continue
        rest = _normalize_command_head(rest)
        lexed = _lex(rest)
        head = _command_name(lexed[0][2]) if lexed else None
        if seen_downloader:
            # `curl … | env -S 'bash'` puts the interpreter inside an option
            # VALUE (impl panel round 1, grok #3).
            for payload in payloads:
                p = _lex(payload)
                if p and _command_name(p[0][2]) in _INTERPRETERS:
                    return True
            if head in _INTERPRETERS:
                return True
        if head and _DOWNLOADER.match(head):
            seen_downloader = True
    return False


def _command_substitutions(text):
    """The contents of every `$( … )` and backtick substitution. Their bodies RUN,
    whatever surrounds them — `echo "$(rm -rf /)"` deletes the disk — so each is
    classified as a command in its own right."""
    out = []
    i, n = 0, len(text)
    while i < n:
        if text.startswith("$(", i):
            # Balance parentheses OUTSIDE quotes: `$(rm -rf /var/lib/app '(')`
            # closed early for a quote-blind counter (impl panel round 1,
            # sol:high #5), which left the real body unclassified.
            depth, j, quote = 1, i + 2, ""
            while j < n and depth:
                c = text[j]
                if quote:
                    if c == "\\" and quote == '"' and j + 1 < n:
                        j += 1
                    elif c == quote:
                        quote = ""
                elif c in "\"'":
                    quote = c
                elif c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                j += 1
            if depth == 0:
                out.append(text[i + 2:j - 1])
            i = j
        elif text[i] == "`":
            j = text.find("`", i + 1)
            if j == -1:
                break
            out.append(text[i + 1:j])
            i = j + 1
        else:
            i += 1
    return out


# A QUOTED heredoc tag means the shell performs NO expansion in the body — no
# `$( )`, no backticks — whatever the sink is. The segment rules still read those
# lines (so an interpreter heredoc carrying `rm -rf /` as a COMMAND still blocks,
# because the interpreter runs it), but the SUBSTITUTION scan must not, or every
# `python3 - <<'PY'` whose script mentions a dangerous command inside a Markdown
# code span gets refused. Measured on this repo's own 73 859 source/doc lines:
# that single distinction accounts for 19 of 27 newly-blocked lines, and it is
# not a heuristic — a quoted tag genuinely suppresses expansion in every shell.
_QUOTED_HEREDOC = re.compile(r"""<<-?\s*(['"])(?P<tag>[A-Za-z_][A-Za-z0-9_]*)\1""")
# ... unless the SINK is itself a shell: `bash <<'EOF'` does not expand the body
# when the outer shell reads it, but bash then runs it and expands it there.
_SHELLS = {"sh", "bash", "zsh", "ksh", "dash", "ash"}


def _strip_quoted_heredoc_bodies(text):
    lines = str(text).split("\n")
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        m = _QUOTED_HEREDOC.search(line)
        if not m:
            continue
        lexed = _lex(line)
        sink = _command_name(lexed[0][2]) if lexed else None
        if sink in _SHELLS:
            continue                                   # the body IS a shell script
        tag = m.group("tag")
        j = i
        while j < len(lines) and lines[j].strip() != tag:
            j += 1
        if j >= len(lines):
            continue                                   # unterminated → keep it all
        i = j + 1                                      # drop the inert body
    return "\n".join(out)


def classify_command(command, extra_patterns=None, _depth=0):
    """Return ("block", name, why) or ("allow", None, None). Pure + deterministic.

    `command` may be a str, or a list of argv tokens (Codex `exec_command`), in
    which case the joined form AND each element are checked."""
    if isinstance(command, (list, tuple)):
        try:
            quoted = " ".join(shlex.quote(str(p)) for p in command)
        except Exception:
            quoted = " ".join(str(p) for p in command)
        for part in list(command) + [" ".join(str(p) for p in command), quoted]:
            v = classify_command(part, extra_patterns, _depth)
            if v[0] == "block":
                return v
        return ("allow", None, None)
    if not command or not str(command).strip():
        return ("allow", None, None)
    command = str(command)
    for seg in _SEP.split(command):
        stripped, executes, payloads = _walk_prefix(seg)
        for payload in payloads:                       # `env -S "<command line>"`
            if payload and _depth < 3:
                v = classify_command(payload, extra_patterns, _depth + 1)
                if v[0] == "block":
                    return v
        if not executes:                               # `sudo --version …` prints, runs nothing
            continue
        normalized = _normalize_command_head(stripped)
        hit = (_segment_checks(stripped)
               or _segment_checks(normalized)
               or _segment_checks(_strip_git_globals(normalized)))
        if hit:
            return ("block", hit[0], hit[1])
        inner = _unwrap_shell_c(stripped) or _unwrap_shell_c(normalized)
        if inner and _depth < 3:                       # unwrap `bash -lc "<script>"`
            v = classify_command(inner, extra_patterns, _depth + 1)
            if v[0] == "block":
                return v
    # Task 073 (B0): the whole-command patterns must not fire on DATA — a heredoc
    # body or an echo/printf string being written to a file is text about a
    # command, not the command (the documented bound: echoing dangerous text is
    # fine). Segment checks above already ran on the full text.
    whole_text = _strip_data_regions(command)
    if _depth < 3:
        # On the MASKED text: a quoted heredoc written to a plain file does not
        # expand, so a fixture ABOUT `$(rm -rf /)` stays data (task 073's promise).
        for sub in _command_substitutions(_strip_quoted_heredoc_bodies(whole_text)):
            if sub.strip():
                v = classify_command(sub, extra_patterns, _depth + 1)
                if v[0] == "block":
                    return v
    if _pipes_downloader_into_shell(whole_text):
        return ("block", "pipe-to-shell", _PIPE_WHY)
    for name, rx, why in _WHOLE:
        if rx.search(whole_text):
            return ("block", name, why)
    for pat in (extra_patterns or []):
        # On the MASKED text, like every built-in whole-command rule: a project
        # pattern must not fire on a heredoc fixture either (impl panel round 1,
        # sonnet #2 — the shipped example dodged it only by its `$` anchor).
        try:
            if re.search(pat, whole_text, re.I):
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
        pointer = os.path.join(lane, "sessions", sid, "current_state")
        with open(pointer, encoding="utf-8") as fh:
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
        from tasks.core import (extract_risk_from_text, _status_from_lines,   # fence-aware readers
                                _physical_lines)
        from pathlib import Path as _P
        # ONE read, both answers (task 058, plan panel P2): this used to read
        # task.md for the status and AGAIN inside `extract_risk`, so a write
        # landing between them could pair one task's status with another's risk.
        _text = _P(task_md).read_text(encoding="utf-8", errors="replace")
        if str(_status_from_lines(_physical_lines(_text))).strip().lower() != "in_progress":
            return False        # a done/blocked/pending task in a stale pointer never acknowledges (round 2)
        if str(extract_risk_from_text(_text)).strip().lower() != "irreversible":
            return False
        # RE-READ the pointer: a task switch between the first read and here would
        # otherwise let a stale irreversible task acknowledge for a new one. No
        # lock is taken — a PreToolUse hook must never wait on a writer — and any
        # mismatch or error keeps the dangerous command BLOCKED, which is this
        # function's standing contract ("any failure → False").
        with open(pointer, encoding="utf-8") as fh:
            if fh.read().strip() != num:
                return False
        return True
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
    except Exception as exc:                            # fail-open on any bug —
        # but LOUDLY: PB-COMMAND-FAILURE-POLICY promises the guard says so on
        # stderr whenever it cannot run, and a silent `return 0` turned a BLOCK
        # into an ALLOW with no trace (plan panel 077, sol:medium #4).
        print("playbook command-guard: classifier error, failing OPEN "
              "(%s: %s)" % (exc.__class__.__name__, exc), file=sys.stderr)
        return 0
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
