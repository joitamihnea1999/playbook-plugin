#!/usr/bin/env python3
"""Step 6 tests: records, manifest, lock, resume, bench spend journal, and the
`run` command wired end-to-end with the FakeRunner (`bench/lib/records.py`,
`bench/judgebench.py::cmd_run`).

Hermetic: temp corpus + temp runs dir; the FakeRunner never spawns anything.
The journal test plants a poison `.agent/` ANCESTOR above the run dir and cwd
(the ancestry-isolation pattern of tests/test_journal_ancestry_isolation.py)
and proves the bench's spend record lands ONLY under the run dir.
"""
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

SHA40 = "0123456789abcdef0123456789abcdef01234567"
ENTRY = _ROOT / "bench" / "judgebench.py"


def _mk_corpus(root: Path, ids=("c-a", "c-b")):
    for cid in ids:
        d = root / "cases" / cid
        d.mkdir(parents=True)
        (d / "case.json").write_text(json.dumps({
            "id": cid, "source": {"workspace": "w", "task": "001", "repo": "r"},
            "repo_base_sha": SHA40, "diff_of": SHA40[:7], "kind": "feature", "area": "enforcement",
            "difficulty": "medium", "truth_version": 1}), encoding="utf-8")
        (d / "truth.json").write_text(json.dumps({"findings": [], "known_rejects": []}), encoding="utf-8")
        (d / "spec.md").write_text(f"# {cid}\n\n## Intent\nx\n", encoding="utf-8")
        (d / "diff.patch").write_text("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n", encoding="utf-8")
    (root / "corpus.json").write_text(json.dumps({"version": 1, "cases": list(ids)}), encoding="utf-8")
    return cases.load_corpus(root)


def _cli(*args, cwd=None):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, str(ENTRY), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300, env=env,
                          cwd=str(cwd) if cwd else None)


class ResultFileTests(unittest.TestCase):
    def test_append_read_and_torn_line(self):
        with tempfile.TemporaryDirectory() as td:
            rd = Path(td)
            records.append_result(rd, "lab", {"case_id": "c-a", "label": "lab", "status": "ok"})
            records.append_result(rd, "lab", {"case_id": "c-b", "label": "lab", "status": "dnf"})
            with open(records.result_path(rd, "lab"), "a", encoding="utf-8") as f:
                f.write('{"case_id": "c-c", "label": "lab", "sta')          # crash mid-append
            recs, torn = records.read_results(rd, "lab")
            self.assertEqual([r["case_id"] for r in recs], ["c-a", "c-b"])
            self.assertEqual(torn, 1)
            done, torn_total = records.completed_pairs(rd, ["lab", "nolab"])
            self.assertEqual(done, {("c-a", "lab"), ("c-b", "lab")})
            self.assertEqual(torn_total, 1)
            self.assertEqual(records.read_results(rd, "nolab"), ([], 0))
            self.assertEqual(set(records.all_results(rd)), {"lab"})

    def test_raw_written_under_candidate_dir(self):
        with tempfile.TemporaryDirectory() as td:
            rel = records.write_raw(Path(td), "lab", "c-a", "hello ünïcode")
            self.assertEqual(rel, "lab/raw/c-a.txt")
            self.assertEqual((Path(td) / rel).read_text(encoding="utf-8"), "hello ünïcode")


class LockTests(unittest.TestCase):
    def test_exclusive_and_released(self):
        with tempfile.TemporaryDirectory() as td:
            rd = Path(td) / "run"
            with records.RunLock(rd):
                self.assertTrue((rd / ".lock").is_file())
                self.assertEqual((rd / ".lock").read_text(), str(os.getpid()))
                with self.assertRaisesRegex(records.RunLocked, str(os.getpid())):
                    with records.RunLock(rd):
                        pass
            self.assertFalse((rd / ".lock").exists())
            with records.RunLock(rd):                       # re-acquirable after release
                pass


class ManifestTests(unittest.TestCase):
    def test_hashes_and_mismatch_detection(self):
        with tempfile.TemporaryDirectory() as td:
            corpus = _mk_corpus(Path(td) / "corpus")
            cands = runner.parse_candidates("a=opus,b=codex:m")
            pk = {c.id: package.build_package(c) for c in corpus.cases}
            m = records.build_manifest(run_id="r", mode="fake", corpus=corpus, selected_cases=corpus.cases,
                                       packages=pk, candidates=cands, soft_timeout=1, hard_timeout=2,
                                       concurrency=2, source_repos={}, template_version="v1",
                                       template_sha256="x" * 64)
            for key in ("run_id", "mode", "created_at", "host", "playbook_repo_sha", "corpus",
                        "source_repos", "template", "candidates", "timeouts", "concurrency", "sandbox",
                        "web_search", "retry_policy", "manual_quota_notes"):
                self.assertIn(key, m)
            self.assertEqual(set(m["corpus"]["hashes"]), {"c-a", "c-b"})
            self.assertEqual(set(m["corpus"]["hashes"]["c-a"]), {"spec_md", "diff_patch", "context",
                                                                 "prompt", "truth_version"})
            self.assertEqual([c["cli_version"] for c in m["candidates"]], ["fake", "fake"])
            self.assertFalse(m["web_search"])
            rd = Path(td) / "run"; rd.mkdir()
            records.write_manifest(rd, m)
            self.assertEqual(records.read_manifest(rd)["run_id"], "r")
            self.assertEqual(records.check_manifest(m, selected_cases=corpus.cases, packages=pk,
                                                    candidates=cands), [])
            # Edit the corpus → mismatch named per case/field.
            (corpus.cases[0].diff_path).write_text("changed\n", encoding="utf-8")
            pk2 = {c.id: package.build_package(c) for c in corpus.cases}
            probs = records.check_manifest(m, selected_cases=corpus.cases, packages=pk2, candidates=cands)
            self.assertTrue(any("c-a: diff_patch changed" in p for p in probs), probs)
            self.assertTrue(any("c-a: prompt changed" in p for p in probs), probs)
            probs = records.check_manifest(m, selected_cases=corpus.cases, packages=pk,
                                           candidates=runner.parse_candidates("a=sonnet,z=opus"))
            self.assertTrue(any("candidate a: spec" in p for p in probs), probs)
            self.assertTrue(any("candidate z: not in" in p for p in probs), probs)


class RunCommandTests(unittest.TestCase):
    """`judgebench run --fake` end-to-end through the real CLI."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        # Poison ancestor: a real-looking .agent lane ABOVE both cwd and runs dir.
        (self.root / ".agent" / "journal").mkdir(parents=True)
        (self.root / ".agent" / "tasks").mkdir()
        self.corpus_dir = self.root / "work" / "corpus"
        _mk_corpus(self.corpus_dir)
        self.runs = self.root / "work" / "runs"
        self.cwd = self.root / "work"

    def tearDown(self):
        self._td.cleanup()

    def _run(self, *extra, run_id="r1", cands="a=opus,b=codex:gpt-5.6-sol:medium"):
        return _cli("run", "--cases", "all", "--candidates", cands, "--run-id", run_id,
                    "--corpus", str(self.corpus_dir), "--runs-dir", str(self.runs), *extra, cwd=self.cwd)

    def test_fake_run_writes_results_manifest_journal_only_under_run_dir(self):
        p = self._run("--fake")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        rd = self.runs / "r1"
        self.assertTrue((rd / "manifest.json").is_file())
        self.assertEqual(records.read_manifest(rd)["mode"], "fake")
        for label in ("a", "b"):
            recs, torn = records.read_results(rd, label)
            self.assertEqual(sorted(r["case_id"] for r in recs), ["c-a", "c-b"])
            self.assertEqual(torn, 0)
            for r in recs:
                self.assertEqual(r["status"], "ok")
                self.assertTrue((rd / r["raw_path"]).is_file())
                self.assertEqual(len(r["prompt_sha256"]), 64)
                self.assertEqual(r["usage"], {"status": "unknown"})
        # Spend journal: 4 bench records in the RUN dir, none anywhere else.
        jl = rd / "journal" / "enforcement.jsonl"
        self.assertTrue(jl.is_file())
        lines = [json.loads(x) for x in jl.read_text(encoding="utf-8").splitlines() if x.strip()]
        self.assertEqual(len(lines), 4)
        self.assertEqual({(x["hook"], x["decision"], x["kind"]) for x in lines},
                         {("review", "record", "bench")})
        self.assertEqual({x["seat"] for x in lines}, {"opus", "codex:gpt-5.6-sol:medium"})
        self.assertFalse((self.root / ".agent" / "journal" / "enforcement.jsonl").exists(),
                         "bench spend must never reach a production lane")
        self.assertFalse((self.root / "work" / ".agent").exists())
        self.assertFalse((rd / ".lock").exists(), "lock released")
        self.assertIn("4 invocations", p.stdout)

    def test_run_refuses_existing_run_id_without_resume_and_resumes_only_missing(self):
        p = self._run("--fake")
        self.assertEqual(p.returncode, 0, p.stderr)
        p = self._run("--fake")
        self.assertEqual(p.returncode, 2)
        self.assertIn("--resume", p.stderr)
        rd = self.runs / "r1"
        before = {label: records.read_results(rd, label)[0] for label in ("a", "b")}
        # Simulate an interrupted run: drop b/c-b, tear a/c-b.
        rb = records.result_path(rd, "b")
        keep = [json.loads(x) for x in rb.read_text(encoding="utf-8").splitlines() if x.strip()]
        rb.write_text(json.dumps([x for x in keep if x["case_id"] == "c-a"][0]) + "\n", encoding="utf-8")
        ra = records.result_path(rd, "a")
        ra.write_text(ra.read_text(encoding="utf-8") + '{"case_id": "c-b", "label": "a", "tor', encoding="utf-8")
        # (the torn line duplicates c-b for `a`, so drop the good c-b line to make it truly missing)
        good = [x for x in ra.read_text(encoding="utf-8").splitlines() if x.strip()]
        ra.write_text("\n".join([ln for ln in good if '"case_id": "c-a"' in ln or "tor" in ln]) + "\n",
                      encoding="utf-8")
        p = self._run("--fake", "--resume")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("2 invocations", p.stdout)               # exactly the two missing pairs
        self.assertIn("1 torn", p.stdout)
        for label in ("a", "b"):
            recs, torn = records.read_results(rd, label)
            self.assertEqual(sorted(r["case_id"] for r in recs), ["c-a", "c-b"])
            same = [r for r in recs if r["case_id"] == "c-a"][0]
            self.assertEqual(same["ts"], [r for r in before[label] if r["case_id"] == "c-a"][0]["ts"],
                             "completed pairs were not re-run")
        # Nothing left to do → 0 invocations, still exit 0.
        p = self._run("--fake", "--resume")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("0 invocations", p.stdout)

    def test_resume_refuses_when_corpus_changed(self):
        self.assertEqual(self._run("--fake").returncode, 0)
        (self.corpus_dir / "cases" / "c-a" / "diff.patch").write_text("tampered\n", encoding="utf-8")
        p = self._run("--fake", "--resume")
        self.assertEqual(p.returncode, 2)
        self.assertIn("diff_patch changed", p.stderr)

    def test_lock_held_refuses_and_writes_nothing(self):
        rd = self.runs / "r1"
        rd.mkdir(parents=True)
        (rd / ".lock").write_text("424242", encoding="utf-8")
        p = self._run("--fake")
        self.assertEqual(p.returncode, 2)
        self.assertIn("424242", p.stderr)
        self.assertFalse((rd / "manifest.json").exists())
        self.assertFalse((rd / "a").exists())
        self.assertTrue((rd / ".lock").exists(), "a foreign lock is never removed")

    def test_exit_1_with_dnf_and_statuses_distinct(self):
        script = self.root / "script.json"
        script.write_text(json.dumps({"c-a|a": {"status": "dnf"}, "c-b|a": {"status": "timeout"},
                                      "c-a|b": {"status": "malformed"}, "default": {"status": "ok"}}),
                          encoding="utf-8")
        p = self._run("--fake", "--fake-script", str(script))
        self.assertEqual(p.returncode, 1, p.stderr)
        rd = self.runs / "r1"
        a = {r["case_id"]: r["status"] for r in records.read_results(rd, "a")[0]}
        b = {r["case_id"]: r["status"] for r in records.read_results(rd, "b")[0]}
        self.assertEqual(a, {"c-a": "dnf", "c-b": "timeout"})
        self.assertEqual(b, {"c-a": "malformed", "c-b": "ok"})
        for word in ("dnf", "timeout", "malformed"):
            self.assertIn(word, p.stdout)

    def test_bad_candidate_or_case_is_exit_2(self):
        p = self._run("--fake", cands="nosuch:x")
        self.assertEqual(p.returncode, 2)
        p = _cli("run", "--cases", "zzz", "--candidates", "opus", "--run-id", "x", "--fake",
                 "--corpus", str(self.corpus_dir), "--runs-dir", str(self.runs), cwd=self.cwd)
        self.assertEqual(p.returncode, 2)

    def test_run_on_empty_corpus_is_exit_2_and_leaves_no_run_dir(self):
        with tempfile.TemporaryDirectory() as td:
            p = _cli("run", "--cases", "all", "--candidates", "opus", "--run-id", "e", "--fake",
                     "--corpus", td, "--runs-dir", str(self.runs), cwd=self.cwd)
            self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
            self.assertIn("no cases", p.stderr)
            self.assertNotIn("Traceback", p.stderr)
            self.assertFalse((self.runs / "e").exists())

    def test_live_boundary_make_runner(self):
        # --fake → FakeRunner; --live → LiveRunner; neither → refusal. In-process
        # so we can assert the TYPE (the CLI test above proves the exit code).
        sys.path.insert(0, str(_ROOT / "bench"))
        import judgebench
        fake = judgebench.make_runner(fake=True, live=False, fake_script=None, source_repos={})
        self.assertIsInstance(fake, runner.FakeRunner)
        live = judgebench.make_runner(fake=False, live=True, fake_script=None, source_repos={})
        self.assertIsInstance(live, runner.LiveRunner)
        with self.assertRaises(ValueError):
            judgebench.make_runner(fake=False, live=False, fake_script=None, source_repos={})


if __name__ == "__main__":
    unittest.main()
