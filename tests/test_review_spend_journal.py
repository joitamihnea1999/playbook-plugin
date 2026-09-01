#!/usr/bin/env python3
"""Review-spend journal (task 042) — every judge invocation appends one
best-effort spend record to the enforcement journal (`hook="review"`,
`decision="record"`, `reason="review spend"`) via `pb_journal.append_review`,
emitted from the review runner where the subprocess already completed.

What this pins:
  * `append_review` format — the envelope + review fields, byte-cap, round
    coercion, and the `usage` unknown/known marker (numbers are NEVER
    fabricated);
  * the same HARD CONTRACT as the enforcement journal — an UNWRITABLE journal
    changes nothing (the review's exit code and judge log are byte-for-byte what
    they are without a journal; the negative control drives the write to a hard
    failure by making the journal dir a regular file);
  * end-to-end: a record lands PER SEAT on a panel and ONE on a single judge,
    all carrying kind/seat/round/duration_ms/status.

The two review helpers `_judge_status` / `_parse_judge_usage` / `_next_review_round`
are unit-pinned too. Stdlib only. Run: python3 tests/test_review_spend_journal.py
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PLAYBOOK = _HERE.parent / "plugins" / "playbook"
sys.path.insert(0, str(_PLAYBOOK))

from tasks import review  # noqa: E402


def _load_pb_journal():
    p = _PLAYBOOK / "scripts" / "pb_journal.py"
    spec = importlib.util.spec_from_file_location("_pbj_test", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pbj = _load_pb_journal()


def _read_journal(agent_dir: Path) -> "list[dict]":
    p = agent_dir / "journal" / "enforcement.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


@contextlib.contextmanager
def _chdir(d: Path):
    prev = Path.cwd()
    os.chdir(d)
    try:
        yield
    finally:
        os.chdir(prev)


# ── append_review: format + contract ─────────────────────────────────────────
class AppendReviewFormat(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.agent = Path(self._tmp.name) / ".agent"
        (self.agent / "tasks").mkdir(parents=True)

    def test_envelope_and_fields_pinned(self):
        pbj.append_review(self.agent, session_id="sid", seat="claude:opus",
                          task="042", round_no=3, kind="panel",
                          duration_ms=1234, status="ok", usage=None)
        recs = _read_journal(self.agent)
        self.assertEqual(len(recs), 1)
        r = recs[0]
        # Envelope shared with every enforcement record.
        self.assertEqual(r["hook"], "review")
        self.assertEqual(r["decision"], "record")
        self.assertEqual(r["reason"], "review spend")
        self.assertEqual(r["session_id"], "sid")
        self.assertRegex(r["ts"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        # Review-specific fields.
        self.assertEqual(r["kind"], "panel")
        self.assertEqual(r["seat"], "claude:opus")
        self.assertEqual(r["task"], "042")
        self.assertEqual(r["round"], 3)
        self.assertEqual(r["duration_ms"], 1234)
        self.assertEqual(r["status"], "ok")
        # usage=None → the explicit unknown marker, never a fabricated number.
        self.assertEqual(r["usage"], {"status": "unknown"})
        # Exactly the expected key set — a new field must be a deliberate change.
        self.assertEqual(set(r), {
            "ts", "session_id", "hook", "decision", "reason",
            "kind", "seat", "task", "round", "duration_ms", "status", "usage"})

    def test_known_usage_recorded_verbatim(self):
        pbj.append_review(self.agent, seat="claude:opus", task="1", round_no=1,
                          kind="single", duration_ms=5, status="ok",
                          usage={"status": "known", "in": 10, "out": 20})
        self.assertEqual(_read_journal(self.agent)[0]["usage"],
                         {"status": "known", "in": 10, "out": 20})

    def test_round_coerced_and_duration_optional(self):
        # A non-int round → 0 (unknown); a missing duration → field absent.
        pbj.append_review(self.agent, seat="x", task="1", round_no="bad",
                          kind="single", duration_ms=None, status="ok")
        r = _read_journal(self.agent)[0]
        self.assertEqual(r["round"], 0)
        self.assertNotIn("duration_ms", r)

    def test_seat_byte_capped_line_stays_single_and_small(self):
        pbj.append_review(self.agent, seat="claude:" + "z" * 500, task="1",
                          round_no=1, kind="panel", duration_ms=1, status="ok")
        raw = (self.agent / "journal" / "enforcement.jsonl").read_bytes()
        self.assertEqual(raw.count(b"\n"), 1)          # exactly one line
        self.assertLess(len(raw), 512)                 # under the PIPE_BUF floor

    def test_usage_normalized_not_copied_verbatim(self):
        # An arbitrary/oversized usage dict must NOT be serialized verbatim
        # (impl-panel codex: a big dict blew the PIPE_BUF bound). Only the fixed
        # {status,in,out} schema survives; unknown shapes degrade to unknown.
        pbj.append_review(self.agent, seat="claude:opus", task="1", round_no=1,
                          kind="single", duration_ms=1, status="ok",
                          usage={"status": "known", "in": 10, "out": 20,
                                 "junk": "z" * 5000, "cost": 1.23})
        pbj.append_review(self.agent, seat="claude:opus", task="1", round_no=1,
                          kind="single", duration_ms=1, status="ok",
                          usage={"weird": "shape"})
        recs = _read_journal(self.agent)
        self.assertEqual(recs[0]["usage"], {"status": "known", "in": 10, "out": 20})
        self.assertEqual(recs[1]["usage"], {"status": "unknown"})
        # Non-int token counts are never fabricated → unknown.
        pbj.append_review(self.agent, seat="x", task="1", round_no=1, kind="single",
                          usage={"status": "known", "in": "lots", "out": 5})
        self.assertEqual(_read_journal(self.agent)[2]["usage"], {"status": "unknown"})

    def test_pathological_numeric_magnitudes_capped(self):
        # Numeric fields (round, duration_ms, usage in/out) must be magnitude-
        # capped so a pathological caller cannot blow the PIPE_BUF line bound
        # (impl-panel round 2). A 1000-digit int must not produce a 4KB line.
        big = 10 ** 1000
        pbj.append_review(self.agent, seat="claude:opus:high", task="1",
                          round_no=big, kind="single", duration_ms=big,
                          status="ok",
                          usage={"status": "known", "in": big, "out": big})
        raw = (self.agent / "journal" / "enforcement.jsonl").read_bytes()
        self.assertEqual(raw.count(b"\n"), 1)
        self.assertLess(len(raw), 512)
        r = _read_journal(self.agent)[0]
        cap = 10 ** 15 - 1
        self.assertEqual(r["round"], cap)
        self.assertEqual(r["duration_ms"], cap)
        self.assertEqual(r["usage"], {"status": "known", "in": cap, "out": cap})

    def test_oversized_session_id_capped_line_small(self):
        pbj.append_review(self.agent, session_id="s" * 5000, seat="claude:opus",
                          task="1", round_no=1, kind="single", duration_ms=1,
                          status="ok", usage={"status": "known", "in": 1, "out": 2})
        raw = (self.agent / "journal" / "enforcement.jsonl").read_bytes()
        self.assertEqual(raw.count(b"\n"), 1)
        self.assertLess(len(raw), 512)

    def test_control_chars_do_not_expand_the_line(self):
        # json escapes each control char to 6 bytes (\uXXXX), so a byte cap alone
        # would not bound the SERIALIZED line — an 80-NUL seat once serialized to
        # ~694 bytes (impl-panel round 3). Control chars are stripped, so the line
        # stays one record under 512 bytes.
        pbj.append_review(self.agent, session_id="\x00" * 200, seat="\x00" * 200,
                          task="\x01" * 50, round_no=1, kind="\x1f" * 50,
                          status="ok\x00", duration_ms=1)
        raw = (self.agent / "journal" / "enforcement.jsonl").read_bytes()
        self.assertEqual(raw.count(b"\n"), 1)
        self.assertLess(len(raw), 512)
        r = _read_journal(self.agent)[0]           # still valid JSON
        self.assertEqual(r["seat"], "")            # control chars dropped
        self.assertEqual(r["status"], "ok")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires mkfifo (POSIX)")
    def test_fifo_journal_does_not_hang(self):
        # A rogue judge that swaps enforcement.jsonl for a FIFO must not turn the
        # next write into an indefinite hang (impl-panel round 3). O_NONBLOCK
        # makes the open fail fast; the call returns and records nothing. The test
        # completing at all is the proof it did not block.
        (self.agent / "journal").mkdir()
        os.mkfifo(self.agent / "journal" / "enforcement.jsonl")
        pbj.append_review(self.agent, seat="claude:opus:high", task="1",
                          round_no=1, kind="single", duration_ms=1, status="ok")

    @unittest.skipUnless(hasattr(os, "symlink"), "requires symlink")
    def test_symlinked_journal_file_writes_nothing_outside_lane(self):
        # O_NOFOLLOW: a symlinked enforcement.jsonl must not let a write escape
        # the lane (impl-panel round 3).
        outside = Path(self._tmp.name) / "outside.txt"
        outside.write_text("", encoding="utf-8")
        (self.agent / "journal").mkdir()
        os.symlink(outside, self.agent / "journal" / "enforcement.jsonl")
        pbj.append_review(self.agent, seat="claude:opus:high", task="1",
                          round_no=1, kind="single", duration_ms=1, status="ok")
        self.assertEqual(outside.read_text(encoding="utf-8"), "")

    def test_unwritable_lane_is_silent_noop(self):
        # journal PATH is a regular file → mkdir + open both fail. Must not raise.
        (self.agent / "journal").write_text("i am a file", encoding="utf-8")
        pbj.append_review(self.agent, seat="x", task="1", round_no=1,
                          kind="single", duration_ms=1, status="ok")
        # nonexistent lane dir → also a silent no-op.
        pbj.append_review(self.agent.parent / "nope", seat="x", task="1",
                          round_no=1, kind="single")


# ── review helpers ───────────────────────────────────────────────────────────
class SpendHelpers(unittest.TestCase):
    def test_judge_status_mapping(self):
        self.assertEqual(review._judge_status("1. **Note** — fine"), "ok")
        self.assertEqual(review._judge_status("x", timed_out=True), "timeout")
        self.assertEqual(review._judge_status("(error: boom)"), "dnf")
        # The tail-cert path reports its timeout as an error string, not a raise.
        self.assertEqual(
            review._judge_status("(error: tail-cert judge timed out)"), "timeout")
        # A FAILED-marked block classifies as fail, not ok.
        self.assertEqual(review._judge_status("(FAILED — exit 1)\n..."), "fail")

    def test_parse_usage_unknown_and_known(self):
        self.assertIsNone(review._parse_judge_usage("plain review, no usage line"))
        self.assertIsNone(review._parse_judge_usage(""))
        # Known usage is recognized ONLY from a structured JSON envelope, not prose.
        got = review._parse_judge_usage(
            '{"usage":{"input_tokens":123,"output_tokens":45}}')
        self.assertEqual(got, {"status": "known", "in": 123, "out": 45})
        # A JSON envelope without the usage shape → unknown (None).
        self.assertIsNone(review._parse_judge_usage('{"result":"ok"}'))

    def test_parse_usage_prose_quote_is_never_fabricated(self):
        """The self-poison the impl panel (sonnet) caught: a judge that merely
        QUOTES a usage-shaped string in its free-form review must NOT be recorded
        as real token spend. Anchoring to a whole-JSON envelope defuses it — prose
        never parses as one JSON object."""
        prose = (
            "The test at line 150 contains the literal "
            '`"usage":{"input_tokens":123,"output_tokens":45}}` which I am '
            "citing as a finding, not reporting as my own usage.")
        self.assertIsNone(review._parse_judge_usage(prose))
        # Even a leading brace that is not a valid single object stays None.
        self.assertIsNone(review._parse_judge_usage(
            '{ blah "usage":{"input_tokens":1,"output_tokens":2} trailing prose'))

    def test_claude_effort_constant_matches_the_adapter(self):
        # The recorded claude seat's :effort is only truthful while
        # _CLAUDE_JUDGE_EFFORT equals the --effort the adapter actually passes
        # (impl-panel round 3). Pin the two literals together.
        adapter_src = (_PLAYBOOK / "provider" / "adapters" / "claude.py").read_text(
            encoding="utf-8")
        self.assertIn(f'"--effort", "{review._CLAUDE_JUDGE_EFFORT}"', adapter_src)
        cli_src = (_PLAYBOOK / "tasks" / "review.py").read_text(encoding="utf-8")
        self.assertIn(f'"--effort", "{review._CLAUDE_JUDGE_EFFORT}"', cli_src)

    def test_tail_cert_seat_resolves_model_effort(self):
        # Exercise the REAL _tail_cert_seat wiring (the e2e monkeypatches it), so
        # a regression dropping the effort suffix is caught (impl-panel round 3).
        with tempfile.TemporaryDirectory() as d:
            proj = Path(d)
            (proj / ".agent" / "tasks").mkdir(parents=True)
            (proj / ".agent" / "models.json").write_text(
                json.dumps({"default_judge": "opus"}), encoding="utf-8")
            with _chdir(proj):
                # resolve_judge_spec expands the "opus" alias to the model id;
                # the point is the claude :high effort suffix is appended.
                seat = review._tail_cert_seat(proj)
                self.assertTrue(seat.startswith("claude:"), seat)
                self.assertTrue(seat.endswith(":high"), seat)
            (proj / ".agent" / "models.json").write_text(
                json.dumps({"default_judge": "codex:gpt-5.6-terra:medium"}),
                encoding="utf-8")
            with _chdir(proj):
                self.assertEqual(review._tail_cert_seat(proj),
                                 "codex:gpt-5.6-terra:medium")

    def test_next_review_round(self):
        with tempfile.TemporaryDirectory() as d:
            tdir = Path(d) / ".agent" / "tasks" / "042-x"
            tdir.mkdir(parents=True)
            tf = tdir / "task.md"
            tf.write_text("# 042\n", encoding="utf-8")
            # No judge.md yet → round 1.
            self.assertEqual(review._next_review_round(d, tf), 1)
            # One recorded panel round → next is 2.
            (tdir / "judge.md").write_text(
                "# Panel Plan Review — a/042-x/task.md\n\nsome findings\n",
                encoding="utf-8")
            self.assertEqual(review._next_review_round(d, tf), 2)

    def test_seat_with_effort(self):
        # Claude runs at a fixed --effort not in the variant → appended.
        self.assertEqual(review._seat_with_effort("claude", "opus"), "claude:opus:high")
        self.assertEqual(review._seat_with_effort("claude", None), "claude:high")
        # codex/grok already encode effort in the variant → left as-is.
        self.assertEqual(review._seat_with_effort("codex", "gpt-5.6-terra:medium"),
                         "codex:gpt-5.6-terra:medium")
        self.assertEqual(review._seat_with_effort("grok", "grok-4.6:high"),
                         "grok:grok-4.6:high")

    def test_next_review_round_counts_archive(self):
        """After retention (5) archives older rounds, counting judge.md alone
        would cap the round at 6 (impl-panel codex). Both files must be summed."""
        with tempfile.TemporaryDirectory() as d:
            tdir = Path(d) / ".agent" / "tasks" / "042-x"
            tdir.mkdir(parents=True)
            tf = tdir / "task.md"
            tf.write_text("# 042\n", encoding="utf-8")
            # 5 retained rounds in judge.md + 4 archived → 9 recorded → next = 10.
            hdr = "# Panel Impl Review — a/042-x/task.md"
            (tdir / "judge.md").write_text(
                "\n\n".join(f"{hdr}\n\nround {i}" for i in range(5)), encoding="utf-8")
            (tdir / "judge-archive.md").write_text(
                "\n\n".join(f"{hdr}\n\nround {i}" for i in range(4)), encoding="utf-8")
            self.assertEqual(review._next_review_round(d, tf), 10)


# ── end-to-end: records land per invocation ──────────────────────────────────
class _E2EBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name)
        self.agent = self.project / ".agent"
        tdir = self.agent / "tasks" / "042-demo"
        tdir.mkdir(parents=True)
        (tdir / "task.md").write_text(
            "# 042 - demo\n## Status\npending\n## Intent\nx\n"
            "## Work Plan\n- [ ] a gate\n", encoding="utf-8")
        (self.agent / "models.json").write_text(
            json.dumps({"panel": ["claude:opus", "claude:sonnet"],
                        "default_judge": "claude:opus"}), encoding="utf-8")
        # Reset the module-level pb_journal cache so each project resolves fresh.
        review._PB_JOURNAL_MOD = None
        review._PB_JOURNAL_LOADED = False


class PanelSpendE2E(_E2EBase):
    def test_one_record_per_seat(self):
        from provider.adapters.claude import ClaudeAdapter
        _orig_avail = ClaudeAdapter.is_available
        _orig_run = ClaudeAdapter.run_headless_judge
        ClaudeAdapter.is_available = classmethod(lambda cls: True)
        ClaudeAdapter.run_headless_judge = (
            lambda self, **kw: "1. **Note** — looks fine.\n")
        try:
            with _chdir(self.project):
                with contextlib.suppress(SystemExit):
                    review.cmd_panel_review(
                        ["042", "--models", "claude:opus,claude:sonnet"])
        finally:
            ClaudeAdapter.is_available = _orig_avail
            ClaudeAdapter.run_headless_judge = _orig_run

        recs = [r for r in _read_journal(self.agent) if r["hook"] == "review"]
        self.assertEqual(len(recs), 2, recs)
        # Claude seats carry the fixed judge effort (model:effort) — the owner ask.
        self.assertEqual({r["seat"] for r in recs},
                         {"claude:opus:high", "claude:sonnet:high"})
        for r in recs:
            self.assertEqual(r["kind"], "panel")
            self.assertEqual(r["task"], "042")
            self.assertEqual(r["decision"], "record")
            self.assertIn("duration_ms", r)
            self.assertEqual(r["status"], "ok")
        # Both seats share the one panel round.
        self.assertEqual(len({r["round"] for r in recs}), 1)


class SingleSpendE2E(_E2EBase):
    def _run_single(self):
        """Run a claude single plan-review with a faked subprocess; return the
        review's observable outcome (exit code, judge-log text) for the negative
        control's byte-for-byte compare."""
        import shutil
        import types
        from provider import sandbox
        _orig_run, _orig_which = sandbox.run, shutil.which
        sandbox.run = lambda agent, args, **kw: types.SimpleNamespace(
            returncode=0, stdout="1. **Note** — looks fine.\n", stderr="")
        shutil.which = lambda name: "/usr/bin/" + name
        code = None
        try:
            with _chdir(self.project):
                try:
                    review.cmd_single_review(
                        "plan-review", ["042", "--backend", "claude", "--model", "opus"])
                    code = 0
                except SystemExit as e:
                    code = e.code
        finally:
            sandbox.run, shutil.which = _orig_run, _orig_which
        log = self.agent / "tasks" / "042-demo" / "judge.log"
        return code, (log.read_text(encoding="utf-8") if log.exists() else None)

    def test_one_single_record(self):
        self._run_single()
        recs = [r for r in _read_journal(self.agent) if r["hook"] == "review"]
        self.assertEqual(len(recs), 1, recs)
        r = recs[0]
        self.assertEqual(r["kind"], "single")
        self.assertEqual(r["seat"], "claude:opus:high")   # model:effort (owner ask)
        self.assertEqual(r["task"], "042")
        self.assertEqual(r["status"], "ok")
        self.assertIn("duration_ms", r)

    def _run_single_timeout(self, *, tampered=False):
        """Force the hard-timeout bail path (a finite --timeout + a dispatch that
        raises TimeoutExpired) and return the journal records."""
        import shutil
        import subprocess
        import unittest.mock as mock
        from provider import sandbox

        def _boom(agent, args, **kw):
            raise subprocess.TimeoutExpired(cmd="claude", timeout=5)

        patches = [
            mock.patch.object(sandbox, "run", _boom),
            mock.patch.object(shutil, "which", lambda name: "/usr/bin/" + name),
            mock.patch.object(review, "_detect_tamper_safe",
                              lambda pp, t, b: (["dirty"] if tampered else [])),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        with _chdir(self.project):
            with contextlib.suppress(SystemExit):
                review.cmd_single_review(
                    "plan-review",
                    ["042", "--backend", "claude", "--model", "opus", "--timeout", "5"])
        return [r for r in _read_journal(self.agent) if r["hook"] == "review"]

    def test_timeout_records_timeout_status(self):
        recs = self._run_single_timeout()
        self.assertEqual(len(recs), 1, recs)
        self.assertEqual(recs[0]["status"], "timeout")
        self.assertEqual(recs[0]["kind"], "single")
        self.assertEqual(recs[0]["seat"], "claude:opus:high")

    def test_timeout_with_tamper_records_nothing(self):
        # A timed-out judge that also tampered: the banner already fired, and the
        # uniform "tamper records nothing" rule applies (gated on _to_changes).
        self.assertEqual(self._run_single_timeout(tampered=True), [])

    def test_unwritable_journal_changes_nothing(self):
        # Baseline: writable journal.
        base_code, base_log = self._run_single()
        base_recs = _read_journal(self.agent)
        self.assertTrue(base_recs)                     # it did record
        # Now sabotage the journal: make its dir a regular FILE so mkdir+open
        # both fail on the next run. The review outcome must be identical.
        for f in (self.agent / "journal").glob("*"):
            f.unlink()
        (self.agent / "journal").rmdir()
        (self.agent / "journal").write_text("not a dir", encoding="utf-8")
        sab_code, sab_log = self._run_single()
        self.assertEqual(sab_code, base_code)          # same exit
        self.assertEqual(sab_log, base_log)            # same judge log, byte-for-byte
        # And nothing was appended (the file stayed the sabotage content).
        self.assertEqual((self.agent / "journal").read_text(encoding="utf-8"),
                         "not a dir")


class TailCertSpendE2E(_E2EBase):
    def _run(self, *, tampered=False, raw="TAIL-CERT deadbeef: PASS"):
        import unittest.mock as mock
        tf = self.agent / "tasks" / "042-demo" / "task.md"
        patches = [
            mock.patch.object(review, "_tail_cert_review_diff",
                              lambda pp, snap: "diff --git a b\n+one line\n"),
            mock.patch.object(review, "_run_tail_cert_judge_raw",
                              lambda pp, prompt, ts: raw),
            mock.patch.object(review, "_snapshot_repo_state", lambda pp, t: {}),
            mock.patch.object(review, "_detect_tamper_safe",
                              lambda pp, t, b: (["dirty"] if tampered else [])),
            mock.patch.object(review, "_tail_cert_seat", lambda pp: "claude:opus"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        with _chdir(self.project):
            return review.run_tail_cert_judge(
                self.project, {}, ["non-behavioral note"], "panel summary",
                task_file=tf)

    def test_tail_cert_records_one(self):
        self._run()
        recs = [r for r in _read_journal(self.agent)
                if r["hook"] == "review" and r["kind"] == "tail-cert"]
        self.assertEqual(len(recs), 1, recs)
        r = recs[0]
        self.assertEqual(r["seat"], "claude:opus")
        self.assertEqual(r["task"], "042")
        self.assertEqual(r["status"], "ok")
        self.assertIn("duration_ms", r)

    def test_tail_cert_tamper_records_nothing(self):
        # A tamper hard-stop returns None (block) and records NO spend — the same
        # uniform rule the panel/single paths follow (no write before the stop).
        self.assertIsNone(self._run(tampered=True))
        self.assertEqual(
            [r for r in _read_journal(self.agent) if r["hook"] == "review"], [])


class TamperRecordsNothing(_E2EBase):
    def test_panel_tamper_no_records(self):
        import unittest.mock as mock
        from provider.adapters.claude import ClaudeAdapter
        _oa, _or = ClaudeAdapter.is_available, ClaudeAdapter.run_headless_judge
        ClaudeAdapter.is_available = classmethod(lambda cls: True)
        ClaudeAdapter.run_headless_judge = lambda self, **kw: "1. **Note** — fine\n"
        p = mock.patch.object(review, "_detect_tamper_safe", lambda pp, t, b: ["dirty"])
        p.start()
        try:
            with _chdir(self.project):
                with contextlib.suppress(SystemExit):
                    review.cmd_panel_review(["042", "--models", "claude:opus"])
        finally:
            p.stop()
            ClaudeAdapter.is_available, ClaudeAdapter.run_headless_judge = _oa, _or
        self.assertEqual(
            [r for r in _read_journal(self.agent) if r["hook"] == "review"], [],
            "a tampered panel must record no spend (write must not precede the banner)")


if __name__ == "__main__":
    unittest.main()
