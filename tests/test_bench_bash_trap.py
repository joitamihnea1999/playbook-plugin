#!/usr/bin/env python3
"""`scripts/bench_bash_trap.py` must not let a failed hook invocation pollute the
timing sample.

The bench runs the two fused PreToolUse hooks per measured call. A hook that
exits non-zero (a crash, or a block) still consumes wall-clock, and if its time
is folded into the median/p95 the reported latency is silently wrong — a
measuring tool that cannot tell a good sample from a broken one. So each run's
exit status is asserted: a run where any hook exits non-zero is counted as a
FAILURE and excluded from the timing sample, and failures are reported
separately from timings.

Run: python3 -m unittest tests.test_bench_bash_trap
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import stat
import tempfile
import unittest
from pathlib import Path

from tests._bashcheck import bash_or_skip

_HERE = Path(__file__).resolve().parent
_BENCH = _HERE.parent / "scripts" / "bench_bash_trap.py"


def _load_bench():
    loader = importlib.machinery.SourceFileLoader("_pb_bench_under_test", str(_BENCH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _stub_hook(dir_: Path, name: str, code: int) -> Path:
    p = dir_ / name
    # newline="\n": on Windows the default text mode would translate \n -> \r\n,
    # so git-bash would run `exit 0\r` — an invalid numeric argument that exits 1,
    # not 0 (the exact failure this guards). Keep LF-only. No-op on POSIX.
    p.write_text(f"#!/usr/bin/env bash\nexit {code}\n", encoding="utf-8", newline="\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


class BenchExitStatus(unittest.TestCase):
    def test_run_fused_once_returns_exit_codes(self):
        # Use the RESOLVED bash (skips if none usable): bare `bash` on Windows is
        # the System32 WSL stub, which exits non-zero without running the script,
        # so both hooks would read as 1 regardless of their real exit code.
        bash = bash_or_skip()
        bench = _load_bench()
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            good = _stub_hook(d, "ok-hook", 0)
            bad = _stub_hook(d, "bad-hook", 1)
            rcs = bench._run_fused_once(td, dict(os.environ), hooks=(good, bad), bash=bash)
        self.assertEqual([0, 1], list(rcs),
                         "_run_fused_once must surface each hook's exit code")

    def test_run_ok_classifies_failure(self):
        bench = _load_bench()
        self.assertTrue(bench._run_ok([0, 0]))
        self.assertFalse(bench._run_ok([0, 1]))
        self.assertFalse(bench._run_ok([2, 0]))

    def test_failed_runs_excluded_and_counted(self):
        # A full tiny bench run against a FAILING hook must record zero valid
        # timing samples for every arm and a positive failure count — never fold
        # the broken run into the median.
        bash = bash_or_skip()
        bench = _load_bench()
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            bad_a = _stub_hook(d, "bad-a", 1)
            bad_b = _stub_hook(d, "bad-b", 3)
            result = bench.run_bench(runs=3, warmup=1, hooks=(bad_a, bad_b), bash=bash)
        self.assertTrue(any(v > 0 for v in result["failures"].values()),
                        "failing hooks must be counted as failures")
        for arm, summ in result["arms"].items():
            self.assertEqual(0, summ["n"],
                             f"arm {arm} folded a failed run into its timings")


if __name__ == "__main__":
    unittest.main()
