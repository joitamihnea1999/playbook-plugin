#!/usr/bin/env python3
"""arena/replay.py — the free tier of the arena (Phase 1).

The arena's expensive question is "does a harness change make agents produce
BETTER outcomes?" — that needs live agent runs (Phase 2). This module answers the
cheap prerequisite first, for free: **did the change alter the harness's
decisions at all?** If it didn't, there is nothing to measure and no trial is
warranted. Most changes are settled here without spending a token.

Mechanism: a playbook hook is a pure function of (project state, tool payload) →
decision (exit 2 = BLOCK with a `BLOCKED:` reason, exit 0 = ALLOW). For each
recorded fixture we run BOTH the baseline hooks (extracted from a git ref) and
the working-tree hooks over the same input and diff the decisions. A difference
is the signal that a live A/B trial (Phase 2) could be worth its cost.

Dev tool — lives beside tests/, never shipped in plugins/playbook/. Stdlib only.

    python3 arena/replay.py [--baseline REF] [--fixtures DIR] [--json]

Exit 0 = no behavioral change; exit 1 = decisions changed (signal); exit 2 = the
replay itself could not run.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent                      # the playbook-plugin repo root
_LIVE_SCRIPTS = _REPO / "plugins" / "playbook" / "scripts"
_SCRIPTS_REL = "plugins/playbook/scripts"


def _extract_baseline_scripts(ref: str, dest: Path) -> Path:
    """Materialize plugins/playbook/scripts as of `ref` into dest; return its path.
    Uses `git archive` (no worktree churn); the whole scripts/ dir comes along so
    a hook's sourced gate-echo-lib.sh and sibling helpers resolve."""
    dest.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(["git", "archive", ref, "--", _SCRIPTS_REL],
                          cwd=str(_REPO), capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git archive {ref} failed: {proc.stderr.decode('utf-8', 'replace')[:200]}")
    tar = subprocess.run(["tar", "-x", "-C", str(dest)], input=proc.stdout, capture_output=True)
    if tar.returncode != 0:
        raise RuntimeError(f"tar extract failed: {tar.stderr.decode('utf-8', 'replace')[:200]}")
    scripts = dest / _SCRIPTS_REL
    if not scripts.is_dir():
        raise RuntimeError(f"{_SCRIPTS_REL} not present at {ref}")
    for f in scripts.iterdir():          # git archive preserves +x, but be safe
        if f.is_file():
            os.chmod(f, 0o755)
    return scripts


def _build_project(files: "dict[str, str]", current_state: "str | None") -> Path:
    """A throwaway project dir matching a fixture's declared state. `.agent/tasks`
    must exist for find_project_root to recognize a playbook project."""
    root = Path(tempfile.mkdtemp(prefix="arena-proj-"))
    (root / ".agent" / "tasks").mkdir(parents=True)
    if current_state is not None:
        sess = root / ".agent" / "sessions" / "pid-arena"
        sess.mkdir(parents=True)
        (sess / "current_state").write_text(current_state, encoding="utf-8")
    for rel, content in (files or {}).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return root


def _decision(scripts: Path, hook: str, project: Path, payload: dict) -> dict:
    """Run one hook over one payload; return the decision as a comparable dict."""
    hook_path = scripts / hook
    env = dict(os.environ)
    env["PLAYBOOK_SESSION_ID"] = "pid-arena"   # deterministic session namespace
    env.pop("PLAYBOOK_ROLE", None)             # don't let a monitor role skip it
    try:
        proc = subprocess.run(
            ["bash", str(hook_path)], cwd=str(project), env=env,
            input=json.dumps(payload), capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return {"error": str(e)[:120]}
    block = next((ln for ln in proc.stderr.splitlines() if ln.startswith("BLOCKED:")), None)
    return {
        "exit": proc.returncode,
        "verdict": "BLOCK" if proc.returncode == 2 else "ALLOW" if proc.returncode == 0 else f"ERR({proc.returncode})",
        "reason": block,
    }


def _scripts_for(ref: "str | None", tmps: list) -> Path:
    """Resolve a scripts/ dir for a ref, or the live working tree when ref is None.
    Extracted temp dirs are appended to `tmps` for the caller to clean up."""
    if ref is None:
        return _LIVE_SCRIPTS
    tmp = Path(tempfile.mkdtemp(prefix="arena-scripts-"))
    tmps.append(tmp)
    return _extract_baseline_scripts(ref, tmp)


def run(baseline_ref: str, fixtures_dir: Path, treatment_ref: "str | None" = None) -> dict:
    """Diff hook decisions between `baseline_ref` and `treatment_ref` (or the
    working tree when treatment_ref is None) over every fixture."""
    fixtures = sorted(fixtures_dir.glob("*.json"))
    if not fixtures:
        return {"ok": False, "error": f"no fixtures in {fixtures_dir}"}
    import shutil
    tmps: list = []
    try:
        baseline_scripts = _scripts_for(baseline_ref, tmps)
        treatment_scripts = _scripts_for(treatment_ref, tmps)
        results = []
        for fx in fixtures:
            spec = json.loads(fx.read_text(encoding="utf-8"))
            hook = spec["hook"]
            proj = _build_project(spec.get("files", {}), spec.get("current_state"))
            try:
                base = _decision(baseline_scripts, hook, proj, spec["payload"])
                work = _decision(treatment_scripts, hook, proj, spec["payload"])
            finally:
                shutil.rmtree(proj, ignore_errors=True)
            changed = (base.get("exit"), base.get("reason")) != (work.get("exit"), work.get("reason"))
            results.append({"name": spec.get("name", fx.stem), "hook": hook,
                            "baseline": base, "working": work, "changed": changed})
    finally:
        for t in tmps:
            shutil.rmtree(t, ignore_errors=True)
    return {"ok": True, "baseline_ref": baseline_ref,
            "treatment_ref": treatment_ref or "working-tree", "results": results,
            "changed": [r["name"] for r in results if r["changed"]]}


def main() -> int:
    ap = argparse.ArgumentParser(description="Arena free tier: did the harness change any decisions?")
    ap.add_argument("--baseline", default="HEAD",
                    help="git ref for the 'before' hooks (default HEAD → compares HEAD vs working tree)")
    ap.add_argument("--treatment", default=None,
                    help="git ref for the 'after' hooks (default: the working tree)")
    ap.add_argument("--fixtures", default=str(_HERE / "fixtures"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        out = run(args.baseline, Path(args.fixtures), treatment_ref=args.treatment)
    except RuntimeError as e:
        print(f"arena: replay could not run — {e}", file=sys.stderr)
        return 2
    if not out["ok"]:
        print(f"arena: {out['error']}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(out, indent=2))
    else:
        def _short(r):
            return r[:7] if len(r) == 40 and all(c in "0123456789abcdef" for c in r) else r
        n, changed = len(out["results"]), out["changed"]
        span = f"{_short(out['baseline_ref'])} → {_short(out['treatment_ref'])}"
        for r in out["results"]:
            mark = "≠ CHANGED" if r["changed"] else "= same"
            print(f"  [{mark}] {r['name']}: {r['baseline'].get('verdict')} "
                  f"→ {r['working'].get('verdict')}")
        print()
        if changed:
            print(f"arena: {len(changed)}/{n} decision(s) changed ({span}) — {', '.join(changed)}.")
            print("→ behavioral delta detected; a live A/B trial (arena Phase 2) may be "
                  "worth its cost. (No agents were run.)")
        else:
            print(f"arena: 0/{n} decisions changed ({span}) — no behavioral delta. "
                  "No trial warranted; nothing to measure.")
    return 1 if out["changed"] else 0


if __name__ == "__main__":
    sys.exit(main())
