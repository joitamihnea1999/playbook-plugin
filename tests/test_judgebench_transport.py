#!/usr/bin/env python3
"""Task 049 / plan §5.1: `corpus validate --transport` — per case, the rendered prompt's
chars/bytes and whether each Test A/B seat's TRANSPORT (stdin for codex/claude, argv for
grok; POSIX per-element cap, Windows whole-line cap, production's char budget) can carry it,
decided by the adapters' own `headless_argv` (the same seam `LiveRunner.preflight` uses)."""
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

from bench.lib import DEFAULT_CORPUS_DIR, cases, runner, transport  # noqa: E402
from provider.adapter import Invocation  # noqa: E402

_ENTRY = _ROOT / "bench" / "judgebench.py"
SHA = "a" * 40
SPEC = "## Intent\nDo the thing.\n\n## Work Plan\n- [ ] W1 build\n"


def _mk_corpus(root: Path, cid="c1", diff_chars=100):
    d = root / "cases" / cid
    d.mkdir(parents=True)
    (d / "case.json").write_text(json.dumps({"id": cid, "source": {"workspace": "w", "task": "001", "repo": "r"},
        "repo_base_sha": SHA, "diff_of": f"{SHA}..{SHA}", "kind": "feature", "area": "server",
        "difficulty": "easy", "truth_version": 1}), encoding="utf-8")
    (d / "truth.json").write_text(json.dumps({"findings": [], "known_rejects": []}), encoding="utf-8")
    (d / "spec.md").write_text(SPEC, encoding="utf-8")
    (d / "diff.patch").write_text("+x" * (diff_chars // 2) + "\n", encoding="utf-8")
    (root / "corpus.json").write_text(json.dumps({"version": 1, "cases": [cid]}), encoding="utf-8")
    return cases.load_corpus(root)


class _StubAdapter:
    """Mirrors the real adapters' transport shape without any CLI: codex/claude → stdin,
    grok → argv `-p <prompt>`."""
    def __init__(self, backend):
        self.backend = backend

    def headless_argv(self, prompt, model, **kw):
        if self.backend == "grok":
            return Invocation(["-p", prompt, "-m", model or "x"], stdin=None)
        return Invocation(["exec", "-"], stdin=prompt)


def _factory(backend, project_root):
    return _StubAdapter(backend)


def _run(*args, env_extra=None, cwd=None):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(_ENTRY), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd=cwd or str(_ROOT), env=env, timeout=300)


class TransportRowsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.corpus = _mk_corpus(Path(self.tmp.name))
        self.cands = runner.parse_candidates("sol-med,sol-high,grok-med,grok-high")

    def test_rows_carry_sizes_and_a_per_preset_transport_verdict(self):
        rows = transport.transport_rows(self.corpus.cases, self.cands, repo_root=_ROOT,
                                        adapter_factory=_factory, platform_nt=False)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["case_id"], "c1")
        self.assertGreater(r["chars"], 0)
        self.assertGreaterEqual(r["bytes"], r["chars"])
        self.assertEqual(r["seats"]["sol-med"]["transport"], "stdin")
        self.assertEqual(r["seats"]["grok-high"]["transport"], "argv")
        self.assertTrue(all(s["fits"] for s in r["seats"].values()), r)
        self.assertTrue(r["fits_all"])

    def test_char_budget_env_makes_the_argv_seat_not_fit_and_names_the_reason(self):
        # production refuses a budget under 10k (a typo, not a choice) — so use 10k and a bigger prompt
        os.environ["PLAYBOOK_REVIEW_CONTEXT_CHARS"] = "10000"
        self.addCleanup(os.environ.pop, "PLAYBOOK_REVIEW_CONTEXT_CHARS", None)
        corpus = _mk_corpus(Path(self.tmp.name) / "mid", diff_chars=20_000)
        rows = transport.transport_rows(corpus.cases, self.cands, repo_root=_ROOT,
                                        adapter_factory=_factory, platform_nt=False)
        r = rows[0]
        self.assertFalse(r["seats"]["grok-med"]["fits"])
        self.assertIn("budget", r["seats"]["grok-med"]["reason"])
        self.assertTrue(r["seats"]["sol-med"]["fits"])          # stdin budget is the larger one
        self.assertFalse(r["fits_all"])

    def test_windows_simulation_applies_the_whole_command_line_cap_to_argv_seats(self):
        corpus = _mk_corpus(Path(self.tmp.name) / "big", diff_chars=40_000)
        rows = transport.transport_rows(corpus.cases, self.cands, repo_root=_ROOT,
                                        adapter_factory=_factory, platform_nt=True)
        r = rows[0]
        self.assertFalse(r["seats"]["grok-med"]["fits"])
        self.assertIn("Windows", r["seats"]["grok-med"]["reason"])
        self.assertTrue(r["seats"]["sol-med"]["fits"])
        rows_posix = transport.transport_rows(corpus.cases, self.cands, repo_root=_ROOT,
                                             adapter_factory=_factory, platform_nt=False)
        self.assertTrue(rows_posix[0]["seats"]["grok-med"]["fits"])

    def test_real_adapters_are_used_when_no_factory_is_given(self):
        # The point of the report is the ADAPTERS' decision — construction must not need the CLI.
        rows = transport.transport_rows(self.corpus.cases, self.cands, repo_root=_ROOT, platform_nt=False)
        self.assertEqual(rows[0]["seats"]["sol-med"]["transport"], "stdin")
        self.assertEqual(rows[0]["seats"]["grok-med"]["transport"], "argv")


class TransportCliTests(unittest.TestCase):
    def test_validate_transport_prints_rows_and_exits_zero_when_all_fit(self):
        with tempfile.TemporaryDirectory() as td:
            _mk_corpus(Path(td))
            p = _run("corpus", "validate", "--transport", "--corpus", td)
            self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
            self.assertIn("c1", p.stdout)
            for seat in ("sol-med", "sol-high", "grok-med", "grok-high"):
                self.assertIn(seat, p.stdout)
            self.assertRegex(p.stdout, r"\d[\d,]* chars")
            self.assertIn("argv", p.stdout); self.assertIn("stdin", p.stdout)

    def test_validate_transport_exits_one_naming_the_case_that_does_not_fit(self):
        with tempfile.TemporaryDirectory() as td:
            _mk_corpus(Path(td), diff_chars=20_000)
            p = _run("corpus", "validate", "--transport", "--corpus", td, env_extra={"PLAYBOOK_REVIEW_CONTEXT_CHARS": "10000"})
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            self.assertIn("c1", p.stdout + p.stderr)
            self.assertIn("grok", p.stdout + p.stderr)

    def test_platform_windows_flag_is_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            _mk_corpus(Path(td))
            p = _run("corpus", "validate", "--transport", "--platform", "windows", "--corpus", td)
            self.assertIn(p.returncode, (0, 1), p.stderr)
            self.assertIn("windows", p.stdout.lower())


class FrozenCorpusTransportTests(unittest.TestCase):
    def test_every_frozen_case_fits_every_test_ab_seat_on_posix(self):
        corpus = cases.load_corpus(DEFAULT_CORPUS_DIR)
        if not corpus.cases:
            self.skipTest("empty corpus")
        rows = transport.transport_rows(corpus.cases, runner.parse_candidates("sol-med,sol-high,grok-med,grok-high"),
                                        repo_root=_ROOT, platform_nt=False)
        bad = [(r["case_id"], s, v["reason"]) for r in rows for s, v in r["seats"].items() if not v["fits"]]
        self.assertEqual(bad, [], bad)


if __name__ == "__main__":
    unittest.main()
