#!/usr/bin/env python3
"""Task 049 / plan §5.2: `run --spec-mode compact` — the spec is Intent + Why + References +
Work Plan only (Design Phase dropped), recorded in the manifest and every result line, pinned
by --resume, and reportable by `corpus validate --transport --spec-mode compact`. It is the
fallback for a case that would not otherwise fit a seat; used identically for every candidate."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bench.lib import cases, package, records, runner  # noqa: E402

_ENTRY = _ROOT / "bench" / "judgebench.py"
SHA = "a" * 40
SPEC = ("## Status\npending\n\n## Risk\nreversible\n\n## Intent\nKEEP_INTENT\n\n## Why\nKEEP_WHY\n\n"
        "## References\n- KEEP_REFS\n\n## Design Phase\n- [x] Restate — DROP_DESIGN\n\n## Work Plan\n- [ ] W1 KEEP_WP\n\n"
        "## Pre-review\n- [ ] tests DROP_PREREVIEW\n")


def _mk_corpus(root: Path, cid="c1"):
    d = root / "cases" / cid
    d.mkdir(parents=True)
    (d / "case.json").write_text(json.dumps({"id": cid, "source": {"workspace": "w", "task": "001", "repo": "r"},
        "repo_base_sha": SHA, "diff_of": f"{SHA}..{SHA}", "kind": "feature", "area": "server",
        "difficulty": "easy", "truth_version": 1}), encoding="utf-8")
    (d / "truth.json").write_text(json.dumps({"findings": [], "known_rejects": []}), encoding="utf-8")
    (d / "spec.md").write_text(SPEC, encoding="utf-8")
    (d / "diff.patch").write_text("+x\n", encoding="utf-8")
    (root / "corpus.json").write_text(json.dumps({"version": 1, "cases": [cid]}), encoding="utf-8")
    return cases.load_corpus(root)


def _run(*args, cwd):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, str(_ENTRY), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd=cwd, env=env, timeout=300)


class CompactSpecTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.corpus = _mk_corpus(self.root / "corpus")
        self.case = self.corpus.cases[0]

    def test_compact_keeps_only_intent_why_references_work_plan(self):
        pkg = package.build_package(self.case, spec_mode="compact")
        for keep in ("KEEP_INTENT", "KEEP_WHY", "KEEP_REFS", "KEEP_WP"):
            self.assertIn(keep, pkg.spec, keep)
        for drop in ("DROP_DESIGN", "DROP_PREREVIEW", "## Status", "## Risk"):
            self.assertNotIn(drop, pkg.spec, drop)
        self.assertEqual(pkg.spec_mode, "compact")
        full = package.build_package(self.case)
        self.assertEqual(full.spec_mode, "full")
        self.assertIn("DROP_DESIGN", full.spec)
        self.assertLess(pkg.prompt_chars, full.prompt_chars)

    def test_unknown_spec_mode_is_an_error(self):
        with self.assertRaises(ValueError):
            package.build_package(self.case, spec_mode="terse")

    def test_compact_still_runs_the_leak_scan(self):
        (self.case.spec_path).write_text(SPEC.replace("KEEP_WP", "KEEP_WP PANEL VERDICT: PASS"), encoding="utf-8")
        with self.assertRaises(package.LeakageError):
            package.build_package(self.case, spec_mode="compact")

    def test_run_records_spec_mode_in_manifest_and_result_lines_and_resume_pins_it(self):
        runs = self.root / "runs"
        p = _run("run", "--fake", "--cases", "all", "--candidates", "sol-med,sol-high", "--run-id", "r1",
                 "--spec-mode", "compact", "--corpus", str(self.root / "corpus"), "--runs-dir", str(runs), cwd=str(_ROOT))
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        manifest = json.loads((runs / "r1" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["spec_mode"], "compact")
        for lb in ("sol-med", "sol-high"):
            recs, _torn = records.read_results(runs / "r1", lb)
            self.assertTrue(recs)
            self.assertTrue(all(r["spec_mode"] == "compact" for r in recs), recs)
        # the corpus hashes in the manifest are of the COMPACT package (what the judge saw; the run adds
        # the default time-budget clause, so build with the same soft/hard timeouts)
        pkg = package.build_package(self.case, spec_mode="compact", soft_timeout_secs=900, hard_timeout_secs=1200)
        self.assertEqual(manifest["corpus"]["hashes"]["c1"]["prompt"], records.case_hashes(self.case, pkg)["prompt"])
        # resume without the flag (= full) must refuse
        p2 = _run("run", "--fake", "--cases", "all", "--candidates", "sol-med,sol-high", "--run-id", "r1", "--resume",
                  "--corpus", str(self.root / "corpus"), "--runs-dir", str(runs), cwd=str(_ROOT))
        self.assertEqual(p2.returncode, 2, p2.stdout + p2.stderr)
        self.assertIn("spec_mode", p2.stderr + p2.stdout)
        p3 = _run("run", "--fake", "--cases", "all", "--candidates", "sol-med,sol-high", "--run-id", "r1", "--resume",
                  "--spec-mode", "compact", "--corpus", str(self.root / "corpus"), "--runs-dir", str(runs), cwd=str(_ROOT))
        self.assertEqual(p3.returncode, 0, p3.stdout + p3.stderr)

    def test_default_run_records_full(self):
        runs = self.root / "runs"
        p = _run("run", "--fake", "--cases", "all", "--candidates", "sol-med", "--run-id", "r2",
                 "--corpus", str(self.root / "corpus"), "--runs-dir", str(runs), cwd=str(_ROOT))
        self.assertEqual(p.returncode, 0, p.stderr)
        manifest = json.loads((runs / "r2" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["spec_mode"], "full")

    def test_transport_report_honours_spec_mode(self):
        p = _run("corpus", "validate", "--transport", "--spec-mode", "compact", "--corpus", str(self.root / "corpus"), cwd=str(_ROOT))
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertIn("spec_mode=compact", p.stdout)


if __name__ == "__main__":
    unittest.main()
