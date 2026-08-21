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
                        template sections the file lacks are appended at the
                        end in template order; EVERYTHING else — preamble
                        above the first template heading, the project's own
                        `#` title, custom sections — is preserved byte-for-
                        byte. Project-specific content belongs in its own
                        sections, exactly as the template header instructs.
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
    ".agent/chat_log.md",
    ".agent/*/chat_log.md",
    ".agent/chat_log_counter*",
    ".agent/*/chat_log_counter*",
    ".agent/current_user",
    ".agent/models.json",
)


def template_body(template_text: str, project_name: str) -> str:
    """Template with the instruction header stripped and the name filled in."""
    body = HEADER_RE.sub("", template_text, count=1)
    return body.replace(PLACEHOLDER_TITLE, f"# {project_name}", 1)


def split_sections(text: str) -> "tuple[str, list[tuple[str, str]]]":
    """(preamble, [(heading_line, body)]) split on level-2 headings.

    Level-2 only — `###` subsections travel inside their parent's body, and
    the `#` title (plus anything the project keeps above its first `##`)
    stays in the preamble untouched.
    """
    lines = text.splitlines(keepends=True)
    preamble: list[str] = []
    sections: "list[tuple[str, str]]" = []
    current: "tuple[str, list[str]] | None" = None
    for line in lines:
        if line.startswith("## "):
            if current is not None:
                sections.append((current[0], "".join(current[1])))
            current = (line.rstrip("\n"), [])
        elif current is not None:
            current[1].append(line)
        else:
            preamble.append(line)
    if current is not None:
        sections.append((current[0], "".join(current[1])))
    return "".join(preamble), sections


def merge_claude_md(template_text: str, existing: "str | None",
                    project_name: str) -> str:
    """The deterministic merge described in the module docstring."""
    fresh = template_body(template_text, project_name)
    if existing is None or not existing.strip():
        return fresh

    _, tmpl_sections = split_sections(fresh)
    tmpl_map = {h.strip().lower(): (h, b) for h, b in tmpl_sections}

    preamble, existing_sections = split_sections(existing)
    out: list[str] = [preamble]
    seen: set[str] = set()
    for heading, body in existing_sections:
        key = heading.strip().lower()
        if key in tmpl_map:
            th, tb = tmpl_map[key]
            out.append(th + "\n" + tb)
            seen.add(key)
        else:
            out.append(heading + "\n" + body)

    for heading, body in tmpl_sections:
        if heading.strip().lower() not in seen:
            out.append(heading + "\n" + body)

    merged = ""
    for part in out:
        if merged and not merged.endswith("\n\n"):
            merged = merged.rstrip("\n") + "\n\n"
        merged += part
    return merged.rstrip("\n") + "\n"


def merge_gitignore(existing: "str | None") -> "str | None":
    """Existing + the marker-guarded block; None when already present."""
    if existing is not None and GITIGNORE_MARKER in existing:
        return None
    block = GITIGNORE_MARKER + "\n" + "\n".join(GITIGNORE_ENTRIES) + "\n"
    if existing is None or not existing.strip():
        return block
    return existing.rstrip("\n") + "\n\n" + block


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
        existing = claude_md.read_text(encoding="utf-8", errors="replace") if claude_md.exists() else None
        merged = merge_claude_md(template_text, existing, name)
        if existing is None:
            atomic_write(claude_md, merged)
            print("CLAUDE.md:CREATED")
        elif merged != existing:
            atomic_write(claude_md, merged)
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
