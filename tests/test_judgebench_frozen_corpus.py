#!/usr/bin/env python3
"""The FROZEN corpus is data the suite must exercise (task 048 impl-panel round 2, grok #3):
every case's package builds leak-free under the budgets, and the playbook-plugin cases'
truth checks mechanically against THIS repository's history. HowFar cases need the HowFar
checkout and are only schema-validated here (check_truth runs them locally)."""
import io
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bench.lib import DEFAULT_CORPUS_DIR, cases, package  # noqa: E402
from bench.tools import check_truth  # noqa: E402


def _has(sha: str) -> bool:
    return subprocess.run(["git", "-C", str(_ROOT), "cat-file", "-e", f"{sha}^{{commit}}"],
                          capture_output=True).returncode == 0


class FrozenCorpusTests(unittest.TestCase):
    def setUp(self):
        self.corpus = cases.load_corpus(DEFAULT_CORPUS_DIR)
        if not self.corpus.cases:
            self.skipTest("corpus is empty")

    def test_every_frozen_case_builds_leak_free_under_the_budgets(self):
        for c in self.corpus.cases:
            pkg = package.build_package(c)                 # raises LeakageError on a leak
            self.assertLessEqual(pkg.prompt_chars, 90_000, c.id)
            self.assertLessEqual(len(pkg.prompt.encode("utf-8")), 120_000, c.id)

    def test_playbook_plugin_cases_check_truth_against_this_repo(self):
        pb = [c for c in self.corpus.cases if c.source["repo"] == "playbook-plugin"]
        if not pb:
            self.skipTest("no playbook-plugin cases")
        missing = [c.id for c in pb if not _has(c.repo_base_sha)]
        if missing:
            self.skipTest(f"shallow clone — reviewed shas not present: {missing}")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = check_truth.main(["--corpus", str(DEFAULT_CORPUS_DIR), "--cases", ",".join(c.id for c in pb)])
        self.assertEqual(rc, 0, buf.getvalue())


if __name__ == "__main__":
    unittest.main()
