#!/usr/bin/env python3
"""Step 2 tests: case model + corpus loader (`bench/lib/cases.py`).

Hermetic — every corpus is built in a temp dir from the fixtures below. The
schema is the plan's §8 `case.json` + §10 `truth.json`; the loader must name
the offending case in every error so a step-9 corpus builder gets a precise
message, and must stay lenient where the plan says optional (`notes`,
`context/`, `known_rejects`).
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bench.lib import cases  # noqa: E402

SHA = "0123456789abcdef0123456789abcdef01234567"


def _case_json(cid="pb-001-demo", **over):
    d = {
        "id": cid,
        "source": {"workspace": "playbook-plugin-dev", "task": "001", "repo": "playbook-plugin"},
        "repo_base_sha": SHA,
        "diff_of": SHA[:7],
        "kind": "feature",
        "area": "enforcement",
        "difficulty": "medium",
        "truth_version": 1,
        "notes": "",
    }
    d.update(over)
    return d


def _truth(**over):
    d = {"findings": [
        {"id": "T1", "file": "plugins/playbook/tasks/core.py", "symbol": "extract_risk",
         "failure_mode": "fenced-heading shadow", "severity": "Critical",
         "historical_outcome": "accepted+fixed"}],
        "known_rejects": [{"id": "R1", "claim": "x", "why_rejected": "y"}]}
    d.update(over)
    return d


def _write_case(root: Path, cid: str, case=None, truth=None, spec="# 001 - Demo\n",
                diff="--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b\n", dirname=None):
    d = root / "cases" / (dirname or cid)
    d.mkdir(parents=True)
    (d / "case.json").write_text(json.dumps(case if case is not None else _case_json(cid)),
                                 encoding="utf-8")
    (d / "truth.json").write_text(json.dumps(truth if truth is not None else _truth()),
                                  encoding="utf-8")
    (d / "spec.md").write_text(spec, encoding="utf-8")
    (d / "diff.patch").write_text(diff, encoding="utf-8")
    return d


def _write_corpus(root: Path, ids, version=1):
    (root / "corpus.json").write_text(json.dumps({"version": version, "cases": list(ids)}),
                                      encoding="utf-8")


class LoaderTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_valid_corpus_loads(self):
        _write_case(self.root, "pb-001-demo")
        _write_case(self.root, "hf-015-nan")
        _write_corpus(self.root, ["pb-001-demo", "hf-015-nan"])
        c = cases.load_corpus(self.root)
        self.assertEqual(c.version, 1)
        self.assertEqual([x.id for x in c.cases], ["pb-001-demo", "hf-015-nan"])
        case = c.get("pb-001-demo")
        self.assertEqual(case.kind, "feature")
        self.assertEqual(case.truth_version, 1)
        self.assertEqual(case.spec_path.name, "spec.md")
        self.assertEqual(case.diff_path.name, "diff.patch")
        self.assertEqual(case.truth["findings"][0]["id"], "T1")
        self.assertIsNone(c.get("nope"))

    def test_empty_corpus_dir_is_valid_zero_cases(self):
        c = cases.load_corpus(self.root)
        self.assertEqual((c.version, c.cases), (0, []))

    def test_missing_dir_raises(self):
        with self.assertRaises(cases.CorpusError):
            cases.load_corpus(self.root / "nope")

    def test_index_lists_case_with_no_dir(self):
        _write_corpus(self.root, ["ghost"])
        with self.assertRaisesRegex(cases.CorpusError, "ghost"):
            cases.load_corpus(self.root)

    def test_case_dir_not_in_index_is_an_error(self):
        # Bias guard (§8): a case is either frozen in corpus.json or it does not exist.
        _write_case(self.root, "pb-001-demo")
        _write_corpus(self.root, [])
        with self.assertRaisesRegex(cases.CorpusError, "pb-001-demo"):
            cases.load_corpus(self.root)

    def test_duplicate_id_in_index(self):
        _write_case(self.root, "pb-001-demo")
        _write_corpus(self.root, ["pb-001-demo", "pb-001-demo"])
        with self.assertRaisesRegex(cases.CorpusError, "duplicate"):
            cases.load_corpus(self.root)

    def test_id_must_match_dir_name(self):
        _write_case(self.root, "pb-001-demo", dirname="other-dir")
        _write_corpus(self.root, ["other-dir"])
        with self.assertRaisesRegex(cases.CorpusError, "other-dir"):
            cases.load_corpus(self.root)

    def test_missing_required_key(self):
        bad = _case_json(); del bad["kind"]
        _write_case(self.root, "pb-001-demo", case=bad)
        _write_corpus(self.root, ["pb-001-demo"])
        with self.assertRaisesRegex(cases.CorpusError, "pb-001-demo.*kind"):
            cases.load_corpus(self.root)

    def test_bad_enum_values(self):
        for key, val in (("kind", "wat"), ("area", "kitchen"), ("difficulty", "brutal")):
            with self.subTest(key=key):
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    _write_case(root, "pb-001-demo", case=_case_json(**{key: val}))
                    _write_corpus(root, ["pb-001-demo"])
                    with self.assertRaisesRegex(cases.CorpusError, key):
                        cases.load_corpus(root)

    def test_bad_sha_and_truth_version(self):
        for key, val in (("repo_base_sha", "not-a-sha"), ("truth_version", "1"),
                         ("truth_version", 0)):
            with self.subTest(key=key, val=val):
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    _write_case(root, "pb-001-demo", case=_case_json(**{key: val}))
                    _write_corpus(root, ["pb-001-demo"])
                    with self.assertRaisesRegex(cases.CorpusError, key):
                        cases.load_corpus(root)

    def test_missing_spec_or_diff_file(self):
        d = _write_case(self.root, "pb-001-demo")
        _write_corpus(self.root, ["pb-001-demo"])
        (d / "diff.patch").unlink()
        with self.assertRaisesRegex(cases.CorpusError, "diff.patch"):
            cases.load_corpus(self.root)

    def test_truth_schema(self):
        # findings: required fields + severity vocabulary + outcome vocabulary;
        # known_rejects optional; findings ids unique.
        bad_sev = _truth(findings=[dict(_truth()["findings"][0], severity="Blocker")])
        bad_outcome = _truth(findings=[dict(_truth()["findings"][0], historical_outcome="meh")])
        dup = _truth(findings=[_truth()["findings"][0], _truth()["findings"][0]])
        no_rejects = _truth(); del no_rejects["known_rejects"]
        for label, truth, ok in (("sev", bad_sev, False), ("outcome", bad_outcome, False),
                                 ("dup", dup, False), ("no_rejects", no_rejects, True)):
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    _write_case(root, "pb-001-demo", truth=truth)
                    _write_corpus(root, ["pb-001-demo"])
                    if ok:
                        c = cases.load_corpus(root)
                        self.assertEqual(c.get("pb-001-demo").truth["known_rejects"], [])
                    else:
                        with self.assertRaisesRegex(cases.CorpusError, "truth"):
                            cases.load_corpus(root)

    def test_malformed_json_names_the_file(self):
        d = _write_case(self.root, "pb-001-demo")
        _write_corpus(self.root, ["pb-001-demo"])
        (d / "case.json").write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(cases.CorpusError, "case.json"):
            cases.load_corpus(self.root)

    def test_truth_version_mismatch_between_case_and_truth(self):
        # truth.json may carry its own version; when present it must agree.
        _write_case(self.root, "pb-001-demo", truth=_truth(truth_version=2))
        _write_corpus(self.root, ["pb-001-demo"])
        with self.assertRaisesRegex(cases.CorpusError, "truth_version"):
            cases.load_corpus(self.root)

    def test_select_cases_all_and_list(self):
        _write_case(self.root, "a-1"); _write_case(self.root, "b-2")
        _write_corpus(self.root, ["a-1", "b-2"])
        c = cases.load_corpus(self.root)
        self.assertEqual([x.id for x in cases.select_cases(c, "all")], ["a-1", "b-2"])
        self.assertEqual([x.id for x in cases.select_cases(c, "b-2,a-1")], ["b-2", "a-1"])
        with self.assertRaisesRegex(cases.CorpusError, "zzz"):
            cases.select_cases(c, "a-1,zzz")

    def test_select_all_on_empty_corpus_is_an_error(self):
        # A run over zero cases is unusable (exit 2), never a crash or an empty run dir.
        c = cases.load_corpus(self.root)
        with self.assertRaisesRegex(cases.CorpusError, "no cases"):
            cases.select_cases(c, "all")


class CliTests(unittest.TestCase):
    def _run(self, *args):
        return subprocess.run([sys.executable, str(_ROOT / "bench" / "judgebench.py"), *args],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=120)

    def test_validate_and_show(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_case(root, "pb-001-demo")
            _write_corpus(root, ["pb-001-demo"])
            p = self._run("corpus", "validate", "--corpus", td)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("1 cases", p.stdout)
            p = self._run("corpus", "show", "--corpus", td)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("pb-001-demo", p.stdout)
            p = self._run("corpus", "show", "pb-001-demo", "--corpus", td)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("enforcement", p.stdout)
            p = self._run("corpus", "show", "nope", "--corpus", td)
            self.assertEqual(p.returncode, 2)

    def test_invalid_corpus_exit_2_names_case(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_case(root, "pb-001-demo", case=_case_json(kind="wat"))
            _write_corpus(root, ["pb-001-demo"])
            p = self._run("corpus", "validate", "--corpus", td)
            self.assertEqual(p.returncode, 2)
            self.assertIn("pb-001-demo", p.stderr)


if __name__ == "__main__":
    unittest.main()
