#!/usr/bin/env python3
"""Measure the BASH_ENV DEBUG-trap cost on the fused Bash-tool hook path (T4).

`init` sets `BASH_ENV=~/.claude/bash-log.sh`, which installs
`trap '_cpb_log_cmd' DEBUG` (scripts/bash-log.sh) — firing on EVERY command in
EVERY non-interactive bash shell, including the per-Bash-call PreToolUse hook
shells (task-gate-hook, command-guard-hook). Those early-return via the
`${0##*/}` == `*-hook` guard, but the trap still FIRES per command. This script
measures the resulting per-tool-call latency delta: it runs the two fused hooks
once each on a fixed Bash payload, fresh process, N times, under two envs —
WITH `BASH_ENV` pointing at the DEV `scripts/bash-log.sh` (trap active) vs
WITHOUT it (no trap) — and reports median + p95 for each and the delta.

Three arms isolate the cost (T4 panel):
  * `off`         — no BASH_ENV (baseline, no trap).
  * `source_only` — BASH_ENV=an EMPTY file: bash still sources it per shell, so
    this captures the one-time source/parse cost WITHOUT installing any DEBUG
    trap. (bash-log.sh's own source cost differs slightly, but this bounds the
    "sourcing something" overhead.)
  * `on`          — BASH_ENV=the dev bash-log.sh: source + `set -o history` +
    trap install + per-command DEBUG dispatch.
So `total = on-off` is the real per-tool-call cost of the shipped config (the
decision metric); `dispatch ~= on-source_only` isolates the per-command trap
cost that "narrowing the trap" could actually save.

Reproducible + deterministic: fixed payload, a throwaway temp playbook project,
the three arms measured per iteration with their ORDER FLIPPED on alternate
iterations (panel: a fixed within-pair order could bias the delta). Stdlib only.

Owner-ratified decision rule (T4): median delta > 15 ms/call → narrow the trap;
<= 15 ms → change nothing, record the bound.

  python3 scripts/bench_bash_trap.py [--runs 200] [--warmup 10] [--json]

The DEV bash-log.sh is used ON PURPOSE — never the installed ~/.claude copy,
which may be a different version and would not measure this tree's trap.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN = _HERE.parent / "plugins" / "playbook"
SCRIPTS = PLUGIN / "scripts"
TASK_GATE = SCRIPTS / "task-gate-hook"
CMD_GUARD = SCRIPTS / "command-guard-hook"
DEV_BASH_LOG = SCRIPTS / "bash-log.sh"          # the DEV copy, deliberately

PAYLOAD = '{"tool_name":"Bash","tool_input":{"command":"echo benchmark"}}'


def _make_project() -> tempfile.TemporaryDirectory:
    """A well-formed root-lane playbook project (has .agent/tasks/) so the hooks
    take their normal path rather than the fresh-clone / no-project shortcut."""
    td = tempfile.TemporaryDirectory(prefix="pb-bench-")
    root = Path(td.name)
    (root / ".agent" / "tasks").mkdir(parents=True)
    (root / ".agent" / "sessions" / "default").mkdir(parents=True)
    (root / "CLAUDE.md").write_text("tasks bootstrap / tasks work\n", encoding="utf-8")
    return td


def _run_fused_once(cwd: str, env: dict, hooks=(TASK_GATE, CMD_GUARD),
                    bash: str = "bash") -> "list[int]":
    """One Bash-tool call's worth of PreToolUse hooks: task-gate + command-guard,
    each a fresh bash process reading the fixed payload on stdin. Returns each
    hook's exit code so the caller can tell a clean run from a broken one — the
    hooks ALLOW (exit 0) on the benign `echo benchmark` payload, so any non-zero
    is a crash or an unexpected block, not a valid sample.

    `bash` defaults to bare `"bash"` (the dev machine's shell); callers on
    Windows must pass a RESOLVED git-bash, because bare `bash` there is the
    System32 WSL stub, which exits non-zero without running the script."""
    rcs: "list[int]" = []
    for hook in hooks:
        p = subprocess.run(
            [bash, str(hook)], input=PAYLOAD, cwd=cwd, env=env,
            capture_output=True, text=True,
        )
        rcs.append(p.returncode)
    return rcs


def _run_ok(rcs: "list[int]") -> bool:
    """A run is valid for timing only if every hook exited 0 (ALLOW). A non-zero
    exit means the hook crashed or blocked — its wall-clock is not a
    representative sample and must not enter the median/p95."""
    return bool(rcs) and all(rc == 0 for rc in rcs)


def _time_call(cwd: str, env: dict, hooks=(TASK_GATE, CMD_GUARD),
               bash: str = "bash") -> "tuple[float, list[int]]":
    t0 = time.perf_counter()
    rcs = _run_fused_once(cwd, env, hooks, bash)
    return (time.perf_counter() - t0) * 1000.0, rcs   # (ms, exit codes)


def _summary(samples: list[float]) -> dict:
    s = sorted(samples)
    if not s:
        # An arm whose every run failed has no representative timing. Report it
        # as such rather than crashing statistics.median([]) or, worse, inventing
        # a number.
        return {"n": 0, "median_ms": None, "p95_ms": None, "mean_ms": None}
    return {
        "n": len(s),
        "median_ms": round(statistics.median(s), 3),
        "p95_ms": round(s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))], 3),
        "mean_ms": round(statistics.fmean(s), 3),
    }


def run_bench(runs: int, warmup: int, hooks=(TASK_GATE, CMD_GUARD),
              bash: str = "bash") -> dict:
    """Measure the three arms `runs` times each and return the result dict.

    Every run's hook exit codes are checked: a run where any hook exits non-zero
    is counted in `failures[arm]` and EXCLUDED from that arm's timing sample, so
    a crashed/blocked hook can never pollute the median/p95. When any arm has no
    valid samples (or any failure occurred), the delta/verdict are left None and
    the verdict is INVALID — a compromised measurement must not read as a result.
    """
    base_env = dict(os.environ)
    # Neutralise dogfood vars that would perturb the measurement, and force the
    # non-trap arm to truly have no BASH_ENV.
    for k in ("BASH_ENV", "PLAYBOOK_SESSION_ID", "PLAYBOOK_ROLE",
              "PLAYBOOK_EVAL_CONFIG", "PLAYBOOK_ALLOW_DANGEROUS"):
        base_env.pop(k, None)
    with tempfile.NamedTemporaryFile(
            "w", suffix="-empty-bashenv.sh", delete=False) as _ef:
        empty_env = _ef.name                            # a valid, empty BASH_ENV
    try:
        arms = {
            "off": dict(base_env),
            "source_only": dict(base_env, BASH_ENV=empty_env),
            "on": dict(base_env, BASH_ENV=str(DEV_BASH_LOG)),
        }
        samples: dict[str, list[float]] = {k: [] for k in arms}
        failures: dict[str, int] = {k: 0 for k in arms}
        with _make_project() as proj:
            cwd = str(Path(proj))
            for _ in range(max(0, warmup)):
                for env in arms.values():
                    _time_call(cwd, env, hooks, bash)
            names = list(arms)
            for i in range(runs):
                # Flip arm order on alternate iterations so no arm is
                # systematically first/last (panel: fixed order can bias).
                order = names if i % 2 == 0 else list(reversed(names))
                for k in order:
                    ms, rcs = _time_call(cwd, arms[k], hooks, bash)
                    if _run_ok(rcs):
                        samples[k].append(ms)
                    else:
                        failures[k] += 1
    finally:
        try:
            os.unlink(empty_env)
        except OSError:
            pass

    s = {k: _summary(v) for k, v in samples.items()}
    total_failures = sum(failures.values())
    complete = total_failures == 0 and all(s[k]["n"] > 0 for k in s)
    if complete:
        total = {
            "median_ms": round(s["on"]["median_ms"] - s["off"]["median_ms"], 3),
            "p95_ms": round(s["on"]["p95_ms"] - s["off"]["p95_ms"], 3),
        }
        source_cost = round(s["source_only"]["median_ms"] - s["off"]["median_ms"], 3)
        dispatch_cost = round(s["on"]["median_ms"] - s["source_only"]["median_ms"], 3)
        verdict = "NARROW-TRAP" if total["median_ms"] > 15.0 else "NO-CHANGE"
    else:
        total = {"median_ms": None, "p95_ms": None}
        source_cost = dispatch_cost = None
        verdict = "INVALID"
    return {"runs": runs, "payload": PAYLOAD,
            "dev_bash_log": str(DEV_BASH_LOG), "arms": s,
            "failures": failures, "total_failures": total_failures,
            "total_median_ms": total["median_ms"], "total_p95_ms": total["p95_ms"],
            "source_cost_median_ms": source_cost,
            "dispatch_cost_median_ms": dispatch_cost,
            "decision_threshold_ms": 15.0, "verdict": verdict}


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", type=int, default=200)
    ap.add_argument("--warmup", type=int, default=10)
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args(argv)

    if not DEV_BASH_LOG.is_file():
        print(f"ERROR: dev bash-log.sh not found at {DEV_BASH_LOG}", file=sys.stderr)
        return 2

    result = run_bench(args.runs, args.warmup)
    s = result["arms"]
    total = {"median_ms": result["total_median_ms"], "p95_ms": result["total_p95_ms"]}
    source_cost = result["source_cost_median_ms"]
    dispatch_cost = result["dispatch_cost_median_ms"]

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        def _ms(v):
            return f"{v:+.2f} ms" if isinstance(v, (int, float)) else "n/a (failed runs)"
        print(f"fused Bash-tool hook path (task-gate + command-guard), "
              f"N={args.runs}, fresh process each")
        for k, label in (("off", "no BASH_ENV "), ("source_only", "source-only "),
                         ("on", "with trap  ")):
            a = s[k]
            if a["n"]:
                print(f"  {label}: median {a['median_ms']:.2f} ms  "
                      f"p95 {a['p95_ms']:.2f} ms  mean {a['mean_ms']:.2f} ms  "
                      f"(n={a['n']}, {result['failures'][k]} failed)")
            else:
                print(f"  {label}: NO VALID SAMPLES — all {result['failures'][k]} "
                      "run(s) had a hook exit non-zero")
        print(f"  TOTAL (on-off)       : median {_ms(total['median_ms'])}  "
              f"p95 {_ms(total['p95_ms'])}   <- decision metric")
        print(f"  source cost (src-off): median {_ms(source_cost)}")
        print(f"  dispatch (on-src)    : median {_ms(dispatch_cost)}")
        print(f"  threshold 15.00 ms median (on TOTAL)  ->  verdict: {result['verdict']}")
    if result["total_failures"]:
        # A failed hook invocation means the sample is not trustworthy — say so
        # loudly and fail the process rather than reporting a clean-looking run.
        print(f"WARNING: {result['total_failures']} hook run(s) exited non-zero "
              f"and were excluded from the timings: {result['failures']}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
