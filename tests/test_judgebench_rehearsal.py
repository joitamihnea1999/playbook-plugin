#!/usr/bin/env python3
"""Task 049 / plan §5.4: the rehearsal — run → adjudicate → report → contamination over the REAL
frozen corpus with a mixed fake script (ok / malformed / timeout / dnf / fail / empty), each status
in its own column; interactive adjudication driven by scripted stdin runs ONLY against a temp copy
of the corpus (the `v` command rewrites truth.json/case.json) and the frozen tree stays byte-identical."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bench.lib import DEFAULT_CORPUS_DIR, cases  # noqa: E402

_ENTRY = _ROOT / "bench" / "judgebench.py"
SCRIPT = _ROOT / "bench" / "fake-scripts" / "rehearsal.json"
CANDS = "sol-med,sol-high,grok-med,grok-high"


def _run(*args, stdin=None, cwd=None):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, str(_ENTRY), *args], capture_output=True, text=True, input=stdin,
                          encoding="utf-8", errors="replace", cwd=cwd or str(_ROOT), env=env, timeout=600)


def _tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.name != ".lock":
            h.update(p.relative_to(root).as_posix().encode()); h.update(p.read_bytes())
    return h.hexdigest()


class RehearsalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = cases.load_corpus(DEFAULT_CORPUS_DIR)
        if not cls.corpus.cases:
            raise unittest.SkipTest("empty corpus")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.runs = Path(cls.tmp.name) / "runs"
        cls.frozen_before = _tree_digest(DEFAULT_CORPUS_DIR)
        cls.p_run = _run("run", "--fake", "--fake-script", str(SCRIPT), "--cases", "all", "--candidates", CANDS,
                         "--run-id", "rehearsal", "--runs-dir", str(cls.runs))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_script_covers_every_case_and_candidate(self):
        script = json.loads(SCRIPT.read_text(encoding="utf-8"))
        for c in self.corpus.cases:
            for lb in CANDS.split(","):
                self.assertIn(f"{c.id}|{lb}", script, f"{c.id}|{lb}")

    def test_run_exits_one_because_timeout_and_dnf_are_scripted(self):
        self.assertEqual(self.p_run.returncode, 1, self.p_run.stdout + self.p_run.stderr)
        for st in ("malformed", "timeout", "dnf", "fail"):
            self.assertIn(st, self.p_run.stdout)

    def test_auto_adjudication_credits_the_real_truth_hit_and_leaves_novel_findings_pending(self):
        p = _run("adjudicate", "rehearsal", "--auto", "--runs-dir", str(self.runs))
        self.assertEqual(p.returncode, 0, p.stderr)
        adj = json.loads((self.runs / "rehearsal" / "adjudication.json").read_text(encoding="utf-8"))
        self.assertIn("auto truth=", p.stdout)
        self.assertRegex(p.stdout, r"auto truth=[1-9]\d*")          # the scripted real hits auto-match
        self.assertRegex(p.stdout, r"pending=[1-9]\d*")             # the novel ones wait for a human
        self.assertTrue(adj)

    def test_report_shows_each_status_in_its_own_column(self):
        _run("adjudicate", "rehearsal", "--auto", "--runs-dir", str(self.runs))
        p = _run("report", "rehearsal", "--md", str(self.runs / "rehearsal" / "report.md"), "--runs-dir", str(self.runs))
        self.assertEqual(p.returncode, 0, p.stderr)
        rows = {l.split()[0]: l.split() for l in p.stdout.splitlines() if l.startswith(("sol-", "grok-"))}
        cols = [l for l in p.stdout.splitlines() if l.startswith("candidate")][0].split()
        idx = {c: i for i, c in enumerate(cols)}
        gm = rows["grok-med"]
        for st in ("malformed", "timeout", "dnf", "fail"):
            self.assertGreater(int(gm[idx[st]]), 0, st)
        self.assertEqual(int(rows["grok-high"][idx["ok"]]), len(self.corpus.cases))
        self.assertGreater(int(rows["sol-med"][idx["valid"]]), 0)
        self.assertGreater(int(rows["sol-med"][idx["pending"]]), 0)
        self.assertIn("contam?", p.stdout)
        self.assertTrue((self.runs / "rehearsal" / "report.md").is_file())

    def test_contamination_scan_on_the_rehearsal_flags_nothing(self):
        ws_plugin = _ROOT.parent
        ws_hf = Path.home() / "Documents" / "Workspace" / "HowFarAI-v2"
        args = ["contamination", "rehearsal", "--runs-dir", str(self.runs)]
        for name, root in (("playbook-plugin-dev", ws_plugin), ("HowFarAI-v2", ws_hf)):
            if (root / ".agent" / "tasks").is_dir():
                args += ["--history", f"{name}={root}"]
        mapped = {name for name, root in (("playbook-plugin-dev", ws_plugin), ("HowFarAI-v2", ws_hf))
                  if (root / ".agent" / "tasks").is_dir()}
        if not mapped:
            self.skipTest("no history workspace present — the false-positive bound needs the records")
        p = _run(*args)
        self.assertIn(p.returncode, (0, 1), p.stderr)                # 1 = some workspace absent (CI)
        scan = json.loads((self.runs / "rehearsal" / "contamination.json").read_text(encoding="utf-8"))
        for lb, cs in scan["labels"].items():
            for c, e in cs.items():
                ws = self.corpus.get(c).source["workspace"]
                if ws in mapped and e.get("raw_path"):
                    self.assertEqual(e["count"], 0, (lb, c, e))         # scanned AND clean — never None (grok #2)

    def test_auto_adjudication_works_on_a_read_only_corpus(self):
        # impl-panel codex:sol #1 / codex:terra #1: --auto never writes the corpus, so it must not need a lock there
        if os.name == "nt":
            self.skipTest("read-only directories do not block file creation on Windows")
        copy = Path(self.tmp.name) / "corpus-ro"
        shutil.copytree(DEFAULT_CORPUS_DIR, copy, ignore=shutil.ignore_patterns(".lock"))
        runs = Path(self.tmp.name) / "runs-ro"
        p = _run("run", "--fake", "--fake-script", str(SCRIPT), "--cases", self.corpus.cases[0].id, "--candidates",
                 "sol-med", "--run-id", "ro", "--corpus", str(copy), "--runs-dir", str(runs))
        self.assertEqual(p.returncode, 0, p.stderr)
        for d in [copy, *copy.rglob("*")]:
            if d.is_dir():
                os.chmod(d, 0o555)
        try:
            p = _run("adjudicate", "ro", "--auto", "--corpus", str(copy), "--runs-dir", str(runs))
            self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        finally:
            for d in [copy, *copy.rglob("*")]:
                if d.is_dir():
                    os.chmod(d, 0o755)

    def test_interactive_adjudication_runs_on_a_temp_copy_and_never_touches_the_frozen_tree(self):
        copy = Path(self.tmp.name) / "corpus-copy"
        shutil.copytree(DEFAULT_CORPUS_DIR, copy, ignore=shutil.ignore_patterns(".lock"))
        runs = Path(self.tmp.name) / "runs-copy"
        p = _run("run", "--fake", "--fake-script", str(SCRIPT), "--cases", self.corpus.cases[0].id, "--candidates",
                 "sol-med,sol-high", "--run-id", "interactive", "--corpus", str(copy), "--runs-dir", str(runs))
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        case = self.corpus.cases[0]
        tid = case.truth["findings"][0]["id"]
        # the human loop is presented the NOVEL findings (the real hit auto-matches first):
        #   sol-med novel → m <real truth id>  (equivalence to an existing truth entry)
        #   sol-high novel #1 → v + failure mode line (valid-new → appended to the COPY's truth.json)
        #   sol-high novel #2 → i (false positive); then q
        stdin = f"m {tid}\nv\nrehearsal valid-new failure mode\ni\nq\n"
        p = _run("adjudicate", "interactive", "--corpus", str(copy), "--runs-dir", str(runs), stdin=stdin)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        adj = json.loads((runs / "interactive" / "adjudication.json").read_text(encoding="utf-8"))
        blob = json.dumps(adj)
        self.assertIn(tid, blob)
        self.assertIn("valid-new", p.stdout + blob)
        truth_copy = json.loads((copy / "cases" / case.id / "truth.json").read_text(encoding="utf-8"))
        self.assertTrue(any(f.get("historical_outcome") == "valid-new" for f in truth_copy["findings"]), truth_copy)
        meta_copy = json.loads((copy / "cases" / case.id / "case.json").read_text(encoding="utf-8"))
        self.assertEqual(meta_copy["truth_version"], case.truth_version + 1)
        self.assertEqual(_tree_digest(DEFAULT_CORPUS_DIR), self.frozen_before)      # the freeze is untouched

    def test_frozen_corpus_is_untouched_by_the_whole_rehearsal(self):
        self.assertEqual(_tree_digest(DEFAULT_CORPUS_DIR), self.frozen_before)


if __name__ == "__main__":
    unittest.main()
