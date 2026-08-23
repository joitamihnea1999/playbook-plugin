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


def _run_fused_once(cwd: str, env: dict) -> None:
    """One Bash-tool call's worth of PreToolUse hooks: task-gate + command-guard,
    each a fresh bash process reading the fixed payload on stdin."""
    for hook in (TASK_GATE, CMD_GUARD):
        subprocess.run(
            ["bash", str(hook)], input=PAYLOAD, cwd=cwd, env=env,
            capture_output=True, text=True,
        )


def _time_call(cwd: str, env: dict) -> float:
    t0 = time.perf_counter()
    _run_fused_once(cwd, env)
    return (time.perf_counter() - t0) * 1000.0        # ms


def _summary(samples: list[float]) -> dict:
    s = sorted(samples)
    return {
        "n": len(s),
        "median_ms": round(statistics.median(s), 3),
        "p95_ms": round(s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))], 3),
        "mean_ms": round(statistics.fmean(s), 3),
    }


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", type=int, default=200)
    ap.add_argument("--warmup", type=int, default=10)
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args(argv)

    if not DEV_BASH_LOG.is_file():
        print(f"ERROR: dev bash-log.sh not found at {DEV_BASH_LOG}", file=sys.stderr)
        return 2

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
        with _make_project() as proj:
            cwd = str(Path(proj))
            for _ in range(max(0, args.warmup)):
                for env in arms.values():
                    _time_call(cwd, env)
            names = list(arms)
            for i in range(args.runs):
                # Flip arm order on alternate iterations so no arm is
                # systematically first/last (panel: fixed order can bias).
                order = names if i % 2 == 0 else list(reversed(names))
                for k in order:
                    samples[k].append(_time_call(cwd, arms[k]))
    finally:
        try:
            os.unlink(empty_env)
        except OSError:
            pass

    s = {k: _summary(v) for k, v in samples.items()}
    total = {
        "median_ms": round(s["on"]["median_ms"] - s["off"]["median_ms"], 3),
        "p95_ms": round(s["on"]["p95_ms"] - s["off"]["p95_ms"], 3),
    }
    source_cost = round(s["source_only"]["median_ms"] - s["off"]["median_ms"], 3)
    dispatch_cost = round(s["on"]["median_ms"] - s["source_only"]["median_ms"], 3)
    result = {"runs": args.runs, "payload": PAYLOAD,
              "dev_bash_log": str(DEV_BASH_LOG), "arms": s,
              "total_median_ms": total["median_ms"], "total_p95_ms": total["p95_ms"],
              "source_cost_median_ms": source_cost,
              "dispatch_cost_median_ms": dispatch_cost,
              "decision_threshold_ms": 15.0,
              "verdict": ("NARROW-TRAP" if total["median_ms"] > 15.0 else "NO-CHANGE")}

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"fused Bash-tool hook path (task-gate + command-guard), "
              f"N={args.runs}, fresh process each")
        for k, label in (("off", "no BASH_ENV "), ("source_only", "source-only "),
                         ("on", "with trap  ")):
            print(f"  {label}: median {s[k]['median_ms']:.2f} ms  "
                  f"p95 {s[k]['p95_ms']:.2f} ms  mean {s[k]['mean_ms']:.2f} ms")
        print(f"  TOTAL (on-off)       : median {total['median_ms']:+.2f} ms  "
              f"p95 {total['p95_ms']:+.2f} ms   <- decision metric")
        print(f"  source cost (src-off): median {source_cost:+.2f} ms")
        print(f"  dispatch (on-src)    : median {dispatch_cost:+.2f} ms")
        print(f"  threshold 15.00 ms median (on TOTAL)  ->  verdict: {result['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
