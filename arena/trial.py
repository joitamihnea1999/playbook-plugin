#!/usr/bin/env python3
"""arena/trial.py — the paid tier of the arena (Phase 2): live A/B efficacy.

Tier 1 (replay.py) tells us a harness change altered a *decision*. Tier 2 answers
the expensive question it can't: does the change make an agent produce a *better
outcome*? It runs a real agent on a frozen CASE under the baseline harness and the
treatment harness, N reps each, scores each run by the case's own deterministic
check, and applies a frozen decision rule → ADOPT / REJECT / RETEST.

This costs real tokens and wall-clock, so it is never automatic: you invoke it,
with an explicit `--yes`, under a hard `--max-runs` budget cap.

Design — the orchestration, scoring, decision rule, budget cap, and append-only
ledger are pure and deterministic, exercised in tests via a scripted FakeRunner
and an offline NullRunner (`--runner null`, the free canary). The live path
(SandboxRunner) shells out to the existing `scripts/sandbox` one-shot headless
runner, wiring each arm's hooks to that arm's harness variant. Swapping the
runner cannot change the verdict logic — only where the agent's work comes from.

Dev tool — lives beside tests/, never shipped in plugins/playbook/. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import replay  # noqa: E402  — reuse _scripts_for / _extract_baseline_scripts

_ARMS = ("baseline", "treatment")


# ── runners: where an arm's agent work comes from ─────────────────────────────

class NullRunner:
    """Does nothing — the workspace is left as-is. For the offline canary: both
    arms behave identically, so the trial must return RETEST (no measured effect)
    while proving the whole pipeline runs without an agent or a token."""
    label = "null"

    def run(self, arm, rep, workspace, prompt, scripts, agent):
        return {"executed": True, "note": "null runner — no work performed"}


class FakeRunner:
    """Deterministic scripted agent for tests. `behaviors[arm]` is a callable
    (arm, rep, workspace)->None that mutates the workspace to simulate the agent's
    work (or raises to simulate a crashed run)."""
    label = "fake"

    def __init__(self, behaviors):
        self.behaviors = behaviors

    def run(self, arm, rep, workspace, prompt, scripts, agent):
        fn = self.behaviors.get(arm)
        if fn is None:
            return {"executed": True}
        try:
            fn(arm, rep, workspace)
            return {"executed": True}
        except Exception as e:            # simulate a run that failed to execute
            return {"executed": False, "error": str(e)[:120]}


class SandboxRunner:
    """The live path: run a real headless agent on the case, with this arm's
    harness variant active. LIVE — spends tokens; validate on first real use.

    Wires the arm by generating the workspace's `.claude/settings.json` hooks to
    point at THIS arm's extracted scripts/ (absolute paths), so the agent runs
    under the variant under test, then launches `scripts/sandbox` headless."""
    label = "sandbox"

    _HEADLESS = {  # per-agent one-shot headless invocation (best-effort)
        "claude": lambda prompt: ["--print", prompt],
        "codex": lambda prompt: ["exec", prompt],
    }

    def run(self, arm, rep, workspace, prompt, scripts, agent):
        args_fn = self._HEADLESS.get(agent)
        if args_fn is None:
            return {"executed": False, "error": f"no headless recipe for agent {agent!r}"}
        self._wire_hooks(workspace, scripts)
        sandbox = scripts / "sandbox"
        try:
            proc = subprocess.run(
                ["bash", str(sandbox), "--agent", agent, "--", *args_fn(prompt)],
                cwd=str(workspace), capture_output=True, text=True, timeout=1800)
        except (OSError, subprocess.SubprocessError) as e:
            return {"executed": False, "error": str(e)[:160]}
        return {"executed": proc.returncode == 0, "exit": proc.returncode,
                "stderr_tail": proc.stderr[-400:]}

    @staticmethod
    def _wire_hooks(workspace, scripts):
        cdir = workspace / ".claude"
        cdir.mkdir(exist_ok=True)
        def hook(name):
            return {"hooks": [{"type": "command", "command": f'bash "{scripts / name}"'}]}
        settings = {"hooks": {
            "PreToolUse": [{"matcher": "Edit|Write|MultiEdit|NotebookEdit|Bash",
                            **hook("task-gate-hook")}],
            "PostToolUse": [{"matcher": ".*", **hook("state-echo-hook")}],
        }}
        (cdir / "settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")


# ── trial ─────────────────────────────────────────────────────────────────────

def _run_check(check_cmd, workspace) -> bool:
    if not check_cmd:
        return True
    try:
        return subprocess.run(check_cmd, cwd=str(workspace), shell=True,
                              capture_output=True, timeout=300).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _decide(agg) -> str:
    """FROZEN decision rule. Compares pass RATES over runs that actually executed;
    an arm with zero executed runs makes the comparison impossible → RETEST.
    Strictly-better → ADOPT, strictly-worse → REJECT, equal → RETEST. Raw counts
    are always reported so a human can judge whether small-N is just noise."""
    b, t = agg["baseline"], agg["treatment"]
    if b["executed"] == 0 or t["executed"] == 0:
        return "RETEST"
    rb, rt = b["passed"] / b["executed"], t["passed"] / t["executed"]
    if rt > rb:
        return "ADOPT"
    if rt < rb:
        return "REJECT"
    return "RETEST"


def run_trial(case_dir, baseline_ref, treatment_ref=None, reps=None,
              max_runs=None, runner=None, ledger_path=None, agent_override=None):
    case_dir = Path(case_dir)
    spec = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    reps = reps or int(spec.get("reps", 3))
    agent = agent_override or spec.get("agent", "claude")
    prompt = spec.get("prompt", "")
    check_cmd = spec.get("check", "")
    ws_template = case_dir / spec.get("workspace", "workspace")
    runner = runner or SandboxRunner()

    planned = len(_ARMS) * reps
    if max_runs is not None and planned > max_runs:
        reps = max(0, max_runs // len(_ARMS))
        planned = len(_ARMS) * reps

    agg = {a: {"executed": 0, "passed": 0} for a in _ARMS}
    records = []
    tmps: list = []
    ledger = open(ledger_path, "a", encoding="utf-8") if ledger_path else None
    try:
        scripts = {"baseline": replay._scripts_for(baseline_ref, tmps),
                   "treatment": replay._scripts_for(treatment_ref, tmps)}
        for arm in _ARMS:
            for rep in range(reps):
                ws = Path(tempfile.mkdtemp(prefix=f"arena-{arm}-"))
                if ws_template.is_dir():
                    shutil.copytree(ws_template, ws, dirs_exist_ok=True)
                try:
                    rr = runner.run(arm, rep, ws, prompt, scripts[arm], agent)
                    executed = bool(rr.get("executed"))
                    passed = executed and _run_check(check_cmd, ws)
                finally:
                    shutil.rmtree(ws, ignore_errors=True)
                if executed:
                    agg[arm]["executed"] += 1
                    agg[arm]["passed"] += 1 if passed else 0
                rec = {"arm": arm, "rep": rep, "executed": executed,
                       "passed": bool(passed), "runner": runner.label, "detail": rr}
                records.append(rec)
                if ledger:
                    ledger.write(json.dumps(rec) + "\n")
    finally:
        if ledger:
            ledger.close()
        for t in tmps:
            shutil.rmtree(t, ignore_errors=True)

    decision = _decide(agg)
    return {"case": spec.get("name", case_dir.name), "agent": agent, "reps": reps,
            "baseline_ref": baseline_ref, "treatment_ref": treatment_ref or "working-tree",
            "runner": runner.label, "agg": agg, "records": records, "decision": decision}


def _short(r):
    return r[:7] if len(r) == 40 and all(c in "0123456789abcdef" for c in r) else r


def _fmt(out) -> str:
    b, t = out["agg"]["baseline"], out["agg"]["treatment"]
    return "\n".join([
        f"arena trial: {out['case']}  (agent={out['agent']}, reps={out['reps']}, runner={out['runner']})",
        f"  baseline  ({_short(out['baseline_ref'])}):  {b['passed']}/{b['executed']} passed",
        f"  treatment ({_short(out['treatment_ref'])}): {t['passed']}/{t['executed']} passed",
        f"  → {out['decision']}",
    ])


def main() -> int:
    ap = argparse.ArgumentParser(description="Arena Phase 2: live A/B efficacy trial (spends tokens).")
    ap.add_argument("case", help="path to a case dir (with case.json)")
    ap.add_argument("--baseline", default="HEAD", help="git ref for the baseline harness")
    ap.add_argument("--treatment", default=None, help="git ref for the treatment harness (default: working tree)")
    ap.add_argument("--reps", type=int, default=None)
    ap.add_argument("--max-runs", type=int, default=6, help="hard cap on total agent runs (budget)")
    ap.add_argument("--runner", choices=["sandbox", "null"], default="sandbox")
    ap.add_argument("--agent", default=None)
    ap.add_argument("--ledger", default=None, help="append-only JSONL run log path")
    ap.add_argument("--yes", action="store_true", help="required to actually launch live agent runs")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    case_dir = Path(args.case)
    if not (case_dir / "case.json").exists():
        print(f"arena: no case.json in {case_dir}", file=sys.stderr)
        return 2

    runner = NullRunner() if args.runner == "null" else SandboxRunner()
    reps = args.reps or json.loads((case_dir / "case.json").read_text(encoding="utf-8")).get("reps", 3)
    planned = min(2 * reps, args.max_runs)
    if args.runner == "sandbox" and not args.yes:
        print(f"arena: this would launch up to {planned} LIVE agent runs (spends tokens).")
        print(f"       re-run with --yes to proceed, or --runner null for the offline canary.")
        return 0

    out = run_trial(case_dir, args.baseline, treatment_ref=args.treatment,
                    reps=args.reps, max_runs=args.max_runs, runner=runner,
                    ledger_path=args.ledger, agent_override=args.agent)
    print(json.dumps(out, indent=2) if args.json else _fmt(out))
    return {"ADOPT": 0, "RETEST": 0, "REJECT": 1}[out["decision"]]


if __name__ == "__main__":
    sys.exit(main())
