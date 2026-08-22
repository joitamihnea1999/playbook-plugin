#!/usr/bin/env python3
"""Regression + class guard for the dogfooded-ancestry config leak.

The live defect: an owner ran `/playbook:init` on the PARENT of a checkout of
this repo, minting a real `.agent/` (config.json + a live judge `models.json`)
*above* the repo. `tasks intent`'s default runner resolved the judge spec via
`load_judge_config()` with NO project root — a `cwd` walk-up — so a test whose
temp project lived elsewhere still escaped into that ancestor `.agent`, built a
non-Claude adapter instead of the stubbed ClaudeAdapter, and (when the resolved
judge's binary was on PATH) launched a REAL judge CLI against the owner's own
budget: 44s and a spend, not a 0.03s stub call.

The owning boundary is `intent.make_default_runner`: it already anchors the
*budget* to the project root it is handed, but resolved the *judge spec* from
cwd. `load_judge_config`'s own docstring says callers that know the root should
pass it. This module pins that contract with a harness that simulates the
ancestry independently of any real machine layout.

Pure stdlib unittest (stdlib-only runtime invariant). The simulated ancestor's
judge is pinned to a provider whose binary is never on PATH (`gemini`/agy), so a
regression here can never spend real money while the suite runs.

Run: python3 tests/test_intent_ancestry_isolation.py
"""
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

_HERE = Path(__file__).resolve().parent
import sys
sys.path.insert(0, str(_HERE.parent / "plugins/playbook"))

from provider.sandbox import load_judge_config, resolve_judge_spec  # noqa: E402

_ENV_VARS = ("PLAYBOOK_JUDGE_BUDGET_USD", "PLAYBOOK_REVIEW_TIMEOUT_SECS")

# A judge whose binary is never installed here or on CI: resolving it can never
# launch a real CLI, so a reintroduced leak errors on the adapter mismatch
# rather than spending. `gemini` → the agy adapter (see resolve_judge_spec).
POISON_JUDGE = "gemini"
POISON_PROVIDER = "agy"


@contextmanager
def dogfooded_ancestry(judge=POISON_JUDGE):
    """Yield a checkout dir that sits BENEATH a freshly-minted `.agent/`.

    Reproduces `/playbook:init` on a checkout's parent: an ancestor holding a
    real config.json (a live-looking budget) and a models.json pinning a live
    judge panel. `cwd` is moved into the checkout for the body, so any code that
    resolves `.agent` by walking up from cwd finds THIS ancestor — never the
    tester's real machine layout. cwd and env are restored on exit.
    """
    saved_cwd = os.getcwd()
    saved_env = {k: os.environ.pop(k, None) for k in _ENV_VARS}
    tmp = tempfile.TemporaryDirectory(prefix="dogfood-ancestor-")
    try:
        ancestor = Path(tmp.name)
        agent = ancestor / ".agent"
        agent.mkdir()
        (agent / "config.json").write_text(
            json.dumps({"judge_budget_usd": 10}), encoding="utf-8")
        (agent / "models.json").write_text(
            json.dumps({"default_judge": judge, "panel": [judge]}),
            encoding="utf-8")
        checkout = ancestor / "checkout"
        checkout.mkdir()
        os.chdir(checkout)
        yield ancestor, checkout
    finally:
        os.chdir(saved_cwd)
        tmp.cleanup()
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class AncestryHarnessIsArmedTest(unittest.TestCase):
    """Negative control: prove the harness actually plants a trap a leak trips.

    If `dogfooded_ancestry` did not effectively plant a config that an UNPINNED
    (cwd walk-up) resolution picks up, the regression test below could pass for
    the wrong reason — a green that proves nothing. So assert the trap directly:
    the unpinned resolution DOES escape to the poison ancestor, while a
    resolution pinned to an isolated project root does NOT. Reintroduce the leak
    (drop the
    project_root arg in make_default_runner) and the regression test fails; this
    control certifies that trap is real, not a dead fixture.
    """

    def test_unpinned_resolution_escapes_but_pinned_does_not(self):
        with dogfooded_ancestry() as (_ancestor, _checkout):
            # UNPINNED: exactly what the leaking runner did — cwd walk-up.
            leaked = load_judge_config()
            self.assertEqual(
                leaked.get("default_judge"), POISON_JUDGE,
                "harness is not armed: an unpinned resolution should have "
                "escaped into the simulated ancestor .agent/models.json",
            )
            self.assertEqual(resolve_judge_spec(leaked["default_judge"])[0],
                             POISON_PROVIDER)

            # PINNED: an isolated project with no models.json must fall back to
            # the shipped default (opus → claude), NOT the poison ancestor.
            with tempfile.TemporaryDirectory() as proj:
                pinned = load_judge_config(Path(proj))
                self.assertNotEqual(
                    pinned.get("default_judge"), POISON_JUDGE,
                    "a resolution pinned to an isolated root must NOT see the "
                    "ancestor",
                )
                self.assertEqual(resolve_judge_spec(
                    pinned.get("default_judge") or "opus")[0], "claude")


class IntentRunnerIsAncestrySafeTest(unittest.TestCase):
    """Red-first regression for the reported failure, at the owning boundary.

    Runs the intent default-runner from inside a checkout beneath a live-judge
    ancestor and asserts it still builds the adapter its OWN project root
    resolves (the stubbed ClaudeAdapter), passing the project's budget — never
    the ancestor's judge. Before the fix this raised KeyError('budget_usd')
    because a non-Claude adapter was built and the stub never ran.
    """

    def test_runner_anchors_to_its_project_not_the_ancestor(self):
        from provider.adapters import claude as claude_mod
        from tasks import core, intent as intent_mod

        with dogfooded_ancestry() as (_ancestor, _checkout):
            # The runner's OWN project: isolated, its own budget, no models.json.
            with tempfile.TemporaryDirectory() as proj:
                project = Path(proj)
                (project / ".agent").mkdir()
                (project / ".agent" / "config.json").write_text(
                    json.dumps({"judge_budget_usd": 7}), encoding="utf-8")
                core._warn_bad_config_value_once.cache_clear()

                seen = {}

                def fake_judge(self, prompt, model, system_context, *,
                               web_search, timeout_secs, budget_usd):
                    seen["budget_usd"] = budget_usd
                    seen["timeout_secs"] = timeout_secs
                    return "report"

                orig = claude_mod.ClaudeAdapter.run_headless_judge
                claude_mod.ClaudeAdapter.run_headless_judge = fake_judge
                self.addCleanup(lambda: setattr(
                    claude_mod.ClaudeAdapter, "run_headless_judge", orig))

                runner = intent_mod.make_default_runner(project, timeout_secs=1200)
                runner("chat", "prompt --- EVIDENCE\nstuff")

                # If resolution escaped to the ancestor, a non-Claude adapter is
                # built and the stub never runs → this KeyErrors (the red).
                self.assertEqual(seen.get("budget_usd"), "7")
                self.assertEqual(seen.get("timeout_secs"), 1200)


if __name__ == "__main__":
    unittest.main()
