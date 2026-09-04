"""Scoring, part 1 — the findings parser (plan §11, step 4).

Reads the TRAILING machine-parsed block the judge prompt template requires:

    FINDINGS:
    1. FILE: <path>
       SYMBOL: <name or ->
       SEVERITY: <Critical|Important|Minor>
       WHY: <paragraph, may span lines>
    2. …
    END FINDINGS            (or `FINDINGS:` / `NONE` / `END FINDINGS` for no defects)

Contract: NEVER raises. A judge that cannot follow the format is a RESULT
(`status="malformed"`), not an error. Lenient where leniency cannot leak or
inflate: keys are case-insensitive, whitespace is loose, the LAST block wins
(judges revise), an unterminated block parses to EOF (flagged), fenced code
inside the block is ignored (a judge quoting the template must not mint
entries), exact duplicate entries collapse (flagged). Strict where it matters:
an entry with no FILE is skipped (flagged), an unknown severity is kept
verbatim but `severity_known=False` (the report shows it; it never counts as a
known severity).

Part 2 (matching / adjudication / aggregation) lands in step 7.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

SEVERITIES = ("Critical", "Important", "Minor")

_BLOCK_OPEN_RE = re.compile(r"^\s*FINDINGS\s*:\s*$", re.IGNORECASE | re.MULTILINE)
_BLOCK_CLOSE_RE = re.compile(r"^\s*END\s+FINDINGS\s*$", re.IGNORECASE | re.MULTILINE)
_ENTRY_RE = re.compile(r"^\s*(\d{1,4})[.)]\s*(.*)$")
_KEY_RE = re.compile(r"^\s*(FILE|SYMBOL|SEVERITY|WHY)\s*:\s*(.*)$", re.IGNORECASE)
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_NONE_RE = re.compile(r"^\s*NONE\s*\.?\s*$", re.IGNORECASE)
_LINE_SUFFIX_RE = re.compile(r"^(.*?):(\d+)(?::\d+)?$")
_PAREN_LINE_RE = re.compile(r"^(.*?)\s*\(\s*(?:line|L)\s*(\d+)\s*\)\s*$", re.IGNORECASE)
_MAX_INPUT = 4_000_000          # chars; beyond this we look only at the tail


@dataclass(frozen=True)
class Finding:
    n: int
    file: str
    symbol: "str | None"
    line: "int | None"
    claimed_severity: str
    severity_known: bool
    text: str

    def to_dict(self) -> dict:
        return {"n": self.n, "file": self.file, "symbol": self.symbol, "line": self.line,
                "claimed_severity": self.claimed_severity,
                "severity_known": self.severity_known, "text": self.text}

    @classmethod
    def from_dict(cls, d: dict) -> "Finding":
        return cls(n=int(d.get("n", 0)), file=str(d.get("file", "")),
                   symbol=d.get("symbol"), line=d.get("line"),
                   claimed_severity=str(d.get("claimed_severity", "")),
                   severity_known=bool(d.get("severity_known", False)),
                   text=str(d.get("text", "")))


@dataclass
class ParsedFindings:
    status: str                              # ok | empty | malformed
    findings: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"status": self.status, "findings": [f.to_dict() for f in self.findings],
                "errors": list(self.errors)}


def normalize_file_ref(raw: str) -> tuple:
    """`\\`./a/b.py:12\\`` → ("a/b.py", 12). Strips quotes/backticks, `./`, a
    trailing `:line[:col]` or `(line N)`, and normalizes backslashes."""
    s = (raw or "").strip().strip("`'\"").strip()
    line = None
    m = _PAREN_LINE_RE.match(s)
    if m:
        s, line = m.group(1).strip(), int(m.group(2))
    else:
        m = _LINE_SUFFIX_RE.match(s)
        if m:
            s, line = m.group(1), int(m.group(2))
    s = s.strip().strip("`'\"").replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return s, line


def _canonical_severity(raw: str) -> tuple:
    s = (raw or "").strip().strip("`*").strip()
    for sev in SEVERITIES:
        if s.lower() == sev.lower():
            return sev, True
    return s, False


def _last_block(text: str) -> tuple:
    """(block_body, errors, found). The LAST `FINDINGS:` opener wins; an
    unterminated block runs to EOF and is flagged."""
    opens = list(_BLOCK_OPEN_RE.finditer(text))
    if not opens:
        return "", ["no FINDINGS block found"], False
    start = opens[-1].end()
    close = _BLOCK_CLOSE_RE.search(text, start)
    errors = []
    if close is None:
        errors.append("FINDINGS block not terminated by END FINDINGS (parsed to end of output)")
        body = text[start:]
    else:
        body = text[start:close.start()]
    return body, errors, True


def parse_findings(text) -> ParsedFindings:
    try:
        return _parse(text)
    except Exception as exc:                      # the contract: never raise
        return ParsedFindings("malformed", [], [f"parser error: {type(exc).__name__}: {exc}"])


def _parse(text) -> ParsedFindings:
    if not isinstance(text, str) or not text.strip():
        return ParsedFindings("malformed", [], ["empty judge output"])
    if len(text) > _MAX_INPUT:
        text = text[-_MAX_INPUT:]
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    body, errors, found = _last_block(text)
    if not found:
        return ParsedFindings("malformed", [], errors)

    lines = body.split("\n")
    # NONE sentinel: the only non-blank content is NONE.
    nonblank = [ln for ln in lines if ln.strip()]
    if nonblank and all(_NONE_RE.match(ln) for ln in nonblank):
        return ParsedFindings("empty", [], errors)

    entries = []            # list of dicts: n, fields{}, why_lines[]
    cur = None
    cur_key = None
    in_fence = False
    for ln in lines:
        if _FENCE_RE.match(ln):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        em = _ENTRY_RE.match(ln)
        if em:
            cur = {"n": int(em.group(1)), "fields": {}, "why": []}
            entries.append(cur)
            cur_key = None
            rest = em.group(2)
            km = _KEY_RE.match(rest)
            if km:
                cur_key = km.group(1).upper()
                _set_field(cur, cur_key, km.group(2))
            elif rest.strip():
                cur["why"].append(rest.strip())      # prose on the number line
            continue
        if cur is None:
            continue                               # prose before the first entry
        km = _KEY_RE.match(ln)
        if km:
            cur_key = km.group(1).upper()
            _set_field(cur, cur_key, km.group(2))
            continue
        if cur_key == "WHY" and ln.strip():
            cur["why"].append(ln.strip())          # WHY continues over lines

    findings = []
    seen = set()
    dup = 0
    for e in entries:
        f = e["fields"]
        if not f.get("FILE", "").strip():
            errors.append(f"entry {e['n']}: no FILE — skipped")
            continue
        file, line = normalize_file_ref(f["FILE"])
        if not file:
            errors.append(f"entry {e['n']}: empty FILE — skipped")
            continue
        sym = (f.get("SYMBOL") or "").strip().strip("`'\"").strip()
        symbol = None if sym in ("", "-", "—", "n/a", "N/A", "none", "None") else sym
        sev, known = _canonical_severity(f.get("SEVERITY", ""))
        if not known:
            errors.append(f"entry {e['n']}: unknown severity {sev!r}"
                          if sev else f"entry {e['n']}: missing SEVERITY")
        why = " ".join(e["why"]).strip()
        key = (file, symbol, sev, why)
        if key in seen:
            dup += 1
            continue
        seen.add(key)
        findings.append(Finding(n=e["n"], file=file, symbol=symbol, line=line,
                                claimed_severity=sev, severity_known=known, text=why))
    if dup:
        errors.append(f"{dup} exact duplicate entries collapsed")
    if not findings:
        errors.append("FINDINGS block present but no parseable entry (and not NONE)")
        return ParsedFindings("malformed", [], errors)
    return ParsedFindings("ok", findings, errors)


def _set_field(entry: dict, key: str, value: str) -> None:
    if key == "WHY":
        entry["fields"]["WHY"] = "1"
        if value.strip():
            entry["why"].append(value.strip())
    else:
        entry["fields"][key] = value
