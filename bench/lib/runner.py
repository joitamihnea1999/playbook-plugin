"""Candidate invocation (plan §7 execution primitive, §15, §18; step 5).

`LiveRunner` is a bench-local, thin copy of the SHAPE of production's tail-cert
raw runner (`tasks.review._run_tail_cert_judge_raw`): resolve the seat spec →
adapter class → `run_headless_judge(prompt, model=variant, system_context="",
web_search=False, timeout_secs, budget_usd)` inside the read-only provider
sandbox, capture stdout. It is NOT imported from production because that
function reads `default_judge` from config and returns tail-cert-worded errors;
copying ~15 lines keeps the bench honest about what it runs and keeps the
production symbol private. Status/usage extraction IS imported
(`_judge_status`, `_parse_judge_usage`) so the bench and the spend journal
speak the same enum.

Classification over the REAL adapter envelope (`provider.sandbox.format_judge_output`):

    "(error: … not found on PATH)"          → dnf, no retry (deterministic)
    "(error: … timed out)" / TimeoutExpired → timeout, no retry
    "(error: …)" other                      → dnf, ONE retry (transport class)
    "(FAILED — exit N)" + "(no output captured)" → dnf, ONE retry (transport class)
    "(FAILED — exit N)" with output         → fail (data: the judge ran and broke)
    parseable output                        → ok (findings) | ok-empty | malformed

`FakeRunner` scripts any of these per (case, candidate) and never touches an
adapter — it is what every test and `run --fake` use.

Transport preflight (plan §27.1, panel F3): before ANY candidate runs, the
rendered prompt is checked against each candidate's transport — the adapter's
own `headless_argv` says whether the prompt rides argv or stdin; argv gets
`argv_guard.argv_byte_error` (the physical POSIX cap) and both get production's
`resolve_review_context_chars` char budget as the fairness cap. If ANY candidate
is oversize the case is `excluded` for ALL candidates in the run — paired inputs
are never trimmed per candidate.
"""
from __future__ import annotations

import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from bench.lib import REPO_ROOT, PLUGIN_ROOT  # noqa: F401  (sys.path bootstrap)
from bench.lib import scoring
from bench.lib.snapshot import snapshot_tree

STATUSES = ("ok", "fail", "timeout", "dnf", "malformed", "excluded")
SCORABLE = ("ok", "malformed", "fail")          # the judge ran; DNF/timeout/excluded never score


class CandidateError(ValueError):
    pass


@dataclass(frozen=True)
class Candidate:
    label: str
    spec: str
    backend: str
    variant: "str | None"

    def to_dict(self) -> dict:
        return {"label": self.label, "spec": self.spec, "backend": self.backend,
                "variant": self.variant}


# Bench-local presets so the plan's literal commands (§14/§24: `sol-med,sol-high`)
# resolve to the Test A/B seats (impl-panel sol #5). `label=spec` always wins.
PRESETS = {
    "sol-med": "codex:gpt-5.6-sol:medium",
    "sol-high": "codex:gpt-5.6-sol:high",
    "grok-med": "grok:grok-4.6:medium",
    "grok-high": "grok:grok-4.6:high",
}


def parse_candidates(csv: str) -> list:
    """`label=provider:model:effort,…`, a bench preset (`sol-med`), or a bare spec
    (label = spec with ':'→'-'). Validated through production's
    `resolve_judge_spec` grammar; labels unique."""
    from provider.sandbox import resolve_judge_spec
    out, seen = [], set()
    for item in (s.strip() for s in (csv or "").split(",")):
        if not item:
            continue
        label, _, spec = item.rpartition("=") if "=" in item else ("", "", item)
        spec = spec.strip()
        if not label and spec in PRESETS:
            label, spec = spec, PRESETS[spec]
        try:
            backend, variant = resolve_judge_spec(spec)
        except ValueError as exc:
            raise CandidateError(f"candidate {spec!r}: {exc}") from exc
        label = (label.strip() or spec.replace(":", "-").replace("/", "_"))
        if not label.replace("-", "").replace("_", "").replace(".", "").isalnum():
            raise CandidateError(f"candidate label {label!r} must be [A-Za-z0-9._-]")
        if label in seen:
            raise CandidateError(f"duplicate candidate label {label!r}")
        seen.add(label)
        out.append(Candidate(label=label, spec=spec, backend=backend, variant=variant))
    if not out:
        raise CandidateError("no candidates given")
    return out


@dataclass
class Invocation:
    status: str
    raw: str = ""
    usage: dict = field(default_factory=lambda: {"status": "unknown"})
    duration_ms: int = 0
    retries: int = 0
    findings: "scoring.ParsedFindings | None" = None
    note: str = ""
    attempts: list = field(default_factory=list)      # earlier (retried) attempts, never dropped

    def to_dict(self) -> dict:
        return {"status": self.status, "raw": self.raw, "usage": self.usage,
                "duration_ms": self.duration_ms, "retries": self.retries,
                "findings": self.findings.to_dict() if self.findings else None,
                "note": self.note, "attempts": list(self.attempts)}


# ── classification ───────────────────────────────────────────────────────────

def classify(raw: str, timed_out: bool = False) -> tuple:
    """(status, retry_ok) over the real adapter envelope. Status is the spend
    enum from `tasks.review._judge_status` refined for the bench: a `(FAILED`
    with no output is a transport failure (dnf), and a clean review is `ok` or
    `malformed` depending on whether the FINDINGS block parses."""
    from tasks.review import _judge_status
    t = (raw or "").lstrip()
    if timed_out:
        return "timeout", False
    if t.startswith("(error:"):
        low = t.lower()
        if "timed out" in low:
            return "timeout", False
        if "not found on path" in low:
            return "dnf", False
        return "dnf", True
    if t.startswith("(FAILED"):
        if "(no output captured)" in t:
            return "dnf", True
        return "fail", False
    base = _judge_status(raw)
    if base != "ok":
        return base, False
    return "ok", False


def finish(raw: str, *, timed_out: bool, duration_ms: int, retries: int) -> Invocation:
    from tasks.review import _parse_judge_usage
    status, _ = classify(raw, timed_out)
    usage = _parse_judge_usage(raw) or {"status": "unknown"}
    parsed = None
    if status == "ok":
        parsed = scoring.parse_findings(raw)
        if parsed.status == "malformed":
            status = "malformed"
    return Invocation(status=status, raw=raw or "", usage=usage, duration_ms=duration_ms,
                      retries=retries, findings=parsed)


# ── runners ──────────────────────────────────────────────────────────────────

class FakeRunner:
    """Scripted outputs; never touches an adapter. `script` maps
    `"<case-id>|<label>"` (or `"default"`) → {"status": ok|empty|malformed|timeout|dnf|fail,
    "findings": [{file, symbol, severity, why}], "raw": "...", "duration_ms": N}."""
    needs_tree = False

    def __init__(self, script=None):
        self.script = dict(script or {})
        self.calls = []
        self._lock = threading.Lock()

    @staticmethod
    def render(entry: dict) -> tuple:
        status = entry.get("status", "ok")
        if "raw" in entry:
            return entry["raw"], False
        if status == "ok":
            fs = entry.get("findings") or [{"file": "src/demo.py", "symbol": "demo",
                                             "severity": "Important", "why": "scripted finding"}]
            lines = ["Free-text review (fake).", "", "FINDINGS:"]
            for i, f in enumerate(fs, 1):
                lines += [f"{i}. FILE: {f.get('file', 'src/demo.py')}",
                          f"   SYMBOL: {f.get('symbol') or '-'}",
                          f"   SEVERITY: {f.get('severity', 'Important')}",
                          f"   WHY: {f.get('why', 'scripted')}"]
            lines.append("END FINDINGS")
            return "\n".join(lines) + "\n", False
        if status == "empty":
            return "Looks fine.\n\nFINDINGS:\nNONE\nEND FINDINGS\n", False
        if status == "malformed":
            return "I think the code is fine. No structured block here.\n", False
        if status == "timeout":
            return "(error: judge timed out)", True
        if status == "dnf":
            return "(error: fakecli not found on PATH)", False
        if status == "fail":
            return "(FAILED — exit 1)\n[stderr tail]\nboom", False
        raise ValueError(f"unknown fake status {status!r}")

    def invoke(self, case, candidate, package, tree, *, soft_timeout, hard_timeout) -> Invocation:
        with self._lock:
            self.calls.append((case.id, candidate.label))
        entry = self.script.get(f"{case.id}|{candidate.label}") or self.script.get("default") or {}
        raw, timed_out = self.render(entry)
        return finish(raw, timed_out=timed_out, duration_ms=int(entry.get("duration_ms", 1234)),
                      retries=0)

    def preflight(self, candidates, package, repo_root):
        return {}


def _adapter_invoke(backend, variant, prompt, project_root, timeout_secs, budget_usd) -> str:
    """The production seam, verbatim in shape (tail-cert raw runner)."""
    from provider.subagent import _adapter_class
    try:
        adapter = _adapter_class(backend)(session_id="bench", project_root=Path(project_root))
        return adapter.run_headless_judge(prompt=prompt, model=variant, system_context="",
                                          web_search=False, timeout_secs=timeout_secs,
                                          budget_usd=budget_usd)
    except subprocess.TimeoutExpired:
        return "(error: bench judge timed out)"
    except Exception as exc:                       # spawn/resolution error → dnf envelope
        return f"(error: bench judge spawn failed: {exc})"


class LiveRunner:
    """Real providers. `invoke` and `adapter_factory` are injectable for tests."""
    needs_tree = True

    def __init__(self, repo_root: Path, *, invoke=None, adapter_factory=None, budget_usd=None):
        self.repo_root = Path(repo_root)
        self._invoke = invoke or _adapter_invoke
        self._adapter_factory = adapter_factory
        self.budget_usd = budget_usd
        self.calls = []
        self._lock = threading.Lock()

    def _budget(self) -> str:
        if self.budget_usd is not None:
            return str(self.budget_usd)
        try:
            from tasks.core import resolve_judge_budget
            return resolve_judge_budget(self.repo_root)
        except Exception:
            return "10"

    def _adapter(self, candidate, project_root):
        if self._adapter_factory:
            return self._adapter_factory(candidate.backend, project_root)
        from provider.subagent import _adapter_class
        return _adapter_class(candidate.backend)(session_id="bench", project_root=Path(project_root))

    def preflight(self, candidates, package, repo_root) -> dict:
        """label → error string for candidates whose transport cannot carry the
        prompt; {} when all fit. Uses each adapter's own transport decision."""
        from provider.argv_guard import argv_byte_error
        from tasks.core import resolve_review_context_chars
        errors = {}
        for cand in candidates:
            try:
                inv = self._adapter(cand, repo_root).headless_argv(package.prompt, cand.variant)
            except Exception as exc:
                errors[cand.label] = f"preflight could not build argv: {exc}"
                continue
            argv_transport = getattr(inv, "stdin", None) is None
            if argv_transport:
                err = argv_byte_error(list(getattr(inv, "argv", [])), cand.backend)
                if err:
                    errors[cand.label] = err
                    continue
            try:
                budget = resolve_review_context_chars(Path(repo_root), stdin=not argv_transport)
            except Exception:
                budget = None
            if budget is not None and len(package.prompt) > budget:
                errors[cand.label] = (f"(excluded: prompt is {len(package.prompt):,} chars; "
                                      f"{cand.backend} {'stdin' if not argv_transport else 'argv'} "
                                      f"budget is {budget:,} chars)")
        return errors

    def invoke(self, case, candidate, package, tree, *, soft_timeout, hard_timeout) -> Invocation:
        with self._lock:
            self.calls.append((case.id, candidate.label))
        retries = 0
        t0 = time.monotonic()
        raw = self._invoke(candidate.backend, candidate.variant, package.prompt, tree,
                           hard_timeout, self._budget())
        status, retry_ok = classify(raw)
        attempts = []
        if retry_ok:
            # Keep the first attempt on record (r2 sonnet #2): it may have been billed
            # even though its output looked like a transport failure.
            from tasks.review import _parse_judge_usage
            attempts.append({"status": status, "usage": _parse_judge_usage(raw) or {"status": "unknown"},
                             "raw_head": (raw or "")[:300],
                             "duration_ms": int((time.monotonic() - t0) * 1000)})
            retries = 1
            raw = self._invoke(candidate.backend, candidate.variant, package.prompt, tree,
                               hard_timeout, self._budget())
        duration_ms = int((time.monotonic() - t0) * 1000)
        inv = finish(raw, timed_out=False, duration_ms=duration_ms, retries=retries)
        inv.attempts = attempts
        return inv


# ── one case × N candidates ──────────────────────────────────────────────────

def run_case(case, candidates, runner, package, *, source_repo=None, soft_timeout=900,
             hard_timeout=1200, concurrency=2, skip=frozenset(), snapshot_parent=None):
    """Invoke every candidate not in `skip` against one case. Builds ONE snapshot
    (live runners only), runs candidates with at most `concurrency` in flight,
    and tears the snapshot down after the last finishes. Returns
    [(candidate, Invocation)] in candidate order. Never raises for a provider
    failure — that is a `dnf` result."""
    todo = [c for c in candidates if c.label not in skip]
    if not todo:
        return []
    pre = runner.preflight(todo, package, source_repo or REPO_ROOT) or {}
    if pre:
        why = "; ".join(f"{k}: {v}" for k, v in sorted(pre.items()))
        return [(c, Invocation(status="excluded", note=f"transport preflight — {why}"))
                for c in todo]

    def _one(cand, tree):
        try:
            return runner.invoke(case, cand, package, tree, soft_timeout=soft_timeout,
                                 hard_timeout=hard_timeout)
        except Exception as exc:                   # a runner bug must not abort the run
            return Invocation(status="dnf", raw=f"(error: runner raised {type(exc).__name__}: {exc})",
                              note="runner exception")

    def _all(tree):
        with ThreadPoolExecutor(max_workers=max(1, min(concurrency, len(todo)))) as ex:
            futs = [ex.submit(_one, c, tree) for c in todo]
            return [(c, f.result()) for c, f in zip(todo, futs)]

    if getattr(runner, "needs_tree", False):
        repo = Path(source_repo) if source_repo else REPO_ROOT
        try:
            with snapshot_tree(repo, case.repo_base_sha, parent_dir=snapshot_parent) as tree:
                return _all(tree)
        except Exception as exc:                   # snapshot failure → every candidate dnf
            return [(c, Invocation(status="dnf", raw=f"(error: snapshot failed: {exc})",
                                   note="snapshot")) for c in todo]
    return _all(None)
