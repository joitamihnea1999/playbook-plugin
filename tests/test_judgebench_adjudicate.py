#!/usr/bin/env python3
"""Step 7 tests: deterministic matching + adjudication + validity
(`bench/lib/scoring.py` part 2, `cases.py` additive schema, `judgebench adjudicate`).

Auto-matching credits a finding ONLY when its (file, symbol) key hits exactly
one truth entry; collisions and novel findings go to a human whose stdin is
scripted here. `valid-new` appends to truth.json (deduped) and bumps
truth_version in both truth.json and case.json; the corpus must reload clean.
"""
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bench.lib import cases, records, scoring  # noqa: E402

SHA40 = "0123456789abcdef0123456789abcdef01234567"
ENTRY = _ROOT / "bench" / "judgebench.py"

TRUTH = {
    "findings": [
        {"id": "T1", "file": "plugins/playbook/tasks/core.py", "symbol": "extract_risk",
         "failure_mode": "fenced heading shadow", "severity": "Critical", "historical_outcome": "accepted+fixed"},
        {"id": "T2", "file": "plugins/playbook/tasks/core.py", "symbol": "extract_risk",
         "failure_mode": "status parsed from archive", "severity": "Important", "historical_outcome": "accepted+parked"},
        {"id": "T3", "file": "plugins/playbook/tasks/lifecycle.py", "symbol": "close_task",
         "failure_mode": "verify skipped on --force", "severity": "Important", "historical_outcome": "accepted+fixed"},
        {"id": "T4", "file": "plugins/playbook/tasks/audit.py", "symbol": None,
         "failure_mode": "stale marker", "severity": "Minor", "historical_outcome": "accepted+fixed"},
    ],
    "known_rejects": [
        {"id": "R1", "claim": "atomic_write not fsynced", "why_rejected": "by design",
         "file": "plugins/playbook/tasks/atomic.py", "symbol": "atomic_write"},
        {"id": "R2", "claim": "prose-only reject", "why_rejected": "no key → never auto"},
    ],
}


def _finding(file, symbol=None, n=1, sev="Important", text="why", known=True):
    return scoring.Finding(n=n, file=file, symbol=symbol, line=None, claimed_severity=sev,
                           severity_known=known, text=text)


def _mk_corpus(root: Path, truth=TRUTH, ids=("c-a",)):
    for cid in ids:
        d = root / "cases" / cid
        d.mkdir(parents=True)
        (d / "case.json").write_text(json.dumps({
            "id": cid, "source": {"workspace": "w", "task": "001", "repo": "r"},
            "repo_base_sha": SHA40, "diff_of": SHA40[:7], "kind": "feature", "area": "enforcement",
            "difficulty": "medium", "truth_version": 1}), encoding="utf-8")
        (d / "truth.json").write_text(json.dumps(truth), encoding="utf-8")
        (d / "spec.md").write_text("# spec\n", encoding="utf-8")
        (d / "diff.patch").write_text("--- a\n+++ b\n", encoding="utf-8")
    (root / "corpus.json").write_text(json.dumps({"version": 1, "cases": list(ids)}), encoding="utf-8")
    return cases.load_corpus(root)


def _mk_run(run_dir: Path, per_label: dict, case_id="c-a"):
    """per_label: label → list of finding dicts (file, symbol, severity, why)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    records.write_manifest(run_dir, {"run_id": run_dir.name, "mode": "fake", "candidates": []})
    for label, fs in per_label.items():
        findings = [{"n": i, "file": f["file"], "symbol": f.get("symbol"), "line": None,
                     "claimed_severity": f.get("severity", "Important"), "severity_known": True,
                     "text": f.get("why", "w")} for i, f in enumerate(fs, 1)]
        records.append_result(run_dir, label, {
            "case_id": case_id, "label": label, "spec": label, "status": "ok",
            "findings": {"status": "ok" if findings else "empty", "findings": findings, "errors": []}})


class NormalizeTests(unittest.TestCase):
    def test_tables(self):
        self.assertEqual(scoring.normalize_path("./a\\b.py"), "a/b.py")
        self.assertEqual(scoring.normalize_path("`/a/b.py/`"), "a/b.py")
        self.assertEqual(scoring.normalize_symbol("Extract_Risk()"), "extract_risk")
        self.assertIsNone(scoring.normalize_symbol("-"))
        self.assertIsNone(scoring.normalize_symbol(None))
        self.assertEqual(scoring.normalize_failure_mode("  Fenced   HEADING shadow "), "fenced heading shadow")


class MatchTests(unittest.TestCase):
    def test_match_table(self):
        t = TRUTH
        # unique file+symbol → truth
        self.assertEqual(scoring.match_finding(_finding("plugins/playbook/tasks/lifecycle.py", "close_task"), t),
                         {"kind": "truth", "id": "T3"})
        # normalized variants still match
        self.assertEqual(scoring.match_finding(_finding("./plugins/playbook/tasks/lifecycle.py", "Close_Task()"), t)["id"], "T3")
        # two truths in one symbol → collision (never auto-credited)
        m = scoring.match_finding(_finding("plugins/playbook/tasks/core.py", "extract_risk"), t)
        self.assertEqual((m["kind"], sorted(m["ids"])), ("collision", ["T1", "T2"]))
        # symbol-less finding on a file whose ONE truth entry is also symbol-less → truth
        self.assertEqual(scoring.match_finding(_finding("plugins/playbook/tasks/audit.py"), t),
                         {"kind": "truth", "id": "T4"})
        # symbol-less finding vs a truth that NAMES a symbol → unmatched, never auto-credited
        # (impl-panel grok F1: a vague file-only hit must go to the human)
        self.assertEqual(scoring.match_finding(_finding("plugins/playbook/tasks/lifecycle.py"), t)["kind"],
                         "unmatched")
        self.assertEqual(scoring.match_finding(_finding("plugins/playbook/tasks/core.py"), t)["kind"], "unmatched")
        # a symbol-carrying finding vs a symbol-less truth → unmatched too (asymmetric keys)
        self.assertEqual(scoring.match_finding(_finding("plugins/playbook/tasks/audit.py", "helper"), t)["kind"],
                         "unmatched")
        # wrong symbol on a known file → unmatched (not a same-file guess)
        self.assertEqual(scoring.match_finding(_finding("plugins/playbook/tasks/lifecycle.py", "other"), t)["kind"],
                         "unmatched")
        # keyed reject → reject; prose-only reject never auto-matches
        self.assertEqual(scoring.match_finding(_finding("plugins/playbook/tasks/atomic.py", "atomic_write"), t),
                         {"kind": "reject", "id": "R1"})
        self.assertEqual(scoring.match_finding(_finding("nowhere.py", "x"), t), {"kind": "unmatched"})


class SchemaTests(unittest.TestCase):
    def test_reject_keys_optional_but_typed_and_valid_new_outcome_allowed(self):
        with tempfile.TemporaryDirectory() as td:
            c = _mk_corpus(Path(td))
            self.assertEqual(c.get("c-a").truth["known_rejects"][0]["file"], "plugins/playbook/tasks/atomic.py")
        bad = json.loads(json.dumps(TRUTH)); bad["known_rejects"][0]["symbol"] = 7
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(cases.CorpusError, "symbol"):
                _mk_corpus(Path(td), truth=bad)
        vn = json.loads(json.dumps(TRUTH)); vn["findings"][0]["historical_outcome"] = "valid-new"
        with tempfile.TemporaryDirectory() as td:
            _mk_corpus(Path(td), truth=vn)          # loads


class AutoAdjudicateTests(unittest.TestCase):
    def test_auto_records_only_unambiguous(self):
        with tempfile.TemporaryDirectory() as td:
            corpus = _mk_corpus(Path(td) / "corpus")
            rd = Path(td) / "runs" / "r"
            _mk_run(rd, {"a": [{"file": "plugins/playbook/tasks/lifecycle.py", "symbol": "close_task"},
                               {"file": "plugins/playbook/tasks/core.py", "symbol": "extract_risk"},
                               {"file": "plugins/playbook/tasks/atomic.py", "symbol": "atomic_write"},
                               {"file": "novel.py", "symbol": "f"}]})
            results = records.all_results(rd)
            counts = scoring.adjudicate(rd, corpus, results, auto_only=True)
            self.assertEqual((counts["auto_truth"], counts["auto_reject"], counts["pending"]), (1, 1, 2))
            adj = scoring.load_adjudication(rd)
            d = adj["decisions"]
            self.assertEqual(d["c-a|a|1"], {**d["c-a|a|1"], "verdict": "truth", "truth_id": "T3", "by": "auto"})
            self.assertEqual(d["c-a|a|3"]["verdict"], "reject")
            self.assertNotIn("c-a|a|2", d)          # collision stays pending
            self.assertNotIn("c-a|a|4", d)          # novel stays pending
            # idempotent
            counts2 = scoring.adjudicate(rd, corpus, results, auto_only=True)
            self.assertEqual((counts2["auto_truth"], counts2["pending"]), (0, 2))


class InteractiveTests(unittest.TestCase):
    def test_scripted_loop_m_v_i_u_q_and_truth_append(self):
        with tempfile.TemporaryDirectory() as td:
            corpus = _mk_corpus(Path(td) / "corpus")
            case = corpus.get("c-a")
            rd = Path(td) / "runs" / "r"
            _mk_run(rd, {"a": [{"file": "plugins/playbook/tasks/core.py", "symbol": "extract_risk", "why": "shadow"},
                               {"file": "novel.py", "symbol": "f", "severity": "Critical", "why": "Leaks the key. Bad."},
                               {"file": "meh.py", "symbol": "g"},
                               {"file": "hmm.py", "symbol": "h"},
                               {"file": "later.py", "symbol": "z"}],
                         "b": [{"file": "novel.py", "symbol": "F()", "why": "totally different wording"}]})
            results = records.all_results(rd)
            script = "\n".join(["bogus", "m T9", "m T1",           # bad cmd, bad id, then class as T1
                                "v", "key leak",                    # valid-new with failure mode
                                "i", "u", "q"]) + "\n"
            out = io.StringIO()
            counts = scoring.adjudicate(rd, corpus, results, stdin=io.StringIO(script), stdout=out)
            self.assertEqual(counts["human"], 4)
            self.assertEqual(counts["valid_new_added"], 1)
            text = out.getvalue()
            self.assertIn("no truth id 'T9'", text)
            self.assertIn("commands:", text)
            d = scoring.load_adjudication(rd)["decisions"]
            self.assertEqual((d["c-a|a|1"]["verdict"], d["c-a|a|1"]["truth_id"], d["c-a|a|1"]["by"]),
                             ("truth", "T1", "human"))
            self.assertEqual((d["c-a|a|2"]["verdict"], d["c-a|a|2"]["truth_id"]), ("valid-new", "T5"))
            self.assertEqual(d["c-a|a|3"]["verdict"], "invalid")
            self.assertEqual(d["c-a|a|4"]["verdict"], "unclear")
            self.assertNotIn("c-a|a|5", d)                      # quit before it
            self.assertNotIn("c-a|b|1", d)
            # truth.json appended + version bumped in BOTH files; corpus reloads clean
            truth = json.loads(case.truth_path.read_text(encoding="utf-8"))
            self.assertNotIn("truth_version", truth)          # case.json is the sole authority
            added = truth["findings"][-1]
            self.assertEqual((added["id"], added["file"], added["symbol"], added["failure_mode"],
                              added["severity"], added["historical_outcome"]),
                             ("T5", "novel.py", "f", "key leak", "Critical", "valid-new"))
            self.assertEqual(json.loads((case.path / "case.json").read_text())["truth_version"], 2)
            corpus2 = cases.load_corpus(Path(td) / "corpus")
            self.assertEqual(corpus2.get("c-a").truth_version, 2)
            # Second session: `b`'s differently-worded finding on the same key now
            # auto-matches the NEW truth entry (dedup across runs / candidates).
            counts = scoring.adjudicate(rd, corpus2, results, auto_only=True)
            self.assertEqual(counts["auto_truth"], 1)
            d = scoring.load_adjudication(rd)["decisions"]
            self.assertEqual((d["c-a|b|1"]["verdict"], d["c-a|b|1"]["truth_id"]), ("truth", "T5"))
            # `v` again for an equivalent (file, symbol, failure_mode) → reuses T5, no bump
            _mk_run(rd, {"c": [{"file": "novel.py", "symbol": "f", "why": "x"}]})
            results = records.all_results(rd)
            # c|1 auto-matches T5 already (same key) — force the dedup path directly:
            tid = scoring.append_valid_new(corpus2.get("c-a"), _finding("novel.py", "f"), "KEY  leak", "Minor")
            self.assertEqual(tid, "T5")
            self.assertEqual(json.loads((case.path / "case.json").read_text(encoding="utf-8"))["truth_version"], 2)

    def test_crash_between_truth_and_case_writes_leaves_corpus_loadable(self):
        # impl-panel opus F2 / sol #3 / terra #2 / grok F3: case.json is the SOLE authority for
        # truth_version; truth.json never carries it after adjudicate touches it, so a crash
        # after the first write cannot desync the pair.
        with tempfile.TemporaryDirectory() as td:
            corpus = _mk_corpus(Path(td) / "corpus")
            case = corpus.get("c-a")
            # corpus builder wrote a truth_version into truth.json (allowed while consistent)
            t = json.loads(case.truth_path.read_text()); t["truth_version"] = 1
            case.truth_path.write_text(json.dumps(t), encoding="utf-8")
            calls = {"n": 0}
            real = scoring._atomic_write
            def flaky(path, text):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise OSError("simulated crash before the second write")
                real(path, text)
            scoring._atomic_write = flaky
            try:
                with self.assertRaises(OSError):
                    scoring.append_valid_new(case, _finding("novel.py", "f"), "key leak", "Minor")
            finally:
                scoring._atomic_write = real
            corpus2 = cases.load_corpus(Path(td) / "corpus")            # still loads
            c2 = corpus2.get("c-a")
            self.assertEqual(c2.truth_version, 1)                       # version not bumped …
            self.assertEqual(c2.truth["findings"][-1]["file"], "novel.py")   # … finding present
            self.assertNotIn("truth_version", json.loads(c2.truth_path.read_text()))
            # the next append completes the bump
            tid = scoring.append_valid_new(c2, _finding("other.py", "g"), "x", "Minor")
            self.assertEqual(tid, "T6")
            self.assertEqual(cases.load_corpus(Path(td) / "corpus").get("c-a").truth_version, 2)

    def test_adjudicate_holds_the_run_lock(self):
        # impl-panel sol #3 / terra #3: two adjudicators on one run are refused, not interleaved.
        with tempfile.TemporaryDirectory() as td:
            corpus = _mk_corpus(Path(td) / "corpus")
            rd = Path(td) / "runs" / "r"
            _mk_run(rd, {"a": [{"file": "novel.py", "symbol": "f"}]})
            (rd / ".lock").write_text("999", encoding="utf-8")
            with self.assertRaises(records.RunLocked):
                scoring.adjudicate(rd, corpus, records.all_results(rd), stdin=io.StringIO(""),
                                   stdout=io.StringIO())
            (rd / ".lock").unlink()
            scoring.adjudicate(rd, corpus, records.all_results(rd), stdin=io.StringIO(""), stdout=io.StringIO())
            self.assertFalse((rd / ".lock").exists())

    def test_eof_saves_and_stops(self):
        with tempfile.TemporaryDirectory() as td:
            corpus = _mk_corpus(Path(td) / "corpus")
            rd = Path(td) / "runs" / "r"
            _mk_run(rd, {"a": [{"file": "novel.py", "symbol": "f"}]})
            counts = scoring.adjudicate(rd, corpus, records.all_results(rd), stdin=io.StringIO(""),
                                        stdout=io.StringIO())
            self.assertEqual(counts["human"], 0)
            self.assertTrue((rd / "adjudication.json").is_file())


class ValidityTests(unittest.TestCase):
    def test_unique_valid_is_human_classed_not_string_equal(self):
        with tempfile.TemporaryDirectory() as td:
            corpus = _mk_corpus(Path(td) / "corpus")
            rd = Path(td) / "runs" / "r"
            _mk_run(rd, {
                "a": [{"file": "plugins/playbook/tasks/lifecycle.py", "symbol": "close_task"},   # T3 auto
                      {"file": "novel.py", "symbol": "f", "why": "wording one"}],
                "b": [{"file": "plugins/playbook/tasks/lifecycle.py", "symbol": "close_task"},   # T3 auto
                      {"file": "novel.py", "symbol": "f", "why": "completely different wording"},
                      {"file": "plugins/playbook/tasks/atomic.py", "symbol": "atomic_write"}],   # R1 auto → FP
                "c": [{"file": "plugins/playbook/tasks/audit.py"},                                # T4 auto
                      {"file": "plugins/playbook/tasks/audit.py", "symbol": None, "why": "dup credit"}],
                "d": []})
            results = records.all_results(rd)
            adj = scoring.load_adjudication(rd)
            scoring.auto_adjudicate(results, corpus, adj)
            # human classes both novel wordings as the SAME new defect
            adj["decisions"]["c-a|a|2"] = {"verdict": "valid-new", "truth_id": "T5", "by": "human"}
            adj["decisions"]["c-a|b|2"] = {"verdict": "truth", "truth_id": "T5", "by": "human"}
            per = scoring.resolve_validity(results, adj)
            a, b, c = per[("c-a", "a")], per[("c-a", "b")], per[("c-a", "c")]
            self.assertEqual(set(a["valid"]), {"T3", "T5"}); self.assertEqual(a["unique"], set())
            self.assertEqual(set(b["valid"]), {"T3", "T5"}); self.assertEqual(b["unique"], set())
            self.assertEqual(len(b["fp"]), 1)
            self.assertEqual(set(c["valid"]), {"T4"}); self.assertEqual(c["unique"], {"T4"})
            self.assertEqual(len(c["valid"]), 1, "two findings on one truth id = one credit")
            self.assertNotIn(("c-a", "d"), per)            # empty result → no findings to resolve
            self.assertEqual(a["pending"], [])


class CliTests(unittest.TestCase):
    def test_cli_output_survives_a_cp1252_console(self):
        # The Windows lane runs a cp1252 console; every judgebench print carries
        # non-cp1252 glyphs (→, ×). Reproduce that console on every platform via
        # PYTHONIOENCODING and require the CLI to reconfigure stdio like tasks.cli does.
        import os
        with tempfile.TemporaryDirectory() as td:
            _mk_corpus(Path(td) / "corpus")
            rd = Path(td) / "runs" / "r"
            _mk_run(rd, {"a": [{"file": "plugins/playbook/tasks/lifecycle.py", "symbol": "close_task"}]})
            env = dict(os.environ, PYTHONIOENCODING="cp1252")
            common = ["--corpus", str(Path(td) / "corpus"), "--runs-dir", str(Path(td) / "runs")]
            for argv in (["adjudicate", "r", "--auto"], ["report", "r"]):
                p = subprocess.run([sys.executable, str(ENTRY), *argv, *common], capture_output=True,
                                   text=True, encoding="utf-8", errors="replace", timeout=120, env=env)
                self.assertEqual(p.returncode, 0, f"{argv}: {p.stderr}")
                self.assertNotIn("UnicodeEncodeError", p.stderr)

    def test_adjudicate_auto_via_cli(self):
        with tempfile.TemporaryDirectory() as td:
            _mk_corpus(Path(td) / "corpus")
            rd = Path(td) / "runs" / "r"
            _mk_run(rd, {"a": [{"file": "plugins/playbook/tasks/lifecycle.py", "symbol": "close_task"},
                               {"file": "novel.py", "symbol": "f"}]})
            p = subprocess.run([sys.executable, str(ENTRY), "adjudicate", "r", "--auto", "--corpus",
                                str(Path(td) / "corpus"), "--runs-dir", str(Path(td) / "runs")],
                               capture_output=True, text=True, encoding="utf-8", timeout=120)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("auto truth=1", p.stdout)
            self.assertIn("pending=1", p.stdout)
            p = subprocess.run([sys.executable, str(ENTRY), "adjudicate", "nope", "--auto", "--corpus",
                                str(Path(td) / "corpus"), "--runs-dir", str(Path(td) / "runs")],
                               capture_output=True, text=True, encoding="utf-8", timeout=120)
            self.assertEqual(p.returncode, 2)


if __name__ == "__main__":
    unittest.main()
