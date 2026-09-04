"""Aggregation + rendering (plan §11, §17; step 8).

Raw measurements live in result.jsonl + adjudication.json; everything here is
COMPUTED AT REPORT TIME and labeled with its parameters. No composite is ever
stored. Severity weights default to 8/3/1 (Critical/Important/Minor) and are
printed with the derived line; the report says "point estimates only" because
v1 has no bootstrap CIs (plan §27.4 — deferred, disclosed).

Per candidate: invocations by status (ok / malformed / fail / timeout / dnf /
excluded — each its own column so a DNF can never look like a bad score),
valid, unique-valid, valid by TRUTH severity, false positives, FP rate,
pending (unadjudicated), severity-weighted valid, tokens known/in/out,
p50/p95 wall-clock, timeout and DNF rates, USD estimate (only where usage AND a
rate exist). Plus a per-case matrix: who caught what.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from bench.lib import rates as _rates
from bench.lib import scoring

RAN = ("ok", "malformed", "fail", "timeout")           # the judge actually ran (latency counts)


def parse_weights(text: str) -> tuple:
    parts = [p.strip() for p in (text or "").split(",")]
    if len(parts) != 3:
        raise ValueError("--weights needs three numbers: Critical,Important,Minor (e.g. 8,3,1)")
    try:
        w = tuple(float(p) for p in parts)
    except ValueError:
        raise ValueError(f"--weights must be numeric, got {text!r}") from None
    if any(x < 0 for x in w):
        raise ValueError("--weights must be non-negative")
    return w


def percentile(values, pct: float):
    """Nearest-rank percentile; None for no data."""
    vals = sorted(v for v in values if isinstance(v, (int, float)))
    if not vals:
        return None
    import math
    k = max(1, math.ceil(pct / 100.0 * len(vals)))       # nearest-rank: ceil(p·n)
    return vals[min(k, len(vals)) - 1]


@dataclass
class Row:
    label: str
    spec: str = ""
    counts: dict = field(default_factory=dict)
    valid: int = 0
    unique_valid: int = 0
    by_severity: dict = field(default_factory=lambda: {"Critical": 0, "Important": 0, "Minor": 0})
    severity_mismatch: int = 0
    fp: int = 0
    pending: int = 0
    weighted: float = 0.0
    tokens_known: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    p50_ms: "int | None" = None
    p95_ms: "int | None" = None
    usd: "float | None" = None
    usd_partial: bool = False
    unique_undetermined: bool = False       # some peer never scored a case → unique is n/a

    @property
    def invocations(self) -> int:
        return sum(self.counts.values())

    def rate(self, status) -> "float | None":
        n = self.invocations
        return (self.counts.get(status, 0) / n) if n else None

    @property
    def fp_rate(self) -> "float | None":
        d = self.valid + self.fp
        return (self.fp / d) if d else None

    @property
    def weighted_per_1k_out(self) -> "float | None":
        return (self.weighted / (self.tokens_out / 1000.0)) if self.tokens_out else None


@dataclass
class Report:
    run_id: str
    weights: tuple
    rows: list
    matrix: dict            # case_id → label → cell text
    cases: list
    labels: list
    pending_total: int
    notes: list = field(default_factory=list)
    missing_pairs: int = 0


def _truth_severity(corpus, case_id, truth_id):
    case = corpus.get(case_id) if corpus else None
    if not case:
        return None
    for t in case.truth.get("findings", []):
        if t["id"] == truth_id:
            return t.get("severity")
    return None


def aggregate(run_id: str, results: dict, adj: dict, corpus, weights=(8.0, 3.0, 1.0),
              manifest=None) -> Report:
    wC, wI, wM = weights
    # Candidates and cases come from the MANIFEST when present (r2 sol #1): a seat with
    # no result yet must show as missing, never vanish from the comparison.
    specs = {}
    for c in (manifest or {}).get("candidates", []):
        specs[c.get("label")] = (c.get("spec", ""), c.get("backend", ""), c.get("variant"))
    labels = sorted(set(results) | set(specs))
    case_ids = list((manifest or {}).get("corpus", {}).get("cases", []))
    validity = scoring.resolve_validity(results, adj, expected_labels=set(labels))
    rows = {}
    matrix = {}
    for label in labels:
        row = Row(label=label, spec=specs.get(label, ("", "", None))[0])
        latencies = []
        usd_total, usd_missing = 0.0, False
        for rec in results.get(label, []):
            cid = rec["case_id"]
            if cid not in case_ids:
                case_ids.append(cid)
            st = rec.get("status", "dnf")
            row.counts[st] = row.counts.get(st, 0) + 1
            if not row.spec:
                row.spec = rec.get("spec", "")
            if st in RAN:
                latencies.append(rec.get("duration_ms"))
            usage = rec.get("usage") or {}
            if usage.get("status") == "known":
                row.tokens_known += 1
                row.tokens_in += int(usage.get("in", 0))
                row.tokens_out += int(usage.get("out", 0))
            for att in rec.get("attempts") or []:          # retried attempts' tokens count too
                au = att.get("usage") or {}
                if au.get("status") == "known":
                    row.tokens_in += int(au.get("in", 0))
                    row.tokens_out += int(au.get("out", 0))
            est = _rates.estimate_usd(rec.get("backend", specs.get(label, ("", "", None))[1]),
                                      rec.get("variant"), usage) if st in RAN else None
            if st in RAN:
                if est is None:
                    usd_missing = True
                else:
                    usd_total += est
            slot = validity.get((cid, label))
            cell = st
            if slot:
                tids = sorted(slot["valid"])
                if tids:
                    cell += " " + ",".join(tids)
                    for tid in tids:
                        sev = _truth_severity(corpus, cid, tid)
                        if sev in row.by_severity:
                            row.by_severity[sev] += 1
                        claimed = slot["valid"][tid].claimed_severity
                        if sev and claimed and claimed != sev:
                            row.severity_mismatch += 1
                if slot["fp"]:
                    cell += f" fp={len(slot['fp'])}"
                if slot["pending"]:
                    cell += f" ?={len(slot['pending'])}"
                row.valid += len(tids)
                row.unique_valid += len(slot["unique"])
                if slot.get("unique_undetermined"):
                    row.unique_undetermined = True
                row.fp += len(slot["fp"])
                row.pending += len(slot["pending"])
            matrix.setdefault(cid, {})[label] = cell
        row.weighted = (row.by_severity["Critical"] * wC + row.by_severity["Important"] * wI
                        + row.by_severity["Minor"] * wM)
        row.p50_ms = percentile(latencies, 50)
        row.p95_ms = percentile(latencies, 95)
        ran = sum(row.counts.get(s, 0) for s in RAN)
        row.usd = usd_total if (ran and not usd_missing) else (usd_total if usd_total else None)
        row.usd_partial = bool(usd_total) and usd_missing
        rows[label] = row
    missing = 0
    for cid in case_ids:
        for label in labels:
            if label not in matrix.get(cid, {}):
                matrix.setdefault(cid, {})[label] = "missing"
                missing += 1
    pending_total = sum(r.pending for r in rows.values())
    notes = [f"severity weights Critical={wC:g} Important={wI:g} Minor={wM:g} — report-time "
             "parameters, not stored; placeholders pending calibration (plan §27.6)",
             "point estimates only — no bootstrap CIs in v1 (plan §27.4)",
             "latency (p50/p95) is the FINAL attempt's wall-clock and INCLUDES timed-out invocations "
             "(a timeout is latency data for the seat); retried attempts' durations sit in `attempts`",
             f"USD = API-equivalent from bench/lib/rates.py (as of {_rates.AS_OF}, non-authoritative); "
             "n/a where tokens are unknown or no rate is on file — never estimated"]
    if pending_total:
        notes.append(f"{pending_total} finding(s) still await adjudication — run `judgebench adjudicate`; "
                     "pending findings count as neither valid nor false positive")
    if any(r.unique_undetermined for r in rows.values()):
        notes.append("unique = n/a where some candidate has no scorable result for a case — uniqueness "
                     "is only defined against peers that ran")
    if missing:
        notes.append(f"{missing} (case, candidate) pair(s) have no result — the run is INCOMPLETE; "
                     "resume it before comparing candidates")
    return Report(run_id=run_id, weights=weights, rows=[rows[lb] for lb in labels], matrix=matrix,
                  cases=case_ids, labels=labels, pending_total=pending_total, notes=notes,
                  missing_pairs=missing)


def _fmt_rate(x) -> str:
    return "n/a" if x is None else f"{100 * x:.0f}%"


def _fmt_ms(x) -> str:
    return "n/a" if x is None else f"{x / 1000:.0f}s"


def _fmt_usd(row: Row) -> str:
    if row.usd is None:
        return "n/a"
    return f"${row.usd:.2f}" + ("~" if row.usd_partial else "")


COLUMNS = ("candidate", "inv", "ok", "malformed", "fail", "timeout", "dnf", "excluded", "valid",
           "unique", "Crit", "Imp", "Min", "sev-mis", "fp", "fp-rate", "pending", "weighted",
           "tok-known", "tok-out", "w/1k-out", "p50", "p95", "timeout-rate", "dnf-rate", "usd")


def _row_cells(r: Row) -> list:
    w1k = r.weighted_per_1k_out
    return [r.label, str(r.invocations), str(r.counts.get("ok", 0)), str(r.counts.get("malformed", 0)),
            str(r.counts.get("fail", 0)), str(r.counts.get("timeout", 0)), str(r.counts.get("dnf", 0)),
            str(r.counts.get("excluded", 0)), str(r.valid),
            ("n/a" if r.unique_undetermined else str(r.unique_valid)),
            str(r.by_severity["Critical"]), str(r.by_severity["Important"]), str(r.by_severity["Minor"]),
            str(r.severity_mismatch), str(r.fp), _fmt_rate(r.fp_rate), str(r.pending), f"{r.weighted:g}",
            f"{r.tokens_known}/{r.invocations}", str(r.tokens_out) if r.tokens_known else "n/a",
            ("n/a" if w1k is None else f"{w1k:.2f}"), _fmt_ms(r.p50_ms), _fmt_ms(r.p95_ms),
            _fmt_rate(r.rate("timeout")), _fmt_rate(r.rate("dnf")), _fmt_usd(r)]


def render_text(rep: Report) -> str:
    table = [list(COLUMNS)] + [_row_cells(r) for r in rep.rows]
    widths = [max(len(row[i]) for row in table) for i in range(len(COLUMNS))]
    out = [f"judgebench report — run {rep.run_id}", ""]
    for i, row in enumerate(table):
        out.append("  ".join(c.ljust(widths[j]) for j, c in enumerate(row)).rstrip())
        if i == 0:
            out.append("  ".join("-" * w for w in widths))
    out += ["", "per-case matrix (status, valid truth ids, fp=, ?=pending):"]
    lw = max([len(c) for c in rep.cases] + [4])
    out.append("  " + "case".ljust(lw) + "  " + "  ".join(lb.ljust(18) for lb in rep.labels))
    for cid in rep.cases:
        out.append("  " + cid.ljust(lw) + "  "
                   + "  ".join(rep.matrix.get(cid, {}).get(lb, "-").ljust(18) for lb in rep.labels))
    out += ["", "derived composite: weighted = Crit×%g + Imp×%g + Min×%g; w/1k-out = weighted per 1,000 "
            "known output tokens (the §25 decision rule; n/a where tokens are unknown → fall back to "
            "per-invocation parity and SAY SO)" % rep.weights]
    for n in rep.notes:
        out.append(f"note: {n}")
    return "\n".join(out) + "\n"


def render_markdown(rep: Report) -> str:
    out = [f"# judgebench report — run `{rep.run_id}`", ""]
    out.append("| " + " | ".join(COLUMNS) + " |")
    out.append("|" + "|".join("---" for _ in COLUMNS) + "|")
    for r in rep.rows:
        out.append("| " + " | ".join(_row_cells(r)) + " |")
    out += ["", "## Per-case matrix", "", "| case | " + " | ".join(rep.labels) + " |",
            "|---|" + "|".join("---" for _ in rep.labels) + "|"]
    for cid in rep.cases:
        out.append(f"| {cid} | " + " | ".join(rep.matrix.get(cid, {}).get(lb, "-") for lb in rep.labels) + " |")
    out += ["", "## Derived composite (labeled, not stored)", "",
            "weighted = Crit×%g + Imp×%g + Min×%g; w/1k-out = weighted per 1,000 known output tokens "
            "(plan §25 decision rule; n/a where tokens are unknown → per-invocation parity, stated)" % rep.weights, ""]
    for n in rep.notes:
        out.append(f"- {n}")
    return "\n".join(out) + "\n"
