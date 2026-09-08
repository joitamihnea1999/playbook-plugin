#!/usr/bin/env python3
"""`tasks dashboard` + the bootstrap panel-health block (task 053).

Invariants:
  * changes no setting — rendering changes no existing byte under the project;
    its ONE write is `.agent/model-catalog.json` (the catalog baseline), only
    with a provider listing and only when the catalog changed (task 054);
  * per-seat stats come from the review-spend journal over the health window —
    the last 14 days, cut at the most recent panel change (models.json mtime) —
    tolerant of every malformed line (skipped and counted, never raised on,
    never imputed);
  * exactly three trigger kinds, each with an exact command; drift needs a
    baseline (no verdict without one); model-gap fires on a DEAD PIN (a seated
    codex/grok id the catalog no longer lists) or an id NEW since the recorded
    catalog — "available but not seated" is information, never a trigger —
    comparing bare ids so an alias-resolved seat is not a gap; template compares
    the live sha with the newest LIVE exam's recorded sha (fake runs are never
    exams);
  * the bootstrap block is exactly six lines with data and without.

Run: python3 tests/test_dashboard.py
"""
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "plugins/playbook"))
from tasks import dashboard as db  # noqa: E402

NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)


def _rec(days_ago: float, seat: str, status: str = "ok", duration_ms=120_000, **extra) -> str:
    ts = (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    obj = {"ts": ts, "session_id": "pid-1", "hook": "review", "decision": "record",
           "reason": "review spend", "kind": "panel", "seat": seat, "task": "042",
           "round": 1, "status": status, "usage": {"status": "unknown"}}
    if duration_ms is not None:
        obj["duration_ms"] = duration_ms
    obj.update(extra)
    return json.dumps(obj)


def _project(tmp: Path, *, journal_lines=None, models=True, bench=False) -> Path:
    (tmp / ".agent" / "tasks").mkdir(parents=True)
    (tmp / ".agent" / "config.json").write_text(json.dumps({
        "verify": "python3 -m unittest",
        "panel_required_for": ["assertive", "irreversible"],
        "review_soft_timeout_secs": 900, "review_timeout_secs": 1200,
        "judge_budget_usd": 7,
    }), encoding="utf-8")
    if models:
        (tmp / ".agent" / "models.json").write_text(json.dumps({
            "panel": ["opus", "codex:gpt-5.6-sol:high", "grok:grok-4.6:medium"],
            "default_judge": "codex:gpt-5.6-sol:high",
        }), encoding="utf-8")
        # the health window starts at the most recent panel change (models.json
        # mtime, task 054): pin it 60 days before NOW so the default window is the
        # plain 14 days; a test that wants a bounded window re-touches the file.
        _touch(tmp / ".agent" / "models.json", NOW - timedelta(days=60))
    if journal_lines is not None:
        (tmp / ".agent" / "journal").mkdir()
        (tmp / ".agent" / "journal" / "enforcement.jsonl").write_text(
            "\n".join(journal_lines) + "\n", encoding="utf-8")
    if bench:
        _bench(tmp / "bench")
    return tmp


def _bench(b: Path, *, template_text="<!-- judgebench template v1 -->\nprompt\n", live_sha=None):
    (b / "corpus").mkdir(parents=True)
    (b / "corpus" / "corpus.json").write_text(json.dumps({"version": 3, "cases": ["c1", "c2"]}), encoding="utf-8")
    (b / "lib" / "templates").mkdir(parents=True)
    # write BYTES: on Windows write_text would turn "\n" into "\r\n" and the file's sha
    # (what the code and the real manifests hash) would differ from the string's —
    # the exact CRLF fixture bug the Windows CI lane caught on commit bd4e04d.
    (b / "lib" / "templates" / "judge_prompt.md").write_bytes(template_text.encode("utf-8"))
    sha = live_sha or hashlib.sha256(template_text.encode("utf-8")).hexdigest()
    for run_id, mode, created, labels in (("testA", "live", "2026-09-07T16:53:13Z", ("sol-med", "sol-high")),
                                          ("smoke", "fake", "2026-09-08T09:53:21Z", ("sol-med", "sol-high")),   # NEWER than testA: only the live filter keeps it out
                                          ("old", "live", "2026-08-01T00:00:00Z", ("sol-med", "sol-high")),
                                          ("solo", "live", "2026-09-08T10:00:00Z", ("sol-med",))):   # live but single-seat: not a pair, not an exam
        r = b / "runs" / run_id
        r.mkdir(parents=True)
        (r / "manifest.json").write_text(json.dumps({
            "run_id": run_id, "mode": mode, "created_at": created,
            "candidates": [{"backend": "codex", "label": lab,
                            "spec": "codex:gpt-5.6-sol:" + {"med": "medium", "high": "high"}[lab.split("-")[1]]} for lab in labels],
            "template": {"version": "v1", "sha256": sha if run_id != "old" else "0" * 64},
            "spec_mode": "full", "concurrency": 2, "timeouts": {"soft_secs": 900, "hard_secs": 1200},
            "source_repos": {"HowFar": {"head": "abc", "path": "/w/HowFar"}},
        }), encoding="utf-8")
        (r / "report.md").write_text(
            f"# judgebench report — run `{run_id}`\n\n"
            "| candidate | inv | weighted | p50 |\n|---|---|---|---|\n"
            "| sol-high | 19 | 133 | 495s |\n| sol-med | 19 | 98 | 249s |\n\n"
            "## Per-case matrix\n\n| case | sol-high | sol-med |\n|---|---|---|\n| c1 | ok | ok |\n\n"
            "## §25 decision (harness plan §25)\n\nRule: the costlier configuration (sol-high) wins only if its "
            "weighted findings exceed the cheaper's. **Output tokens are `unknown` for every invocation — so the rule "
            "falls back to parity, and this report says so.** On parity: **sol-high exceeds sol-med on the fallback "
            "measure, so the costlier configuration wins Test A** — caveats.\n\n## Contamination disclosure\n\n"
            "**No** quoting found; nobody wins here.\n", encoding="utf-8")


def _touch(path: Path, when: datetime) -> None:
    os.utime(path, (when.timestamp(), when.timestamp()))


def _tree_digest(root: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


class JournalParsing(unittest.TestCase):
    def test_tolerant_load_counts_skips_and_keeps_review_records_only(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "j.jsonl"
            p.write_text("\n".join([
                _rec(1, "codex:gpt-5.6-sol:high"),
                "not json at all",
                json.dumps({"ts": "2026-09-08T00:00:00Z", "hook": "task-gate", "decision": "allow"}),
                json.dumps({"ts": "garbage", "hook": "review", "seat": "x", "status": "ok"}),
                _rec(2, "grok:grok-4.6:medium", "timeout", duration_ms=-5),
                _rec(3, "grok:grok-4.6:medium", "ok", duration_ms=None),
                "[]",
                "",
            ]) + "\n", encoding="utf-8")
            recs, skipped = db.load_review_records(p)
        self.assertEqual([r["seat"] for r in recs], ["codex:gpt-5.6-sol:high", "grok:grok-4.6:medium", "grok:grok-4.6:medium"])
        self.assertEqual(skipped, 2)                       # bad json + bad ts; the task-gate line is simply not review
        self.assertIsNone(recs[1]["duration_ms"])          # negative → unknown, never imputed
        self.assertIsNone(recs[2]["duration_ms"])          # absent → unknown

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO test is POSIX-only")
    def test_fifo_journal_does_not_hang(self):
        # plan-panel grok#3: the writers skip FIFOs; the reader must too (a blocking
        # open here would hang every bootstrap). O_NONBLOCK open + fstat S_ISREG.
        d = Path(tempfile.mkdtemp())
        fifo = d / "enforcement.jsonl"
        os.mkfifo(fifo)
        # the discriminating assertion: a non-regular file is REFUSED (None), not
        # merely read-as-empty (a non-blocking FIFO with no writer reads EOF too)
        self.assertIsNone(db._read_regular_text(fifo))
        self.assertEqual(db.load_review_records(fifo), ([], 0))
        link = d / "link.jsonl"
        os.symlink(d / "nowhere.jsonl", link)
        self.assertEqual(db.load_review_records(link), ([], 0))
        self.assertIsNone(db._read_regular_text(d))                 # a directory

    def test_other_lane_journals_are_named_not_aggregated(self):
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td), journal_lines=[_rec(1, "codex:gpt-5.6-sol:high")])
            (p / ".agent" / "alice" / "tasks").mkdir(parents=True)
            (p / ".agent" / "alice" / "journal").mkdir()
            (p / ".agent" / "alice" / "journal" / "enforcement.jsonl").write_text(
                _rec(1, "grok:grok-4.6:medium") + "\n", encoding="utf-8")
            (p / ".agent" / "current_user").write_text("alice\n", encoding="utf-8")
            jp = db.journal_path(p)
            self.assertEqual(jp, p / ".agent" / "alice" / "journal" / "enforcement.jsonl")
            self.assertEqual(db.other_lane_journals(p, jp), ["(root)"])
            out = db.render_dashboard(p, now=NOW, detect=False)
        self.assertIn("other lanes with journals (NOT aggregated): (root)", out)
        self.assertIn("1 review records total", out)          # alice's one, not root's

    def test_unknown_status_is_malformed_not_a_run(self):
        # impl-panel r2 codex-med#4: the contract allows ok|fail|timeout|dnf only
        lines = [_rec(1, "s", "ok"), _rec(1, "s", "bogus"), _rec(1, "s", "dnf")]
        recs, skipped = db.load_review_records(self._write(lines))
        self.assertEqual(len(recs), 2)
        self.assertEqual(skipped, 1)

    def test_deeply_nested_json_line_is_malformed_not_a_crash(self):
        # r3 codex#3: json.loads raises RecursionError on a valid but deeply nested line
        lines = [_rec(1, "s", "ok"), "[" * 100000 + "]" * 100000]
        recs, skipped = db.load_review_records(self._write(lines))
        self.assertEqual((len(recs), skipped), (1, 1))

    def test_missing_journal_is_empty_not_error(self):
        self.assertEqual(db.load_review_records(Path("/nonexistent/x.jsonl")), ([], 0))
        self.assertEqual(db.load_review_records(None), ([], 0))

    def test_tagged_status_missing_and_tail_truncation(self):
        # impl-panel codex#4: "no spend" must not look like "could not read", and a
        # journal over the cap must yield its NEWEST tail, not its oldest head.
        recs, skipped, status = db.load_review_journal(Path(tempfile.mkdtemp()) / "none.jsonl")
        self.assertEqual((recs, skipped, status), ([], 0, "missing"))
        self.assertEqual(db.load_review_journal(None)[2], "unresolved")
        old = [_rec(40 + d, "old-seat") for d in range(30)]
        new = [_rec(1, "new-seat"), _rec(2, "new-seat")]
        p = self._write(old + new)
        cap = len(("\n".join(new) + "\n").encode("utf-8")) + 40     # room for the two newest + a partial line
        recs, skipped, status = db.load_review_journal(p, cap=cap)
        self.assertEqual(status, "truncated")
        self.assertEqual({r["seat"] for r in recs}, {"new-seat"})
        self.assertEqual(len(recs), 2)
        self.assertEqual(skipped, 0)                                   # the partial first line is dropped, not counted as malformed

    def test_seat_stats_window_and_median(self):
        lines = [_rec(1, "s", "ok", 100_000), _rec(2, "s", "timeout", 300_000), _rec(5, "s", "ok", 200_000),
                 _rec(20, "s", "ok", 900_000)]          # outside the 14d window
        recs, _ = db.load_review_records(self._write(lines))
        st = db.window_stats(recs, NOW)["s"]
        self.assertEqual((st["runs"], st["ok"], st["timeout"], st["median_ms"]), (3, 2, 1, 200_000))

    def _write(self, lines):
        d = tempfile.mkdtemp()
        p = Path(d) / "j.jsonl"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p


class Formatting(unittest.TestCase):
    def test_fmt_and_pct(self):
        self.assertEqual(db.fmt_ms(None), "n/a")
        self.assertEqual(db.fmt_ms(12_400), "12s")
        self.assertEqual(db.fmt_ms(194_587), "3m15s")
        self.assertEqual(db.pct(1, 0), "n/a")
        self.assertEqual(db.pct(1, 3), "33%")

    def test_seat_label_matches_review_runner_rule(self):
        self.assertEqual(db.seat_label("claude", "claude-opus-4-8[1m]"), "claude:claude-opus-4-8[1m]:high")
        self.assertEqual(db.seat_label("codex", "gpt-5.6-sol:high"), "codex:gpt-5.6-sol:high")
        self.assertEqual(db.seat_label("grok", None), "grok")
        # r3 opus#2: parity PROVEN against the runner's own function, not restated
        from tasks.review import _seat_with_effort
        for prov, var in (("claude", "claude-opus-4-8[1m]"), ("claude", None), ("codex", "gpt-5.6-sol:high"),
                          ("codex", "gpt-5.6-terra"), ("grok", "grok-4.6:medium"), ("grok", None), ("agy", None)):
            self.assertEqual(db.seat_label(prov, var), _seat_with_effort(prov, var), (prov, var))


class PanelResolution(unittest.TestCase):
    def test_alias_and_effort_split(self):
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td))
            panel = db.panel_seats(p)
        by = {s["spec"]: s for s in panel["seats"]}
        self.assertEqual(by["opus"]["provider"], "claude")
        self.assertEqual(by["opus"]["effort"], "high")
        self.assertEqual(by["opus"]["label"], "claude:claude-opus-4-8[1m]:high")
        self.assertEqual((by["codex:gpt-5.6-sol:high"]["model"], by["codex:gpt-5.6-sol:high"]["effort"]), ("gpt-5.6-sol", "high"))
        self.assertEqual((by["grok:grok-4.6:medium"]["model"], by["grok:grok-4.6:medium"]["effort"]), ("grok-4.6", "medium"))
        self.assertEqual(panel["default_judge"], "codex:gpt-5.6-sol:high")
        self.assertEqual(panel["required_for"], ["assertive", "irreversible"])

    def test_unknown_spec_is_a_row_not_a_crash(self):
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td), models=False)
            (p / ".agent" / "models.json").write_text(json.dumps({"panel": ["nope:x", "opus"]}), encoding="utf-8")
            panel = db.panel_seats(p)
        self.assertEqual(panel["seats"][0]["provider"], "?")
        self.assertTrue(panel["seats"][0]["error"])


class DriftTrigger(unittest.TestCase):
    def _panel(self):
        return {"panel": ["opus", "codex:gpt-5.6-sol:high", "grok:grok-4.6:medium"],
                "default_judge": "codex:gpt-5.6-sol:high",
                "seats": [{"spec": "opus", "label": "claude:claude-opus-4-8[1m]:high"},
                          {"spec": "codex:gpt-5.6-sol:high", "label": "codex:gpt-5.6-sol:high"},
                          {"spec": "grok:grok-4.6:medium", "label": "grok:grok-4.6:medium"}]}

    def test_timeout_rate_doubling_fires_with_exact_drop_command(self):
        g = "grok:grok-4.6:medium"
        lines = [_rec(d, g, "timeout" if d in (1, 2) else "ok") for d in (1, 2, 3, 4)]       # 50% now
        lines += [_rec(20 + d, g, "timeout" if d == 0 else "ok") for d in range(8)]         # 12.5% before
        recs, _ = db.load_review_records(self._w(lines))
        trig = db.drift_triggers(recs, NOW, self._panel())
        self.assertEqual(len(trig), 1)
        self.assertEqual(trig[0]["kind"], "drift")
        self.assertIn("timeout rate 50% vs 12% in the prior 30d", trig[0]["detail"])
        self.assertEqual(trig[0]["command"],
                         "tasks models set --panel opus,codex:gpt-5.6-sol:high --default-judge codex:gpt-5.6-sol:high")

    def test_median_doubling_fires_and_dropping_the_default_judge_stays_paste_ready(self):
        s = "codex:gpt-5.6-sol:high"
        lines = [_rec(d, s, "ok", 600_000) for d in (1, 2, 3)] + [_rec(20 + d, s, "ok", 200_000) for d in range(3)]
        recs, _ = db.load_review_records(self._w(lines))
        trig = db.drift_triggers(recs, NOW, self._panel())
        self.assertEqual(len(trig), 1)
        self.assertIn("median 10m00s vs 3m20s", trig[0]["detail"])
        # plan-panel grok#2: never the interactive `models select`; propose the first remaining seat as default
        self.assertNotIn("models select", trig[0]["command"])
        self.assertTrue(trig[0]["command"].startswith(
            "tasks models set --panel opus,grok:grok-4.6:medium --default-judge opus"), trig[0]["command"])
        self.assertIn("was the default judge", trig[0]["command"])

    def test_default_judge_matched_by_label_not_raw_spec(self):
        # r4 codex#2 / grok#1: default judge as canonical spec, seat pinned by alias
        panel = {"panel": ["opus", "codex:gpt-5.6-sol:high"], "default_judge": "claude:claude-opus-4-8[1m]",
                 "seats": [{"spec": "opus", "label": "claude:claude-opus-4-8[1m]:high"},
                           {"spec": "codex:gpt-5.6-sol:high", "label": "codex:gpt-5.6-sol:high"}]}
        cmd = db._models_set_command(panel, drop_label="claude:claude-opus-4-8[1m]:high")
        self.assertTrue(cmd.startswith("tasks models set --panel codex:gpt-5.6-sol:high --default-judge codex:gpt-5.6-sol:high"), cmd)

    def test_sole_seat_drift_is_not_an_alarm(self):
        # r4 codex#4: dropping the only seat has no executable remedy → no alarm
        s = "codex:gpt-5.6-sol:high"
        lines = [_rec(d, s, "timeout") for d in (1, 2, 3)] + [_rec(20 + d, s, "ok") for d in range(3)]
        recs, _ = db.load_review_records(self._w(lines))
        panel = {"panel": [s], "default_judge": s, "seats": [{"spec": s, "label": s}]}
        self.assertEqual(db.drift_triggers(recs, NOW, panel), [])

    def test_no_baseline_means_no_verdict(self):
        s = "grok:grok-4.6:medium"
        lines = [_rec(d, s, "timeout") for d in (1, 2, 3)]           # 100% timeouts, but no prior-30d runs
        recs, _ = db.load_review_records(self._w(lines))
        self.assertEqual(db.drift_triggers(recs, NOW, self._panel()), [])
        lines += [_rec(20, s, "ok"), _rec(21, s, "ok")]              # only 2 baseline runs (< MIN_RUNS_FOR_DRIFT)
        recs, _ = db.load_review_records(self._w(lines))
        self.assertEqual(db.drift_triggers(recs, NOW, self._panel()), [])

    def test_stable_seat_does_not_fire(self):
        s = "grok:grok-4.6:medium"
        lines = [_rec(d, s, "ok", 300_000) for d in (1, 2, 3)] + [_rec(20 + d, s, "ok", 280_000) for d in range(3)]
        recs, _ = db.load_review_records(self._w(lines))
        self.assertEqual(db.drift_triggers(recs, NOW, self._panel()), [])

    def test_seat_outside_panel_is_not_an_alarm(self):
        # impl-panel codex#3: every alarm carries a real command; an unseated seat has
        # nothing to drop, so it is a "(not in panel)" row, not a trigger.
        s = "codex:gpt-5.6-terra:medium"
        lines = [_rec(d, s, "timeout") for d in (1, 2, 3)] + [_rec(20 + d, s, "ok") for d in range(3)]
        recs, _ = db.load_review_records(self._w(lines))
        self.assertEqual(db.drift_triggers(recs, NOW, self._panel()), [])

    def test_commands_are_shell_quoted(self):
        panel = {"panel": ["opus", "codex:gpt-5.6-sol:high"], "default_judge": "opus",
                 "seats": [{"spec": "opus", "provider": "claude", "model": "claude-opus-4-8[1m]", "label": "claude:claude-opus-4-8[1m]:high"},
                           {"spec": "codex:gpt-5.6-sol:high", "provider": "codex", "model": "gpt-5.6-sol", "label": "codex:gpt-5.6-sol:high"}]}
        report = {"providers": [{"name": "codex", "installed": True,
                                 "models": [{"id": "gpt-5.6-sol", "efforts": ["high"]}, {"id": "x$(rm -rf /)", "efforts": ["high"]}]}]}
        # task 054: the hostile id is NEW since the baseline (sol seated + listed → no dead pin)
        trig = db.model_gap_triggers(report, panel, {"recorded_at": "2026-09-01T00:00:00Z", "providers": {"codex": ["gpt-5.6-sol"]}})
        self.assertEqual(len(trig), 1)
        # the whole --panel value is single-quoted, so `$(` is inert when pasted
        self.assertIn("--panel 'opus,codex:gpt-5.6-sol:high,codex:x$(rm -rf /):high' --default-judge opus", trig[0]["command"])
        import shlex
        argv = shlex.split(trig[0]["command"].split("   #")[0])
        self.assertEqual(argv[argv.index("--panel") + 1], "opus,codex:gpt-5.6-sol:high,codex:x$(rm -rf /):high")

    def _w(self, lines):
        p = Path(tempfile.mkdtemp()) / "j.jsonl"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p


class ModelGapTrigger(unittest.TestCase):
    PANEL = {"panel": ["opus", "codex:gpt-5.6-sol:high"], "default_judge": "opus",
             "seats": [{"spec": "opus", "provider": "claude", "model": "claude-opus-4-8[1m]", "label": "claude:claude-opus-4-8[1m]:high", "error": ""},
                       {"spec": "codex:gpt-5.6-sol:high", "provider": "codex", "model": "gpt-5.6-sol", "label": "codex:gpt-5.6-sol:high", "error": ""}]}
    REPORT = {"providers": [
        {"name": "claude", "installed": True, "models": [{"id": "claude-opus-4-8[1m]"}, {"id": "claude-opus-4-8"}, {"id": "claude-sonnet-5"}]},
        {"name": "codex", "installed": True, "models": [{"id": "gpt-5.6-sol", "efforts": ["medium", "high"]},
                                                          {"id": "gpt-5.6-terra", "efforts": ["low", "high"]},
                                                          {"id": "gpt-5.6-luna", "efforts": ["medium", "high"]}]},
        {"name": "grok", "installed": False, "models": [{"id": "grok-4.6"}]},
        {"name": "agy", "installed": True, "models": [{"id": "gemini-x"}]},
    ]}

    def test_available_but_unseated_is_information_not_a_trigger(self):
        # task 054: a deliberately selective panel is not an alarm — the catalog
        # matches the baseline and every seat is listed, so NOTHING fires…
        baseline = {"recorded_at": "2026-09-01T00:00:00Z", "providers": db.catalog_from_report(self.REPORT)}
        self.assertEqual(db.model_gap_triggers(self.REPORT, self.PANEL, baseline), [])
        # …and the unseated ids are one informational line per provider (bare-id dedupe: opus[1m]≡opus; agy out of scope)
        info = db.model_gap_info(self.REPORT, self.PANEL)
        self.assertEqual(info, ["claude: claude-sonnet-5", "codex: gpt-5.6-terra, gpt-5.6-luna"])

    def test_catalog_from_report_is_bare_sorted_and_in_scope(self):
        cat = db.catalog_from_report(self.REPORT)
        self.assertEqual(cat, {"claude": ["claude-opus-4-8", "claude-sonnet-5"],
                               "codex": ["gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra"]})   # grok not installed → absent; agy never
        self.assertEqual(db.catalog_from_report(None), {})

    def test_dead_pin_fires_with_the_exact_drop_command(self):
        report = json.loads(json.dumps(self.REPORT))
        report["providers"][1]["models"] = [{"id": "gpt-5.6-terra", "efforts": ["low", "high"]}]   # sol vanished from codex's catalog
        baseline = {"recorded_at": "2026-09-01T00:00:00Z", "providers": db.catalog_from_report(report)}
        trig = db.model_gap_triggers(report, self.PANEL, baseline)
        self.assertEqual([(t["kind"], t["seat"]) for t in trig], [("model-gap", "codex:gpt-5.6-sol:high")])
        self.assertTrue(trig[0]["detail"].startswith("dead pin: gpt-5.6-sol is no longer listed by codex"), trig[0]["detail"])
        self.assertIn("tasks models check", trig[0]["detail"])           # listed ≠ probed — the check confirms
        self.assertEqual(trig[0]["command"], "tasks models set --panel opus --default-judge opus")

    def test_claude_listing_is_configured_not_a_catalog_so_never_a_dead_pin(self):
        report = json.loads(json.dumps(self.REPORT))
        report["providers"][0]["models"] = [{"id": "claude-sonnet-5"}]     # settings.json no longer names opus
        baseline = {"recorded_at": "2026-09-01T00:00:00Z", "providers": db.catalog_from_report(report)}
        self.assertEqual(db.model_gap_triggers(report, self.PANEL, baseline), [])

    def test_uninstalled_or_empty_listing_is_unknown_not_dead(self):
        panel = json.loads(json.dumps(self.PANEL))
        panel["panel"].append("grok:grok-4.6:medium")
        panel["seats"].append({"spec": "grok:grok-4.6:medium", "provider": "grok", "model": "grok-4.6", "label": "grok:grok-4.6:medium", "error": ""})
        report = json.loads(json.dumps(self.REPORT))
        report["providers"][1]["models"] = []                              # codex installed but listed nothing (cache missing)
        baseline = {"recorded_at": "2026-09-01T00:00:00Z", "providers": db.catalog_from_report(report)}
        self.assertEqual(db.model_gap_triggers(report, panel, baseline), [])   # grok not installed, codex empty → no verdict

    def test_new_id_since_the_baseline_fires_with_the_add_command(self):
        baseline = {"recorded_at": "2026-09-01T00:00:00Z",
                    "providers": {"claude": ["claude-opus-4-8", "claude-sonnet-5"], "codex": ["gpt-5.6-sol", "gpt-5.6-terra"]}}
        trig = db.model_gap_triggers(self.REPORT, self.PANEL, baseline)
        self.assertEqual([(t["kind"], t["seat"]) for t in trig], [("model-gap", "codex")])
        self.assertTrue(trig[0]["detail"].startswith("new since 2026-09-01: gpt-5.6-luna"), trig[0]["detail"])
        self.assertIn("listed ≠ probed", trig[0]["detail"])
        # effort from the model's OWN list; shlex-quoted; the existing panel kept
        self.assertEqual(trig[0]["command"], "tasks models set --panel opus,codex:gpt-5.6-sol:high,codex:gpt-5.6-luna:medium --default-judge opus")

    def test_new_id_already_seated_and_a_newly_installed_provider(self):
        baseline = {"recorded_at": "2026-09-01T00:00:00Z", "providers": {"claude": ["claude-opus-4-8", "claude-sonnet-5"]}}   # codex was not installed then
        trig = db.model_gap_triggers(self.REPORT, self.PANEL, baseline)
        self.assertEqual(len(trig), 1)
        self.assertEqual(trig[0]["seat"], "codex")
        # sol is new to the baseline but already seated → not in the list; terra/luna are
        self.assertTrue(trig[0]["detail"].startswith("new since 2026-09-01: gpt-5.6-terra, gpt-5.6-luna"), trig[0]["detail"])
        self.assertIn("codex:gpt-5.6-terra:low", trig[0]["command"])   # terra offers no "medium" → its first

    def test_no_baseline_means_no_new_id_verdict(self):
        # first run: nothing is "new since last recorded" — only a dead pin can fire
        self.assertEqual(db.model_gap_triggers(self.REPORT, self.PANEL, None), [])
        self.assertEqual(db.model_gap_triggers(None, self.PANEL, None), [])
        self.assertEqual(db.model_gap_info(None, self.PANEL), [])

    def test_baseline_roundtrip_records_once_and_skips_an_unchanged_write(self):
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td))
            path = db.catalog_baseline_path(p)
            self.assertEqual(path, p / ".agent" / "model-catalog.json")
            self.assertIsNone(db.load_catalog_baseline(path))
            note = db.record_catalog_baseline(p, self.REPORT, NOW)
            self.assertIn("recorded now", note)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["recorded_at"], "2026-09-08T12:00:00Z")
            self.assertEqual(data["providers"], db.catalog_from_report(self.REPORT))
            before = path.read_bytes()
            note2 = db.record_catalog_baseline(p, self.REPORT, NOW + timedelta(days=1))
            self.assertIn("unchanged since 2026-09-08", note2)
            self.assertEqual(path.read_bytes(), before)                    # no churn on an unchanged catalog
            report = json.loads(json.dumps(self.REPORT))
            report["providers"][1]["models"].append({"id": "gpt-new", "efforts": ["medium"]})
            note3 = db.record_catalog_baseline(p, report, NOW + timedelta(days=2))
            self.assertIn("updated", note3)
            self.assertIn("gpt-new", db.load_catalog_baseline(path)["providers"]["codex"])
            # a malformed baseline is "no baseline", never a crash
            path.write_text("{not json", encoding="utf-8")
            self.assertIsNone(db.load_catalog_baseline(path))
            # a listing that failed / --no-detect records nothing
            self.assertIn("not recorded", db.record_catalog_baseline(p, None, NOW))

    def test_baseline_write_failure_is_a_note_not_a_crash(self):
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td))
            (p / ".agent" / "model-catalog.json").mkdir()                  # a directory where the file goes
            note = db.record_catalog_baseline(p, self.REPORT, NOW)
            self.assertIn("write failed", note)


class HealthWindow(unittest.TestCase):
    def test_window_starts_at_the_most_recent_panel_change(self):
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td))
            self.assertEqual(db.window_bounds(NOW, db.panel_changed_at(p))[1], "last 14 days")   # fixture mtime: 60d ago
            _touch(p / ".agent" / "models.json", NOW - timedelta(days=3))
            changed = db.panel_changed_at(p)
            self.assertEqual(changed, NOW - timedelta(days=3))
            start, label = db.window_bounds(NOW, changed)
            self.assertEqual(start, NOW - timedelta(days=3))
            self.assertIn("since panel change 2026-09-05", label)
            # a future mtime (clock skew) is capped at now, never an empty-window crash
            self.assertEqual(db.window_bounds(NOW, NOW + timedelta(days=1))[0], NOW)
        # plugin defaults (no models.json) → no bound
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td), models=False)
            self.assertIsNone(db.panel_changed_at(p))
            self.assertEqual(db.window_bounds(NOW, None), (NOW - timedelta(days=14), "last 14 days"))

    def test_stats_and_drift_honour_the_bounded_window(self):
        lines = [_rec(10, "codex:gpt-5.6-sol:high"), _rec(5, "codex:gpt-5.6-sol:high", "timeout"),
                 _rec(1, "codex:gpt-5.6-sol:high")]
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td), journal_lines=lines)
            _touch(p / ".agent" / "models.json", NOW - timedelta(days=3))
            out = db.render_dashboard(p, now=NOW, detect=False)
            self.assertIn("window: since panel change 2026-09-05", out)
            row = next(l for l in out.splitlines() if l.startswith("  codex:gpt-5.6-sol:high"))
            self.assertRegex(row, r"\s1\s+100%\s+0%")                    # only the 1d record counts; the 5d timeout is before the change
            block = db.panel_health_lines(p, now=NOW)
            self.assertEqual(len(block), 6)
            self.assertTrue(block[0].startswith("=== PANEL HEALTH (since panel change 2026-09-05"), block[0])
            self.assertIn("reviews: 1 judge runs", block[2])
        # drift compares the bounded window with the 30 days before ITS start
        panel = {"panel": ["codex:gpt-5.6-sol:high", "opus"], "default_judge": "opus",
                 "seats": [{"spec": "codex:gpt-5.6-sol:high", "provider": "codex", "model": "gpt-5.6-sol", "label": "codex:gpt-5.6-sol:high", "error": ""},
                           {"spec": "opus", "provider": "claude", "model": "claude-opus-4-8[1m]", "label": "claude:claude-opus-4-8[1m]:high", "error": ""}]}
        with tempfile.TemporaryDirectory() as td:
            j = Path(td) / "j.jsonl"
            j.write_text("\n".join([_rec(2, "codex:gpt-5.6-sol:high", "timeout"), _rec(1.5, "codex:gpt-5.6-sol:high", "timeout"), _rec(1, "codex:gpt-5.6-sol:high"),
                                    _rec(4, "codex:gpt-5.6-sol:high"), _rec(5, "codex:gpt-5.6-sol:high"), _rec(6, "codex:gpt-5.6-sol:high")]) + "\n", encoding="utf-8")
            recs, _, _ = db.load_review_journal(j)
        start = NOW - timedelta(days=3)
        trig = db.drift_triggers(recs, NOW, panel, start=start)
        self.assertEqual([t["seat"] for t in trig], ["codex:gpt-5.6-sol:high"])   # 2/3 timeouts now vs 0/3 in the 30d before the change
        self.assertEqual(db.drift_triggers(recs, NOW, panel, start=NOW - timedelta(days=14)), [])   # unbounded: 2/6 vs nothing before → no baseline


class Judgebench(unittest.TestCase):
    def test_summary_picks_newest_live_exam_and_parses_verdict(self):
        with tempfile.TemporaryDirectory() as td:
            b = Path(td) / "bench"
            _bench(b)
            s = db.judgebench_summary(b)
        self.assertEqual((s["corpus_version"], s["cases"]), (3, 2))
        self.assertEqual([e["run_id"] for e in s["exams"]], ["testA"])         # smoke (fake) excluded; old superseded
        e = s["exams"][0]
        self.assertEqual(e["labels"], ["sol-high", "sol-med"])
        self.assertEqual(e["weighted"], {"sol-high": 133, "sol-med": 98})
        self.assertEqual(e["verdict"], "sol-high exceeds sol-med on the fallback measure, so the costlier configuration wins Test A")
        self.assertEqual(db.template_triggers(s), [])                         # sha unchanged

    def test_template_change_fires_with_rerun_command(self):
        with tempfile.TemporaryDirectory() as td:
            b = Path(td) / "bench"
            _bench(b, live_sha="f" * 64)
            s = db.judgebench_summary(b)
            trig = db.template_triggers(s)
        self.assertEqual(len(trig), 1)
        self.assertEqual(trig[0]["kind"], "template")
        # impl-panel codex#2 / grok#3: label=spec in MANIFEST order — presets cover only sol-*/grok-*
        # compare as argv (r3 grok#1: a Windows temp path gets shlex-quoted, so a string compare is lane-dependent);
        # called on a bare bench dir (no project) → absolute posix path; via _exams_with_fallback it is project-relative
        import shlex
        argv = shlex.split(trig[0]["command"])
        self.assertEqual(argv[:4], ["python3", f"{b.as_posix()}/judgebench.py", "run", "--cases"])
        self.assertEqual(argv[argv.index("--candidates") + 1], "sol-med=codex:gpt-5.6-sol:medium,sol-high=codex:gpt-5.6-sol:high")
        self.assertEqual(argv[argv.index("--run-id") + 1], "testA-retest")
        # the manifest's run parameters travel with the exact command (r3 codex#1)
        self.assertEqual(argv[argv.index("--spec-mode") + 1], "full")
        self.assertEqual(argv[argv.index("--concurrency") + 1], "2")
        self.assertEqual(argv[argv.index("--soft-timeout") + 1], "900")
        self.assertEqual(argv[argv.index("--timeout") + 1], "1200")
        self.assertEqual(argv[argv.index("--source-repo") + 1], "HowFar=/w/HowFar")
        self.assertEqual(argv[-1], "--live")

    def test_record_copy_fills_in_when_bench_runs_is_gone(self):
        # plan-panel codex#4 / codex-med#2: bench/runs is gitignored; a report copied
        # into a task record (task 052's test-*-report.md) is the durable fallback.
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td))
            b = p / "bench"
            _bench(b)
            for run in (b / "runs").iterdir():
                for f in run.iterdir():
                    f.unlink()
                run.rmdir()
            (b / "runs").rmdir()
            tdir = p / ".agent" / "tasks" / "050-live-runs"
            tdir.mkdir()
            (tdir / "task.md").write_text("## Status\ndone\n", encoding="utf-8")
            (tdir / "test-b-report.md").write_text(
                "<!-- moved verbatim -->\n\n### Test B — report.md\n<!-- pin -->\n\n# judgebench report — run `testB`\n\n"
                "| candidate | inv | weighted | p50 |\n|---|---|---|---|\n| grok-high | 19 | 51 | 727s |\n| grok-med | 19 | 67 | 524s |\n\n"
                "## Run facts (task 050, W2–W3)\n\n- Live run 2026-09-07 12:46Z–16:52Z on this host.\n\n"
                "## §25 decision (harness plan §25)\n\nRule: the costlier configuration (grok-high) wins only if … **Output tokens "
                "are unknown … says so.** On parity: **grok-high does not exceed grok-med, so the costlier configuration does not "
                "win Test B.** Caveats.\n", encoding="utf-8")
            # a live SINGLE-seat smoke copied into a record is not a pair → not an exam
            (tdir / "smoke-copy.md").write_text(
                "# judgebench report — run `smoke2`\n\n| candidate | inv | weighted |\n|---|---|---|\n| sol-med | 1 | 3 |\n\n"
                "## §25 decision\n\n**sol-med wins nothing.**\n", encoding="utf-8")
            recs = db.record_exams(p)
            merged = db.merge_record_exams(db.judgebench_summary(db.find_bench_dir(p)), recs)
            out = db.render_dashboard(p, now=NOW, detect=False)
            block = db.panel_health_lines(p, now=NOW)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["labels"], ["grok-high", "grok-med"])
        self.assertEqual(recs[0]["weighted"], {"grok-high": 51, "grok-med": 67})
        self.assertEqual(recs[0]["verdict"], "grok-high does not exceed grok-med, so the costlier configuration does not win Test B.")
        self.assertEqual(db._date(recs[0]["date"]), "2026-09-07")
        self.assertEqual([e["run_id"] for e in merged["exams"]], ["testB"])
        self.assertEqual(db.template_triggers(merged), [])          # no manifest sha → cannot fire, says so
        self.assertIn("exam testB · 2026-09-07 · grok-high vs grok-med · template sha n/a (record has no manifest)", out)
        self.assertIn("source: record 050-live-runs/test-b-report.md", out)
        self.assertIn("template baseline n/a (record only)", block[4])

    def test_newest_record_wins_and_merged_list_is_date_sorted(self):
        # impl-panel sonnet#1 / codex#1 / grok#2: two record copies of one pair → the newest by date
        def rec(run_id, date):
            return {"run_id": run_id, "date": db.parse_ts(date + "T00:00:00Z"), "labels": ["a", "b"], "candidates": [],
                    "specs": [], "template_sha": "", "template_version": "", "verdict": run_id, "weighted": {}, "source": "record"}
        merged = db.merge_record_exams(None, [rec("jan", "2026-01-01"), rec("sep", "2026-09-01"), rec("mar", "2026-03-01")])
        self.assertEqual([e["run_id"] for e in merged["exams"]], ["sep"])
        other = {"run_id": "x", "date": db.parse_ts("2026-05-01T00:00:00Z"), "labels": ["c", "d"], "candidates": [],
                 "specs": [], "template_sha": "", "template_version": "", "verdict": "x", "weighted": {}, "source": "record"}
        merged = db.merge_record_exams(None, [rec("jan", "2026-01-01"), other])
        self.assertEqual([e["run_id"] for e in merged["exams"]], ["x", "jan"])   # newest first

    def test_manifests_keyed_by_spec_not_label(self):
        with tempfile.TemporaryDirectory() as td:
            b = Path(td) / "bench"
            _bench(b)
            # a NEWER exam of the SAME specs under renamed labels → same pair, it wins
            r = b / "runs" / "renamed"
            r.mkdir()
            (r / "manifest.json").write_text(json.dumps({
                "run_id": "renamed", "mode": "live", "created_at": "2026-09-09T00:00:00Z",
                "candidates": [{"backend": "codex", "label": "slow", "spec": "codex:gpt-5.6-sol:high"},
                               {"backend": "codex", "label": "fast", "spec": "codex:gpt-5.6-sol:medium"}],
                "template": {"version": "v1", "sha256": "e" * 64}}), encoding="utf-8")
            (r / "report.md").write_text("| candidate | inv | weighted |\n|---|---|---|\n| slow | 19 | 5 |\n| fast | 19 | 9 |\n\n"
                                        "## §25 decision\n\n**fast wins.**\n", encoding="utf-8")
            # an INTERRUPTED run — manifest written, no report — must not supersede anything (r3 codex#2)
            (b / "runs" / "crashed").mkdir()
            (b / "runs" / "crashed" / "manifest.json").write_text(json.dumps({
                "run_id": "crashed", "mode": "live", "created_at": "2026-09-10T00:00:00Z",
                "candidates": [{"label": "slow", "spec": "codex:gpt-5.6-sol:high"}, {"label": "fast", "spec": "codex:gpt-5.6-sol:medium"}],
                "template": {"version": "v1", "sha256": "0" * 64}}), encoding="utf-8")
            s = db.judgebench_summary(b)
            trig = db.template_triggers(s)
        self.assertEqual([e["run_id"] for e in s["exams"]], ["renamed"])
        self.assertEqual(s["exams"][0]["labels"], ["fast", "slow"])
        import shlex
        argv = shlex.split(trig[0]["command"])
        self.assertEqual(argv[argv.index("--candidates") + 1], "slow=codex:gpt-5.6-sol:high,fast=codex:gpt-5.6-sol:medium")
        self.assertEqual(argv[argv.index("--run-id") + 1], "renamed-retest")

    def test_three_seat_run_is_not_a_pair(self):
        with tempfile.TemporaryDirectory() as td:
            b = Path(td) / "bench"
            _bench(b)
            r = b / "runs" / "trio"
            r.mkdir()
            (r / "manifest.json").write_text(json.dumps({
                "run_id": "trio", "mode": "live", "created_at": "2026-09-09T00:00:00Z",
                "candidates": [{"label": "a", "spec": "codex:gpt-5.6-sol:high"}, {"label": "b", "spec": "codex:gpt-5.6-sol:medium"},
                               {"label": "c", "spec": "grok:grok-4.6:medium"}],
                "template": {"version": "v1", "sha256": "e" * 64}}), encoding="utf-8")
            (b / "runs" / "bad").mkdir()
            (b / "runs" / "bad" / "manifest.json").write_text(json.dumps({"run_id": "bad", "mode": "live", "candidates": {"x": 1}}), encoding="utf-8")
            s = db.judgebench_summary(b)
        self.assertEqual([e["run_id"] for e in s["exams"]], ["testA"])

    def test_junk_third_candidate_is_not_a_pair(self):
        # r3 grok#2: the gate counts the RAW list, not the dicts left after dropping junk
        with tempfile.TemporaryDirectory() as td:
            b = Path(td) / "bench"
            _bench(b)
            mf = b / "runs" / "testA" / "manifest.json"
            m = json.loads(mf.read_text(encoding="utf-8"))
            m["candidates"].append("junk")
            mf.write_text(json.dumps(m), encoding="utf-8")
            s = db.judgebench_summary(b)
        self.assertEqual([e["run_id"] for e in s["exams"]], ["old"])      # testA dropped; the older valid exam of that pair stands

    def test_verdict_is_the_last_win_sentence_not_a_decoy(self):
        # r3 opus#3: an earlier bold that mentions winning but is not the conclusion
        text = ("## §25 decision\n\nRule: the costlier configuration wins only if x. **Whoever wins here wins on parity, "
                "not tokens.** Data. **sol-high exceeds sol-med, so the costlier configuration wins Test A.**\n\n## Next\n\n**a wins**\n")
        self.assertEqual(db.parse_report_verdict(text)["verdict"],
                         "sol-high exceeds sol-med, so the costlier configuration wins Test A.")

    def test_weighted_table_is_scoped_and_header_case_insensitive(self):
        # r4 opus#1: a later numeric table must not pollute the pair; header may be capitalised
        text = ("| Candidate | inv | Weighted |\n|---|---|---|\n| a | 19 | 5 |\n| b | 19 | 9 |\n\n"
                "| other | n | weighted |\n|---|---|---|\n| c | 1 | 7 |\n\n## §25 decision\n\n**b wins.**\n")
        v = db.parse_report_verdict(text)
        self.assertEqual(v["weighted"], {"a": 5, "b": 9})
        self.assertEqual(v["verdict"], "b wins.")

    def test_bolded_trailing_caveat_does_not_displace_the_verdict(self):
        # r4 opus#2: the conclusion names a candidate; a trailing bold caveat does not
        text = ("| candidate | inv | weighted |\n|---|---|---|\n| sol-high | 19 | 133 |\n| sol-med | 19 | 98 |\n\n"
                "## §25 decision\n\n**sol-high wins Test A.** **Whoever wins, nobody loses here.**\n")
        self.assertEqual(db.parse_report_verdict(text)["verdict"], "sol-high wins Test A.")

    def test_corpus_schema_is_validated(self):
        # r4 codex#5: `"cases": 7` is valid JSON but not a case list
        with tempfile.TemporaryDirectory() as td:
            b = Path(td) / "bench"
            _bench(b)
            (b / "corpus" / "corpus.json").write_text(json.dumps({"version": 3, "cases": 7}), encoding="utf-8")
            s = db.judgebench_summary(b)
        self.assertEqual((s["corpus_version"], s["cases"]), (3, 0))

    def test_rerun_uses_the_manifest_case_set(self):
        # r4 codex#3 / codex-med#1: a subset exam re-runs the same subset, not `all`
        with tempfile.TemporaryDirectory() as td:
            b = Path(td) / "bench"
            _bench(b, live_sha="f" * 64)
            mf = b / "runs" / "testA" / "manifest.json"
            m = json.loads(mf.read_text(encoding="utf-8"))
            m["corpus"] = {"cases": ["c1", "c2"], "hashes": {}}
            mf.write_text(json.dumps(m), encoding="utf-8")
            trig = db.template_triggers(db.judgebench_summary(b))
        import shlex
        argv = shlex.split(trig[0]["command"])
        self.assertEqual(argv[argv.index("--cases") + 1], "c1,c2")

    def test_retest_run_id_is_valid_and_collision_free(self):
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td)
            (runs / "testA-retest").mkdir()
            self.assertEqual(db._retest_run_id("testA", runs), "testA-retest2")
            long_id = "x" * 64
            rid = db._retest_run_id(long_id, runs)
            self.assertLessEqual(len(rid), 64)
            self.assertTrue(rid.endswith("-retest"))
            self.assertEqual(db._retest_run_id("a b/c", runs), "a-b-c-retest")

    def test_template_command_is_quoted_and_uses_the_found_bench_path(self):
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td))
            (p / ".agent" / "config.json").write_text(json.dumps({"code_roots": ["nested"]}), encoding="utf-8")
            b = p / "nested" / "bench"
            _bench(b, live_sha="f" * 64)
            mf = b / "runs" / "testA" / "manifest.json"
            m = json.loads(mf.read_text(encoding="utf-8"))
            m["run_id"] = "t$(x)"
            m["candidates"][0]["label"] = "a;b"
            mf.write_text(json.dumps(m), encoding="utf-8")
            trig = db.template_triggers(db._exams_with_fallback(p))
        import shlex
        self.assertEqual(len(trig), 1)
        argv = shlex.split(trig[0]["command"])
        self.assertEqual(argv[1], "nested/bench/judgebench.py")      # project-relative via _exams_with_fallback
        self.assertEqual(argv[argv.index("--run-id") + 1], "t--x-retest")    # sanitised to the harness's id grammar
        self.assertEqual(argv[argv.index("--candidates") + 1], "a;b=codex:gpt-5.6-sol:medium,sol-high=codex:gpt-5.6-sol:high")
        self.assertNotIn("$(x) ", trig[0]["command"])

    def test_record_fills_only_pairs_without_a_manifest(self):
        # impl-panel r2 codex#2 / grok#2: one surviving manifest must not hide other pairs
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td), bench=True)               # manifest exam: sol-high vs sol-med
            tdir = p / ".agent" / "tasks" / "050-x"
            tdir.mkdir()
            (tdir / "task.md").write_text("## Status\ndone\n", encoding="utf-8")
            (tdir / "copy.md").write_text(
                "# judgebench report — run `testB`\n\n| candidate | inv | weighted |\n|---|---|---|\n| grok-high | 19 | 51 |\n| grok-med | 19 | 67 |\n\n"
                "## Run facts\n\n- Live run 2026-09-07.\n\n## §25 decision\n\n**grok-high does not win Test B.**\n\n"
                "# judgebench report — run `testA-copy`\n\n| candidate | inv | weighted |\n|---|---|---|\n| sol-high | 19 | 1 |\n| sol-med | 19 | 1 |\n\n"
                "## §25 decision\n\n**sol-med wins.**\n", encoding="utf-8")
            bench = db._exams_with_fallback(p)
        self.assertEqual([(e["run_id"], e["source"][:6]) for e in bench["exams"]], [("testA", "bench/"), ("testB", "record")])

    def test_no_bench_dir_means_no_record_scan(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td))
            with mock.patch("tasks.dashboard.record_exams", side_effect=AssertionError("scanned")) as re_:
                self.assertIsNone(db._exams_with_fallback(p))
                db.panel_health_lines(p, now=NOW)
            re_.assert_not_called()

    def test_manifest_exam_wins_over_a_record_for_the_same_pair(self):
        with tempfile.TemporaryDirectory() as td:
            b = Path(td) / "bench"
            _bench(b)
            s = db.judgebench_summary(b)
            merged = db.merge_record_exams(s, [{"run_id": "copy", "date": None, "labels": ["sol-high", "sol-med"], "candidates": [],
                                                "specs": [], "template_sha": "", "template_version": "",
                                                "verdict": "x", "weighted": {}, "source": "record"}])
        self.assertEqual([e["run_id"] for e in merged["exams"]], ["testA"])

    def test_absent_bench(self):
        self.assertIsNone(db.judgebench_summary(None))
        self.assertEqual(db.template_triggers(None), [])
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(db.find_bench_dir(Path(td)))

    def test_find_bench_in_code_root(self):
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td))
            (p / ".agent" / "config.json").write_text(json.dumps({"code_roots": ["nested"]}), encoding="utf-8")
            _bench(p / "nested" / "bench")
            self.assertEqual(db.find_bench_dir(p), p / "nested" / "bench")


class RenderAndReadOnly(unittest.TestCase):
    def test_render_is_read_only_and_has_every_section(self):
        lines = [_rec(1, "codex:gpt-5.6-sol:high"), _rec(2, "grok:grok-4.6:medium", "timeout"),
                 _rec(3, "codex:gpt-5.6-terra:medium")]
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td), journal_lines=lines, bench=True)
            before = _tree_digest(p)
            out = db.render_dashboard(p, now=NOW, detect=False)
            self.assertEqual(_tree_digest(p), before, "dashboard wrote into the project")
        for needle in ("PLAYBOOK DASHBOARD", "read-only", "plugin:", "verify: python3 -m unittest",
                       "default judge: codex:gpt-5.6-sol:high", "panel required for: assertive, irreversible",
                       "codex:gpt-5.6-sol:high", "grok:grok-4.6:medium", "(not in panel)", "codex:gpt-5.6-terra:medium",
                       "soft timeout 900s", "hard timeout 1200s", "judge budget $7",
                       "hooks (doctor's hook checks:", "tasks: 0 open", "judgebench: corpus v3 (2 cases)",
                       "exam testA · 2026-09-07 · sol-high vs sol-med", "weighted sol-high 133 vs sol-med 98",
                       "triggers (3 kinds", "model-gap [skipped (--no-detect)]", "none fired"):
            self.assertIn(needle, out, needle)

    def test_detect_on_writes_only_the_catalog_baseline_and_fires_only_on_new_ids(self):
        # task 054: the one sanctioned write is `.agent/model-catalog.json`; a
        # selective panel is information, a NEW id since the baseline is the trigger
        from unittest import mock
        fake = {"providers": [{"name": "codex", "installed": True,
                               "models": [{"id": "gpt-5.6-sol", "efforts": ["medium", "high"]}, {"id": "gpt-x", "efforts": ["medium"]}]}]}
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td), journal_lines=[_rec(1, "codex:gpt-5.6-sol:high")], bench=True)
            before = _tree_digest(p)
            with mock.patch("tasks.models_check.detect_providers", return_value=fake) as dp:
                out = db.render_dashboard(p, now=NOW, detect=True)
            after = _tree_digest(p)
            self.assertEqual(set(after) - set(before), {os.path.join(".agent", "model-catalog.json")}, "dashboard wrote more than the baseline")
            self.assertEqual({k: v for k, v in after.items() if k in before}, before, "dashboard changed an existing file")
            dp.assert_called_once()
            self.assertIn("none fired", out)
            self.assertIn("info: available but not seated — codex: gpt-x", out)
            self.assertIn("catalog baseline .agent/model-catalog.json recorded now", out)
            self.assertNotIn("model-gap · codex", out)
            # second run, a new id appeared → the trigger, with the add command
            fake2 = json.loads(json.dumps(fake))
            fake2["providers"][0]["models"].append({"id": "gpt-y", "efforts": ["low", "high"]})
            with mock.patch("tasks.models_check.detect_providers", return_value=fake2):
                out2 = db.render_dashboard(p, now=NOW + timedelta(days=1), detect=True)
            self.assertIn("model-gap · codex — new since 2026-09-08: gpt-y", out2)
            self.assertIn("codex:gpt-y:low", out2)
            self.assertIn("catalog baseline .agent/model-catalog.json updated", out2)
            # --no-detect never writes, even with a baseline present
            digest = _tree_digest(p)
            out3 = db.render_dashboard(p, now=NOW + timedelta(days=2), detect=False)
            self.assertEqual(_tree_digest(p), digest)
            self.assertIn("model-gap [skipped (--no-detect)]", out3)

    def test_dead_pin_renders_with_its_seat_label(self):
        from unittest import mock
        fake = {"providers": [{"name": "codex", "installed": True, "models": [{"id": "gpt-5.6-terra", "efforts": ["high"]}]}]}
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td))
            with mock.patch("tasks.models_check.detect_providers", return_value=fake):
                db.render_dashboard(p, now=NOW, detect=True)                 # records the baseline
                out = db.render_dashboard(p, now=NOW, detect=True)
            self.assertIn("model-gap · codex:gpt-5.6-sol:high — dead pin: gpt-5.6-sol is no longer listed by codex", out)
            self.assertIn("tasks models set --panel opus,grok:grok-4.6:medium --default-judge opus", out)   # sol was the default judge

    def test_record_scan_runs_once_per_render_when_a_bench_exists(self):
        # impl-panel r1 opus#1 + r2: the scan is bounded to projects that HAVE a bench dir
        # (see test_no_bench_dir_means_no_record_scan) and runs once per render/block.
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td), bench=True)
            with mock.patch("tasks.dashboard.record_exams", return_value=[]) as re_:
                block = db.panel_health_lines(p, now=NOW)
                db.render_dashboard(p, now=NOW, detect=False)
            self.assertEqual(re_.call_count, 2)
        self.assertEqual(len(block), 6)

    def test_stale_settings_hook_paths_rule(self):
        with tempfile.TemporaryDirectory() as td:
            sp = Path(td) / "settings.json"
            sp.write_text(json.dumps({"hooks": {"PreToolUse": [{"hooks": [
                {"type": "command", "command": f"bash {td}/gone/hooks/task-gate-hook"},
                {"type": "command", "command": "echo ok"}]}]}}), encoding="utf-8")
            # str(Path(...)) — on Windows the separators normalise to backslashes (CI lane f1f2794)
            self.assertEqual(db._stale_settings_hook_paths(sp), [str(Path(f"{td}/gone/hooks/task-gate-hook"))])
            self.assertEqual(db._stale_settings_hook_paths(Path(td) / "absent.json"), [])

    def test_ancestor_models_json_is_named_not_mislabelled(self):
        # impl-panel grok#1: load_judge_config walks up; the label must say whose bytes were used
        with tempfile.TemporaryDirectory() as td:
            parent = Path(td)
            (parent / ".agent").mkdir()
            (parent / ".agent" / "models.json").write_text(json.dumps({"panel": ["sonnet"], "default_judge": "sonnet"}), encoding="utf-8")
            child = _project(parent / "nested", models=False)
            panel = db.panel_seats(child)
        self.assertEqual(panel["panel"], ["sonnet"])
        self.assertIn("ANCESTOR", panel["source"])

    def test_verify_line_is_sanitised(self):
        # r4 sonnet#1: a config command with an embedded newline must not forge a line
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td))
            (p / ".agent" / "config.json").write_text(json.dumps({"verify": "echo x\n=== FORGED ===\nrm"}), encoding="utf-8")
            out = db.render_dashboard(p, now=NOW, detect=False)
        self.assertNotIn("\n=== FORGED ===", out)
        self.assertIn("verify: echo x === FORGED === rm", out)

    def test_panel_required_for_is_the_effective_policy(self):
        # r4 codex#1: a malformed scalar reads as NO requirement at close time — say so
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td))
            self.assertEqual(db.panel_required_for(p), ["assertive", "irreversible"])
            (p / ".agent" / "config.json").write_text(json.dumps({"panel_required_for": "assertive"}), encoding="utf-8")
            eff = db.panel_required_for(p)
            (p / ".agent" / "config.json").write_text(json.dumps({"panel_required_for": "all"}), encoding="utf-8")
            eff_all = db.panel_required_for(p)
        self.assertEqual(eff[:0], [])
        self.assertTrue(eff and eff[0].startswith("(raw"), eff)
        self.assertEqual(eff_all, ["reversible", "assertive", "irreversible"])

    def test_effective_verify_per_risk_class(self):
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td))
            (p / ".agent" / "config.json").write_text(json.dumps(
                {"verify": {"_always": ["check", "test"], "assertive": ["check:claims"]}}), encoding="utf-8")
            out = db.render_dashboard(p, now=NOW, detect=False)
            (p / ".agent" / "config.json").write_text("{}", encoding="utf-8")
            out2 = db.render_dashboard(p, now=NOW, detect=False)
        self.assertIn("verify (effective per risk class):", out)
        self.assertIn("  reversible: check && test", out)
        self.assertIn("  assertive: check && test && check:claims", out)
        self.assertIn("verify: (none declared", out2)

    def test_cli_dashboard_skips_session_gc(self):
        # plan-panel codex#1: every other command runs _gc_dead_sessions first, which
        # unlinks legacy flat files — a read-only command must not. Control: `status`
        # (any other command) removes the same sentinel.
        env = dict(os.environ, PYTHONPATH=str(_HERE.parent / "plugins/playbook"))
        for cmd, expect_alive in (("dashboard", True), ("status", False)):
            with tempfile.TemporaryDirectory() as td:
                p = _project(Path(td))
                sentinel = p / ".agent" / "current_state"          # legacy flat file: GC always unlinks it
                sentinel.write_text("42\n", encoding="utf-8")
                args = [sys.executable, "-m", "tasks.cli", cmd] + (["--no-detect"] if cmd == "dashboard" else [])
                r = subprocess.run(args, cwd=p, env=env, capture_output=True, text=True, timeout=120)
                alive = sentinel.exists()
            self.assertEqual(alive, expect_alive, f"{cmd}: rc={r.returncode} out={r.stdout[-300:]} err={r.stderr[-300:]}")
            if cmd == "dashboard":
                self.assertIn("PLAYBOOK DASHBOARD", r.stdout)

    def test_hooks_health_is_ok_on_a_clean_resolve(self):
        # impl-panel r2 (opus/sonnet/codex/grok, Critical): a for…else had appended
        # "no authoritative hooks.json resolved" on EVERY call, so ok was never True.
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td))
            with mock.patch("tasks.hooks_check.hooks_check_report", return_value=[]), \
                 mock.patch("tasks.hooks_check.grok_enforcement_issues", return_value=[]), \
                 mock.patch("tasks.dashboard._stale_settings_hook_paths", return_value=[]):
                h = db.hooks_health(p)
        self.assertEqual(h["warnings"], [], h)
        self.assertTrue(h["ok"])
        self.assertTrue(h["copy"] and h["version"], h)
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td))
            with mock.patch("tasks.hooks_check.hooks_check_report", return_value=[]), \
                 mock.patch("tasks.hooks_check.grok_enforcement_issues", return_value=[]), \
                 mock.patch("tasks.dashboard._stale_settings_hook_paths", return_value=["/gone/x"]), \
                 mock.patch("tasks.hooks_check.authoritative_hooks_path", return_value=None):
                h2 = db.hooks_health(p)
        self.assertEqual(len(h2["warnings"]), 2, h2)     # one stale path + one "no authoritative copy", once each

    def test_hooks_health_mirrors_doctor_grok_rule(self):
        # W5 correspondence: doctor warns on STALE grok enforcement paths even without
        # AGENTS.md; only a MISSING file is gated on AGENTS.md.
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td))
            stale = ["stale PreToolUse: script missing (/old/1.4.7/scripts/task-gate-hook)"]
            with mock.patch("tasks.hooks_check.grok_enforcement_issues", return_value=stale), \
                 mock.patch("tasks.hooks_check.grok_enforcement_report",
                            return_value=[("hooks: grok enforcement", "PreToolUse: script missing (/old/1.4.7/scripts/task-gate-hook)")]), \
                 mock.patch("tasks.hooks_check.hooks_check_report", return_value=[]):
                h_stale = db.hooks_health(p)
            with mock.patch("tasks.hooks_check.grok_enforcement_issues", return_value=["missing ~/.grok/enforcement"]), \
                 mock.patch("tasks.hooks_check.grok_enforcement_report", return_value=[("hooks: grok enforcement", "missing")]), \
                 mock.patch("tasks.hooks_check.hooks_check_report", return_value=[]):
                h_missing_plain = db.hooks_health(p)
                (p / "AGENTS.md").write_text("# grok\n", encoding="utf-8")
                h_missing_grok = db.hooks_health(p)
        self.assertTrue(any("1.4.7" in w for w in h_stale["warnings"]), h_stale)
        self.assertFalse(any("grok" in w for w in h_missing_plain["warnings"]), h_missing_plain)
        self.assertTrue(any("grok enforcement — missing" in w for w in h_missing_grok["warnings"]), h_missing_grok)

    def test_render_without_anything(self):
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td), models=False)
            out = db.render_dashboard(p, now=NOW, detect=False)
        self.assertIn("judgebench: not present", out)
        self.assertIn("no journal file yet (no review has run in this lane)", out)   # missing ≠ zero
        self.assertIn("plugin defaults (no .agent/models.json here or above)", out)

    def test_hostile_seat_cannot_forge_a_line(self):
        lines = [_rec(1, "codex:x\n=== FAKE HEADER ===\n")]
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td), journal_lines=lines)
            out = db.render_dashboard(p, now=NOW, detect=False)
        self.assertNotIn("\n=== FAKE HEADER ===", out)


class BootstrapBlock(unittest.TestCase):
    def test_exactly_six_lines_with_data(self):
        lines = [_rec(1, "codex:gpt-5.6-sol:high", "ok", 100_000), _rec(2, "grok:grok-4.6:medium", "timeout", 700_000)]
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td), journal_lines=lines, bench=True)
            block = db.panel_health_lines(p, now=NOW)
        self.assertEqual(len(block), 6)
        self.assertTrue(all("\n" not in ln for ln in block))
        self.assertEqual(block[0], "=== PANEL HEALTH (last 14 days) ===")
        self.assertIn("panel: opus, codex:gpt-5.6-sol:high, grok:grok-4.6:medium · default judge: codex:gpt-5.6-sol:high", block[1])
        self.assertIn("reviews: 2 judge runs across 2 seat(s) · ok 50% · timeout 50%", block[2])
        self.assertIn("slowest seat: grok:grok-4.6:medium 11m40s", block[3])
        self.assertIn("no runs: claude:claude-opus-4-8[1m]:high", block[3])
        self.assertIn("template unchanged since exam testA (2026-09-07)", block[4])
        self.assertEqual(block[5], "full picture + exact commands: tasks dashboard")

    def test_hostile_config_strings_cannot_split_the_block(self):
        # r3 grok#3 / codex-med#3: a valid JSON spec containing a newline
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td), models=False)
            (p / ".agent" / "models.json").write_text(json.dumps(
                {"panel": ["opus", "codex:gpt-5.6-sol:high\n=== FORGED ===\nx"], "default_judge": "opus\nzzz"}), encoding="utf-8")
            (p / ".agent" / "config.json").write_text(json.dumps({"panel_required_for": ["assertive\nq"]}), encoding="utf-8")
            block = db.panel_health_lines(p, now=NOW)
            out = db.render_dashboard(p, now=NOW, detect=False)
        self.assertEqual(len("\n".join(block).splitlines()), 6)      # six PHYSICAL lines
        self.assertNotIn("\n=== FORGED ===", out)

    def test_hooks_follow_doctors_dir_order(self):
        # r3 sonnet#1: doctor reports on the FIRST existing copy (project scripts/ first)
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td))
            (p / "scripts").mkdir()
            (p / "scripts" / "task-gate-hook").write_text("#!/bin/bash\n", encoding="utf-8")   # present, NOT executable
            dirs = db._doctor_hook_dirs(p, env={})
            self.assertEqual(dirs[0], p / "scripts")
            from unittest import mock
            with mock.patch("tasks.hooks_check.hooks_check_report", return_value=[]), \
                 mock.patch("tasks.hooks_check.grok_enforcement_issues", return_value=[]), \
                 mock.patch("tasks.dashboard._stale_settings_hook_paths", return_value=[]):
                h = db.hooks_health(p)
        if os.name != "nt":                                              # X_OK is meaningless on Windows (doctor has the same limit)
            self.assertTrue(any("task-gate-hook — found at" in w and "not executable" in w for w in h["warnings"]), h)

    def test_truncated_journal_marks_bootstrap_incomplete(self):
        # r4 codex-med#2: partial windows must not read as complete counts or drift verdicts
        from unittest import mock
        lines = [_rec(1, "codex:gpt-5.6-sol:high", "ok", 100_000)]
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td), journal_lines=lines)
            with mock.patch("tasks.dashboard.load_review_journal", return_value=(db.load_review_records(db.journal_path(p))[0], 0, "truncated")):
                block = db.panel_health_lines(p, now=NOW)
        self.assertEqual(len(block), 6)
        self.assertIn("journal TRUNCATED — counts incomplete", block[2])
        self.assertIn("drift n/a (journal truncated)", block[4])

    def test_exactly_six_lines_without_data(self):
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td), models=False)
            block = db.panel_health_lines(p, now=NOW)
        self.assertEqual(len(block), 6)
        self.assertIn("no review-spend records in the last 14 days", block[2])
        self.assertIn("template: no live exam on record", block[4])

    def test_bootstrap_block_is_six_lines_even_when_the_dashboard_raises(self):
        # impl-panel r2 codex-med#5: the six-line contract holds on the exception path too
        from unittest import mock
        from tasks.project_setup import cmd_bootstrap
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td))
            (p / "MIND_MAP.md").write_text("# Mind Map\n\n[1] **Overview** - x.\n", encoding="utf-8")
            cwd = os.getcwd()
            buf = io.StringIO()
            try:
                os.chdir(p)
                with mock.patch("tasks.dashboard.panel_health_lines", side_effect=RuntimeError("boom\nline2")), \
                     contextlib.redirect_stdout(buf):
                    cmd_bootstrap([])
            finally:
                os.chdir(cwd)
        out = buf.getvalue().splitlines()
        i = out.index("=== PANEL HEALTH (last 14 days) ===")
        self.assertEqual(out[i + 1], "unavailable: boom line2")
        self.assertEqual(out[i + 5], "full picture + exact commands: tasks dashboard")
        self.assertEqual(out[i + 6], "")

    def test_bootstrap_prints_the_block(self):
        from tasks.project_setup import cmd_bootstrap
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td))
            (p / "MIND_MAP.md").write_text("# Mind Map\n\n[1] **Overview** - x.\n", encoding="utf-8")
            cwd = os.getcwd()
            buf = io.StringIO()
            try:
                os.chdir(p)
                with contextlib.redirect_stdout(buf):
                    cmd_bootstrap([])
            finally:
                os.chdir(cwd)
        out = buf.getvalue().splitlines()
        i = out.index("=== PANEL HEALTH (last 14 days) ===")
        self.assertEqual(out[i + 5], "full picture + exact commands: tasks dashboard")
        self.assertTrue(out.index("=== PENDING TASKS ===") < i < out.index("=== CLI REFERENCE ==="))


if __name__ == "__main__":
    unittest.main()
