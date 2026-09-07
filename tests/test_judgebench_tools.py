#!/usr/bin/env python3
"""Step 9 tests: the corpus-builder tools under `bench/tools/` (dev-only, stdlib).

`map_rounds`   — prints the round↔commit evidence table for a historical task
                 (judge.md rounds, task.md receipts, repo commits in the window).
`case_from_task` — emits a case dir (spec.md via reconstruct_spec, diff.patch
                 minus excluded/binary paths, case.json, truth.json skeleton).
`check_truth`  — mechanical proof that every truth finding cites a file that
                 exists at repo_base_sha and a fix commit that touches it, and
                 that diff.patch re-derives from `diff_of` + `diff_excludes`.

Hermetic: a temp git repo + a temp workspace; both are READ by the tools and
never written (the workspace's .agent/ must be byte-identical afterwards).
"""
import hashlib
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

from bench.lib import cases  # noqa: E402
from bench.tools import case_from_task, check_truth, map_rounds  # noqa: E402

_GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
                GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")


def _git(repo, *args, date=None):
    env = dict(_GIT_ENV)
    if date:
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, encoding="utf-8", env=env).stdout.strip()


def _commit(repo, msg, date):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg, date=date)
    return _git(repo, "rev-parse", "HEAD")


def _tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(root).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()


TASK_MD = """# 007 - Demo Feature

## Status
done

## Risk
reversible

## Intent
Add `foo` to a.py so callers get a total.

## Why
Callers need it.

## References
- Context: node [3]

## Design Phase
### Understand
- [x] Restate — sum the list and return it.
- [x] Done looks like — `foo([1,2])` returns 3.

## Plan Review
- [x] Run panel — PANEL VERDICT: PASS LEAK_PLAN_REVIEW
- [x] Triage — accepted #1 LEAK_TRIAGE

## Work Plan
- [x] **W1 — implement foo.** Write the loop. — DONE. impl-panel #1 said the loop was fine LEAK_W1_NOTE
    wrapped note continuation LEAK_W1_WRAP
- [ ] **W2 — docs.** Write docs.

## Implementation Review
- [x] Run panel — PANEL PASS 3/3 LEAK_IMPL
- **F1 — `foo` mishandles an empty list (Critical). ACCEPT + FIX.** LEAK_TRIAGE_F1

## Pre-review
- [x] All tests pass — 12/12 LEAK_PREREVIEW

## Parked
- LEAK_PARKED item

## Pre-Panel Audit

### 2026-08-24T13:25:39+03:00 · PASS · commit 1111111111111111111111111111111111111111

### 2026-08-24T13:06:25+03:00 · PASS · commit 1111111111111111111111111111111111111111

## Verification Receipt

### 2026-08-24T14:33:02+03:00 · risk reversible · commit 1111111111111111111111111111111111111111 (+7 uncommitted file(s))
"""

JUDGE_MD_TMPL = """# Panel Impl Review — .agent/tasks/007-demo/task.md

**PANEL VERDICT: PASS** — 3/3 judges succeeded, quorum 2

**Judges:** 3/3 succeeded | **Quorum:** 2

**Commit:** 1111111111111111111111111111111111111111

**Panel-snapshot:** {{"exclude":[":(exclude).agent"],"scopes":{{"":{{"commit":"1111111111111111111111111111111111111111","dirty":{{}}}},"app":{{"commit":"{fix}","dirty":{{}}}}}},"tree_fp":"abc","v":1}}

════════
## Triage
════════

### codex — F1 something

# Panel Impl Review — .agent/tasks/007-demo/task.md

**PANEL VERDICT: FAIL** — 2/3 judges succeeded, quorum 2

**Judges:** 2/3 succeeded | **Quorum:** 2

**Commit:** 1111111111111111111111111111111111111111

════════
## Triage
════════

### opus — F1 empty list
"""


class _Fixture:
    """Temp git repo (4 commits) + temp workspace holding task 007's record."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "app"
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        (self.repo / "a.py").write_text("def bar():\n    return 1\n", encoding="utf-8")
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "x.md").write_text("# x\n", encoding="utf-8")
        self.c0 = _commit(self.repo, "base", "2026-08-24T10:00:00+03:00")
        # reviewed commit: the feature + a lockfile + a binary + a dist bundle + a ledger
        (self.repo / "a.py").write_text("def bar():\n    return 1\n\n\ndef foo(xs):\n    t = 0\n"
                                        "    for x in xs:\n        t += x\n    return t\n",
                                        encoding="utf-8")
        (self.repo / "package-lock.json").write_text('{"lock": "%s"}\n' % ("x" * 5000), encoding="utf-8")
        (self.repo / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4)
        (self.repo / "dist").mkdir()
        (self.repo / "dist" / "bundle.js").write_text("var a=1;" * 400 + "\n", encoding="utf-8")
        (self.repo / "docs" / "ledger.json").write_text('{"big": "%s"}\n' % ("y" * 3000), encoding="utf-8")
        self.c1 = _commit(self.repo, "feature: add foo", "2026-08-24T13:05:00+03:00")
        # fix commit: touches a.py (the truth's file)
        (self.repo / "a.py").write_text("def bar():\n    return 1\n\n\ndef foo(xs):\n    if not xs:\n"
                                        "        return 0\n    t = 0\n    for x in xs:\n        t += x\n"
                                        "    return t\n", encoding="utf-8")
        self.c2 = _commit(self.repo, "fix: foo empty list (panel F1)", "2026-08-24T13:24:00+03:00")
        (self.repo / "other.py").write_text("x = 1\n", encoding="utf-8")
        self.c3 = _commit(self.repo, "unrelated", "2026-08-24T14:30:00+03:00")
        self.ws = root / "ws"
        self.taskdir = self.ws / ".agent" / "tasks" / "007-demo"
        self.taskdir.mkdir(parents=True)
        (self.taskdir / "task.md").write_text(TASK_MD, encoding="utf-8")
        (self.taskdir / "judge.md").write_text(JUDGE_MD_TMPL.format(fix=self.c2), encoding="utf-8")
        self.ws_digest = _tree_digest(self.ws)
        self.out = root / "out"

    def assert_workspace_untouched(self, tc):
        tc.assertEqual(_tree_digest(self.ws), self.ws_digest, "tool wrote into the workspace")

    def close(self):
        self.tmp.cleanup()


class MapRoundsTests(unittest.TestCase):
    def setUp(self):
        self.fx = _Fixture()
        self.addCleanup(self.fx.close)

    def test_parse_rounds_keeps_file_order_and_extracts_snapshot_scopes(self):
        rounds = map_rounds.parse_rounds((self.fx.taskdir / "judge.md").read_text(encoding="utf-8"))
        self.assertEqual(len(rounds), 2)
        self.assertEqual(rounds[0]["verdict"], "PASS")
        self.assertEqual(rounds[1]["verdict"], "FAIL")
        self.assertEqual(rounds[0]["commit"], "1" * 40)
        self.assertEqual(rounds[0]["snapshot"]["app"], self.fx.c2)
        self.assertEqual(rounds[1]["snapshot"], {})
        self.assertEqual(rounds[0]["judges"], "3/3")

    def test_parse_receipts_sorted_chronologically_with_kind(self):
        rec = map_rounds.parse_receipts((self.fx.taskdir / "task.md").read_text(encoding="utf-8"))
        self.assertEqual([r["ts"] for r in rec],
                         ["2026-08-24T13:06:25+03:00", "2026-08-24T13:25:39+03:00",
                          "2026-08-24T14:33:02+03:00"])
        self.assertEqual([r["kind"] for r in rec], ["audit", "audit", "verification"])
        self.assertEqual(rec[0]["commit"], "1" * 40)

    def test_main_prints_rounds_receipts_and_window_commits_and_reads_only(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = map_rounds.main(["--workspace", str(self.fx.ws), "--task", "7",
                                  "--repo", str(self.fx.repo)])
        out = buf.getvalue()
        self.assertEqual(rc, 0, out)
        for sha in (self.fx.c1, self.fx.c2):          # inside the receipt window
            self.assertIn(sha[:10], out)
        self.assertNotIn(self.fx.c0[:10], out.split("COMMITS")[1] if "COMMITS" in out else out,
                         "the base commit predates the task window")
        self.assertIn("PASS", out)
        self.assertIn("FAIL", out)
        self.assertIn("13:06:25", out)
        self.assertIn("app=" + self.fx.c2[:10], out)   # snapshot pairing printed
        self.assertIn("newest-first", out.lower())     # the file-order caveat is stated
        self.fx.assert_workspace_untouched(self)

    def test_unknown_task_number_exits_2(self):
        rc = map_rounds.main(["--workspace", str(self.fx.ws), "--task", "99",
                              "--repo", str(self.fx.repo)])
        self.assertEqual(rc, 2)


class CaseFromTaskTests(unittest.TestCase):
    def setUp(self):
        self.fx = _Fixture()
        self.addCleanup(self.fx.close)

    def _build(self, *extra):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = case_from_task.main(["--workspace", str(self.fx.ws), "--task", "007",
                                      "--repo", str(self.fx.repo), "--reviewed", self.fx.c1[:9],
                                      "--id", "demo-007-r1", "--kind", "feature", "--area", "server",
                                      "--difficulty", "easy", "--repo-name", "app",
                                      "--out", str(self.fx.out), *extra])
        return rc, buf.getvalue()

    def test_writes_a_loadable_case_dir_with_reviewed_sha_and_skeleton_truth(self):
        rc, out = self._build()
        case_dir = self.fx.out / "demo-007-r1"
        for name in ("case.json", "spec.md", "diff.patch", "truth.json"):
            self.assertTrue((case_dir / name).is_file(), name)
        meta = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["repo_base_sha"], self.fx.c1)          # FULL sha of the reviewed commit
        self.assertEqual(meta["diff_of"], f"{self.fx.c0}..{self.fx.c1}")
        self.assertEqual(meta["source"], {"workspace": "ws", "task": "007", "repo": "app"})
        self.assertEqual(meta["truth_version"], 1)
        self.assertIn("diff_excludes", meta)
        truth = json.loads((case_dir / "truth.json").read_text(encoding="utf-8"))
        self.assertEqual(truth, {"findings": [], "known_rejects": []})
        loaded = cases.load_case(case_dir, "demo-007-r1")             # the harness accepts it
        self.assertEqual(loaded.repo_base_sha, self.fx.c1)
        self.fx.assert_workspace_untouched(self)

    def test_spec_is_reconstructed_and_leaks_are_reported_with_exit_1(self):
        rc, out = self._build()
        spec = (self.fx.out / "demo-007-r1" / "spec.md").read_text(encoding="utf-8")
        for leak in ("LEAK_PLAN_REVIEW", "LEAK_TRIAGE", "LEAK_W1_NOTE", "LEAK_W1_WRAP", "LEAK_IMPL",
                     "LEAK_TRIAGE_F1", "LEAK_PREREVIEW", "LEAK_PARKED"):
            self.assertNotIn(leak, spec, leak)
        self.assertIn("## Intent", spec)
        self.assertIn("- [ ] **W1 — implement foo.** Write the loop.", spec)
        # The fixture's Design answer is clean, so the reconstructed spec has no leak
        # tokens → exit 0. (The exit-1 path is covered by the dirty-design test.)
        self.assertEqual(rc, 0, out)
        self.assertIn("prompt", out.lower())
        self.assertRegex(out, r"\b\d[\d,]* chars\b")

    def test_dirty_design_answer_trips_leak_scan_and_exits_1_but_still_writes(self):
        md = (self.fx.taskdir / "task.md").read_text(encoding="utf-8")
        md = md.replace("- [x] Restate — sum the list and return it.",
                        "- [x] Restate — sum the list; revised after PANEL VERDICT: FAIL round 1.")
        (self.fx.taskdir / "task.md").write_text(md, encoding="utf-8")
        self.fx.ws_digest = _tree_digest(self.fx.ws)
        rc, out = self._build()
        self.assertEqual(rc, 1, out)
        self.assertIn("leak", out.lower())
        self.assertIn("PANEL VERDICT: FAIL", out)
        self.assertTrue((self.fx.out / "demo-007-r1" / "spec.md").is_file())

    def test_diff_excludes_lockfiles_dist_binaries_and_explicit_globs(self):
        rc, out = self._build("--exclude", "docs/ledger.json")
        diff = (self.fx.out / "demo-007-r1" / "diff.patch").read_text(encoding="utf-8")
        self.assertIn("a/a.py", diff)
        self.assertIn("def foo(xs):", diff)
        self.assertNotIn("package-lock.json", diff)
        self.assertNotIn("dist/bundle.js", diff)
        self.assertNotIn("img.png", diff)
        self.assertNotIn("docs/ledger.json", diff)
        meta = json.loads((self.fx.out / "demo-007-r1" / "case.json").read_text(encoding="utf-8"))
        self.assertIn("docs/ledger.json", meta["diff_excludes"])
        self.assertIn("img.png", meta["diff_excludes"])      # binaries are recorded, not silent
        self.assertIn("excluded", out.lower())

    def test_refuses_to_overwrite_an_existing_case_dir(self):
        rc, _ = self._build()
        self.assertEqual(rc, 0)
        rc, out = self._build()
        self.assertEqual(rc, 2)
        self.assertIn("exists", out.lower())

    def test_unresolvable_reviewed_sha_exits_2(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = case_from_task.main(["--workspace", str(self.fx.ws), "--task", "007",
                                      "--repo", str(self.fx.repo), "--reviewed", "deadbeefcafe",
                                      "--id", "x", "--kind", "feature", "--area", "server",
                                      "--difficulty", "easy", "--out", str(self.fx.out)])
        self.assertEqual(rc, 2)


class CheckTruthTests(unittest.TestCase):
    def setUp(self):
        self.fx = _Fixture()
        self.addCleanup(self.fx.close)
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            case_from_task.main(["--workspace", str(self.fx.ws), "--task", "007",
                                 "--repo", str(self.fx.repo), "--reviewed", self.fx.c1,
                                 "--id", "demo-007-r1", "--kind", "feature", "--area", "server",
                                 "--difficulty", "easy", "--repo-name", "app",
                                 "--out", str(self.fx.out / "cases"), "--exclude", "docs/ledger.json"])
        self.case_dir = self.fx.out / "cases" / "demo-007-r1"
        (self.fx.out / "corpus.json").write_text(json.dumps({"version": 1, "cases": ["demo-007-r1"]}),
                                                 encoding="utf-8")

    def _truth(self, findings, rejects=()):
        (self.case_dir / "truth.json").write_text(
            json.dumps({"findings": findings, "known_rejects": list(rejects)}, indent=2), encoding="utf-8")

    def _run(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = check_truth.main(["--corpus", str(self.fx.out), "--source-repo", f"app={self.fx.repo}"])
        return rc, buf.getvalue()

    def _good(self):
        return [{"id": "F1", "file": "a.py", "symbol": "foo", "failure_mode": "empty list returns garbage",
                 "severity": "Critical", "historical_outcome": "accepted+fixed", "fix_commit": self.fx.c2}]

    def test_good_case_passes(self):
        self._truth(self._good())
        rc, out = self._run()
        self.assertEqual(rc, 0, out)
        self.assertIn("demo-007-r1", out)
        self.assertIn("OK", out)
        self.fx.assert_workspace_untouched(self)

    def test_file_missing_at_reviewed_sha_fails(self):
        f = self._good()
        f[0]["file"] = "other.py"                    # only exists at c3, after the review
        f[0]["symbol"] = None
        self._truth(f)
        rc, out = self._run()
        self.assertEqual(rc, 1)
        self.assertIn("other.py", out)

    def test_symbol_absent_from_file_at_sha_fails(self):
        f = self._good()
        f[0]["symbol"] = "nonexistent_symbol"
        self._truth(f)
        rc, out = self._run()
        self.assertEqual(rc, 1)
        self.assertIn("nonexistent_symbol", out)

    def test_fix_commit_not_touching_file_fails(self):
        f = self._good()
        f[0]["fix_commit"] = self.fx.c3               # touches other.py only
        self._truth(f)
        rc, out = self._run()
        self.assertEqual(rc, 1)
        self.assertIn(self.fx.c3[:10], out)

    def test_unresolvable_fix_commit_fails(self):
        f = self._good()
        f[0]["fix_commit"] = "deadbeefdeadbeef"
        self._truth(f)
        rc, out = self._run()
        self.assertEqual(rc, 1)
        self.assertIn("deadbeef", out)

    def test_accepted_fixed_without_fix_commit_fails_but_parked_passes(self):
        f = self._good()
        del f[0]["fix_commit"]
        self._truth(f)
        rc, out = self._run()
        self.assertEqual(rc, 1)
        self.assertIn("fix_commit", out)
        f[0]["historical_outcome"] = "accepted+parked"
        self._truth(f)
        rc, out = self._run()
        self.assertEqual(rc, 0, out)

    def test_fix_commit_must_be_after_the_reviewed_commit(self):
        f = self._good()
        f[0]["fix_commit"] = self.fx.c0               # touches a.py but is an ANCESTOR of the review
        self._truth(f)
        rc, out = self._run()
        self.assertEqual(rc, 1)
        self.assertIn("ancestor", out.lower())

    def test_diff_patch_must_rederive_from_diff_of_and_excludes(self):
        self._truth(self._good())
        p = self.case_dir / "diff.patch"
        p.write_text(p.read_text(encoding="utf-8") + "\n+tampered\n", encoding="utf-8")
        rc, out = self._run()
        self.assertEqual(rc, 1)
        self.assertIn("diff.patch", out)

    def test_missing_source_repo_mapping_fails_loud(self):
        self._truth(self._good())
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = check_truth.main(["--corpus", str(self.fx.out)])
        self.assertEqual(rc, 1)
        self.assertIn("app", buf.getvalue())

    def test_prompt_size_budget_is_reported_and_enforced(self):
        self._truth(self._good())
        rc, out = self._run()
        self.assertRegex(out, r"prompt\s+[\d,]+ chars")
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = check_truth.main(["--corpus", str(self.fx.out), "--source-repo", f"app={self.fx.repo}",
                                   "--max-prompt-chars", "100"])
        self.assertEqual(rc, 1)
        self.assertIn("budget", buf.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
