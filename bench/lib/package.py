"""Frozen review-package construction + historical-leakage filter (plan §9, §20).

Every candidate in a run receives the IDENTICAL text built here:

    render_prompt(spec, diff, context) -> the judge prompt from
    `templates/judge_prompt.md` (version-stamped; sha256 recorded in the manifest)

The spec is the task.md **as it stood before any review**. Because a task.md is
edited during review rounds, `reconstruct_spec` approximates that state with a
deterministic, OVER-STRIPPING filter — when in doubt it drops text, never leaks
it (§27.2: per-case deviations belong in `case.json.notes`):

  * ALLOWLIST of `## ` sections kept — Status, Risk, Intent, Why, References,
    Design Phase, Work Plan, Pre-review (`KEPT_SECTIONS`); EVERY other section is
    dropped: Plan Review, Implementation Review, Triage, Handoff, Blocked,
    Verification Receipt(s), Debrief, Parked, Standing Orders, and any section
    this filter has never heard of (plan-review panel, opus: a blocklist can only
    prove KNOWN spellings are filtered; an allowlist bounds the unknown);
  * any `<!-- playbook:*-review-findings -->` … `<!-- /playbook:… -->` span
    dropped wherever it appears (findings written back into task.md);
  * `## Status` body normalized to `pending` (a `done` status is itself a leak);
  * in Work Plan / Pre-review sections every checked gate `- [x] …` becomes an
    unchecked `- [ ] …` with its outcome note (the text after the first ` — `
    that follows the gate's leading bold title, if any) REMOVED — the note is
    what records fixes, panel rounds and verdicts. Design Phase answers are kept:
    by the gate discipline they are written before the plan review.

Heading detection is deliberately fence-agnostic: a `## Plan Review` inside a
code fence is treated as a heading and cut — over-stripping is the safe
direction for a leakage filter (production's fence-aware parsers in
`tasks.core` solve the opposite problem: never MISSING a live heading).

Context files pass a deny-list (`DENIED_CONTEXT_FILES`); a denied artifact in
`context/` raises `LeakageError` so a corpus-building mistake fails loud instead
of silently poisoning every candidate.
"""
from __future__ import annotations

import fnmatch
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
DEFAULT_TEMPLATE = TEMPLATES_DIR / "judge_prompt.md"

# The ONLY sections that survive (normalized H2 title, case-insensitive, exact).
# Everything else — Plan Review, Implementation Review, Triage, Handoff, Blocked,
# Verification Receipt(s), Debrief, Parked, Standing Orders, and any unknown
# heading — is dropped. Parked is deliberately out: its entries routinely cite
# panel findings ("parked per impl-panel grok #3").
KEPT_SECTIONS = ("status", "risk", "intent", "why", "references", "design phase",
                 "work plan", "pre-review")
# Sections whose checked gates are reset + outcome-stripped (the post-design record).
OUTCOME_STRIPPED_SECTIONS = ("work plan", "pre-review")

# Never allowed in a candidate package (§20). Matched on the file NAME (fnmatch).
DENIED_CONTEXT_FILES = ("judge.md", "judge-archive.md", "judge-*.log", "judge*.partial.log",
                        "task-archive.md", "vetting-ledger.json", "truth.json", "case.json",
                        "MIND_MAP*.md", "chat_log*.md", "enforcement.jsonl")

_H2_RE = re.compile(r"^ {0,3}##(?!#)\s*(.*?)\s*(?:#+\s*)?$")   # CommonMark: ≤3 leading spaces
_FINDINGS_OPEN_RE = re.compile(r"<!--\s*playbook:[a-z-]*review-findings\s*-->")
_FINDINGS_CLOSE_RE = re.compile(r"<!--\s*/playbook:[a-z-]*review-findings\s*-->")
_CHECKED_GATE_RE = re.compile(r"^(\s*[-*]\s*)\[[xX]\](\s*)(.*)$")
_BOLD_TITLE_RE = re.compile(r"^(\*\*.*?\*\*)(.*)$", re.DOTALL)
_OUTCOME_SEP = " — "


class LeakageError(ValueError):
    """A denied historical artifact was about to enter a candidate package."""


@dataclass
class Package:
    case_id: str
    spec: str
    diff: str
    context: list = field(default_factory=list)      # [(relative name, text)]
    prompt: str = ""
    template_version: str = ""
    template_sha256: str = ""

    @property
    def prompt_chars(self) -> int:
        return len(self.prompt)


def _h2_title(line: str):
    m = _H2_RE.match(line.rstrip("\n"))
    if not m:
        return None
    return m.group(1).strip().lower()


def _drop_findings_spans(text: str) -> str:
    """Remove every open…close findings span; an unclosed open drops to EOF
    (over-strip: a broken marker pair must never leak the tail)."""
    out = []
    dropping = False
    for line in text.splitlines(keepends=True):
        if not dropping and _FINDINGS_OPEN_RE.search(line):
            dropping = True
            continue
        if dropping:
            if _FINDINGS_CLOSE_RE.search(line):
                dropping = False
            continue
        out.append(line)
    return "".join(out)


def strip_outcome(gate_line: str) -> str:
    """`- [x] **W1 — title.** body … — DONE. note` → `- [ ] **W1 — title.** body …`.

    The outcome note starts at the first ` — ` AFTER a leading bold title (a
    title like `**W1 — foo**` keeps its own dash). Without a bold title the first
    ` — ` on the line starts the note. Over-strips a spec sentence that itself
    contains ` — ` — the safe direction. Unchecked gates are returned unchanged.
    """
    m = _CHECKED_GATE_RE.match(gate_line.rstrip("\n"))
    if not m:
        return gate_line
    lead, sp, rest = m.group(1), m.group(2) or " ", m.group(3)
    bm = _BOLD_TITLE_RE.match(rest)
    if bm:
        title, tail = bm.group(1), bm.group(2)
    else:
        title, tail = "", rest
    cut = tail.find(_OUTCOME_SEP)
    if cut != -1:
        tail = tail[:cut]
    nl = "\n" if gate_line.endswith("\n") else ""
    return f"{lead}[ ]{sp}{title}{tail}".rstrip() + nl


def reconstruct_spec(task_md: str) -> str:
    """Approximate the pre-review task.md (see module docstring)."""
    text = _drop_findings_spans(task_md)
    out = []
    dropping = False
    strip_outcomes = False
    in_status = False
    in_stripped_gate = False        # inside a CHECKED gate's wrapped note (drop continuations)
    for line in text.splitlines(keepends=True):
        title = _h2_title(line)
        if title is not None:
            dropping = title not in KEPT_SECTIONS
            strip_outcomes = title in OUTCOME_STRIPPED_SECTIONS
            in_status = title == "status"
            in_stripped_gate = False
            if not dropping:
                out.append(line)
            continue
        if dropping:
            continue
        if strip_outcomes:
            # A checked gate's outcome note may wrap onto INDENTED continuation lines
            # (impl-panel grok F2): drop them until the next list item, blank line or
            # heading. An OPEN gate's wrapped text is spec and is kept.
            if _CHECKED_GATE_RE.match(line.rstrip("\n")):
                in_stripped_gate = True
                out.append(strip_outcome(line))
                continue
            if in_stripped_gate:
                if line.strip() and line[:1] in (" ", "\t") and not line.lstrip().startswith(("-", "*")):
                    continue
                in_stripped_gate = False
            out.append(line)
            continue
        if in_status:
            # Normalize the first non-blank body line (the status value) to `pending`.
            if line.strip() and not line.lstrip().startswith(">"):
                out.append("pending\n")
                in_status = False
                continue
            out.append(line)
            continue
        out.append(line)
    return "".join(out)


# Final fail-loud scan over the RENDERED spec (impl-panel r2 opus F2): kept sections
# pass through verbatim, so a review-informed edit inside Intent/Why/References/Design
# — or an H3 review note there — must be caught here, not silently frozen.
# Scoped to OUTCOME evidence (r3 opus F2): a pre-review Work Plan legitimately PLANS the
# panel ("run the impl panel; triage when judge.md lands; check judge.md has a PANEL
# VERDICT") — that is design text. What leaks is a verdict VALUE, a finding reference,
# a findings marker, a CAP line, a triage decision, or a review/triage heading.
_LEAK_TOKENS = (
    re.compile(r"panel verdict\s*:\s*(pass|fail)", re.IGNORECASE),
    re.compile(r"playbook:[a-z-]*review-findings", re.IGNORECASE),
    re.compile(r"^\s{0,3}#{2,6}\s.*\b(triage|implementation review|impl review|plan review)\b",
               re.IGNORECASE | re.MULTILINE),
    re.compile(r"\b(impl|plan)[- ]panel\b[^\n]*?(#\d+|\bF\d+\b|\bround \d+|\bfound\b|\bsaid\b|"
               r"\bflagged\b|\bcaught\b)", re.IGNORECASE),
    re.compile(r"^\s*CAP:\s*\d+/\d+", re.MULTILINE),
    re.compile(r"\btriage\b[^\n]*\b(accept|reject|park)(ed)?\b", re.IGNORECASE),
)


def leak_scan(spec: str) -> list:
    """Review-verdict tokens still present in a reconstructed spec ([] = clean)."""
    hits = []
    for rx in _LEAK_TOKENS:
        m = rx.search(spec or "")
        if m:
            hits.append(m.group(0).strip()[:80])
    return hits


def is_denied_context_file(name: str) -> bool:
    """Case-INSENSITIVE on every platform: `fnmatch.fnmatch` is case-sensitive on
    POSIX, so `Truth.json` would slip past a naive match (impl-panel sonnet #2)."""
    base = Path(name).name.lower()
    return any(fnmatch.fnmatchcase(base, pat.lower()) for pat in DENIED_CONTEXT_FILES)


def load_context(case) -> list:
    """[(relative name, text)] for every file under `context/`; a denied name
    raises LeakageError (corpus-builder mistake → fail loud)."""
    files = []
    ctx_root = case.context_dir.resolve()
    for p in case.context_files():
        rel = p.relative_to(case.path).as_posix()
        # A symlink (impl-panel r2 terra #4) could alias a denied artifact under an
        # innocent basename, or point outside context/ — refuse both, fail loud.
        if p.is_symlink() or any(q.is_symlink() for q in p.relative_to(case.context_dir).parents
                                 for q in [case.context_dir / q]):
            raise LeakageError(f"case {case.id}: symlink in context/ is not allowed: {rel}")
        try:
            p.resolve().relative_to(ctx_root)
        except ValueError:
            raise LeakageError(f"case {case.id}: context file resolves outside context/: {rel}") from None
        if is_denied_context_file(p.name):
            raise LeakageError(f"case {case.id}: denied artifact in context/: {rel}")
        files.append((rel, p.read_text(encoding="utf-8", errors="replace")))
    return files


def load_template(path: Path = DEFAULT_TEMPLATE) -> tuple:
    """(text, version, sha256). Version = the `<!-- judgebench template vN -->` stamp."""
    raw = Path(path).read_bytes()
    text = raw.decode("utf-8")
    m = re.search(r"<!--\s*judgebench template (v[0-9A-Za-z.-]+)\s*-->", text)
    if not m:
        raise ValueError(f"template {path} has no '<!-- judgebench template vN -->' stamp")
    return text, m.group(1), hashlib.sha256(raw).hexdigest()


def time_budget_clause(soft_timeout_secs, hard_timeout_secs) -> str:
    """Production's own wind-down paragraph (`tasks.template.time_budget_instruction`)
    so bench judges get the same steering the panel gives; "" when no soft budget."""
    try:
        from tasks.template import time_budget_instruction
        return time_budget_instruction(soft_timeout_secs, hard_timeout_secs).strip()
    except Exception:
        return ""


def render_prompt(spec: str, diff: str, context: list, template_text: str,
                  time_budget: str = "") -> str:
    ctx = ""
    if context:
        parts = ["\n=== FROZEN CONTEXT ARTIFACTS ==="]
        for name, body in context:
            parts.append(f"--- {name} ---\n{body.rstrip()}\n")
        parts.append("=== END FROZEN CONTEXT ===\n")
        ctx = "\n".join(parts)
    for token in ("{{SPEC}}", "{{DIFF}}", "{{CONTEXT}}", "{{TIME_BUDGET}}"):
        if token not in template_text:
            raise ValueError(f"template missing placeholder {token}")
    tb = ("\n" + time_budget.strip() + "\n") if time_budget.strip() else ""
    values = {"SPEC": spec.rstrip("\n"), "DIFF": diff.rstrip("\n"), "CONTEXT": ctx, "TIME_BUDGET": tb}
    # SINGLE pass over the TEMPLATE (r3 grok #4): a body that itself contains
    # `{{DIFF}}` must stay literal, never be substituted in turn.
    return re.sub(r"\{\{(SPEC|DIFF|CONTEXT|TIME_BUDGET)\}\}", lambda m: values[m.group(1)], template_text)


def build_package(case, template_path: Path = DEFAULT_TEMPLATE, *,
                  soft_timeout_secs=None, hard_timeout_secs=None) -> Package:
    """The frozen package for one case: spec.md is used AS FROZEN in the corpus
    (the corpus builder ran `reconstruct_spec` when freezing; running it again
    here is idempotent and guards a hand-edited spec.md against leaks)."""
    spec = reconstruct_spec(case.spec_path.read_text(encoding="utf-8", errors="replace"))
    leaks = leak_scan(spec)
    if leaks:
        raise LeakageError(f"case {case.id}: spec.md still carries review tokens after "
                           f"reconstruction — clean it by hand (record why in case.json notes): {leaks}")
    diff = case.diff_path.read_text(encoding="utf-8", errors="replace")
    context = load_context(case)
    tpl, version, sha = load_template(template_path)
    prompt = render_prompt(spec, diff, context, tpl,
                           time_budget=time_budget_clause(soft_timeout_secs, hard_timeout_secs))
    return Package(case_id=case.id, spec=spec, diff=diff, context=context, prompt=prompt,
                   template_version=version, template_sha256=sha)
