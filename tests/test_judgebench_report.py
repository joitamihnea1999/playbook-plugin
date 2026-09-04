#!/usr/bin/env python3
"""Step 8 tests: aggregation + report (`bench/lib/report.py`, `bench/lib/rates.py`,
`judgebench report`) and the full offline pipeline run --fake → adjudicate → report
(plan §24 acceptance: DNF / timeout / malformed demonstrably distinct)."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bench.lib import cases, rates, records, report, scoring  # noqa: E402

SHA40 = "0123456789abcdef0123456789abcdef01234567"
ENTRY = _ROOT / "bench" / "judgebench.py"

TRUTH = {"findings": [
    {"id": "T1", "file": "src/a.py", "symbol": "f", "failure_mode": "x", "severity": "Critical",
     "historical_outcome": "accepted+fixed"},
    {"id": "T2", "file": "src/b.py", "symbol": "g", "failure_mode": "y", "severity": "Important",
     "historical_outcome": "accepted+fixed"},
    {"id": "T3", "file": "src/c.py", "symbol": "h", "failure_mode": "z", "severity": "Minor",
     "historical_outcome": "accepted+parked"}],
    "known_rejects": [{"id": "R1", "claim": "c", "why_rejected": "w", "file": "src/r.py", "symbol": "rj"}]}


def _mk_corpus(root: Path, ids=("c-a", "c-b")):
    for cid in ids:
        d = root / "cases" / cid
        d.mkdir(parents=True)
        (d / "case.json").write_text(json.dumps({
            "id": cid, "source": {"workspace": "w", "task": "001", "repo": "r"},
            "repo_base_sha": SHA40, "diff_of": SHA40[:7], "kind": "feature", "area": "enforcement",
            "difficulty": "medium", "truth_version": 1}), encoding="utf-8")
        (d / "truth.json").write_text(json.dumps(TRUTH), encoding="utf-8")
        (d / "spec.md").write_text("# spec\n", encoding="utf-8")
        (d / "diff.patch").write_text("--- a\n+++ b\n", encoding="utf-8")
    (root / "corpus.json").write_text(json.dumps({"version": 1, "cases": list(ids)}), encoding="utf-8")
    return cases.load_corpus(root)


def _rec(case_id, label, status, findings=(), duration_ms=1000, usage=None, backend="codex", variant="m"):
    fs = [{"n": i, "file": f[0], "symbol": f[1], "line": None, "claimed_severity": f[2],
           "severity_known": True, "text": "w"} for i, f in enumerate(findings, 1)]
    return {"case_id": case_id, "label": label, "spec": f"{backend}:{variant}", "backend": backend,
            "variant": variant, "status": status, "duration_ms": duration_ms,
            "usage": usage or {"status": "unknown"},
            "findings": ({"status": "ok" if fs else "empty", "findings": fs, "errors": []}
                         if status == "ok" else None)}


class RatesTests(unittest.TestCase):
    def test_no_fabrication(self):
        self.assertIsNone(rates.estimate_usd("codex", "gpt-5.6-sol:medium", {"status": "unknown"}))
        self.assertIsNone(rates.estimate_usd("codex", "gpt-5.6-sol:medium", {"status": "known", "in": 1000, "out": 10}))
        self.assertIsNone(rates.rate_for("codex", "gpt-5.6-sol:medium"))
        self.assertEqual(rates.pool_of("grok"), "xai")

    def test_known_rate_and_usage(self):
        usd = rates.estimate_usd("agy", "gemini-3.8-flash:high", {"status": "known", "in": 1_000_000, "out": 100_000})
        self.assertAlmostEqual(usd, 0.75 + 0.375)


class AggregateTests(unittest.TestCase):
    def _build(self, td):
        corpus = _mk_corpus(Path(td) / "corpus")
        rd = Path(td) / "runs" / "r"
        rd.mkdir(parents=True)
        recs = {
            "a": [_rec("c-a", "a", "ok", [("src/a.py", "f", "Critical"), ("src/b.py", "g", "Minor"),
                                         ("src/r.py", "rj", "Minor"), ("new.py", "n", "Important")],
                       duration_ms=2000, usage={"status": "known", "in": 10, "out": 2000}),
                  _rec("c-b", "a", "timeout", duration_ms=1200000)],
            "b": [_rec("c-a", "b", "ok", [("src/a.py", "f", "Critical"), ("src/c.py", "h", "Minor")], duration_ms=4000),
                  _rec("c-b", "b", "dnf", duration_ms=0)],
            "c": [_rec("c-a", "c", "malformed", duration_ms=3000),
                  _rec("c-b", "c", "excluded", duration_ms=0)],
        }
        for label, rs in recs.items():
            for r in rs:
                records.append_result(rd, label, r)
        results = records.all_results(rd)
        adj = scoring.load_adjudication(rd)
        scoring.auto_adjudicate(results, corpus, adj)
        scoring.save_adjudication(rd, adj)
        return corpus, rd, results, adj

    def test_numbers_and_distinct_statuses(self):
        with tempfile.TemporaryDirectory() as td:
            corpus, rd, results, adj = self._build(td)
            rep = report.aggregate("r", results, adj, corpus, weights=(8, 3, 1))
            rows = {r.label: r for r in rep.rows}
            a, b, c = rows["a"], rows["b"], rows["c"]
            self.assertEqual(a.counts, {"ok": 1, "timeout": 1})
            self.assertEqual(b.counts, {"ok": 1, "dnf": 1})
            self.assertEqual(c.counts, {"malformed": 1, "excluded": 1})
            # a: T1 (Critical) + T2 (Important, claimed Minor → mismatch) valid; R1 fp; new.py pending
            self.assertEqual((a.valid, a.unique_valid, a.fp, a.pending), (2, 1, 1, 1))
            self.assertEqual(a.by_severity, {"Critical": 1, "Important": 1, "Minor": 0})
            self.assertEqual(a.severity_mismatch, 1)
            self.assertEqual(a.weighted, 8 + 3)
            self.assertAlmostEqual(a.fp_rate, 1 / 3)
            self.assertEqual((a.tokens_known, a.tokens_out), (1, 2000))
            self.assertAlmostEqual(a.weighted_per_1k_out, 11 / 2.0)
            self.assertIsNone(a.usd)                                 # known usage but no rate on file
            # b: T1 shared with a (not unique), T3 unique
            self.assertEqual((b.valid, b.unique_valid, b.fp), (2, 1, 0))
            self.assertEqual(b.by_severity, {"Critical": 1, "Important": 0, "Minor": 1})
            self.assertEqual(b.weighted, 9)
            self.assertIsNone(b.weighted_per_1k_out)                 # tokens unknown → n/a, never estimated
            self.assertEqual(c.valid, 0)
            # latency only over invocations that ran
            self.assertEqual((a.p50_ms, a.p95_ms), (2000, 1200000))
            self.assertEqual((c.p50_ms, c.p95_ms), (3000, 3000))
            self.assertEqual(a.rate("timeout"), 0.5); self.assertEqual(b.rate("dnf"), 0.5)
            self.assertEqual(rep.matrix["c-a"]["a"], "ok T1,T2 fp=1 ?=1")
            self.assertEqual(rep.matrix["c-b"]["b"], "dnf")
            self.assertEqual(rep.matrix["c-b"]["c"], "excluded")
            self.assertEqual(rep.pending_total, 1)

    def test_render_text_and_markdown_label_everything(self):
        with tempfile.TemporaryDirectory() as td:
            corpus, rd, results, adj = self._build(td)
            rep = report.aggregate("r", results, adj, corpus, weights=(8, 3, 1))
            txt = report.render_text(rep)
            md = report.render_markdown(rep)
            for out in (txt, md):
                for col in ("malformed", "timeout", "dnf", "excluded", "unique", "fp-rate", "p95", "usd"):
                    self.assertIn(col, out)
                self.assertIn("point estimates only", out)
                self.assertIn("Critical=8 Important=3 Minor=1", out)
                self.assertIn("non-authoritative", out)
                self.assertIn("await adjudication", out)
                self.assertIn("ok T1,T2 fp=1 ?=1", out)
            self.assertIn("| candidate |", md)
            self.assertIn("## Per-case matrix", md)
            # table row for a: the three failure classes are separate columns with separate counts
            row_a = [ln for ln in txt.splitlines() if ln.startswith("a ")][0].split()
            cols = list(report.COLUMNS)
            self.assertEqual(row_a[cols.index("timeout")], "1")
            self.assertEqual(row_a[cols.index("dnf")], "0")
            self.assertEqual(row_a[cols.index("malformed")], "0")
            row_c = [ln for ln in txt.splitlines() if ln.startswith("c ")][0].split()
            self.assertEqual(row_c[cols.index("malformed")], "1")
            self.assertEqual(row_c[cols.index("excluded")], "1")

    def test_manifest_candidates_and_missing_pairs_are_visible(self):
        # impl-panel r2 sol #1: an interrupted run must not present a seat with no results
        # as absent, nor a half-run seat as complete. opus F3: sev-mismatch is rendered.
        with tempfile.TemporaryDirectory() as td:
            corpus, rd, results, adj = self._build(td)
            manifest = {"run_id": "r", "corpus": {"cases": ["c-a", "c-b", "c-c"]},
                        "candidates": [{"label": "a", "spec": "codex:m"}, {"label": "b", "spec": "codex:m"},
                                       {"label": "c", "spec": "codex:m"}, {"label": "d", "spec": "grok:x"}]}
            rep = report.aggregate("r", results, adj, corpus, weights=(8, 3, 1), manifest=manifest)
            self.assertEqual([r.label for r in rep.rows], ["a", "b", "c", "d"])
            self.assertEqual(rep.rows[3].invocations, 0)
            self.assertEqual(rep.matrix["c-c"]["a"], "missing")
            self.assertEqual(rep.matrix["c-a"]["d"], "missing")
            self.assertEqual(rep.missing_pairs, 3 + 3)              # d × 3 cases + c-c × a,b,c
            txt = report.render_text(rep)
            self.assertIn("missing", txt)
            self.assertIn("6 (case, candidate) pair(s) have no result", txt)
            self.assertIn("sev-mis", txt)
            cols = list(report.COLUMNS)
            row_a = [ln for ln in txt.splitlines() if ln.startswith("a ")][0].split()
            self.assertEqual(row_a[cols.index("sev-mis")], "1")

    def test_weights_and_percentile_helpers(self):
        self.assertEqual(report.parse_weights("8,3,1"), (8.0, 3.0, 1.0))
        for bad in ("8,3", "a,b,c", "-1,0,0"):
            with self.assertRaises(ValueError):
                report.parse_weights(bad)
        self.assertIsNone(report.percentile([], 50))
        self.assertEqual(report.percentile([5, 1, 3], 50), 3)
        self.assertEqual(report.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95), 10)
        self.assertEqual(report.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 50), 5)


class PipelineTests(unittest.TestCase):
    """§24: run --fake → adjudicate --auto → report, offline, through the real CLI."""

    def test_full_pipeline(self):
        with tempfile.TemporaryDirectory() as td:
            corpus_dir = Path(td) / "corpus"; _mk_corpus(corpus_dir)
            runs = Path(td) / "runs"
            script = Path(td) / "script.json"
            script.write_text(json.dumps({
                "default": {"status": "ok", "findings": [
                    {"file": "src/a.py", "symbol": "f", "severity": "Critical", "why": "x"}]},
                "c-b|sol-high": {"status": "dnf"}, "c-a|sol-high": {"status": "malformed"},
                "c-b|sol-med": {"status": "timeout"}}), encoding="utf-8")
            common = ["--corpus", str(corpus_dir), "--runs-dir", str(runs)]
            def cli(*a):
                return subprocess.run([sys.executable, str(ENTRY), *a], capture_output=True, text=True,
                                      encoding="utf-8", errors="replace", timeout=300)
            p = cli("run", "--cases", "all", "--candidates",
                    "sol-med=codex:gpt-5.6-sol:medium,sol-high=codex:gpt-5.6-sol:high",
                    "--run-id", "smoke", "--fake", "--fake-script", str(script), *common)
            self.assertEqual(p.returncode, 1, p.stderr)            # completed-with-DNFs
            p = cli("adjudicate", "smoke", "--auto", *common)
            self.assertEqual(p.returncode, 0, p.stderr)
            md = Path(td) / "report.md"
            p = cli("report", "smoke", "--md", str(md), "--weights", "8,3,1", *common)
            self.assertEqual(p.returncode, 0, p.stderr)
            out = p.stdout
            self.assertIn("judgebench report — run smoke", out)
            self.assertTrue(md.is_file())
            cols = list(report.COLUMNS)
            med = [ln for ln in out.splitlines() if ln.startswith("sol-med ")][0].split()
            high = [ln for ln in out.splitlines() if ln.startswith("sol-high ")][0].split()
            self.assertEqual((med[cols.index("ok")], med[cols.index("timeout")], med[cols.index("valid")]),
                             ("1", "1", "1"))
            self.assertEqual((high[cols.index("malformed")], high[cols.index("dnf")], high[cols.index("valid")]),
                             ("1", "1", "0"))
            self.assertIn("c-a", out); self.assertIn("ok T1", out)
            # bad weights / missing run → exit 2
            self.assertEqual(cli("report", "smoke", "--weights", "1,2", *common).returncode, 2)
            self.assertEqual(cli("report", "nope", *common).returncode, 2)


if __name__ == "__main__":
    unittest.main()
