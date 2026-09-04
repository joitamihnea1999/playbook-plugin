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
    # Stable ordinals BY POSITION (impl-panel sol #2): a judge that numbers two distinct
    # entries "1." must not make them share one adjudication key.
    findings = [Finding(n=i, file=f.file, symbol=f.symbol, line=f.line,
                        claimed_severity=f.claimed_severity, severity_known=f.severity_known,
                        text=f.text) for i, f in enumerate(findings, 1)]
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


# ═══════════════════════════════════════════════════════════════════════════
# Part 2 — deterministic matching, adjudication, validity (plan §10; step 7)
# ═══════════════════════════════════════════════════════════════════════════
#
# A finding is auto-matched ONLY when the match is unambiguous (plan-review
# panel F2): its normalized (file, symbol) key hits exactly ONE truth finding —
# two truth findings in one symbol, or a symbol-less finding on a file with
# several truth entries, is a COLLISION and goes to the human. `known_rejects`
# auto-match only when they carry a `file` (+`symbol`) key and that key is
# unique; a reject with only prose never auto-matches. Everything else is
# UNMATCHED → human. The human assigns the equivalence class explicitly
# (`m <truth-id>`), so unique-valid never depends on string equality of prose.
#
# Decisions live in `<run-dir>/adjudication.json`, keyed `"<case>|<label>|<n>"`,
# written atomically after every decision. `valid-new` appends to the case's
# `truth.json` (deduped by normalized file+symbol+failure_mode) and bumps
# `truth_version` in BOTH truth.json and case.json (the loader requires them to
# agree). Historical silence is never proof of invalidity.

import json as _json
import sys as _sys
import time as _time
from pathlib import Path as _Path

VALID_VERDICTS = ("truth", "valid-new")
FP_VERDICTS = ("reject", "invalid")
PENDING_VERDICTS = ("unclear", None)
ADJUDICATION_NAME = "adjudication.json"


def normalize_path(p) -> str:
    s = (p or "").strip().strip("`'\"").replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return s.strip("/")


def normalize_symbol(sym):
    s = (sym or "").strip().strip("`'\"").strip()
    if s.endswith("()"):
        s = s[:-2]
    if s in ("", "-", "—", "n/a", "N/A", "none", "None"):
        return None
    return s.lower()


def normalize_failure_mode(fm) -> str:
    return " ".join((fm or "").lower().split())


def _key(file, symbol):
    return (normalize_path(file), normalize_symbol(symbol))


def _hits(finding, entries):
    """Entries whose normalized (file, symbol) key EQUALS the finding's — a
    symbol-less finding only hits symbol-less entries and vice-versa (impl-panel
    grok F1: a vague file-only hit must never auto-credit a truth that names a
    symbol). Entries without a `file` key never hit."""
    key = _key(finding.file, finding.symbol)
    return [e for e in entries if e.get("file") and _key(e["file"], e.get("symbol")) == key]


def match_finding(finding, truth: dict) -> dict:
    """{'kind': truth|reject|collision|unmatched, 'id': …, 'ids': […]}"""
    th = _hits(finding, truth.get("findings", []))
    if len(th) == 1:
        return {"kind": "truth", "id": th[0]["id"]}
    if len(th) > 1:
        return {"kind": "collision", "ids": [t["id"] for t in th]}
    rh = _hits(finding, truth.get("known_rejects", []))
    if len(rh) == 1:
        return {"kind": "reject", "id": rh[0]["id"]}
    if len(rh) > 1:
        return {"kind": "collision", "ids": [r["id"] for r in rh]}
    return {"kind": "unmatched"}


def decision_key(case_id, label, n) -> str:
    return f"{case_id}|{label}|{n}"


def load_adjudication(run_dir) -> dict:
    p = _Path(run_dir) / ADJUDICATION_NAME
    if not p.is_file():
        return {"version": 1, "decisions": {}}
    obj = _json.loads(p.read_text(encoding="utf-8"))
    obj.setdefault("decisions", {})
    return obj


def save_adjudication(run_dir, adj: dict) -> None:
    from tasks.atomic import atomic_write
    atomic_write(_Path(run_dir) / ADJUDICATION_NAME,
                 _json.dumps(adj, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def _now() -> str:
    return _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())


def iter_findings(results: dict):
    """Yield (record, Finding) for every finding of every `ok` result line."""
    for label, recs in results.items():
        for rec in recs:
            if rec.get("status") != "ok" or not rec.get("findings"):
                continue
            for fd in rec["findings"].get("findings", []):
                yield rec, Finding.from_dict(fd)


def next_truth_id(truth: dict) -> str:
    used = {t["id"] for t in truth.get("findings", [])} | {r["id"] for r in truth.get("known_rejects", [])}
    n = 1
    while f"T{n}" in used:
        n += 1
    return f"T{n}"


def find_equivalent_truth(truth: dict, file, symbol, failure_mode):
    k = (_key(file, symbol), normalize_failure_mode(failure_mode))
    for t in truth.get("findings", []):
        if (_key(t.get("file"), t.get("symbol")), normalize_failure_mode(t.get("failure_mode"))) == k:
            return t["id"]
    return None


def _atomic_write(path, text) -> None:
    from tasks.atomic import atomic_write
    atomic_write(path, text)


def append_valid_new(case, finding, failure_mode: str, severity: str) -> str:
    """Append a `valid-new` truth finding (deduped) and bump `truth_version`.

    `case.json` is the SOLE authority for `truth_version` (impl-panel opus F2,
    sol #3, terra #2, grok F3): truth.json is written FIRST with the new finding
    and WITHOUT a `truth_version` key, then case.json is bumped. A crash between
    the two writes therefore leaves a corpus that still LOADS (finding present,
    version one behind — the next append completes the bump) instead of a
    version pair the loader refuses. Returns the truth id used."""
    truth = _json.loads(case.truth_path.read_text(encoding="utf-8"))
    truth.setdefault("findings", []); truth.setdefault("known_rejects", [])
    # Recoverable transaction marker (r3 sol #2): truth.json records the version its
    # content EXPECTS (`truth_version_pending`); if case.json is behind — a crash landed
    # between the two writes — complete the bump FIRST, even on the dedup path, so an
    # identical retry of the interrupted operation converges instead of returning early.
    pending = truth.get("truth_version_pending")
    current = int(case.meta.get("truth_version", 1))
    if isinstance(pending, int) and pending > current:
        meta = _json.loads((case.path / "case.json").read_text(encoding="utf-8"))
        meta["truth_version"] = pending
        _atomic_write(case.path / "case.json", _json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
        case.meta["truth_version"] = pending
    existing = find_equivalent_truth(truth, finding.file, finding.symbol, failure_mode)
    if existing:
        return existing
    tid = next_truth_id(truth)
    truth["findings"].append({
        "id": tid, "file": normalize_path(finding.file), "symbol": finding.symbol,
        "failure_mode": failure_mode.strip(), "severity": severity,
        "historical_outcome": "valid-new", "added_by": "adjudicate", "added_at": _now()})
    new_version = int(case.meta.get("truth_version", 1)) + 1
    truth.pop("truth_version", None)              # case.json is the only authority from now on
    truth["truth_version_pending"] = new_version  # …and this is the recovery marker
    meta = _json.loads((case.path / "case.json").read_text(encoding="utf-8"))
    meta["truth_version"] = new_version
    _atomic_write(case.truth_path, _json.dumps(truth, indent=2, ensure_ascii=False) + "\n")
    case.truth = truth
    _atomic_write(case.path / "case.json", _json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
    case.meta["truth_version"] = new_version
    return tid


def auto_adjudicate(results: dict, corpus, adj: dict) -> dict:
    """Record deterministic matches for every not-yet-decided finding. Returns
    counts {auto_truth, auto_reject, pending}."""
    counts = {"auto_truth": 0, "auto_reject": 0, "pending": 0}
    dec = adj["decisions"]
    for rec, f in iter_findings(results):
        k = decision_key(rec["case_id"], rec["label"], f.n)
        if k in dec:
            continue
        case = corpus.get(rec["case_id"])
        if case is None:
            continue
        m = match_finding(f, case.truth)
        if m["kind"] == "truth":
            dec[k] = {"verdict": "truth", "truth_id": m["id"], "by": "auto", "ts": _now()}
            counts["auto_truth"] += 1
        elif m["kind"] == "reject":
            dec[k] = {"verdict": "reject", "reject_id": m["id"], "by": "auto", "ts": _now()}
            counts["auto_reject"] += 1
        else:
            counts["pending"] += 1
    return counts


def _describe(rec, f, case) -> str:
    lines = [f"── case {case.id} · candidate {rec['label']} ({rec.get('spec', '')}) · finding {f.n}",
             f"   FILE: {f.file}" + (f":{f.line}" if f.line else ""),
             f"   SYMBOL: {f.symbol or '-'}",
             f"   SEVERITY (claimed): {f.claimed_severity or '?'}",
             f"   WHY: {f.text[:600]}", "   truth findings:"]
    for t in case.truth.get("findings", []):
        lines.append(f"     {t['id']:<5} {t.get('file')}::{t.get('symbol') or '-'} — "
                     f"{t.get('failure_mode')} [{t.get('severity')}, {t.get('historical_outcome')}]")
    if case.truth.get("known_rejects"):
        lines.append("   known rejects:")
        for r in case.truth["known_rejects"]:
            lines.append(f"     {r['id']:<5} {r.get('claim')} — rejected: {r.get('why_rejected')}")
    lines.append("   verdict? [m <truth-id>] same defect · [v] valid-new · [r <reject-id>] known reject"
                 " · [i] invalid · [u] unclear · [s] skip · [q] quit+save")
    return "\n".join(lines)


def adjudicate(run_dir, corpus, results: dict, *, stdin=None, stdout=None, auto_only=False) -> dict:
    """Deterministic matches first, then a terminal loop for the remainder.
    Returns summary counts. Saves after EVERY decision (crash-safe). Holds the
    run's exclusive lock for the whole session so two adjudicators cannot
    interleave read-modify-write updates (impl-panel sol #3 / terra #3); a held
    lock raises `records.RunLocked`."""
    from bench.lib.records import RunLock
    # Corpus lock too (impl-panel r2 sonnet #1 / terra #2): `valid-new` mutates the SHARED
    # corpus, and Test A / Test B may be adjudicated from two terminals.
    with RunLock(corpus.root), RunLock(run_dir):
        return _adjudicate_locked(run_dir, corpus, results, stdin=stdin, stdout=stdout,
                                  auto_only=auto_only)


def _adjudicate_locked(run_dir, corpus, results, *, stdin, stdout, auto_only) -> dict:
    stdin = stdin or _sys.stdin
    stdout = stdout or _sys.stdout
    adj = load_adjudication(run_dir)
    counts = auto_adjudicate(results, corpus, adj)
    save_adjudication(run_dir, adj)
    counts.update({"human": 0, "skipped": 0, "valid_new_added": 0})
    if auto_only:
        return counts
    dec = adj["decisions"]
    for rec, f in iter_findings(results):
        k = decision_key(rec["case_id"], rec["label"], f.n)
        if k in dec and dec[k].get("verdict") not in PENDING_VERDICTS:
            continue
        case = corpus.get(rec["case_id"])
        if case is None:
            continue
        print(_describe(rec, f, case), file=stdout, flush=True)
        while True:
            print("> ", end="", file=stdout, flush=True)
            line = stdin.readline()
            if not line:                                   # EOF → save and stop
                save_adjudication(run_dir, adj)
                return counts
            cmd, _, arg = line.strip().partition(" ")
            cmd = cmd.lower()
            arg = arg.strip()
            if cmd == "q":
                save_adjudication(run_dir, adj)
                return counts
            if cmd == "s":
                counts["skipped"] += 1
                break
            if cmd == "m" and arg:
                ids = {t["id"] for t in case.truth.get("findings", [])}
                if arg not in ids:
                    print(f"   no truth id {arg!r} in {case.id} (have {sorted(ids)})", file=stdout)
                    continue
                dec[k] = {"verdict": "truth", "truth_id": arg, "by": "human", "ts": _now()}
            elif cmd == "r" and arg:
                ids = {r["id"] for r in case.truth.get("known_rejects", [])}
                if arg not in ids:
                    print(f"   no reject id {arg!r} in {case.id} (have {sorted(ids)})", file=stdout)
                    continue
                dec[k] = {"verdict": "reject", "reject_id": arg, "by": "human", "ts": _now()}
            elif cmd == "v":
                print("   failure mode (one line; empty = first sentence of WHY): ", end="",
                      file=stdout, flush=True)
                fm = stdin.readline().strip() or (f.text.split(". ")[0][:160] or "unspecified")
                sev = f.claimed_severity if f.severity_known else "Important"
                tid = append_valid_new(case, f, fm, sev)
                if case.truth["findings"][-1]["id"] == tid and case.truth["findings"][-1].get(
                        "added_by") == "adjudicate" and case.truth["findings"][-1].get("failure_mode") == fm.strip():
                    counts["valid_new_added"] += 1
                dec[k] = {"verdict": "valid-new", "truth_id": tid, "by": "human", "ts": _now()}
            elif cmd == "i":
                dec[k] = {"verdict": "invalid", "by": "human", "ts": _now()}
            elif cmd == "u":
                dec[k] = {"verdict": "unclear", "by": "human", "ts": _now()}
            else:
                print("   commands: m <id> · v · r <id> · i · u · s · q", file=stdout)
                continue
            counts["human"] += 1
            save_adjudication(run_dir, adj)
            break
    save_adjudication(run_dir, adj)
    return counts


SCORABLE_STATUSES = ("ok", "malformed", "fail")     # the judge RAN and had its chance


def resolve_validity(results: dict, adj: dict) -> dict:
    """Per (case_id, label): {'valid': {truth_id: Finding}, 'fp': [Finding],
    'pending': [Finding], 'unique': {truth_id}, 'unique_undetermined': bool} —
    unique = held by exactly one candidate within the case, and DETERMINED only
    when every candidate in the run has a scorable result for that case (r3 grok
    #5: a peer that DNF'd never had its chance, so nothing is 'unique' against it)."""
    per = {}
    dec = adj.get("decisions", {})
    labels_all = set(results)
    scored = {}                                     # case_id → labels with a scorable result
    for label, recs in results.items():
        for rec in recs:
            if rec.get("status") in SCORABLE_STATUSES:
                scored.setdefault(rec["case_id"], set()).add(label)
    for rec, f in iter_findings(results):
        slot = per.setdefault((rec["case_id"], rec["label"]),
                              {"valid": {}, "fp": [], "pending": [], "unique": set(),
                               "unique_undetermined": False})
        d = dec.get(decision_key(rec["case_id"], rec["label"], f.n))
        v = d.get("verdict") if d else None
        if v in VALID_VERDICTS:
            slot["valid"].setdefault(d["truth_id"], f)      # one credit per truth id per candidate
        elif v in FP_VERDICTS:
            slot["fp"].append(f)
        else:
            slot["pending"].append(f)
    by_case = {}
    for (cid, label), slot in per.items():
        for tid in slot["valid"]:
            by_case.setdefault(cid, {}).setdefault(tid, set()).add(label)
    for (cid, label), slot in per.items():
        if scored.get(cid, set()) != labels_all:
            slot["unique"] = set()
            slot["unique_undetermined"] = True
        else:
            slot["unique"] = {tid for tid in slot["valid"] if len(by_case[cid][tid]) == 1}
    return per
