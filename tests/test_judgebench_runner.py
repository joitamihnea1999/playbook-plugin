#!/usr/bin/env python3
"""Step 5 tests: snapshot + runner (`bench/lib/snapshot.py`, `bench/lib/runner.py`).

No real provider is ever invoked: the live path is exercised with an injected
`invoke` callable and a stub adapter factory. The snapshot helper is tested
against a throwaway git repo built in a temp dir.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bench.lib import cases, package, runner, snapshot  # noqa: E402

SHA40 = "0123456789abcdef0123456789abcdef01234567"


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          check=True, encoding="utf-8").stdout.strip()


def _mk_repo(root: Path):
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "keep.py").write_text("BASE = 1\n", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "a.txt").write_text("a\n", encoding="utf-8")
    _git(root, "add", "-A"); _git(root, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    (root / "keep.py").write_text("BASE = 2  # FUTURE_FIX\n", encoding="utf-8")
    (root / "later.py").write_text("LATER = True\n", encoding="utf-8")
    _git(root, "add", "-A"); _git(root, "commit", "-q", "-m", "future fix")
    return base


def _mk_case(root: Path, sha, cid="pb-001-demo"):
    d = root / "cases" / cid
    d.mkdir(parents=True)
    (d / "case.json").write_text(json.dumps({
        "id": cid, "source": {"workspace": "w", "task": "001", "repo": "r"},
        "repo_base_sha": sha, "diff_of": sha[:7], "kind": "feature", "area": "enforcement",
        "difficulty": "medium", "truth_version": 1}), encoding="utf-8")
    (d / "truth.json").write_text(json.dumps({"findings": [], "known_rejects": []}), encoding="utf-8")
    (d / "spec.md").write_text("# 001 - Demo\n\n## Intent\nx\n", encoding="utf-8")
    (d / "diff.patch").write_text("--- a/keep.py\n+++ b/keep.py\n@@ -1 +1 @@\n-BASE = 1\n+BASE = 2\n", encoding="utf-8")
    (root / "corpus.json").write_text(json.dumps({"version": 1, "cases": [cid]}), encoding="utf-8")
    return cases.load_corpus(root).get(cid)


class SnapshotTests(unittest.TestCase):
    def test_snapshot_is_tree_at_sha_without_git_or_future(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"; repo.mkdir()
            base = _mk_repo(repo)
            with snapshot.snapshot_tree(repo, base) as tree:
                self.assertTrue((tree / "keep.py").is_file())
                self.assertEqual((tree / "keep.py").read_text(encoding="utf-8"), "BASE = 1\n")
                self.assertTrue((tree / "sub" / "a.txt").is_file())
                self.assertFalse((tree / "later.py").exists())          # the future is absent
                self.assertFalse((tree / ".git").exists())              # no history at all
                self.assertNotIn("FUTURE_FIX", (tree / "keep.py").read_text(encoding="utf-8"))
                kept = tree
            self.assertFalse(kept.exists())                             # removed on exit

    def test_snapshot_removed_even_when_body_raises(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"; repo.mkdir()
            base = _mk_repo(repo)
            with self.assertRaises(RuntimeError):
                with snapshot.snapshot_tree(repo, base) as tree:
                    kept = tree
                    raise RuntimeError("boom")
            self.assertFalse(kept.exists())

    def test_unknown_sha_and_non_repo_raise_snapshot_error(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"; repo.mkdir()
            _mk_repo(repo)
            with self.assertRaises(snapshot.SnapshotError):
                with snapshot.snapshot_tree(repo, SHA40):
                    pass
            with self.assertRaises(snapshot.SnapshotError):
                with snapshot.snapshot_tree(Path(td) / "notarepo", "HEAD"):
                    pass

    def test_sandbox_tolerates_non_git_snapshot_root(self):
        # The provider sandbox asks git for the project's git dir; a plain
        # snapshot has none and must resolve to None, not raise.
        from provider import sandbox as _sb
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(_sb._git_dir_of(Path(td)))


class CandidateTests(unittest.TestCase):
    def test_parse_specs_and_labels(self):
        cs = runner.parse_candidates("codex:gpt-5.6-sol:medium, sol-high=codex:gpt-5.6-sol:high,opus")
        self.assertEqual([c.label for c in cs], ["codex-gpt-5.6-sol-medium", "sol-high", "opus"])
        self.assertEqual((cs[0].backend, cs[0].variant), ("codex", "gpt-5.6-sol:medium"))
        self.assertEqual((cs[1].backend, cs[1].variant), ("codex", "gpt-5.6-sol:high"))
        self.assertEqual(cs[2].backend, "claude")
        self.assertEqual(runner.parse_candidates("grok:grok-4.6:high")[0].variant, "grok-4.6:high")

    def test_presets_make_the_plan_literal_commands_work(self):
        # plan §14/§24 write `--candidates sol-med,sol-high` (impl-panel sol #5): bench-local
        # presets resolve those labels to the Test A/B seats; explicit `label=spec` still wins.
        cs = runner.parse_candidates("sol-med,sol-high,grok-med,grok-high")
        self.assertEqual([c.label for c in cs], ["sol-med", "sol-high", "grok-med", "grok-high"])
        self.assertEqual([c.spec for c in cs], ["codex:gpt-5.6-sol:medium", "codex:gpt-5.6-sol:high",
                                                "grok:grok-4.6:medium", "grok:grok-4.6:high"])
        self.assertEqual(runner.parse_candidates("sol-med=opus")[0].spec, "opus")

    def test_reserved_and_unsafe_labels_are_rejected(self):
        # r3 sol #3 / grok #3: labels become directories under the run dir.
        for bad in ("journal=opus", "manifest.json=opus", ".lock=opus", ".hidden=opus", "x" * 65 + "=opus"):
            with self.subTest(label=bad.split("=")[0][:12]):
                with self.assertRaises(runner.CandidateError):
                    runner.parse_candidates(bad)

    def test_labels_are_portable_path_segments(self):
        # r4 sol #5: Windows folds case and forbids device names / trailing dots.
        with self.assertRaises(runner.CandidateError):
            runner.parse_candidates("A=opus,a=sonnet")
        for bad in ("CON=opus", "nul=opus", "com1=opus", "trail.=opus"):
            with self.subTest(label=bad):
                with self.assertRaises(runner.CandidateError):
                    runner.parse_candidates(bad)

    def test_bad_spec_duplicate_label_and_empty(self):
        with self.assertRaises(runner.CandidateError):
            runner.parse_candidates("nosuchprovider:x")
        with self.assertRaises(runner.CandidateError):
            runner.parse_candidates("a=opus,a=sonnet")
        with self.assertRaises(runner.CandidateError):
            runner.parse_candidates("")


class ClassifyTests(unittest.TestCase):
    def test_envelope_table(self):
        table = [
            ("(error: codex not found on PATH)", ("dnf", False)),
            ("(error: bench judge timed out)", ("timeout", False)),
            ("(error: bench judge spawn failed: boom)", ("dnf", True)),
            ("(FAILED — exit 1)\n(no output captured)", ("dnf", True)),
            ("(FAILED — exit 1)\n[stderr tail]\nauth failure", ("fail", False)),
            ("(no output)", ("fail", False)),
            ("FINDINGS:\nNONE\nEND FINDINGS\n", ("ok", False)),
            ("just prose", ("ok", False)),
        ]
        for raw, want in table:
            with self.subTest(raw=raw[:30]):
                self.assertEqual(runner.classify(raw), want)
        self.assertEqual(runner.classify("anything", timed_out=True), ("timeout", False))
        # r4 grok #2: the adapters' own size-cap envelopes are DETERMINISTIC — never retried.
        for raw in ("(error: grok judge prompt+context is ~40000 chars on argv; Windows caps the command "
                    "line at 32,767 chars and grok reads its prompt from argv — shrink the context)",
                    "(error: grok judge context is 200,000 bytes in a single argv element; this platform "
                    "caps one element at 131,072 bytes (MAX_ARG_STRLEN = 32 * PAGE_SIZE) and grok reads …)"):
            self.assertEqual(runner.classify(raw), ("dnf", False), raw[:40])

    def test_finish_distinguishes_ok_empty_malformed(self):
        ok = runner.finish("FINDINGS:\n1. FILE: a.py\n   SEVERITY: Minor\n   WHY: w\nEND FINDINGS\n",
                           timed_out=False, duration_ms=5, retries=0)
        self.assertEqual((ok.status, len(ok.findings.findings)), ("ok", 1))
        empty = runner.finish("FINDINGS:\nNONE\nEND FINDINGS\n", timed_out=False, duration_ms=5, retries=0)
        self.assertEqual((empty.status, empty.findings.status), ("ok", "empty"))
        mal = runner.finish("no block at all", timed_out=False, duration_ms=5, retries=0)
        self.assertEqual((mal.status, mal.findings.status), ("malformed", "malformed"))
        self.assertEqual(mal.usage, {"status": "unknown"})
        d = ok.to_dict()
        self.assertEqual(set(d), {"status", "raw", "usage", "duration_ms", "retries", "findings", "note",
                                  "attempts"})


class FakeRunnerTests(unittest.TestCase):
    def test_every_status_is_distinct(self):
        with tempfile.TemporaryDirectory() as td:
            case = _mk_case(Path(td), SHA40)
            pkg = package.build_package(case)
            cands = runner.parse_candidates(",".join(f"c{i}=opus" if i == 0 else f"c{i}=codex:m{i}"
                                                    for i in range(6)))
            script = {f"{case.id}|c0": {"status": "ok"}, f"{case.id}|c1": {"status": "empty"},
                      f"{case.id}|c2": {"status": "malformed"}, f"{case.id}|c3": {"status": "timeout"},
                      f"{case.id}|c4": {"status": "dnf"}, f"{case.id}|c5": {"status": "fail"}}
            fr = runner.FakeRunner(script)
            out = runner.run_case(case, cands, fr, pkg, concurrency=2)
            got = {c.label: inv.status for c, inv in out}
            self.assertEqual(got, {"c0": "ok", "c1": "ok", "c2": "malformed", "c3": "timeout",
                                   "c4": "dnf", "c5": "fail"})
            self.assertEqual(dict(out)[cands[1]].findings.status, "empty")
            self.assertEqual(sorted(fr.calls), sorted((case.id, c.label) for c in cands))

    def test_skip_set_and_no_tree_for_fake(self):
        with tempfile.TemporaryDirectory() as td:
            case = _mk_case(Path(td), SHA40)
            pkg = package.build_package(case)
            cands = runner.parse_candidates("a=opus,b=sonnet")
            fr = runner.FakeRunner()
            out = runner.run_case(case, cands, fr, pkg, skip={"a"})
            self.assertEqual([c.label for c, _ in out], ["b"])
            self.assertEqual(fr.calls, [(case.id, "b")])
            self.assertEqual(runner.run_case(case, cands, fr, pkg, skip={"a", "b"}), [])


class _StubInv:
    def __init__(self, argv, stdin):
        self.argv, self.stdin = argv, stdin


class _StubAdapter:
    """Pretends grok-style (argv) or codex-style (stdin) transport."""
    def __init__(self, backend, project_root):
        self.backend = backend
    def headless_argv(self, prompt, model, **kw):
        if self.backend == "grok":
            return _StubInv(["-p", prompt], None)
        return _StubInv(["exec", "-"], prompt)


class LiveRunnerTests(unittest.TestCase):
    def _case(self, td):
        repo = Path(td) / "repo"; repo.mkdir()
        base = _mk_repo(repo)
        case = _mk_case(Path(td) / "corpus", base)
        return repo, case, package.build_package(case)

    def test_routing_and_snapshot_shared_by_all_candidates(self):
        with tempfile.TemporaryDirectory() as td:
            repo, case, pkg = self._case(td)
            seen = []
            lock = threading.Lock()
            def fake_invoke(backend, variant, prompt, project_root, timeout, budget):
                with lock:
                    seen.append((backend, variant, Path(project_root), timeout, budget))
                # the judge sees the base tree, not the future
                assert (Path(project_root) / "keep.py").read_text(encoding="utf-8") == "BASE = 1\n"
                assert not (Path(project_root) / "later.py").exists()
                assert prompt == pkg.prompt
                return "FINDINGS:\n1. FILE: keep.py\n   SEVERITY: Minor\n   WHY: w\nEND FINDINGS\n"
            lr = runner.LiveRunner(repo, invoke=fake_invoke, adapter_factory=_StubAdapter, budget_usd=3)
            cands = runner.parse_candidates("sol-med=codex:gpt-5.6-sol:medium,sol-high=codex:gpt-5.6-sol:high")
            out = runner.run_case(case, cands, lr, pkg, source_repo=repo, hard_timeout=77, concurrency=2)
            self.assertEqual([inv.status for _, inv in out], ["ok", "ok"])
            self.assertEqual(sorted((b, v) for b, v, *_ in seen),
                             [("codex", "gpt-5.6-sol:high"), ("codex", "gpt-5.6-sol:medium")])
            roots = {r for _, _, r, _, _ in seen}
            self.assertEqual(len(roots), 1, "one snapshot per case, shared by both candidates")
            self.assertFalse(next(iter(roots)).exists(), "snapshot removed after the last candidate")
            self.assertEqual({t for *_, t, _ in seen}, {77})
            self.assertEqual({b for *_, b in seen}, {"3"})
            self.assertTrue(all(inv.duration_ms >= 0 for _, inv in out))

    def test_retry_policy_over_real_envelope(self):
        with tempfile.TemporaryDirectory() as td:
            repo, case, pkg = self._case(td)
            cand = runner.parse_candidates("g=grok:grok-4.6:high")[0]
            def run_with(script):
                calls = []
                def fake_invoke(*a):
                    calls.append(1)
                    return script[min(len(calls) - 1, len(script) - 1)]
                lr = runner.LiveRunner(repo, invoke=fake_invoke, adapter_factory=_StubAdapter, budget_usd=1)
                out = runner.run_case(case, [cand], lr, pkg, source_repo=repo)
                return out[0][1], len(calls)
            inv, n = run_with(["(error: grok not found on PATH)"])
            self.assertEqual((inv.status, n, inv.retries), ("dnf", 1, 0))       # deterministic: no retry
            inv, n = run_with(["(error: bench judge timed out)"])
            self.assertEqual((inv.status, n), ("timeout", 1))
            inv, n = run_with(["(error: bench judge spawn failed: x)", "FINDINGS:\nNONE\nEND FINDINGS\n"])
            self.assertEqual((inv.status, n, inv.retries), ("ok", 2, 1))         # transport → retry → ok
            inv, n = run_with(["(FAILED — exit 1)\n(no output captured)", "(FAILED — exit 1)\n(no output captured)"])
            self.assertEqual((inv.status, n, inv.retries), ("dnf", 2, 1))        # still transport after retry
            inv, n = run_with(["(FAILED — exit 2)\n[stderr tail]\n401 unauthorized"])
            self.assertEqual((inv.status, n), ("fail", 1))                       # data: no retry
            inv, n = run_with(["I think it is fine, no block"])
            self.assertEqual((inv.status, n), ("malformed", 1))                  # content: no retry

    def test_retry_keeps_the_first_attempt_on_record(self):
        # impl-panel r2 sonnet #2: a retried call's first attempt may have been billed —
        # its envelope is kept in `attempts`, never silently dropped.
        with tempfile.TemporaryDirectory() as td:
            repo, case, pkg = self._case(td)
            outs = iter(["(error: bench judge spawn failed: x)", "FINDINGS:\nNONE\nEND FINDINGS\n"])
            lr = runner.LiveRunner(repo, invoke=lambda *a: next(outs), adapter_factory=_StubAdapter, budget_usd=1)
            inv = runner.run_case(case, runner.parse_candidates("a=opus"), lr, pkg, source_repo=repo)[0][1]
            self.assertEqual((inv.status, inv.retries), ("ok", 1))
            self.assertEqual(len(inv.attempts), 1)
            self.assertEqual(inv.attempts[0]["status"], "dnf")
            self.assertIn("spawn failed", inv.attempts[0]["raw_head"])
            self.assertEqual(inv.attempts[0]["usage"], {"status": "unknown"})
            self.assertIn("attempts", inv.to_dict())

    def test_transport_preflight_excludes_whole_case_for_all_candidates(self):
        with tempfile.TemporaryDirectory() as td:
            repo, case, pkg = self._case(td)
            calls = []
            lr = runner.LiveRunner(repo, invoke=lambda *a: calls.append(1) or "x",
                                   adapter_factory=_StubAdapter, budget_usd=1)
            cands = runner.parse_candidates("g=grok:grok-4.6:high,c=codex:gpt-5.6-sol:medium")
            # An argv-transport candidate over the physical cap excludes the case for BOTH.
            from provider import argv_guard
            big = package.Package(case_id=case.id, spec="", diff="",
                                  prompt="x" * (argv_guard.max_arg_bytes() + 10))
            out = runner.run_case(case, cands, lr, big, source_repo=repo)
            self.assertEqual({c.label: inv.status for c, inv in out}, {"g": "excluded", "c": "excluded"})
            self.assertIn("preflight", out[0][1].note)
            self.assertEqual(calls, [], "no candidate may run when the case is excluded")
            # Within budget → nothing excluded.
            self.assertEqual(lr.preflight(cands, pkg, repo), {})

    def test_preflight_applies_the_windows_command_line_cap_for_argv_backends(self):
        # r4 grok #2: argv_byte_error is a no-op on Windows; the real cap there is the
        # adapters' ~30k whole-command-line check — preflight must apply it too.
        with tempfile.TemporaryDirectory() as td:
            repo, case, pkg = self._case(td)
            lr = runner.LiveRunner(repo, invoke=lambda *a: "x", adapter_factory=_StubAdapter, budget_usd=1,
                                   platform_nt=True)
            big = package.Package(case_id=case.id, spec="", diff="", prompt="x" * 31_000)
            errs = lr.preflight(runner.parse_candidates("g=grok:grok-4.6:high,c=codex:m"), big, repo)
            self.assertIn("g", errs); self.assertIn("32,767", errs["g"])
            self.assertNotIn("32,767", errs.get("c", ""))          # stdin transport: no cmdline cap
            small = package.Package(case_id=case.id, spec="", diff="", prompt="x" * 1_000)
            self.assertEqual(lr.preflight(runner.parse_candidates("g=grok:grok-4.6:high"), small, repo), {})

    def test_on_result_callback_fires_inside_the_snapshot_per_candidate(self):
        # r4 grok #3: persist each candidate as it completes, while the snapshot still exists.
        with tempfile.TemporaryDirectory() as td:
            repo, case, pkg = self._case(td)
            seen = []
            lr = runner.LiveRunner(repo, invoke=lambda *a: "FINDINGS:\nNONE\nEND FINDINGS\n",
                                   adapter_factory=_StubAdapter, budget_usd=1)
            trees = []
            def cb(cand, inv, tree):
                seen.append(cand.label)
                trees.append(Path(tree).exists())
            out = runner.run_case(case, runner.parse_candidates("a=opus,b=sonnet"), lr, pkg, source_repo=repo,
                                  on_result=cb)
            self.assertEqual(sorted(seen), ["a", "b"])
            self.assertEqual(trees, [True, True])                  # snapshot alive at callback time
            self.assertEqual(len(out), 2)

    @unittest.skipIf(os.name == "nt", "POSIX argv cap only")
    def test_preflight_uses_adapter_transport_not_a_guess(self):
        with tempfile.TemporaryDirectory() as td:
            repo, case, pkg = self._case(td)
            lr = runner.LiveRunner(repo, invoke=lambda *a: "x", adapter_factory=_StubAdapter, budget_usd=1)
            from provider import argv_guard
            big = package.Package(case_id=case.id, spec="", diff="",
                                  prompt="x" * (argv_guard.max_arg_bytes() + 10))
            errs = lr.preflight(runner.parse_candidates("g=grok:grok-4.6:high,c=codex:m"), big, repo)
            self.assertIn("g", errs)                     # argv transport: physical cap applies
            # the stdin candidate may still trip the CHAR budget; either way it is not the argv error
            self.assertNotIn("argv element", errs.get("c", ""))

    def test_preflight_exception_is_contained_per_case(self):
        # r3 sonnet #2: a raising preflight must not abort the whole run.
        with tempfile.TemporaryDirectory() as td:
            repo, case, pkg = self._case(td)
            class Boom(runner.FakeRunner):
                def preflight(self, candidates, package, repo_root):
                    raise RuntimeError("preflight exploded")
            out = runner.run_case(case, runner.parse_candidates("a=opus,b=sonnet"), Boom(), pkg)
            self.assertEqual([inv.status for _, inv in out], ["dnf", "dnf"])
            self.assertTrue(all("preflight" in inv.note for _, inv in out))

    def test_retry_latency_is_the_final_attempt_only(self):
        # r3 opus F4: p50/p95 must not charge a seat for wasted transport retries.
        import time as _t
        with tempfile.TemporaryDirectory() as td:
            repo, case, pkg = self._case(td)
            outs = iter(["(error: bench judge spawn failed: x)", "FINDINGS:\nNONE\nEND FINDINGS\n"])
            def slow_first(*a):
                r = next(outs)
                if r.startswith("(error"):
                    _t.sleep(0.25)
                return r
            lr = runner.LiveRunner(repo, invoke=slow_first, adapter_factory=_StubAdapter, budget_usd=1)
            inv = runner.run_case(case, runner.parse_candidates("a=opus"), lr, pkg, source_repo=repo)[0][1]
            self.assertLess(inv.duration_ms, 200)                     # final attempt only
            self.assertGreaterEqual(inv.attempts[0]["duration_ms"], 200)

    def test_runner_exception_becomes_dnf_not_abort(self):
        with tempfile.TemporaryDirectory() as td:
            repo, case, pkg = self._case(td)
            def boom(*a):
                raise RuntimeError("adapter exploded")
            lr = runner.LiveRunner(repo, invoke=boom, adapter_factory=_StubAdapter, budget_usd=1)
            out = runner.run_case(case, runner.parse_candidates("a=opus"), lr, pkg, source_repo=repo)
            self.assertEqual(out[0][1].status, "dnf")
            self.assertIn("runner raised", out[0][1].raw)

    def test_snapshot_failure_is_dnf_for_all_candidates(self):
        with tempfile.TemporaryDirectory() as td:
            repo, case, pkg = self._case(td)
            bad = _mk_case(Path(td) / "corpus2", SHA40)          # sha not in repo
            lr = runner.LiveRunner(repo, invoke=lambda *a: "x", adapter_factory=_StubAdapter, budget_usd=1)
            out = runner.run_case(bad, runner.parse_candidates("a=opus,b=sonnet"), lr, pkg, source_repo=repo)
            self.assertEqual([inv.status for _, inv in out], ["dnf", "dnf"])
            self.assertTrue(all("snapshot" in inv.note for _, inv in out))

    def test_default_invoke_is_the_adapter_seam_and_missing_cli_is_dnf(self):
        # The real seam, with a provider whose CLI is never on a CI box: the
        # adapter itself returns the "(error: … not found on PATH)" envelope
        # without spawning anything (no sandbox, no network).
        import shutil
        if shutil.which("agy"):
            self.skipTest("agy present on this machine; test needs it absent")
        with tempfile.TemporaryDirectory() as td:
            raw = runner._adapter_invoke("agy", None, "prompt", td, 5, "1")
            self.assertTrue(raw.lstrip().startswith("(error:"), raw)
            self.assertEqual(runner.classify(raw), ("dnf", False))


if __name__ == "__main__":
    unittest.main()
