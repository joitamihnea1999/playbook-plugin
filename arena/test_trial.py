#!/usr/bin/env python3
"""Self-test for the arena trial engine (Tier 2), driven by the deterministic
FakeRunner/NullRunner — no agents, no tokens. Proves the orchestration, scoring,
frozen decision rule, budget cap, and ledger are correct independent of where the
agent work comes from. (The live SandboxRunner is validated on first real use;
its verdict logic is exactly this, tested here.)

Run directly: python3 arena/test_trial.py
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import trial  # noqa: E402


def _make_case(check, reps=2, prompt="do it"):
    d = Path(tempfile.mkdtemp(prefix="arena-case-"))
    (d / "workspace").mkdir()
    (d / "case.json").write_text(json.dumps(
        {"name": "t", "agent": "claude", "prompt": prompt,
         "workspace": "workspace", "check": check, "reps": reps}), encoding="utf-8")
    return d


def _writes(marker):
    def fn(arm, rep, ws):
        (Path(ws) / marker).write_text("ok", encoding="utf-8")
    return fn


class DecisionRule(unittest.TestCase):
    def test_adopt_when_treatment_fixes_a_failing_check(self):
        case = _make_case(check="test -f fixed.txt")
        runner = trial.FakeRunner({"treatment": _writes("fixed.txt")})  # baseline does nothing
        out = trial.run_trial(case, "HEAD", runner=runner)
        self.assertEqual(out["agg"]["baseline"]["passed"], 0)
        self.assertEqual(out["agg"]["treatment"]["passed"], out["agg"]["treatment"]["executed"])
        self.assertEqual(out["decision"], "ADOPT")

    def test_reject_when_treatment_breaks_a_passing_check(self):
        case = _make_case(check="test -f fixed.txt")
        runner = trial.FakeRunner({"baseline": _writes("fixed.txt")})  # treatment does nothing
        out = trial.run_trial(case, "HEAD", runner=runner)
        self.assertEqual(out["decision"], "REJECT")

    def test_retest_when_arms_are_identical(self):
        case = _make_case(check="test -f fixed.txt")
        both = _writes("fixed.txt")
        out = trial.run_trial(case, "HEAD", runner=trial.FakeRunner({"baseline": both, "treatment": both}))
        self.assertEqual(out["decision"], "RETEST")

    def test_retest_when_an_arm_never_executes(self):
        case = _make_case(check="true")
        def boom(arm, rep, ws):
            raise RuntimeError("agent crashed")
        out = trial.run_trial(case, "HEAD", runner=trial.FakeRunner({"treatment": boom}))
        self.assertEqual(out["agg"]["treatment"]["executed"], 0)
        self.assertEqual(out["decision"], "RETEST")


class BudgetAndLedger(unittest.TestCase):
    def test_max_runs_caps_total_agent_runs(self):
        case = _make_case(check="true", reps=5)
        out = trial.run_trial(case, "HEAD", max_runs=4, runner=trial.NullRunner())
        # 2 arms * clamp(4//2=2) = 4 runs total, not 10.
        self.assertEqual(out["reps"], 2)
        self.assertEqual(len(out["records"]), 4)

    def test_ledger_records_every_run(self):
        case = _make_case(check="true", reps=3)
        ledger = Path(tempfile.mkdtemp()) / "runs.jsonl"
        trial.run_trial(case, "HEAD", runner=trial.NullRunner(), ledger_path=str(ledger))
        lines = [l for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual(len(lines), 6)                 # 2 arms * 3 reps
        self.assertTrue(all(json.loads(l)["runner"] == "null" for l in lines))


class OfflineCanary(unittest.TestCase):
    def test_shipped_canary_returns_retest(self):
        canary = _HERE / "cases" / "canary-noop"
        out = trial.run_trial(canary, "HEAD", runner=trial.NullRunner())
        self.assertEqual(out["decision"], "RETEST")
        # identical arms → equal pass rates
        self.assertEqual(out["agg"]["baseline"], out["agg"]["treatment"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
