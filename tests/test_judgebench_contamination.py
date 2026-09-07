#!/usr/bin/env python3
"""Task 049 / plan §5.3: `judgebench contamination <run-id> --history NAME=PATH…` — for each
raw judge output, the word 12-grams it shares with the case's HISTORICAL judge.md /
judge-archive.md (read from the named workspace root, read-only), EXCLUDING n-grams that also
occur in the rendered prompt (a judge quoting the spec/diff is legitimate). Detects QUOTING,
not silent influence; `report` shows a `contam?` column."""
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

from bench.lib import cases, contamination, records  # noqa: E402

_ENTRY = _ROOT / "bench" / "judgebench.py"
SHA = "a" * 40
HIST = ("# Panel Impl Review\n\n**Finding 1 — the flatnode prune has no import-complete guard and deletes "
        "one hundred gigabytes mid import corrupting a twenty four minute build silently.**\n")
QUOTE12 = "the flatnode prune has no import-complete guard and deletes one hundred gigabytes mid import"
SPEC = "## Intent\nGuard the flatnode prune script so a mid import run cannot delete the file being written.\n"


def _mk(root: Path):
    corpus = root / "corpus"; d = corpus / "cases" / "c1"; d.mkdir(parents=True)
    (d / "case.json").write_text(json.dumps({"id": "c1", "source": {"workspace": "hfws", "task": "011", "repo": "r"},
        "repo_base_sha": SHA, "diff_of": f"{SHA}..{SHA}", "kind": "feature", "area": "server",
        "difficulty": "easy", "truth_version": 1}), encoding="utf-8")
    (d / "truth.json").write_text(json.dumps({"findings": [], "known_rejects": []}), encoding="utf-8")
    (d / "spec.md").write_text(SPEC, encoding="utf-8")
    (d / "diff.patch").write_text("+x\n", encoding="utf-8")
    (corpus / "corpus.json").write_text(json.dumps({"version": 1, "cases": ["c1"]}), encoding="utf-8")
    hist = root / "hfws" / ".agent" / "tasks" / "011-selfhost"; hist.mkdir(parents=True)
    (hist / "judge.md").write_text(HIST, encoding="utf-8")
    (hist / "task.md").write_text("# not scanned NOT_SCANNED_TOKEN words words words words words words words words words words\n", encoding="utf-8")
    return corpus, root / "hfws"


def _run(*args, cwd):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, str(_ENTRY), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd=cwd, env=env, timeout=300)


class NgramTests(unittest.TestCase):
    def test_shared_twelve_grams_are_found_case_and_punctuation_insensitively(self):
        raw = "I believe THE FLATNODE PRUNE has no import-complete guard, and deletes one hundred gigabytes mid import!"
        spans = contamination.shared_spans(raw, HIST, exclude_text="", n=12)
        self.assertEqual(len(spans), 1)
        self.assertIn("flatnode prune", spans[0].lower())

    def test_no_match_below_n(self):
        raw = "the flatnode prune has no import-complete guard"          # 8 words only
        self.assertEqual(contamination.shared_spans(raw, HIST, exclude_text="", n=12), [])

    def test_ngrams_present_in_the_prompt_are_excluded(self):
        raw = "Guard the flatnode prune script so a mid import run cannot delete the file being written."
        self.assertEqual(contamination.shared_spans(raw, SPEC + HIST, exclude_text=SPEC, n=12), [])
        self.assertTrue(contamination.shared_spans(raw, SPEC + HIST, exclude_text="", n=12))

    def test_quoting_the_diff_or_the_template_is_not_contamination(self):
        # plan-panel grok #4 / sonnet #2: the exclusion is the WHOLE rendered prompt, not the spec alone
        diff = "+  if (!(areaP > 0) || !Number.isFinite(areaD)) problems.push(the ORS cross-check found a degenerate ring area zero or NaN cannot verify provenance)\n"
        template = "Review through six lenses: (1) Simplify — what's unnecessary or over-engineered? (2) Self-critique — does the code actually fulfill the stated Intent?"
        prompt = SPEC + diff + template
        hist = HIST + diff + template                 # the historical judge quoted both too
        raw_diff = "As the diff shows, if (!(areaP > 0) || !Number.isFinite(areaD)) problems.push(the ORS cross-check found a degenerate ring area zero or NaN cannot verify provenance)."
        raw_tpl = "I will review through six lenses: (1) Simplify — what's unnecessary or over-engineered? (2) Self-critique — does the code actually fulfill the stated Intent?"
        self.assertEqual(contamination.shared_spans(raw_diff, hist, exclude_text=prompt, n=12), [])
        self.assertEqual(contamination.shared_spans(raw_tpl, hist, exclude_text=prompt, n=12), [])
        self.assertTrue(contamination.shared_spans(QUOTE12, hist, exclude_text=prompt, n=12))   # judge-only text still flags

    def test_shared_jargon_without_a_twelve_word_run_is_not_flagged(self):
        raw = ("This is fail-closed. The impl panel and the gate discipline matter; a Critical finding blocks the close. "
               "Fail-open paths are a Critical class. Playbook enforces panel review and triage of every finding.")
        hist = ("The panel found a fail-closed gap; gate discipline held. Fail-open paths are a Critical class in this "
                "codebase and the impl panel review + triage of every finding is mandatory before the close.")
        self.assertEqual(contamination.shared_spans(raw, hist, exclude_text="", n=12), [])

    def test_longest_span_is_reported_once(self):
        raw = HIST                                                       # quotes everything
        spans = contamination.shared_spans(raw, HIST, exclude_text="", n=12)
        self.assertEqual(len(spans), 1)
        self.assertGreater(len(spans[0].split()), 12)


class ScanRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.corpus_dir, self.hist_root = _mk(self.root)
        self.runs = self.root / "runs"
        # a fake run whose sol-high output quotes the historical judge, sol-med quotes only the spec
        script = {"c1|sol-high": {"raw": f"Review.\n{QUOTE12}.\n\nFINDINGS:\nNONE\nEND FINDINGS\n"},
                  "c1|sol-med": {"raw": "Review. Guard the flatnode prune script so a mid import run cannot delete the file being written.\n\nFINDINGS:\nNONE\nEND FINDINGS\n"}}
        (self.root / "script.json").write_text(json.dumps(script), encoding="utf-8")
        p = _run("run", "--fake", "--fake-script", str(self.root / "script.json"), "--cases", "all",
                 "--candidates", "sol-med,sol-high", "--run-id", "r1", "--corpus", str(self.corpus_dir),
                 "--runs-dir", str(self.runs), cwd=str(_ROOT))
        assert p.returncode == 0, p.stderr + p.stdout

    def test_cli_flags_the_quoting_candidate_only_and_writes_contamination_json(self):
        p = _run("contamination", "r1", "--history", f"hfws={self.hist_root}", "--corpus", str(self.corpus_dir),
                 "--runs-dir", str(self.runs), cwd=str(_ROOT))
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        out = json.loads((self.runs / "r1" / "contamination.json").read_text(encoding="utf-8"))
        self.assertEqual(out["labels"]["sol-high"]["c1"]["count"], 1)
        self.assertIn("flatnode prune", out["labels"]["sol-high"]["c1"]["longest_span"].lower())
        self.assertEqual(out["labels"]["sol-med"]["c1"]["count"], 0)
        self.assertEqual(out["n"], 12)
        self.assertIn("sol-high", p.stdout); self.assertIn("quoting", p.stdout.lower())
        self.assertNotIn("NOT_SCANNED_TOKEN", (self.runs / "r1" / "contamination.json").read_text(encoding="utf-8"))

    def test_missing_history_mapping_is_recorded_not_guessed(self):
        p = _run("contamination", "r1", "--corpus", str(self.corpus_dir), "--runs-dir", str(self.runs), cwd=str(_ROOT))
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        out = json.loads((self.runs / "r1" / "contamination.json").read_text(encoding="utf-8"))
        self.assertEqual(out["labels"]["sol-high"]["c1"]["count"], None)
        self.assertIn("hfws", out["labels"]["sol-high"]["c1"]["note"])

    def test_history_root_without_agent_tasks_is_an_error(self):
        p = _run("contamination", "r1", "--history", f"hfws={self.root / 'nowhere'}", "--corpus", str(self.corpus_dir),
                 "--runs-dir", str(self.runs), cwd=str(_ROOT))
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)

    def test_history_dir_is_resolved_as_NNN_slug_and_only_judge_files_are_read(self):
        p = _run("contamination", "r1", "--history", f"hfws={self.hist_root}", "--corpus", str(self.corpus_dir),
                 "--runs-dir", str(self.runs), cwd=str(_ROOT))
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        out = json.loads((self.runs / "r1" / "contamination.json").read_text(encoding="utf-8"))
        files = out["labels"]["sol-high"]["c1"]["history_files"]
        self.assertEqual([Path(f).name for f in files], ["judge.md"])           # task.md never read
        # two `011-*` dirs → ambiguous → refused for that case (count null + note), never a guess
        (self.hist_root / ".agent" / "tasks" / "011-other").mkdir()
        p = _run("contamination", "r1", "--history", f"hfws={self.hist_root}", "--corpus", str(self.corpus_dir),
                 "--runs-dir", str(self.runs), cwd=str(_ROOT))
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        out = json.loads((self.runs / "r1" / "contamination.json").read_text(encoding="utf-8"))
        self.assertIsNone(out["labels"]["sol-high"]["c1"]["count"])
        self.assertIn("011", out["labels"]["sol-high"]["c1"]["note"])

    def test_latest_result_line_wins_for_a_retried_pair(self):
        # plan-panel opus F5: raw files are <case>.<seq>.txt per attempt — scan the LATEST record's raw_path
        rec_path = records.result_path(self.runs / "r1", "sol-med")
        last = json.loads(rec_path.read_text(encoding="utf-8").splitlines()[-1])
        raw_rel = records.write_raw(self.runs / "r1", "sol-med", "c1", f"second attempt {QUOTE12}\n\nFINDINGS:\nNONE\nEND FINDINGS\n")
        last = dict(last, raw_path=raw_rel, status="ok")
        with open(rec_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(last) + "\n")
        p = _run("contamination", "r1", "--history", f"hfws={self.hist_root}", "--corpus", str(self.corpus_dir),
                 "--runs-dir", str(self.runs), cwd=str(_ROOT))
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        out = json.loads((self.runs / "r1" / "contamination.json").read_text(encoding="utf-8"))
        self.assertEqual(out["labels"]["sol-med"]["c1"]["count"], 1)           # the retried raw quotes history
        self.assertEqual(out["labels"]["sol-med"]["c1"]["raw_path"], raw_rel)

    def test_scan_is_refused_while_the_run_lock_is_held_and_writes_atomically(self):
        with records.RunLock(self.runs / "r1"):
            p = _run("contamination", "r1", "--history", f"hfws={self.hist_root}", "--corpus", str(self.corpus_dir),
                     "--runs-dir", str(self.runs), cwd=str(_ROOT))
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertFalse((self.runs / "r1" / "contamination.json").exists())
        p = _run("contamination", "r1", "--history", f"hfws={self.hist_root}", "--corpus", str(self.corpus_dir),
                 "--runs-dir", str(self.runs), cwd=str(_ROOT))
        self.assertEqual(p.returncode, 0)
        self.assertEqual([q.name for q in (self.runs / "r1").glob("contamination*")], ["contamination.json"])   # no temp left

    def test_report_marks_stale_entries_when_a_new_result_line_lands_after_the_scan(self):
        _run("contamination", "r1", "--history", f"hfws={self.hist_root}", "--corpus", str(self.corpus_dir),
             "--runs-dir", str(self.runs), cwd=str(_ROOT))
        rec_path = records.result_path(self.runs / "r1", "sol-high")
        last = json.loads(rec_path.read_text(encoding="utf-8").splitlines()[-1])
        raw_rel = records.write_raw(self.runs / "r1", "sol-high", "c1", "fresh output\n\nFINDINGS:\nNONE\nEND FINDINGS\n")
        with open(rec_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(dict(last, raw_path=raw_rel)) + "\n")
        p = _run("report", "r1", "--corpus", str(self.corpus_dir), "--runs-dir", str(self.runs), cwd=str(_ROOT))
        self.assertEqual(p.returncode, 0, p.stderr)
        line = [l for l in p.stdout.splitlines() if l.startswith("sol-high")][0]
        self.assertIn("stale", line)

    def test_report_matrix_marks_the_flagged_case(self):
        _run("contamination", "r1", "--history", f"hfws={self.hist_root}", "--corpus", str(self.corpus_dir),
             "--runs-dir", str(self.runs), cwd=str(_ROOT))
        p = _run("report", "r1", "--corpus", str(self.corpus_dir), "--runs-dir", str(self.runs), cwd=str(_ROOT))
        matrix = p.stdout.split("per-case matrix")[1]
        row = [l for l in matrix.splitlines() if l.strip().startswith("c1")][0]
        self.assertIn("!c", row)
        self.assertEqual(row.count("!c"), 1)                                   # sol-high only

    def test_report_shows_the_contam_column_after_the_scan_and_na_before(self):
        p = _run("report", "r1", "--corpus", str(self.corpus_dir), "--runs-dir", str(self.runs), cwd=str(_ROOT))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("contam?", p.stdout)
        self.assertIn("n/a", p.stdout)
        _run("contamination", "r1", "--history", f"hfws={self.hist_root}", "--corpus", str(self.corpus_dir),
             "--runs-dir", str(self.runs), cwd=str(_ROOT))
        p = _run("report", "r1", "--corpus", str(self.corpus_dir), "--runs-dir", str(self.runs), cwd=str(_ROOT))
        self.assertEqual(p.returncode, 0, p.stderr)
        line = [l for l in p.stdout.splitlines() if l.startswith("sol-high")][0]
        self.assertRegex(line, r"\b1\b")
        self.assertIn("quoting", p.stdout.lower())          # the disclosure note


if __name__ == "__main__":
    unittest.main()


class FrozenCorpusHistoryTests(unittest.TestCase):
    """Every playbook-plugin-dev case of the frozen corpus must resolve ≥1 historical judge file in
    THIS workspace (plan-panel sonnet #4 / grok #3) — skipped where the workspace is absent (CI)."""

    def test_history_resolves_for_every_plugin_case(self):
        from bench.lib import DEFAULT_CORPUS_DIR
        ws = _ROOT.parent
        if not (ws / ".agent" / "tasks").is_dir():
            self.skipTest("outer workspace not present")
        corpus = cases.load_corpus(DEFAULT_CORPUS_DIR)
        pb = [c for c in corpus.cases if c.source["workspace"] == "playbook-plugin-dev"]
        if not pb:
            self.skipTest("no playbook-plugin-dev cases")
        for c in pb:
            files = contamination.history_files(c, {"playbook-plugin-dev": ws})
            self.assertTrue(files, c.id)
            self.assertTrue(all(Path(f).name.startswith("judge") for f in files), files)
