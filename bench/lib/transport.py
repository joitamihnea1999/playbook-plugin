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


def seat_verdict(candidate, prompt: str, repo_root, *, adapter_factory=None, platform_nt=None) -> dict:
    """{'transport': 'stdin'|'argv'|'?', 'fits': bool, 'reason': str} for one seat."""
    from provider.argv_guard import argv_byte_error
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
            err = argv_byte_error(argv, candidate.backend)
            if err:
                return {"transport": transport, "fits": False, "reason": err}
        else:
            payload = sum(len(a) + 1 for a in argv)
            if payload > WINDOWS_CMDLINE_CAP:
                return {"transport": transport, "fits": False,
                        "reason": (f"(excluded: {candidate.backend} prompt is ~{payload:,} chars on argv; "
                                   f"Windows caps the command line at 32,767 chars)")}
    try:
        budget = resolve_review_context_chars(Path(repo_root), stdin=not argv_transport)
    except Exception:
        budget = None
    if budget is not None and len(prompt) > budget:
        return {"transport": transport, "fits": False,
                "reason": (f"(excluded: prompt is {len(prompt):,} chars; {candidate.backend} "
                           f"{transport} budget is {budget:,} chars)")}
    return {"transport": transport, "fits": True, "reason": ""}


def preflight_errors(candidates, prompt: str, repo_root, *, adapter_factory=None, platform_nt=None) -> dict:
    """label → reason for every seat that cannot carry the prompt ({} = all fit)."""
    out = {}
    for cand in candidates:
        v = seat_verdict(cand, prompt, repo_root, adapter_factory=adapter_factory, platform_nt=platform_nt)
        if not v["fits"]:
            out[cand.label] = v["reason"]
    return out


def transport_rows(case_list, candidates, *, repo_root, adapter_factory=None, platform_nt=None,
                   spec_mode: str = "full") -> list:
    """One row per case: sizes of the rendered prompt and each seat's verdict."""
    rows = []
    for case in case_list:
        pkg = _package.build_package(case, spec_mode=spec_mode) if spec_mode != "full" \
            else _package.build_package(case)
        seats = {c.label: seat_verdict(c, pkg.prompt, repo_root, adapter_factory=adapter_factory,
                                       platform_nt=platform_nt) for c in candidates}
        rows.append({"case_id": case.id, "chars": pkg.prompt_chars, "bytes": len(pkg.prompt.encode("utf-8")),
                     "seats": seats, "fits_all": all(v["fits"] for v in seats.values())})
    return rows


def render_rows(rows: list, candidates, platform_label: str) -> str:
    labels = [c.label for c in candidates]
    head = f"{'case':<14}{'chars':>9}{'bytes':>9}  " + "  ".join(f"{lb:<12}" for lb in labels)
    lines = [f"transport report ({platform_label}; seats decide via their adapters' headless_argv)", head,
             "-" * len(head)]
    for r in rows:
        cells = []
        for lb in labels:
            v = r["seats"][lb]
            cells.append(f"{v['transport']}:{'fits' if v['fits'] else 'NO'}".ljust(12))
        lines.append(f"{r['case_id']:<14}{r['chars']:>9,}{r['bytes']:>9,}  " + "  ".join(cells))
    bad = [(r["case_id"], lb, r["seats"][lb]["reason"]) for r in rows for lb in labels if not r["seats"][lb]["fits"]]
    for cid, lb, reason in bad:
        lines.append(f"  ! {cid} / {lb}: {reason}")
    lines.append(f"{len(rows) - len({b[0] for b in bad})}/{len(rows)} cases fit every seat")
    return "\n".join(lines)
