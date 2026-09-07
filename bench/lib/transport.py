"""Per-seat transport verdicts for a rendered prompt (task 049, plan §5.1).

One helper decides, for a candidate seat, whether its adapter's transport can carry the
prompt — the SAME decision `LiveRunner.preflight` makes before a live invocation, so the
`corpus validate --transport` report and the run-time exclusion cannot drift:

  * the adapter's own `headless_argv` says whether the prompt rides stdin (codex, claude)
    or argv (grok);
  * an argv seat is capped per element on POSIX (`provider.argv_guard.argv_byte_error`,
    32 × page size bytes) and by the adapters' ~30k whole-command-line guard on Windows;
  * every seat is capped by production's context budget
    (`tasks.core.resolve_review_context_chars`, stdin vs argv).

Adapters are constructed like the runner constructs them (no CLI needed for `headless_argv`);
tests inject a stub factory. Read-only; nothing here spends quota.
"""
from __future__ import annotations

import os
from pathlib import Path

from bench.lib import REPO_ROOT, PLUGIN_ROOT  # noqa: F401  (sys.path bootstrap)
from bench.lib import package as _package

WINDOWS_CMDLINE_CAP = 30_000       # the adapters' whole-command-line guard (grok.py / agy / pi)


def _adapter(candidate, project_root, adapter_factory=None):
    if adapter_factory:
        return adapter_factory(candidate.backend, project_root)
    from provider.subagent import _adapter_class
    return _adapter_class(candidate.backend)(session_id="bench", project_root=Path(project_root))


def posix_arg_limit() -> int:
    """The POSIX per-element argv cap (32 × page size), computable on ANY host so a Windows
    operator can simulate POSIX (plan-panel codex:sol #4 / terra #2): `argv_guard.max_arg_bytes`
    where `os.sysconf` exists, else the 4 KB-page value it documents."""
    try:
        from provider.argv_guard import max_arg_bytes
        return int(max_arg_bytes())
    except Exception:
        return 32 * 4096


def seat_verdict(candidate, prompt: str, repo_root, *, adapter_factory=None, platform_nt=None,
                 budget_root=None) -> dict:
    """{'transport': 'stdin'|'argv'|'?', 'fits': bool, 'reason': str} for one seat, under the
    SIMULATED platform (`platform_nt`), never the host's `os.name`. The char budget is resolved
    against `budget_root` (default `repo_root`) — ONE policy root for the report and the live
    preflight (impl-panel codex:sol #2 / grok #4), never a case's source repo."""
    from tasks.core import resolve_review_context_chars
    nt = (os.name == "nt") if platform_nt is None else bool(platform_nt)
    try:
        inv = _adapter(candidate, repo_root, adapter_factory).headless_argv(prompt, candidate.variant)
    except Exception as exc:                          # pragma: no cover — adapter construction failure
        return {"transport": "?", "fits": False, "reason": f"preflight could not build argv: {exc}"}
    argv_transport = getattr(inv, "stdin", None) is None
    transport = "argv" if argv_transport else "stdin"
    if argv_transport:
        argv = list(getattr(inv, "argv", []))
        if not nt:
            limit = posix_arg_limit()
            worst = max((len(a.encode("utf-8")) for a in argv), default=0)
            if worst >= limit:                       # same rule as argv_guard: strictly under the cap
                return {"transport": transport, "fits": False,
                        "reason": (f"(excluded: {candidate.backend} argv element is {worst:,} bytes; the POSIX "
                                   f"per-argument cap is {limit:,} bytes)")}
        else:
            payload = sum(len(a) + 1 for a in argv)
            if payload > WINDOWS_CMDLINE_CAP:
                return {"transport": transport, "fits": False,
                        "reason": (f"(excluded: {candidate.backend} prompt is ~{payload:,} chars on argv; the adapters "
                                   f"cap the Windows command line at {WINDOWS_CMDLINE_CAP:,} chars (OS limit 32,767))")}
    try:
        budget = resolve_review_context_chars(Path(budget_root or repo_root), stdin=not argv_transport)
    except Exception:
        budget = None
    if budget is not None and len(prompt) > budget:
        return {"transport": transport, "fits": False,
                "reason": (f"(excluded: prompt is {len(prompt):,} chars; {candidate.backend} "
                           f"{transport} budget is {budget:,} chars)")}
    return {"transport": transport, "fits": True, "reason": ""}


def preflight_errors(candidates, prompt: str, repo_root, *, adapter_factory=None, platform_nt=None,
                     budget_root=None) -> dict:
    """label → reason for every seat that cannot carry the prompt ({} = all fit)."""
    out = {}
    for cand in candidates:
        v = seat_verdict(cand, prompt, repo_root, adapter_factory=adapter_factory, platform_nt=platform_nt,
                         budget_root=budget_root)
        if not v["fits"]:
            out[cand.label] = v["reason"]
    return out


RUN_SOFT_TIMEOUT, RUN_HARD_TIMEOUT = 900, 1200      # `run`'s defaults — the clause is part of the prompt


def transport_rows(case_list, candidates, *, repo_root, adapter_factory=None, platform_nt=None,
                   spec_mode: str = "full", soft_timeout=RUN_SOFT_TIMEOUT, hard_timeout=RUN_HARD_TIMEOUT) -> list:
    """One row per case: sizes of the rendered prompt (as `run` renders it — time-budget clause
    included, plan-panel codex:sol #3), where the chars come from, and each seat's verdict."""
    rows = []
    for case in case_list:
        pkg = _package.build_package(case, spec_mode=spec_mode, soft_timeout_secs=soft_timeout,
                                     hard_timeout_secs=hard_timeout)
        seats = {c.label: seat_verdict(c, pkg.prompt, repo_root, adapter_factory=adapter_factory,
                                       platform_nt=platform_nt) for c in candidates}
        rows.append({"case_id": case.id, "chars": pkg.prompt_chars, "bytes": len(pkg.prompt.encode("utf-8")),
                     "spec_chars": len(pkg.spec), "diff_chars": len(pkg.diff),
                     "seats": seats, "fits_all": all(v["fits"] for v in seats.values())})
    return rows


def render_rows(rows: list, candidates, platform_label: str) -> str:
    labels = [c.label for c in candidates]
    head = f"{'case':<14}{'chars':>9}{'bytes':>9}{'spec':>8}{'diff':>8}  " + "  ".join(f"{lb:<12}" for lb in labels)
    lines = [f"transport report ({platform_label}; seats decide via their adapters' headless_argv)", head,
             "-" * len(head)]
    for r in rows:
        cells = []
        for lb in labels:
            v = r["seats"][lb]
            cells.append(f"{v['transport']}:{'fits' if v['fits'] else 'NO'}".ljust(12))
        lines.append(f"{r['case_id']:<14}{r['chars']:>9,}{r['bytes']:>9,}{r['spec_chars']:>8,}{r['diff_chars']:>8,}  "
                     + "  ".join(cells))
    bad = [(r["case_id"], lb, r["seats"][lb]["reason"]) for r in rows for lb in labels if not r["seats"][lb]["fits"]]
    for cid, lb, reason in bad:
        lines.append(f"  ! {cid} / {lb}: {reason}")
    lines.append(f"{len(rows) - len({b[0] for b in bad})}/{len(rows)} cases fit every seat")
    return "\n".join(lines)
