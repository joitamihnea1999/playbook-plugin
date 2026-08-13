"""Executable pre-panel audit (report P6 / C6).

13 of 28 findings on one real task were mechanically detectable before a judge
spent a token — stale markers, half-merged conflict leftovers, zombie patch
files. The playbook had no equivalent; the local answer lived in prose and
depended on someone remembering to run it. This module makes it a command.

A sweep is a shell search for a BAD pattern, classified by exit code — the grep
convention, stated so no sweep can be a false green:

    exit 0  → FINDINGS   the pattern was found (candidates to fix before review)
    exit 1  → CLEAN      nothing found
    exit ≥2 → ERROR      the sweep itself did not run (bad regex, missing tool) —
                         NOT a pass. A measuring tool that can't run cannot report
                         "clean"; treating exit 2 as clean is the false green this
                         exists to prevent.

Two severities: `error` sweeps FAIL the audit when they find something (unambiguous
breakage — conflict markers, .orig/.rej files); `advisory` sweeps surface findings
without failing (TODO/FIXME are legitimate, just worth not making a judge find).
An ERROR from ANY sweep fails the audit regardless of severity.

Every default sweep has a negative control in tests/test_audit.py proving it fires
on a crafted dirty fixture — a sweep that cannot detect its own target is worse
than none.

The command is written to a temp script and run as `bash <script>`, never
interpolated into `bash -c`: a sweep may contain any quoting. Unlike the verify
runner it is NOT under `set -e` — a grep exiting 1 (clean) is the expected case,
not a failure. Sweeps needing backreferences must use `grep -P` (`grep -E`
silently matches nothing for a backreference — a known trap, carried here).
"""
from __future__ import annotations

import datetime
import os
import re
import subprocess
import tempfile
from pathlib import Path

from tasks.core import load_config

# --exclude-dir keeps the sweeps off build output and the workspace's own state,
# so a `- [ ]` in a task.md or a marker in node_modules is never a finding.
_EXCLUDES = "--exclude-dir=.git --exclude-dir=.agent --exclude-dir=node_modules --exclude-dir=.venv"

DEFAULT_SWEEPS = [
    {
        "name": "conflict-markers",
        "severity": "error",
        "why": "unresolved git conflict markers are half-merged, broken code",
        # `<<<<<<<` / `>>>>>>>` at line start are unambiguous — no legitimate use.
        # `=======` alone is skipped: a 7-char markdown underline collides with it.
        "command": rf"grep -rEn {_EXCLUDES} '^(<<<<<<<|>>>>>>>)' .",
    },
    {
        "name": "merge-artifacts",
        "severity": "error",
        "why": ".orig/.rej files left by a merge or failed patch are zombie files",
        "command": r"find . -path ./.git -prune -o \( -name '*.orig' -o -name '*.rej' \) -print | grep .",
    },
    {
        "name": "stale-markers",
        "severity": "advisory",
        "why": "TODO/FIXME/XXX/HACK — candidates a review should not have to find",
        "command": rf"grep -rEn {_EXCLUDES} '(TODO|FIXME|XXX|HACK)' .",
    },
]

_VALID_SEVERITY = ("error", "advisory")


def resolve_sweeps(project_path) -> "list[dict]":
    """Sweeps to run: the safety defaults plus any declared in
    `.agent/audit.json` ("sweeps": [{name, command, why, severity}]). Set
    "disable_defaults": true to drop the defaults. Malformed project sweeps
    (missing name/command) are skipped — never crash an audit over config."""
    cfg = load_config(project_path)
    audit_cfg = cfg.get("audit") if isinstance(cfg.get("audit"), dict) else {}
    sweeps: "list[dict]" = [] if audit_cfg.get("disable_defaults") else [dict(s) for s in DEFAULT_SWEEPS]
    for s in audit_cfg.get("sweeps", []) if isinstance(audit_cfg.get("sweeps"), list) else []:
        if isinstance(s, dict) and s.get("name") and s.get("command"):
            sev = s.get("severity")
            sweeps.append({
                "name": str(s["name"]),
                "command": str(s["command"]),
                "why": str(s.get("why", "")),
                "severity": sev if sev in _VALID_SEVERITY else "advisory",
            })
    return sweeps


def classify(rc: int) -> str:
    """Map a sweep's exit code to a status. exit ≥2 is ERROR (did not run), never
    a pass — the whole point."""
    if rc == 0:
        return "findings"
    if rc == 1:
        return "clean"
    return "error"


def run_sweep(sweep: dict, project_path, timeout_secs=300) -> dict:
    """Run one sweep and classify it. Returns the sweep dict augmented with rc,
    status, and captured output. Never raises.

    `timeout_secs` (None = unlimited) bounds one sweep: greps are fast, but a
    project-declared sweep can hang, and a hung audit blocks the review it was
    meant to precede. A timeout is rc 124 → classified ERROR — the sweep did NOT
    complete its scan, which is never a pass."""
    command = sweep["command"]
    fd, script = tempfile.mkstemp(prefix="audit-sweep-", suffix=".sh")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(command)
            if not command.endswith("\n"):
                fh.write("\n")
        proc = subprocess.run(
            ["bash", script], cwd=str(project_path),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", timeout=timeout_secs,
        )
        rc, output = proc.returncode, (proc.stdout or "")
    except subprocess.TimeoutExpired as e:
        raw = e.stdout or ""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        rc = 124
        output = (f"(sweep timed out after {timeout_secs}s — scan incomplete, "
                  "NOT a pass)" + ("\n" + raw if raw else ""))
    except OSError as e:
        rc, output = 127, f"(audit runner failed: {e})"
    finally:
        try:
            os.unlink(script)
        except OSError:
            pass
    return {
        "name": sweep.get("name", "(unnamed)"),
        "severity": sweep.get("severity", "advisory"),
        "why": sweep.get("why", ""),
        "command": command,
        "rc": rc,
        "status": classify(rc),
        "output": output,
    }


# Code-file extensions a mind-map path reference is anchored on. Extension-anchored
# matching is what keeps function names, commit hashes, and prose out of the check.
_CODE_EXT = (
    "py js jsx ts tsx go rs java rb php c cpp cc h hpp cs kt swift scala sh bash zsh "
    "css scss html htm sql md json toml yaml yml tf proto gradle lua r pl ex exs vue svelte"
).split()
# Longest extension first + a boundary guard: with `js` tried before `json` and
# no guard, `.agent/config.json` matched as `agent/config.js` and was reported
# stale — a false positive found live in the StrataDB field test. A noisy
# detector gets ignored, which is worse than none.
_MM_PATH_RE = re.compile(
    r"([A-Za-z0-9_./\-]+\.(?:"
    + "|".join(sorted(_CODE_EXT, key=len, reverse=True))
    + r"))(?![A-Za-z0-9])(?::\d+)?"
)
_MM_EXCLUDE_DIRS = {".git", ".agent", "node_modules", ".venv", "__pycache__", "dist", "build"}


def _extract_mindmap_paths(text: str) -> "list[str]":
    """Slash-containing, extension-anchored file paths cited in the mind map.

    High precision on purpose (a noisy staleness check gets ignored, which is
    worse than none): only tokens that look like a real repo path are considered,
    URLs (`//host/…`) and domain-shaped first segments (`foo.com/…`) are dropped,
    and bare filenames without a directory are skipped as too ambiguous."""
    out = []
    for m in _MM_PATH_RE.finditer(text):
        start = m.start(1)
        # Preceded by ':' or '/' → part of a URL (`https:` → `//host/…`) or a
        # longer path token; not a standalone repo path.
        if start >= 1 and text[start - 1] in ":/":
            continue
        cand = m.group(1)
        # `./`-prefix removal, NOT lstrip("./") — lstrip strips a char SET, so it
        # mangled `.agent/config.json` into `agent/config.json` (field FP #3).
        while cand.startswith("./"):
            cand = cand[2:]
        if "/" not in cand:
            continue  # bare filename — too fuzzy to verify precisely
        segs = cand.split("/")
        if any(s in _MM_EXCLUDE_DIRS for s in segs[:-1]):
            continue  # lives in a dir the walker skips — we cannot judge it
        first_seg = segs[0]
        if "." in first_seg and not first_seg.startswith("."):
            continue  # domain-shaped (github.com/…); dot-DIRS (.claude/) are real paths
        out.append(cand)
    return out


def check_mindmap_staleness(project_path) -> "dict | None":
    """Built-in Python check (report P6, 'clean mindmap'): a MIND_MAP.md that
    cites a file path which no longer exists ANYWHERE in the repo (by exact path
    or basename) has drifted from the code — the agent reads its own map and is
    misled. Returns a sweep-shaped result, or None when there is no map to check.

    Deletion/full-rename precision: a path is stale only if neither it nor its
    basename survives, so a file merely moved to a new directory is NOT flagged
    (its basename still exists) — chosen to keep false positives near zero."""
    project_path = Path(project_path)  # str accepted — crashed live when a caller passed one
    mm = project_path / "MIND_MAP.md"
    if not mm.exists():
        return None
    try:
        text = mm.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    candidates = sorted(set(_extract_mindmap_paths(text)))

    relpaths, basenames = set(), set()
    for root, dirs, files in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in _MM_EXCLUDE_DIRS]
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), project_path).replace(os.sep, "/")
            relpaths.add(rel)
            basenames.add(f)

    def _placeholder(cand: str) -> bool:
        # `journal/NNN.md`-style template placeholders are documentation of a
        # NAMING SCHEME, not a citation of a file — an all-caps/digit stem on a
        # path that doesn't exist is a placeholder, not staleness (field FP).
        stem = os.path.splitext(os.path.basename(cand))[0]
        return bool(re.fullmatch(r"[A-Z0-9_]{2,}", stem))

    stale = [c for c in candidates
             if c not in relpaths and os.path.basename(c) not in basenames
             and not _placeholder(c)]
    # Advisory by default (a stale ref can be a legit historical note), but a
    # project that wants zero-tolerance sets audit.mindmap_severity: "error".
    cfg = load_config(project_path)
    audit_cfg = cfg.get("audit") if isinstance(cfg.get("audit"), dict) else {}
    severity = audit_cfg.get("mindmap_severity")
    if severity not in _VALID_SEVERITY:
        severity = "advisory"
    return {
        "name": "mindmap-stale-refs",
        "severity": severity,
        "why": "mind-map cites a file path that no longer exists — the map drifted from the code",
        "command": "(built-in)",
        "rc": 0 if stale else 1,
        "status": "findings" if stale else "clean",
        "output": "\n".join(f"MIND_MAP.md cites missing path: {c}" for c in stale),
    }


def check_task_bloat(project_path) -> "dict | None":
    """Advisory: an OPEN task.md that has outgrown the review context budget will
    be judged through a trimmed keyhole — nudge the sanctioned compaction ritual
    (move old review-round narrative verbatim to task-archive.md) BEFORE a judge
    reviews a partial view. Threshold: half the argv context budget (the task.md
    share of a review payload), overridable via audit.task_bloat_chars. Returns a
    sweep-shaped result, or None when there are no open tasks."""
    from tasks.core import (_extract_status, _iter_task_dirs, load_config,
                            resolve_review_context_chars)
    project_path = Path(project_path)
    cfg = load_config(project_path)
    audit_cfg = cfg.get("audit") if isinstance(cfg.get("audit"), dict) else {}
    try:
        threshold = int(audit_cfg["task_bloat_chars"]) if "task_bloat_chars" in audit_cfg else 0
    except (TypeError, ValueError):
        threshold = 0
    if threshold <= 0:
        threshold = resolve_review_context_chars(project_path) // 2

    findings = []
    seen_any = False
    for _num, _slug, tf in _iter_task_dirs(project_path):
        try:
            if _extract_status(tf).startswith("done"):
                continue
            seen_any = True
            size = tf.stat().st_size
        except OSError:
            continue
        if size > threshold:
            findings.append(
                f"{tf.parent.name}/task.md is {size:,} bytes (> {threshold:,}) — "
                "judges will see a trimmed view; compact old review narrative to "
                "task-archive.md (see the task sticker) or pin what must survive")
    if not seen_any:
        return None
    return {
        "name": "task-bloat",
        "severity": "advisory",
        "why": "an open task.md larger than the review budget gets judged through a keyhole",
        "command": "(built-in)",
        "rc": 0 if findings else 1,
        "status": "findings" if findings else "clean",
        "output": "\n".join(findings),
    }


def run_audit(project_path) -> dict:
    """Run every resolved sweep plus the built-in mind-map staleness check.
    Returns {results, passed}. The audit FAILS when any sweep ERRORED (a broken
    instrument can't certify clean) or any error-severity sweep found FINDINGS
    (unambiguous breakage). Per-sweep ceiling from `audit.timeout_secs`
    (default 300; the review-knob grammar, so 0/none = unlimited)."""
    from tasks.core import _parse_timeout
    cfg = load_config(project_path)
    audit_cfg = cfg.get("audit") if isinstance(cfg.get("audit"), dict) else {}
    try:
        timeout = _parse_timeout(audit_cfg["timeout_secs"]) if "timeout_secs" in audit_cfg else 300
    except (TypeError, ValueError):
        timeout = 300
    results = [run_sweep(s, project_path, timeout_secs=timeout)
               for s in resolve_sweeps(project_path)]
    mm = check_mindmap_staleness(project_path)
    if mm is not None:
        results.append(mm)
    tb = check_task_bloat(project_path)
    if tb is not None:
        results.append(tb)
    passed = not any(
        r["status"] == "error" or (r["status"] == "findings" and r["severity"] == "error")
        for r in results
    )
    return {"results": results, "passed": passed}


def _finding_lines(output: str, limit: int = 5) -> "list[str]":
    lines = [ln for ln in output.splitlines() if ln.strip()]
    return lines[:limit]


# First `###` entry after the heading is the NEWEST (upsert inserts newest-first).
_AUDIT_ENTRY_SHA_RE = re.compile(r"^### .*·\s*commit\s+([0-9a-fA-F]{7,40})", re.MULTILINE)


def audit_freshness_note(task_text: str, head_sha: str) -> "str | None":
    """None when a current audit receipt exists; else a one-line nudge.

    'Once ever' is not freshness: an audit that ran ten commits ago silently
    vouches for code it never scanned. The newest entry's recorded commit must
    match HEAD. With no git HEAD available, stay quiet — there is nothing sound
    to compare against, and a guess would be noise."""
    idx = task_text.find("## Pre-Panel Audit")
    if idx == -1:
        return ("no pre-panel audit on this task — consider `tasks audit <N>` "
                "first to clear mechanically-detectable issues before the panel")
    if not head_sha:
        return None
    m = _AUDIT_ENTRY_SHA_RE.search(task_text, idx)
    if m and (m.group(1) == head_sha or head_sha.startswith(m.group(1))):
        return None
    ran_at = m.group(1)[:7] if m else "(unknown commit)"
    return (f"pre-panel audit is STALE (ran at {ran_at}; HEAD is {head_sha[:7]}) — "
            "re-run `tasks audit <N>` so the panel reviews audited code")


def format_audit_receipt(audit: dict, *, timestamp: str | None = None, head_sha: str = "") -> str:
    """Render ONE receipt ENTRY for the `## Pre-Panel Audit` section (the heading
    belongs to core.upsert_task_section, newest entry first). The `commit` field
    is load-bearing: audit_freshness_note compares it to HEAD to tell a current
    audit from a stale one."""
    ts = timestamp or datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    out = [f"### {ts} · {'PASS' if audit['passed'] else 'FAIL'} · commit {head_sha or '(unknown)'}"]
    for r in audit["results"]:
        tag = {"clean": "CLEAN", "findings": f"FINDINGS", "error": "ERROR"}[r["status"]]
        n = len(_finding_lines(r["output"], limit=10_000)) if r["status"] != "clean" else 0
        count = f"({n})" if r["status"] == "findings" else ""
        sev = "" if r["severity"] == "error" else " ·advisory"
        out.append(f"    - [{tag}{count}] {r['name']}{sev} — {r['why']}")
        if r["status"] in ("findings", "error"):
            for ln in _finding_lines(r["output"], limit=5):
                out.append(f"        {ln[:200]}")
    out.append("")
    return "\n".join(out)
