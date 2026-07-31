#!/usr/bin/env python3
"""Point tests for the per-install review config (.agent/config.json).

Covers the precedence matrix (default / config-file / env) and malformed-value
fallback for resolve_judge_budget / resolve_review_timeout, plus a regression
guard that a configured budget actually reaches the claude judge argv (the panel
path that the plan-review panel flagged as initially mis-wired).

Pure stdlib unittest (no hypothesis — honors the stdlib-only runtime invariant).
Run: python3 tests/test_config_resolve.py   (or: python3 -m unittest ...)
"""
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

# The runtime tree is plugins/playbook/ (dispatcher sets PYTHONPATH there).
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "plugins/playbook"))

from tasks import core  # noqa: E402

_ENV_VARS = ("PLAYBOOK_JUDGE_BUDGET_USD", "PLAYBOOK_REVIEW_TIMEOUT_SECS")


class ConfigResolveTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name)
        (self.project / ".agent").mkdir()
        self._saved_env = {k: os.environ.pop(k, None) for k in _ENV_VARS}
        # lru_cache on the bad-value warner would suppress repeat warnings across
        # tests — clear it so each malformed case is independent.
        core._warn_bad_config_value_once.cache_clear()

    def tearDown(self):
        self._tmp.cleanup()
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _write_config(self, obj):
        (self.project / ".agent" / "config.json").write_text(
            obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")

    # ── defaults ──────────────────────────────────────────────────────────
    def test_defaults_when_no_config(self):
        self.assertEqual(core.resolve_judge_budget(self.project), "2")
        self.assertEqual(core.resolve_review_timeout(self.project), 300)

    # ── config file ───────────────────────────────────────────────────────
    def test_config_file_values(self):
        self._write_config({"judge_budget_usd": 5, "review_timeout_secs": 120})
        self.assertEqual(core.resolve_judge_budget(self.project), "5")
        self.assertEqual(core.resolve_review_timeout(self.project), 120)

    def test_float_budget_preserved(self):
        self._write_config({"judge_budget_usd": 3.5})
        self.assertEqual(core.resolve_judge_budget(self.project), "3.5")

    # ── env overrides file ──────────────────────────────────────────────────
    def test_env_overrides_file(self):
        self._write_config({"judge_budget_usd": 5, "review_timeout_secs": 120})
        os.environ["PLAYBOOK_JUDGE_BUDGET_USD"] = "9"
        os.environ["PLAYBOOK_REVIEW_TIMEOUT_SECS"] = "600"
        self.assertEqual(core.resolve_judge_budget(self.project), "9")
        # env may RAISE the hard timeout above the configured floor.
        self.assertEqual(core.resolve_review_timeout(self.project), 600)

    def test_env_cannot_undercut_the_config_floor(self):
        """config is a reliability floor, not merely a lower-precedence tier.

        An env var (or an agent's --timeout) that would re-introduce a shorter
        kill window than the install chose is clamped up to the configured value.
        """
        self._write_config({"review_timeout_secs": 120})
        os.environ["PLAYBOOK_REVIEW_TIMEOUT_SECS"] = "10"
        self.assertEqual(core.resolve_review_timeout(self.project), 120)

    # ── malformed fallbacks (never crash) ───────────────────────────────────
    def test_non_numeric_timeout_falls_back(self):
        self._write_config({"review_timeout_secs": "banana"})
        self.assertEqual(core.resolve_review_timeout(self.project), 300)

    def test_negative_budget_falls_back(self):
        self._write_config({"judge_budget_usd": -3})
        self.assertEqual(core.resolve_judge_budget(self.project), "2")

    def test_zero_timeout_means_unlimited(self):
        """0 is no longer "malformed, use the default" — it means no hard kill.

        A judge that is still writing must not be killed mid-response, so the
        config gained an explicit unlimited form. This inverts the pre-1.4.7
        behaviour where 0 fell through to 300s.
        """
        self._write_config({"review_timeout_secs": 0})
        self.assertIsNone(core.resolve_review_timeout(self.project))

    def test_negative_timeout_still_falls_back(self):
        self._write_config({"review_timeout_secs": -5})
        self.assertEqual(core.resolve_review_timeout(self.project), 300)

    def test_malformed_json_falls_back(self):
        self._write_config("{ not valid json")
        self.assertEqual(core.resolve_review_timeout(self.project), 300)
        self.assertEqual(core.resolve_judge_budget(self.project), "2")

    def test_non_object_json_ignored(self):
        self._write_config("[1, 2, 3]")
        self.assertEqual(core.load_config(self.project), {})
        self.assertEqual(core.resolve_review_timeout(self.project), 300)

    # ── CLI flag tier (highest precedence) ──────────────────────────────────
    def test_flag_beats_env_and_file(self):
        self._write_config({"judge_budget_usd": 5, "review_timeout_secs": 120})
        os.environ["PLAYBOOK_JUDGE_BUDGET_USD"] = "9"
        os.environ["PLAYBOOK_REVIEW_TIMEOUT_SECS"] = "10"
        self.assertEqual(core.resolve_judge_budget(self.project, "7"), "7")
        # The flag wins over env and file, but only upward — 3s is under the
        # configured 120s floor, so the floor holds. See the floor tests below.
        self.assertEqual(core.resolve_review_timeout(self.project, "3"), 120)
        self.assertEqual(core.resolve_review_timeout(self.project, "900"), 900)

    def test_bad_flag_falls_through_to_env(self):
        os.environ["PLAYBOOK_JUDGE_BUDGET_USD"] = "9"
        os.environ["PLAYBOOK_REVIEW_TIMEOUT_SECS"] = "10"
        self.assertEqual(core.resolve_judge_budget(self.project, "foo"), "9")
        self.assertEqual(core.resolve_review_timeout(self.project, "foo"), 10)

    def test_bad_flag_no_lower_tier_falls_to_default(self):
        self.assertEqual(core.resolve_judge_budget(self.project, "foo"), "2")
        self.assertEqual(core.resolve_review_timeout(self.project, "foo"), 300)

    def test_flag_zero_means_unlimited(self):
        self.assertIsNone(core.resolve_review_timeout(self.project, "0"))

    # ── non-finite + env-tier malformed ─────────────────────────────────────
    def test_nonfinite_budget_falls_back(self):
        for bad in ("nan", "inf", "-inf"):
            self._write_config({"judge_budget_usd": bad})
            self.assertEqual(core.resolve_judge_budget(self.project), "2")

    def test_env_negative_budget_falls_back(self):
        os.environ["PLAYBOOK_JUDGE_BUDGET_USD"] = "-3"
        self.assertEqual(core.resolve_judge_budget(self.project), "2")

    def test_env_nonnumeric_timeout_falls_back(self):
        os.environ["PLAYBOOK_REVIEW_TIMEOUT_SECS"] = "banana"
        self.assertEqual(core.resolve_review_timeout(self.project), 300)


class ParseTimeoutTest(unittest.TestCase):
    """The timeout parser: unlimited forms, rejections, and doctor/runtime parity.

    `tasks doctor` validates the raw JSON value while `_first_valid` stringifies
    before parsing, so the two entry points must agree on every input or doctor
    reports a config clean that the runtime silently ignores.
    """

    UNLIMITED = (0, "0", "none", "null", "unlimited", "inf", "infinite",
                 "UNLIMITED", "  Unlimited  ", float("inf"))
    FINITE = ((300, 300), ("300", 300), (1, 1), ("  600 ", 600))
    REJECTED = (-1, "-5", True, False, 1.5, "1.5", 600.0, float("nan"),
                float("-inf"), "banana", "", None, "3s")

    def test_unlimited_forms_parse_to_none(self):
        for raw in self.UNLIMITED:
            with self.subTest(raw=raw):
                self.assertIsNone(core._parse_timeout(raw))

    def test_finite_forms(self):
        for raw, expected in self.FINITE:
            with self.subTest(raw=raw):
                self.assertEqual(core._parse_timeout(raw), expected)

    def test_rejected_forms_raise(self):
        for raw in self.REJECTED:
            with self.subTest(raw=raw):
                with self.assertRaises((TypeError, ValueError)):
                    core._parse_timeout(raw)

    def test_bool_is_not_a_timeout(self):
        """bool subclasses int — True must not silently mean 1 second."""
        with self.assertRaises(ValueError):
            core._parse_timeout(True)

    def test_doctor_and_runtime_agree_on_every_value(self):
        """The regression this guards: int(1.5) == 1 would make doctor call a
        fractional config clean while the runtime rejected "1.5" and used 300."""
        def outcome(value):
            try:
                return core._parse_timeout(value)
            except (TypeError, ValueError):
                return "INVALID"

        # None is excluded deliberately: neither entry point ever sees it.
        # `_first_valid` skips a tier whose raw value is None, and doctor guards
        # with `if _rt is not None`. Including it would only assert that
        # str(None) == "None" lowercases into the "none" unlimited word — an
        # artifact of this table, not a reachable disagreement.
        table = tuple(
            v for v in self.UNLIMITED + tuple(v for v, _ in self.FINITE) + self.REJECTED
            if v is not None
        )
        for raw in table:
            with self.subTest(raw=raw):
                self.assertEqual(
                    outcome(raw), outcome(str(raw)),
                    f"doctor (raw {raw!r}) and runtime (str) disagree",
                )


class ReviewTimeoutFloorTest(unittest.TestCase):
    """config.json is a floor on the hard timeout, not just a precedence tier.

    Without this, an agent passing `--timeout 600` could re-introduce a kill
    window that the install had deliberately removed.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name)
        (self.project / ".agent").mkdir()
        self._saved_env = {k: os.environ.pop(k, None) for k in _ENV_VARS}
        core._warn_bad_config_value_once.cache_clear()

    def tearDown(self):
        self._tmp.cleanup()
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _write_config(self, obj):
        (self.project / ".agent" / "config.json").write_text(
            json.dumps(obj), encoding="utf-8")

    def test_unlimited_config_ignores_a_finite_flag(self):
        self._write_config({"review_timeout_secs": "unlimited"})
        self.assertIsNone(core.resolve_review_timeout(self.project, "600"))

    def test_unlimited_config_ignores_a_finite_env(self):
        self._write_config({"review_timeout_secs": 0})
        os.environ["PLAYBOOK_REVIEW_TIMEOUT_SECS"] = "600"
        self.assertIsNone(core.resolve_review_timeout(self.project))

    def test_finite_config_floors_a_lower_flag(self):
        self._write_config({"review_timeout_secs": 1800})
        self.assertEqual(core.resolve_review_timeout(self.project, "600"), 1800)

    def test_finite_config_allows_a_higher_flag(self):
        self._write_config({"review_timeout_secs": 1800})
        self.assertEqual(core.resolve_review_timeout(self.project, "3600"), 3600)

    def test_flag_unlimited_beats_a_finite_config(self):
        """Unlimited is always "above" a floor — raising is always allowed."""
        self._write_config({"review_timeout_secs": 1800})
        self.assertIsNone(core.resolve_review_timeout(self.project, "unlimited"))

    def test_absent_config_imposes_no_floor(self):
        """A floor is opted into. With no config, the built-in 300s default must
        NOT clamp a deliberate `--timeout 60`, or ordinary precedence breaks on
        every install that never wrote a config file."""
        self.assertEqual(core.resolve_review_timeout(self.project, "60"), 60)

    def test_bare_json_null_means_unset_not_unlimited(self):
        """`"null"` the string is an unlimited spelling; bare JSON `null` is not.

        JSON null idiomatically means "no value", and every other key in this
        file treats it that way, so making it mean "never kill the judge" would
        be a surprising reading of an empty setting. Pinned here because the two
        spellings look alike in a config file and docs/configuration.md now says
        so explicitly.
        """
        self._write_config({"review_timeout_secs": None})
        self.assertEqual(core.resolve_review_timeout(self.project), 300)
        self.assertIsNone(core._parse_timeout("null"))

    def test_malformed_config_imposes_no_floor(self):
        self._write_config({"review_timeout_secs": "banana"})
        self.assertEqual(core.resolve_review_timeout(self.project, "60"), 60)


class ReviewSoftTimeoutTest(unittest.TestCase):
    """The soft deadline: what the judge is told to wind down against.

    Soft is the steering signal; hard is hang safety. They resolve independently
    so an install can say "wind down at 15 minutes, but never be killed".
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name)
        (self.project / ".agent").mkdir()
        self._saved_env = {
            k: os.environ.pop(k, None)
            for k in _ENV_VARS + ("PLAYBOOK_REVIEW_SOFT_TIMEOUT_SECS",)
        }
        core._warn_bad_config_value_once.cache_clear()

    def tearDown(self):
        self._tmp.cleanup()
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _write_config(self, obj):
        (self.project / ".agent" / "config.json").write_text(
            json.dumps(obj), encoding="utf-8")

    def test_default_is_900(self):
        self.assertEqual(
            core.resolve_review_soft_timeout(self.project, hard_timeout_secs=None), 900)

    def test_config_then_env_then_flag(self):
        self._write_config({"review_soft_timeout_secs": 600})
        self.assertEqual(
            core.resolve_review_soft_timeout(self.project, hard_timeout_secs=None), 600)
        os.environ["PLAYBOOK_REVIEW_SOFT_TIMEOUT_SECS"] = "700"
        self.assertEqual(
            core.resolve_review_soft_timeout(self.project, hard_timeout_secs=None), 700)
        self.assertEqual(
            core.resolve_review_soft_timeout(
                self.project, hard_timeout_secs=None, cli_value="800"), 800)

    def test_soft_is_clamped_to_a_finite_hard(self):
        """The prompt must never promise more time than the process will live."""
        self.assertEqual(
            core.resolve_review_soft_timeout(self.project, hard_timeout_secs=300), 300)

    def test_soft_is_not_clamped_when_hard_is_unlimited(self):
        """Regression guard: `None` means two different things here.

        With config hard=300 and `--timeout unlimited`, hard resolves to None (no
        kill). Treating that None as "caller passed nothing" made the resolver
        re-read config and clamp soft to 300 — telling the judge to wind down at
        5 minutes when nothing was going to kill it.
        """
        self._write_config({"review_timeout_secs": 300})
        hard = core.resolve_review_timeout(self.project, "unlimited")
        self.assertIsNone(hard)
        self.assertEqual(
            core.resolve_review_soft_timeout(self.project, hard_timeout_secs=hard), 900)

    def test_omitted_hard_resolves_against_config_not_cli(self):
        self._write_config({"review_timeout_secs": 1800})
        self.assertEqual(core.resolve_review_soft_timeout(self.project), 900)

    def test_zero_soft_means_no_wind_down_instruction(self):
        self._write_config({"review_soft_timeout_secs": 0})
        self.assertIsNone(
            core.resolve_review_soft_timeout(self.project, hard_timeout_secs=300))

    def test_malformed_soft_falls_back_to_default(self):
        self._write_config({"review_soft_timeout_secs": "banana"})
        self.assertEqual(
            core.resolve_review_soft_timeout(self.project, hard_timeout_secs=None), 900)


class TimeoutLabelTest(unittest.TestCase):
    """Banner / judge.md labels — a killed-at-unlimited banner reading "None s"
    was the original reason these exist."""

    def test_format_timeout_label(self):
        self.assertEqual(core.format_timeout_label(None), "unlimited")
        self.assertEqual(core.format_timeout_label(1800), "1800s")

    def test_format_soft_hard_label(self):
        self.assertEqual(
            core.format_soft_hard_timeout_label(900, 1200),
            "soft 900s / hard 1200s")
        self.assertEqual(
            core.format_soft_hard_timeout_label(900, None),
            "soft 900s / hard unlimited")
        self.assertEqual(
            core.format_soft_hard_timeout_label(None, None),
            "soft unlimited / hard unlimited")

    def test_human_duration(self):
        self.assertEqual(core.human_duration(900), "15 minutes")
        self.assertEqual(core.human_duration(60), "1 minute")
        self.assertEqual(core.human_duration(1200), "20 minutes")
        self.assertEqual(core.human_duration(90), "90s")
        self.assertEqual(core.human_duration(30), "30s")


class PanelBudgetThreadingTest(unittest.TestCase):
    """Regression guard for the panel budget path: run_headless_judge must put
    the resolved budget on the claude argv (not a hardcoded value)."""

    def setUp(self):
        self._saved_env = {k: os.environ.pop(k, None) for k in _ENV_VARS}

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _capture_claude_argv(self, **judge_kwargs):
        import shutil
        from provider import sandbox
        from provider.adapters.claude import ClaudeAdapter

        captured = {}
        orig_run, orig_fmt = sandbox.run, sandbox.format_judge_output
        orig_which = shutil.which

        def fake_run(agent, args, **kw):
            captured["args"] = args
            return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

        sandbox.run = fake_run
        sandbox.format_judge_output = lambda r: r.stdout
        shutil.which = lambda name: "/usr/bin/" + name  # pretend claude is installed
        try:
            a = ClaudeAdapter(session_id="judge", project_root=Path("/tmp"))
            a.run_headless_judge(prompt="p", model=None, system_context="c",
                                 web_search=False, timeout_secs=5, **judge_kwargs)
        finally:
            sandbox.run, sandbox.format_judge_output = orig_run, orig_fmt
            shutil.which = orig_which
        return captured["args"]

    def _capture_budget(self, **judge_kwargs):
        args = self._capture_claude_argv(**judge_kwargs)
        return args[args.index("--max-budget-usd") + 1]

    def test_default_budget_on_argv(self):
        self.assertEqual(self._capture_budget(), "2")

    def test_configured_budget_reaches_argv(self):
        self.assertEqual(self._capture_budget(budget_usd="7"), "7")

    def test_claude_judge_runs_at_high_effort(self):
        """A judge is bought for its reasoning, so it does not inherit the
        session default. 'high' rather than 'max' is deliberate: this bills the
        owner's own Claude subscription quota, and max on every panel seat would
        drain the quota they need for their own work."""
        args = self._capture_claude_argv()
        self.assertIn("--effort", args)
        self.assertEqual(args[args.index("--effort") + 1], "high")


class EffortFlagScopeTest(unittest.TestCase):
    """--effort is a claude flag. It must reach BOTH claude judge launch paths
    and no other provider's argv.

    Two paths exist and are easy to forget: the panel seat goes through
    ClaudeAdapter.run_headless_judge, while `plan-review --backend claude`
    assembles its own argv in tasks/cli.py. A depth setting on one and not the
    other means the same judge reviews differently depending on how it was
    invoked.
    """

    PLAYBOOK = Path(__file__).resolve().parent.parent / "plugins" / "playbook"

    def test_both_claude_launch_paths_pass_effort_high(self):
        adapter = (self.PLAYBOOK / "provider" / "adapters" / "claude.py").read_text(
            encoding="utf-8")
        cli = (self.PLAYBOOK / "tasks" / "cli.py").read_text(encoding="utf-8")
        self.assertIn('"--effort", "high"', adapter)
        self.assertIn('"--effort", "high"', cli)

    def test_no_other_adapter_gained_the_flag(self):
        for name in ("codex", "antigravity", "pi", "grok"):
            path = self.PLAYBOOK / "provider" / "adapters" / f"{name}.py"
            with self.subTest(adapter=name):
                self.assertNotIn(
                    '"--effort"', path.read_text(encoding="utf-8"),
                    f"{name} is not claude — it has no --effort flag",
                )


class TimedOutSeatStillCountsAsFailedTest(unittest.TestCase):
    """A salvaged partial must not promote a timed-out panel seat to "succeeded".

    The panel appends the judge's partial output under the `(timed out…)` marker
    so the seat still contributes what it found. `judge_failed` anchors on the
    block START, so the marker has to stay first — this pins that contract, since
    reordering it would silently turn a truncated review into a clean one.
    """

    def test_marker_first_keeps_the_seat_failed(self):
        from tasks.models_check import judge_failed

        salvaged = (
            "(timed out after hard 1200s)\n\n"
            "**INCOMPLETE** — killed mid-response; the findings below may be cut "
            "off and reached no conclusion:\n\n"
            "1. **Important** — line 42 is wrong."
        )
        self.assertTrue(judge_failed(salvaged))

    def test_a_finished_review_is_not_flagged(self):
        from tasks.models_check import judge_failed

        self.assertFalse(judge_failed("1. **Important** — line 42 is wrong."))

    def test_partial_text_alone_would_have_passed(self):
        """Shows the marker is doing the work, not the INCOMPLETE banner."""
        from tasks.models_check import judge_failed

        self.assertFalse(judge_failed("**INCOMPLETE** — killed mid-response"))


@unittest.skipIf(os.name == "nt", "POSIX process-group termination path")
class RunWithTimeoutTest(unittest.TestCase):
    """Regression guard for the B8 fix: sandbox.run(timeout=) must terminate the
    whole tree on expiry — a naive subprocess.run(timeout=) killed only the
    direct child while grandchildren kept the pipe open and hung communicate()."""

    def test_timeout_kills_tree_and_returns_fast(self):
        import time
        from provider import sandbox

        d = tempfile.mkdtemp()
        pidfile = Path(d) / "grandchild.pid"
        # outer sh (process-group leader via start_new_session) spawns a
        # grandchild that records its pid then sleeps; both outlast the 1s
        # timeout, so a lone direct-child kill would hang on the held pipe.
        wrapped = ["sh", "-c",
                   f"(sh -c 'echo $$ > {pidfile}; exec sleep 60') & sleep 60"]
        t0 = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            sandbox._run_with_timeout(
                wrapped, Path(d), dict(os.environ),
                capture_output=True, check=False, kwargs={"timeout": 1, "text": True},
            )
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 15, f"timeout path took {elapsed:.1f}s — did it hang?")

        # The grandchild must be dead (tree killed, not just the leader).
        time.sleep(0.5)
        pid = int(pidfile.read_text().strip())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_partial_output_survives_the_kill(self):
        """A judge killed at its ceiling has usually written most of its findings.

        Popen.communicate() does not attach output to TimeoutExpired the way
        subprocess.run() does, so the reap-after-kill used to discard it and a
        20-minute review returned nothing at all. The output must reach the
        caller so it can be salvaged into the review log.
        """
        from provider import sandbox

        d = tempfile.mkdtemp()
        wrapped = ["sh", "-c", "echo 'PARTIAL FINDING'; echo 'warned' >&2; sleep 60"]
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            sandbox._run_with_timeout(
                wrapped, Path(d), dict(os.environ),
                capture_output=True, check=False, kwargs={"timeout": 2, "text": True},
            )
        self.assertIn("PARTIAL FINDING", caught.exception.stdout or "")
        self.assertIn("warned", caught.exception.stderr or "")


if __name__ == "__main__":
    unittest.main()
