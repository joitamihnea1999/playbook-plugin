#!/usr/bin/env python3
"""Mechanical CLAUDE.md + .gitignore writer for `scripts/init` (F15).

These two files used to be the AGENT half of /playbook:init — the doctrine
(CLAUDE.md teaches the correctness contract; .gitignore keeps machine-local
runtime state out of the record) held only if the agent performed it. The
full-surface gauntlet logged that fragility as F15: a mechanical write
guarantees the doctrine; the agent half shrinks to project-specific
enrichment.

CLAUDE.md merge contract (the same one the template header documents and the
field verified on a live project — a seeded pointer paragraph survived above
the template sections):

  * absent            → template body (header comment stripped, project name
                        substituted) written as-is;
  * present           → template-owned `## ` sections are updated IN PLACE to
                        the current template text (heading position kept);
                        template sections the file lacks are appended in
                        template order at the end — above the first project
                        `#` part, if there is one; EVERYTHING else — preamble
                        above the first template heading, the project's own
                        `#` title, custom sections, and a level-1 `#` part
                        further down (task 093) — is preserved byte-for-
                        byte. Project-specific content belongs in its own
                        sections, exactly as the template header instructs.
                        Headings are ATX lines outside closed fenced code
                        blocks and `<!-- -->` comments; Setext and indented
                        headings are not recognised. A template heading is
                        refreshed at its first occurrence outside the
                        project's `#` parts only; every other same-named
                        section is project text.
  * second run        → byte-identical (idempotent).

.gitignore contract: append (create if absent) one marker-guarded block of
playbook runtime-state entries — sessions, chat log + counters, bash history,
the multi-user marker, machine-local judge pins — covering both the root
`.agent/` and per-user `.agent/<user>/` lanes. Existing content is never
touched; the marker makes re-runs no-ops. Language ignores (__pycache__ …)
stay the project's business.

Usage:
    claude-md-merge.py <template-path> <project-root> <project-name>

Prints one status line per file (`CLAUDE.md:CREATED|MERGED|UNCHANGED`,
`.gitignore:CREATED|APPENDED|UNCHANGED`) for init's summary; `ERROR:<msg>`
and exit 1 on failure. Pure stdlib; importable for tests.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

# The atomic-write primitive lives in the tasks package (tasks/atomic.py, sibling
# of this scripts/ dir). This standalone init script is NOT run with the tasks
# package on sys.path, so path-load the single stdlib-only file by location —
# the same idiom scripts/verify uses to load product code from a dev script,
# keeping ONE primitive rather than a copy. atomic.py always ships beside tasks/.
_ATOMIC_PATH = Path(__file__).resolve().parent.parent / "tasks" / "atomic.py"
_atomic_spec = importlib.util.spec_from_file_location("_pb_atomic_write", _ATOMIC_PATH)
_atomic_mod = importlib.util.module_from_spec(_atomic_spec)
_atomic_spec.loader.exec_module(_atomic_mod)
atomic_write = _atomic_mod.atomic_write

HEADER_RE = re.compile(r"\A\s*<!--.*?-->\s*\n", re.DOTALL)
PLACEHOLDER_TITLE = "# Project Name"

GITIGNORE_MARKER = "# --- playbook runtime state (machine-local; managed by playbook init) ---"
GITIGNORE_ENTRIES = (
    ".agent/sessions/",
    ".agent/*/sessions/",
    ".agent/backups/",
    ".agent/bash_history",
    ".agent/*/bash_history",
    # Task 088: bash-log.sh rotates a history past 50 MB to this name; unignored,
    # an archive is a ~50 MB untracked file in `git status` and in the close's
    # tree fingerprint.
    ".agent/bash_history.archived-*",
    ".agent/*/bash_history.archived-*",
    ".agent/chat_log.md",
    ".agent/*/chat_log.md",
    ".agent/chat_log_counter*",
    ".agent/*/chat_log_counter*",
    ".agent/journal/",
    ".agent/*/journal/",
    ".agent/current_user",
    ".agent/models.json",
    ".agent/model-catalog.json",
    # Task 058: the per-task transaction lock files are machine-local runtime
    # state, never part of the record (and a stray `?? …/task.md.lock` in a
    # TRACKED task dir would otherwise read as working-tree tamper).
    ".agent/tasks/*/*.lock",
    ".agent/*/tasks/*/*.lock",
    ".agent/chat_log_counter.lock",
    ".agent/*/chat_log_counter.lock",
)


def template_body(template_text: str, project_name: str) -> str:
    """Template with the instruction header stripped and the name filled in."""
    body = HEADER_RE.sub("", template_text, count=1)
    return body.replace(PLACEHOLDER_TITLE, f"# {project_name}", 1)


# A fence line: up to three spaces, three or more backticks or tildes, then its info string.
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})([^\n]*)")
# The start of an HTML comment block.
COMMENT_RE = re.compile(r"^ {0,3}<!--")
# A level-1 ATX heading (`# Title`, or a bare `#`); `##` and deeper are not.
H1_RE = re.compile(r"^#(?:[ \t]|\r?\n|$)")
# A thematic break (`---`, `***`, `___`, spaces allowed between the marks).
BREAK_RE = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*\r?\n?$")
# A Setext level-2 underline: a pure run of `-` (no inner spaces) — right under a
# text line it underlines that line; `- - -` there is still a thematic break.
SETEXT_RE = re.compile(r"^ {0,3}-+[ \t]*\r?\n?$")


def _masked_lines(lines: "list[str]") -> "set[int]":
    """Indices of the lines that are text whatever they look like: inside (and
    including) a CLOSED fenced code block or a CLOSED `<!-- … -->` comment.

    A fence closer is the opener's mark, at least as long, with nothing after it; a
    backtick opener whose info string holds a backtick is not a fence (CommonMark).
    An opener that is never closed masks nothing: CommonMark would run it to the end
    of the file, which here would turn every later heading into text and let a
    refreshed template section swallow the project's sections below it — the very
    loss this module exists to prevent. Unclosed, it is ordinary text (the
    behaviour before task 093). Other HTML blocks (`<div>` …) are not tracked."""
    out: "set[int]" = set()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = FENCE_RE.match(line)
        if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
            mark = m.group(1)
            for j in range(i + 1, len(lines)):
                c = FENCE_RE.match(lines[j])
                if c and c.group(1)[0] == mark[0] and len(c.group(1)) >= len(mark) \
                        and not c.group(2).strip():
                    out.update(range(i, j + 1))
                    i = j + 1
                    break
            else:
                i += 1                 # never closed: not a fence
            continue
        c = COMMENT_RE.match(line)
        if c:
            ends = [j for j in range(i, len(lines))
                    if "-->" in (lines[j][c.end():] if j == i else lines[j])]
            if ends:
                out.update(range(i, ends[0] + 1))
                i = ends[0] + 1
                continue
        i += 1
    return out


def split_sections(text: str) -> "tuple[str, list[tuple[str | None, str, bool]]]":
    """(preamble, [(heading_line, body, in_project_part)]) split on level-2 headings.

    `###` subsections travel inside their parent's body, and the `#` title (plus
    anything the project keeps above its first `##`) stays in the preamble
    untouched. A LEVEL-1 heading below the first `##` opens a new top-level part
    that belongs to the project (task 093): it ends the section above it and
    becomes a chunk with heading None — never a template key — whose text starts
    with the thematic break (`---`) right above the heading, if a blank line
    precedes that break (`text` + `---` is a Setext underline and stays put), so
    refreshing the template section above cannot swallow it. Every section after
    such a part is flagged `in_project_part`: it belongs to the project, whatever
    its heading (task 093 r2). Masked lines (`_masked_lines`: closed fences and
    comments) are text, never headings. A `#` line ABOVE the first `##` is the
    title area and stays in the preamble.
    """
    lines = text.splitlines(keepends=True)
    masked = _masked_lines(lines)
    preamble: list[str] = []
    sections: "list[tuple[str | None, str, bool]]" = []
    current: "tuple[str | None, list[str]] | None" = None
    in_part = False
    for i, line in enumerate(lines):
        if i in masked:
            pass
        elif line.startswith("## "):
            if current is not None:
                sections.append((current[0], "".join(current[1]), in_part))
            current = (line.rstrip("\n"), [])
            continue
        elif current is not None and H1_RE.match(line):
            body = current[1]
            carried: list[str] = []
            while body and (not body[-1].strip() or BREAK_RE.match(body[-1])):
                if SETEXT_RE.match(body[-1]) and len(body) > 1 and body[-2].strip():
                    break              # `text` + `---` is a Setext underline, not a break
                carried.insert(0, body.pop())
            while carried and not carried[0].strip():
                carried.pop(0)         # the joiner puts back exactly one blank line
            sections.append((current[0], "".join(body), in_part))
            in_part = True
            current = (None, carried + [line])
            continue
        if current is not None:
            current[1].append(line)
        else:
            preamble.append(line)
    if current is not None:
        sections.append((current[0], "".join(current[1]), in_part))
    return "".join(preamble), sections


def merge_claude_md(template_text: str, existing: "str | None",
                    project_name: str) -> str:
    """The deterministic merge described in the module docstring."""
    fresh = template_body(template_text, project_name)
    if existing is None or not existing.strip():
        return fresh
    # A file whose every line ends in CRLF is merged in LF and written back in CRLF,
    # so refreshed template text and the joiner's blank lines match it (task 093 r1);
    # a mixed or LF file is left as it was.
    crlf = "\r\n" in existing and existing.count("\n") == existing.count("\r\n")
    if crlf:
        existing = existing.replace("\r\n", "\n")

    _, tmpl_sections = split_sections(fresh)
    tmpl_map = {h.strip().lower(): (h, b) for h, b, _ in tmpl_sections if h is not None}

    preamble, existing_sections = split_sections(existing)
    # One owner per template heading: its first occurrence OUTSIDE the project's `#`
    # parts. A section inside a part is project text whatever its heading — a
    # project's own `## CLI` there was overwritten (task 093 r1, r2). A later
    # same-named section outside the parts is kept as written too.
    owner: "dict[str, int]" = {}
    for idx, (heading, _, in_part) in enumerate(existing_sections):
        key = heading.strip().lower() if heading is not None else None
        if key in tmpl_map and not in_part and key not in owner:
            owner[key] = idx
    missing = [heading + "\n" + body for heading, body, _ in tmpl_sections
               if heading is not None and heading.strip().lower() not in owner]
    out: list[str] = [preamble]
    for idx, (heading, body, _) in enumerate(existing_sections):
        if heading is None:            # a project-owned `#` part, kept verbatim
            # template sections the file lacks go right above its first project
            # part, so they never land inside one (where they would be project text)
            out.extend(missing)
            missing = []
            out.append(body)
            continue
        key = heading.strip().lower()
        if owner.get(key) == idx:
            th, tb = tmpl_map[key]
            out.append(th + "\n" + tb)
        else:
            out.append(heading + "\n" + body)
    out.extend(missing)

    merged = ""
    for part in out:
        if merged and not merged.endswith("\n\n"):
            merged = merged.rstrip("\n") + "\n\n"
        merged += part
    merged = merged.rstrip("\n") + "\n"
    return merged.replace("\n", "\r\n") if crlf else merged


def merge_gitignore(existing: "str | None") -> "str | None":
    """Existing + the marker-guarded block; None when nothing is missing. A file
    that already carries the marker gains any entry added since it was written
    (inserted right under the marker, once) — so an upgraded plugin's new
    machine-local files stay out of every already-inited clone's record too."""
    # the marker counts only as a whole LINE — an inline mention (a comment quoting
    # it) is not the block (task 054 r2 sol-med#5); the file's own line ending is kept
    nl = "\r\n" if existing is not None and "\r\n" in existing else "\n"
    lines = existing.splitlines(keepends=True) if existing else []
    at = next((i for i, line in enumerate(lines) if line.strip() == GITIGNORE_MARKER), None)
    if at is not None:
        present = {line.strip() for line in lines}
        missing = [e for e in GITIGNORE_ENTRIES if e not in present]
        if not missing:
            return None
        lines[at + 1:at + 1] = [e + nl for e in missing]
        return "".join(lines)
    block = GITIGNORE_MARKER + nl + nl.join(GITIGNORE_ENTRIES) + nl
    if existing is None or not existing.strip():
        return block
    return existing.rstrip("\r\n") + nl + nl + block


def main(argv: "list[str]") -> int:
    if len(argv) != 4:
        print("ERROR:usage: claude-md-merge.py <template> <project-root> <name>")
        return 1
    template_path, root, name = Path(argv[1]), Path(argv[2]), argv[3]
    try:
        template_text = template_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"ERROR:cannot read template: {e}")
        return 1

    claude_md = root / "CLAUDE.md"
    try:
        # raw bytes in, `newline=""` out: universal-newline reading and os.linesep
        # writing used to rewrite a CRLF file (and, on Windows, an LF one) before the
        # merge could see its endings (task 093 r2)
        existing = (claude_md.read_bytes().decode("utf-8", errors="replace")
                    if claude_md.exists() else None)
        merged = merge_claude_md(template_text, existing, name)
        if existing is None:
            atomic_write(claude_md, merged, newline="")
            print("CLAUDE.md:CREATED")
        elif merged != existing:
            atomic_write(claude_md, merged, newline="")
            print("CLAUDE.md:MERGED")
        else:
            print("CLAUDE.md:UNCHANGED")
    except OSError as e:
        print(f"ERROR:CLAUDE.md: {e}")
        return 1

    gitignore = root / ".gitignore"
    try:
        existing = gitignore.read_text(encoding="utf-8", errors="replace") if gitignore.exists() else None
        updated = merge_gitignore(existing)
        if updated is None:
            print(".gitignore:UNCHANGED")
        else:
            atomic_write(gitignore, updated)
            print(".gitignore:CREATED" if existing is None else ".gitignore:APPENDED")
    except OSError as e:
        print(f"ERROR:.gitignore: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
