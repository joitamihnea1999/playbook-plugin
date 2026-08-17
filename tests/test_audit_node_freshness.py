#!/usr/bin/env python3
"""Node-freshness audit check + its negative controls.

`check_mindmap_staleness` catches a map that cites a DELETED path. This is its
complement: a node whose cited code still exists but has EVOLVED in >= N commits
since the node was last written — stale institutional memory the agent trusts.

The discipline (Part 4 of the report): a measuring tool must PROVE it can report
failure. So the check is exercised across the full arc — fresh (node and code
committed together → quiet), drifted (code moved on, node did not → fires and
names the node), refreshed (touch the node → quiet again) — plus the guards:
git-only, threshold-respecting, disable-able.

Commit times are pinned via GIT_*_DATE so "newer than the node" is deterministic
and does not depend on wall-clock seconds between commits.

Run: python3 tests/test_audit_node_freshness.py
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "plugins/playbook"))
from tasks.audit import check_mindmap_node_freshness  # noqa: E402

MAP = (
    "# Mind Map\n\n"
    "> **For AI Agents:** routing first.\n\n"
    "[1] **Overview** - the project entry point, see [2].\n\n"
    "[2] **Storage** - persistence lives in src/store.py which owns the cache [1].\n\n"
)


class NodeFreshness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        (self.d / "src").mkdir()
        self._env = os.environ.copy()
        self._env.update({
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        })
        self._git("init", "-q")
        self._git("config", "user.email", "t@t")
        self._git("config", "user.name", "t")

    def tearDown(self):
        self.tmp.cleanup()

    def _git(self, *args, date=None):
        env = dict(self._env)
        if date:
            env["GIT_AUTHOR_DATE"] = date
            env["GIT_COMMITTER_DATE"] = date
        subprocess.run(["git", *args], cwd=self.d, env=env,
                       check=True, capture_output=True, text=True)

    def _write(self, rel, content):
        (self.d / rel).write_text(content, encoding="utf-8")

    def _commit(self, msg, date):
        self._git("add", "-A")
        self._git("commit", "-q", "-m", msg, date=date)

    def _out(self, **audit):
        # audit knobs go into .agent/config.json
        import json
        (self.d / ".agent").mkdir(exist_ok=True)
        (self.d / ".agent" / "config.json").write_text(
            json.dumps({"audit": audit}) if audit else "{}", encoding="utf-8")
        return check_mindmap_node_freshness(self.d)

    # --- the arc -----------------------------------------------------------

    def test_fresh_when_node_and_code_committed_together(self):
        self._write("MIND_MAP.md", MAP)
        self._write("src/store.py", "cache = {}\n")
        self._commit("init", "2020-01-01T00:00:00")
        r = check_mindmap_node_freshness(self.d)
        self.assertEqual(r["status"], "clean", r["output"])

    def test_fires_and_names_node_when_code_drifts(self):
        self._write("MIND_MAP.md", MAP)
        self._write("src/store.py", "cache = {}\n")
        self._commit("init", "2020-01-01T00:00:00")
        # Two later commits touch the cited file, node untouched → drift.
        self._write("src/store.py", "cache = {}\n# v2\n")
        self._commit("v2", "2020-01-02T00:00:00")
        self._write("src/store.py", "cache = {}\n# v3\n")
        self._commit("v3", "2020-01-03T00:00:00")
        r = check_mindmap_node_freshness(self.d)
        self.assertEqual(r["status"], "findings", r["output"])
        self.assertIn("[2]", r["output"])
        self.assertIn("src/store.py", r["output"])
        # The overview node cites no path → never flagged.
        self.assertNotIn("[1]", r["output"])

    def test_refreshing_the_node_clears_it(self):
        self._write("MIND_MAP.md", MAP)
        self._write("src/store.py", "cache = {}\n")
        self._commit("init", "2020-01-01T00:00:00")
        self._write("src/store.py", "cache = {}\n# v2\n")
        self._commit("v2", "2020-01-02T00:00:00")
        self._write("src/store.py", "cache = {}\n# v3\n")
        self._commit("v3", "2020-01-03T00:00:00")
        self.assertEqual(check_mindmap_node_freshness(self.d)["status"], "findings")
        # Re-write the node (its blame time becomes the newest commit) → clean.
        self._write("MIND_MAP.md", MAP.replace(
            "persistence lives in src/store.py which owns the cache",
            "persistence lives in src/store.py — refreshed, now owns the LRU cache"))
        self._commit("refresh node", "2020-01-04T00:00:00")
        self.assertEqual(check_mindmap_node_freshness(self.d)["status"], "clean")

    # --- guards ------------------------------------------------------------

    def test_none_outside_a_git_repo(self):
        d = Path(tempfile.mkdtemp())
        try:
            (d / "MIND_MAP.md").write_text(MAP, encoding="utf-8")
            self.assertIsNone(check_mindmap_node_freshness(d))
        finally:
            __import__("shutil").rmtree(d)

    def test_none_when_no_map(self):
        self._write("src/store.py", "x=1\n")
        self._commit("init", "2020-01-01T00:00:00")
        self.assertIsNone(check_mindmap_node_freshness(self.d))

    def test_one_commit_below_default_threshold_stays_quiet(self):
        self._write("MIND_MAP.md", MAP)
        self._write("src/store.py", "cache = {}\n")
        self._commit("init", "2020-01-01T00:00:00")
        self._write("src/store.py", "cache = {}\n# v2\n")
        self._commit("v2", "2020-01-02T00:00:00")     # only ONE later commit
        self.assertEqual(check_mindmap_node_freshness(self.d)["status"], "clean")

    def test_threshold_one_fires_on_a_single_commit(self):
        self._write("MIND_MAP.md", MAP)
        self._write("src/store.py", "cache = {}\n")
        self._commit("init", "2020-01-01T00:00:00")
        self._write("src/store.py", "cache = {}\n# v2\n")
        self._commit("v2", "2020-01-02T00:00:00")
        r = self._out(node_freshness_commits=1)
        self.assertEqual(r["status"], "findings", r["output"])

    def test_disable_flag_returns_none(self):
        self._write("MIND_MAP.md", MAP)
        self._write("src/store.py", "cache = {}\n")
        self._commit("init", "2020-01-01T00:00:00")
        self.assertIsNone(self._out(node_freshness=False))

    def test_severity_is_advisory_by_default(self):
        self._write("MIND_MAP.md", MAP)
        self._write("src/store.py", "cache = {}\n")
        self._commit("init", "2020-01-01T00:00:00")
        self.assertEqual(check_mindmap_node_freshness(self.d)["severity"], "advisory")


if __name__ == "__main__":
    unittest.main(verbosity=2)
