"""Historical-contamination scan (task 049, plan §5.3).

Live judges on this machine can read the historical `.agent/tasks/<NNN>-*/judge.md` files the
corpus was built from. This module detects the one thing that is mechanically detectable —
QUOTING: word 12-grams a judge's raw output shares with that case's historical `judge.md` /
`judge-archive.md`, EXCLUDING every n-gram that also occurs in the rendered prompt (a judge
quoting the spec, the diff or the template is legitimate). It does not detect silent influence
(README says so). Read-only on the history workspaces; writes one file under the run dir.

Resolution rules (plan-panel round 1): the history root is keyed on `case.source.workspace`
(`--history NAME=PATH`); the task dir is `<root>/.agent/tasks/<NNN>-*` via
`bench.tools._common.find_task_dir` (exactly one match, else the case is recorded as
unscanned — never guessed); only `judge.md` and `judge-archive.md` are read; the raw scanned is
the LATEST result record's `raw_path` for the (label, case) pair.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from bench.lib import REPO_ROOT, PLUGIN_ROOT  # noqa: F401  (sys.path bootstrap)
from bench.lib import package as _package, records as _records

DEFAULT_N = 12
CONTAMINATION_NAME = "contamination.json"
HISTORY_FILES = ("judge.md", "judge-archive.md")
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def tokens(text: str) -> list:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def _ngrams(toks: list, n: int) -> set:
    return {tuple(toks[i:i + n]) for i in range(0, max(0, len(toks) - n + 1))}


def shared_spans(raw: str, history: str, *, exclude_text: str = "", n: int = DEFAULT_N) -> list:
    """Maximal runs of tokens in `raw` whose every n-gram occurs in `history` and in none of
    the prompt (`exclude_text`). Returns the spans as text (space-joined tokens), longest first."""
    rt = tokens(raw)
    hist = _ngrams(tokens(history), n) - _ngrams(tokens(exclude_text), n)
    if not hist or len(rt) < n:
        return []
    spans, i = [], 0
    while i <= len(rt) - n:
        if tuple(rt[i:i + n]) in hist:
            j = i + n
            while j < len(rt) and tuple(rt[j - n + 1:j + 1]) in hist:
                j += 1
            spans.append(" ".join(rt[i:j]))
            i = j
        else:
            i += 1
    return sorted(spans, key=len, reverse=True)


def history_files(case, history_roots: dict) -> list:
    """The judge files for a case, or raise ValueError with the reason (unmapped workspace,
    zero/ambiguous task dirs, no judge files)."""
    from bench.tools._common import ToolError, find_task_dir
    ws = case.source.get("workspace", "")
    root = history_roots.get(ws)
    if root is None:
        raise ValueError(f"no --history mapping for workspace {ws!r}")
    try:
        tdir = find_task_dir(Path(root), case.source["task"])
    except ToolError as exc:
        raise ValueError(str(exc)) from None
    files = [str(tdir / name) for name in HISTORY_FILES if (tdir / name).is_file()]
    if not files:
        raise ValueError(f"no judge.md/judge-archive.md under {tdir}")
    return files


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def latest_by_pair(run_dir: Path, labels) -> dict:
    """label → case_id → the LAST parseable result record (append order = time order)."""
    out = {}
    for label in labels:
        recs, _torn = _records.read_results(run_dir, label)
        for r in recs:
            out.setdefault(label, {})[r["case_id"]] = r
    return out


def scan_run(run_dir: Path, corpus, history_roots: dict, *, n: int = DEFAULT_N) -> dict:
    """Build the contamination record for a run (caller holds the run lock and writes it)."""
    manifest = _records.read_manifest(run_dir)
    labels = [c["label"] for c in manifest.get("candidates", [])]
    spec_mode = manifest.get("spec_mode", "full")
    t = manifest.get("timeouts", {})
    latest = latest_by_pair(run_dir, labels)
    out = {"run_id": manifest.get("run_id"), "n": n, "spec_mode": spec_mode,
           "history_roots": {k: str(v) for k, v in history_roots.items()}, "labels": {}}
    unscanned = 0
    for label in labels:
        for case_id, rec in latest.get(label, {}).items():
            case = corpus.get(case_id)
            entry = {"count": None, "longest_span": "", "raw_path": rec.get("raw_path"), "raw_sha256": None,
                     "prompt_sha256": None, "history_files": [], "history_sha256": None, "note": ""}
            raw_p = Path(run_dir) / (rec.get("raw_path") or "")
            if case is None or not rec.get("raw_path") or not raw_p.is_file():
                entry["note"] = "no raw output on disk for this pair (dnf/excluded?)"
                out["labels"].setdefault(label, {})[case_id] = entry
                continue
            raw_bytes = raw_p.read_bytes()
            entry["raw_sha256"] = _sha(raw_bytes)
            try:
                files = history_files(case, history_roots)
            except ValueError as exc:
                entry["note"] = f"unscanned: {exc}"
                unscanned += 1
                out["labels"].setdefault(label, {})[case_id] = entry
                continue
            hist_bytes = b"".join(Path(f).read_bytes() for f in files)
            pkg = _package.build_package(case, spec_mode=spec_mode, soft_timeout_secs=t.get("soft_secs"),
                                         hard_timeout_secs=t.get("hard_secs"))
            spans = shared_spans(raw_bytes.decode("utf-8", errors="replace"),
                                 hist_bytes.decode("utf-8", errors="replace"), exclude_text=pkg.prompt, n=n)
            entry.update({"count": len(spans), "longest_span": spans[0] if spans else "",
                          "prompt_sha256": _sha(pkg.prompt.encode("utf-8")), "history_files": files,
                          "history_sha256": _sha(hist_bytes)})
            out["labels"].setdefault(label, {})[case_id] = entry
    out["unscanned_pairs"] = unscanned
    return out


def write_scan(run_dir: Path, scan: dict) -> Path:
    from tasks.atomic import atomic_write
    p = Path(run_dir) / CONTAMINATION_NAME
    atomic_write(p, json.dumps(scan, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    return p


def load_scan(run_dir: Path):
    p = Path(run_dir) / CONTAMINATION_NAME
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None


def staleness(run_dir: Path, scan: dict) -> set:
    """(label, case_id) pairs whose LATEST raw no longer matches the scanned raw's sha256."""
    stale = set()
    latest = latest_by_pair(run_dir, list(scan.get("labels", {})))
    for label, cases_ in scan.get("labels", {}).items():
        for case_id, entry in cases_.items():
            rec = latest.get(label, {}).get(case_id)
            if not rec or entry.get("raw_sha256") is None:
                continue
            p = Path(run_dir) / (rec.get("raw_path") or "")
            if not p.is_file() or _sha(p.read_bytes()) != entry["raw_sha256"]:
                stale.add((label, case_id))
    return stale


def render_scan(scan: dict) -> str:
    lines = [f"contamination scan — run {scan.get('run_id')}: shared word {scan.get('n')}-grams between each judge's "
             f"raw output and the case's historical judge.md (prompt text excluded). Detects QUOTING, not influence."]
    for label, cases_ in sorted(scan.get("labels", {}).items()):
        flagged = [(c, e) for c, e in cases_.items() if (e.get("count") or 0) > 0]
        unscanned = [c for c, e in cases_.items() if e.get("count") is None]
        lines.append(f"  {label:<20} flagged {len(flagged)}/{len(cases_)}"
                     + (f", unscanned {len(unscanned)}" if unscanned else ""))
        for c, e in flagged:
            lines.append(f"     ! {c}: {e['count']} span(s), longest: \"{e['longest_span'][:100]}\"")
        for c in unscanned:
            lines.append(f"     ? {c}: {cases_[c].get('note')}")
    return "\n".join(lines)
