"""CLI entry point for standalone tasks management."""
from __future__ import annotations

import os
import re
import shutil
import sys
import time
from pathlib import Path
from tasks.core import create_task, list_tasks, task_status, PLAYBOOKS, _find_playbook_skill, resolve_session_id, resolve_agent_dir, require_lane_marker, run_merge_doctor
from tasks.shared import (
    find_project_root, _gc_dead_sessions, _own_session_id, _session_is_dead,
    _merge_verify_module, _merge_verify_issues, _merge_verify_untracked,
)
from tasks.mindmap import _load_mind_map

# Every top-level command the dispatcher accepts, aliases included. Pinned two
# ways by tests/test_cli_dispatch.py: this tuple must equal the dispatch
# chain's literals, and every entry must reach its arm through a real
# `python3 -m tasks.cli <cmd>` invocation — so a module peel can never orphan
# an arm silently. Keep it in dispatch order.
COMMANDS = (
    "work", "new", "init", "bootstrap", "list", "ls", "panel-review",
    "models", "plan-review", "impl-review", "judge", "context", "intent",
    "timeline", "tagger", "tag", "retro", "status", "audit", "blocked",
    "parked", "freehand", "doctor", "merge-doctor", "mindmap-sync", "log",
    "prepare-merge",
)


def _state_file(project_path: Path) -> Path:
    """Return per-session state file under .agent/sessions/<id>/current_state."""
    session_id = resolve_session_id()
    state_dir = resolve_agent_dir(project_path) / "sessions" / session_id
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "current_state"


def _capture_recent_chat(project_path: Path, max_messages: int = 10,
                         max_gap_seconds: int = 10800) -> list[str]:
    """Capture recent chat_log messages for task attribution.

    Scans backwards from end of chat_log.md. Stops at:
    - Previous 'tasks done' or 'tasks work done' in message text
    - A time gap > max_gap_seconds (default 3h) between consecutive messages
    - max_messages reached (default 10)

    Returns list of message blocks (most recent last), each as:
    "**[MNNN]** [timestamp]\\n<text truncated to 200 chars>"
    """
    import re
    from datetime import datetime

    chat_log = resolve_agent_dir(project_path) / "chat_log.md"
    if not chat_log.exists():
        return []

    content = chat_log.read_text(encoding="utf-8", errors="replace")
    # Split into message blocks on --- separator
    msg_pattern = re.compile(
        r'\*\*\[(M\d+)\]\*\*\s+\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC\]\s+`\w+`\s*\n\s*\n(.*?)(?=\n---|\Z)',
        re.DOTALL
    )

    messages = []
    for m in msg_pattern.finditer(content):
        msg_id = m.group(1)
        timestamp_str = m.group(2)
        text = m.group(3).strip()
        try:
            ts = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        messages.append((msg_id, ts, timestamp_str, text))

    if not messages:
        return []

    # Scan backwards
    captured = []
    prev_ts = None
    for msg_id, ts, ts_str, text in reversed(messages):
        # Stop at time gap
        if prev_ts is not None:
            gap = (prev_ts - ts).total_seconds()
            if gap > max_gap_seconds:
                break
        prev_ts = ts

        # Stop at task-done marker
        text_lower = text.lower()
        if "tasks done" in text_lower or "tasks work done" in text_lower:
            break

        # Truncate long messages
        display_text = text[:200] + "..." if len(text) > 200 else text
        captured.append(f"**[{msg_id}]** [{ts_str}]\n{display_text}")

        if len(captured) >= max_messages:
            break

    # Reverse to chronological order
    captured.reverse()
    return captured


def _inject_chat_into_task(task_file: Path, messages: list[str]) -> None:
    """Inject captured chat messages into task.md References section."""
    if not messages:
        return

    import re

    def _utf8_safe(text: str) -> str:
        """Replace non-UTF-8-survivable code points like lone surrogates."""
        return text.encode("utf-8", errors="replace").decode("utf-8")

    content = task_file.read_text(encoding="utf-8")

    chat_block = "\n### Recent Chat (auto-captured at activation — review and remove unrelated)\n"
    for msg in messages:
        chat_block += f"\n{_utf8_safe(msg)}\n"

    # Insert after the first --- (end of References section, before Design Phase)
    first_sep = content.find("\n---\n")
    if first_sep >= 0:
        references = content[:first_sep]
        references = re.sub(
            r'\n### Recent Chat \(auto-captured at activation — review and remove unrelated\)\n.*\Z',
            "",
            references,
            flags=re.DOTALL,
        )
        content = references.rstrip() + "\n" + chat_block + content[first_sep:]
        task_file.write_text(_utf8_safe(content), encoding="utf-8")


def _panel_triage_frame() -> list[str]:
    """Return the lines to append to a panel-review judge.md so the reading
    agent meets the triage discipline alongside the findings.

    Same wording for plan and impl modes (the panel-review assembly is shared);
    mirrors the per-task pushback gate from `template.judge_section()` /
    `template.judge_impl_section()` but lives in the file the agent actually
    reads after the panel runs.
    """
    bar = "═" * 60
    return [
        bar,
        "## Triage",  # No indent — must match `^## ` line-start parsers (impl-review F4).
        bar + "\n",
        (
            "These findings are opinion, not gospel. Before applying any of "
            "them, decide per-finding: real correctness issue, speculative "
            "concern, or wrong call. Document accept (with rationale) / park "
            "(with rationale) / reject (with rationale). Verify file:line "
            "claims before applying — panel judges sometimes cite wrong "
            "locations. The panel doesn't live with the outcomes — you do. "
            "Push back where you have concrete evidence the panel doesn't."
        ),
        "",
        # P11: name what a panel structurally CANNOT catch, at the moment the
        # agent is tempted to read a clean panel as "all clear". Three classes
        # went through 46 real panels untouched because everyone assumed the
        # panel covered them. A panel does CONFORMANCE (does the code match the
        # intent); it cannot do these — so a green panel is NOT evidence on them.
        "**A clean panel does NOT clear these — they are outside what any judge can verify:**",
        (
            "- **Correspondence** — does the result match the WORLD, not just the "
            "intent? A judge reads code and text, not reality. If the work is "
            "user-facing or asserts a measured fact, YOU check the real artifact "
            "(screenshot/recording/actual output) or the measuring instrument — "
            "the panel cannot."
        ),
        (
            "- **Disclosure** — provenance, secrets, attribution, AI-authorship in "
            "public files. This is a mechanical grep (`tasks audit` / a pre-commit "
            "scan), not a judgement call — run it; do not expect a judge to."
        ),
        (
            "- **Irreversibility / blast radius** — a panel weighs correctness, not "
            "consequence. If `## Risk` is `irreversible` or `assertive`, the "
            "rollback plan or the claim-and-its-instrument needs YOUR explicit "
            "sign-off regardless of how clean the findings are."
        ),
        "",
    ]


def _snapshot_repo_state(project_path: Path, task_file: Path | None) -> dict:
    """Capture the repo's mutable state before spawning judges, so a rogue judge
    that writes the working tree can be detected afterward (#1 tamper guard).

    Judges are read-only evaluators; nothing they run should change the repo. On
    platforms with OS containment `project_writable=False` blocks writes, but the
    sandbox falls back to UNCONTAINED direct exec when no seatbelt/bwrap exists
    (Windows) or when already nested — there this snapshot/compare is the ONLY
    tamper defense, so it is mandatory, not belt-and-braces.

    Two best-effort signals:
      - `git status --porcelain`: repo-wide; catches edits to tracked files and
        new non-ignored files (e.g. a rogue's task_audit.md). Gitignored runtime
        churn (.agent/**/sessions, chat_log, bash_history) is excluded by design,
        so legitimate judge-session hook writes don't false-positive. None when
        the project is not a git repo.
      - sha256 of task.md: the primary tamper target (the rogue rewrote work-plan
        gates); the only signal when the project isn't a git repo.
    """
    import subprocess
    porcelain = None
    try:
        r = subprocess.run(
            # -uall: enumerate untracked files INDIVIDUALLY. A collapsed
            # `?? newdir/` line hides what is inside — the F22 monitor
            # exclusion needs full paths to match, and naming each rogue
            # file is strictly better evidence than naming its directory.
            ["git", "-C", str(project_path), "status", "--porcelain", "-uall"],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
        )
        if r.returncode == 0:
            porcelain = r.stdout
    except (OSError, subprocess.SubprocessError):
        porcelain = None
    task_hash = None
    if task_file and task_file.exists():
        import hashlib
        task_hash = hashlib.sha256(task_file.read_bytes()).hexdigest()
    return {"porcelain": porcelain, "task_hash": task_hash}


# Each mode accepts BOTH placeholder generations: pre-1.5.2 templates say
# "findings appear here", the panel-first templates say "triage appears here".
# The single-judge fallback write-back must anchor in either — caught live by
# the 1.5.3 gauntlet, where a new-template task refused the fallback's write.
_REVIEW_SECTIONS = {
    "plan": ("## Plan Review",
             ("(plan review findings appear here)",
              "(plan review triage appears here)")),
    "impl": ("## Implementation Review",
             ("(implementation review findings appear here)",
              "(implementation review triage appears here)")),
}


def _findings_markers(review_mode: str) -> tuple[str, str]:
    """Open/close sentinels delimiting parent-written findings in task.md."""
    return (f"<!-- playbook:{review_mode}-review-findings -->",
            f"<!-- /playbook:{review_mode}-review-findings -->")


def _neutralise_markers(findings: str, review_mode: str) -> str:
    """Defang sentinel tokens inside judge output.

    Findings are UNTRUSTED text. If they contained our own markers, the next
    rerun's replace would bind to the wrong span and could eat the surrounding
    gates — the same class of damage the tamper guard exists to prevent. Break
    the tokens so they can never be mistaken for delimiters.
    """
    out = findings
    for marker in _findings_markers(review_mode):
        out = out.replace(marker, marker.replace("<!--", "<!_-").replace("-->", "-_>"))
    return out


def _write_review_findings(task_file: Path, review_mode: str, findings: str) -> str | None:
    """Write a single judge's findings into task.md. Returns None on success,
    else a human-readable reason it refused.

    The judge is sandboxed read-only (`project_writable=False`) and must stay
    that way, so the trusted parent performs this write — the same division the
    panel path already uses. Idempotent across reruns: findings live between
    explicit sentinels, so a re-review replaces a delimited region instead of
    guessing where the previous findings ended.

    Refuses rather than guesses. If the section has neither its placeholder nor
    exactly one well-ordered sentinel pair, nothing is written and the caller
    reports it — the findings still exist in the judge log, so refusing costs
    the operator nothing, while a wrong insertion could destroy work-plan gates.
    """
    section = _REVIEW_SECTIONS.get(review_mode)
    if section is None:
        return f"unknown review mode {review_mode!r}"
    heading, placeholders = section
    open_m, close_m = _findings_markers(review_mode)
    body = _neutralise_markers(findings.strip(), review_mode)
    block = f"{open_m}\n{body}\n{close_m}"

    try:
        text = task_file.read_text(encoding="utf-8")
    except OSError as e:
        return f"could not read {task_file.name}: {e}"

    # Operate ONLY inside the named section. Searching the whole file would let a
    # placeholder or marker quoted anywhere else — prose, a nested example, the
    # other review section's text — capture the write and land findings in the
    # wrong place, or silently target text that is not a section at all.
    sec_start = text.find(f"\n{heading}\n")
    if sec_start == -1:
        sec_start = 0 if text.startswith(f"{heading}\n") else -1
    if sec_start == -1:
        return f"{heading} section not found"
    body_start = text.index("\n", sec_start + 1) + 1 if sec_start else len(heading) + 1
    next_heading = text.find("\n## ", body_start)
    sec_end = len(text) if next_heading == -1 else next_heading + 1
    section, before, after = text[body_start:sec_end], text[:body_start], text[sec_end:]

    n_open, n_close = section.count(open_m), section.count(close_m)
    if n_open or n_close:
        if n_open != 1 or n_close != 1:
            return (f"{heading} has {n_open} opening and {n_close} closing "
                    f"findings markers — expected exactly one of each")
        start, end = section.index(open_m), section.index(close_m)
        if end < start:
            return f"{heading} findings markers are out of order"
        section = section[:start] + block + section[end + len(close_m):]
    else:
        _once = [p for p in placeholders if section.count(p) == 1]
        _multi = [p for p in placeholders if section.count(p) > 1]
        if _once:
            section = section.replace(_once[0], block, 1)
        elif _multi:
            return f"{heading} placeholder appears more than once"
        else:
            return (f"{heading} has neither its placeholder nor findings markers "
                    f"(hand-edited?)")
    new_text = before + section + after

    # Atomic: task.md IS the execution trace, so an interrupt must not truncate
    # it. Same-directory temp + os.replace, mirroring models_check.py.
    tmp = task_file.with_suffix(f".tmp.{os.getpid()}")
    try:
        tmp.write_text(new_text, encoding="utf-8")
        os.replace(tmp, task_file)
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        return f"could not write {task_file.name}: {e}"
    return None


def _judge_log_name(backend: str) -> str:
    """Review-log filename for a backend. Shared by the save path and the hard
    timeout path, which writes `<stem>.partial.log` beside it — they must agree
    on the name or a salvaged partial would not be findable next to its review."""
    return {
        "claude": "judge.log",
        "codex": "judge-codex.log",
        "antigravity": "judge-agy.log",
        "grok": "judge-grok.log",
        "pi": "judge-pi.log",
    }.get(backend, "judge.log")


def _detect_tamper(project_path: Path, task_file: Path | None, before: dict) -> list[str]:
    """Compare current repo state against a `_snapshot_repo_state` result.
    Returns human-readable change descriptions (empty list = no tamper).

    Known gap: a judge editing a file that was ALREADY dirty before the run
    produces an identical porcelain line, so a content-only change to an
    already-modified non-task.md file is not caught. task.md itself is covered
    by its hash regardless. Acceptable — the rogue incident created new files
    and rewrote task.md, both caught here."""
    after = _snapshot_repo_state(project_path, task_file)
    changes: list[str] = []
    b_porc, a_porc = before.get("porcelain"), after.get("porcelain")
    if b_porc is not None and a_porc is not None and b_porc != a_porc:
        # F22: the conversation monitor writes trace.md/session.md under
        # `.agent/monitor/` (or `.agent/<user>/monitor/`) WHILE panels run —
        # a sanctioned concurrent writer whose own sandbox confines it to
        # exactly that directory, so churn there is expected, not a judge
        # writing the repo. Everything else under .agent still flags.
        _monitor_re = re.compile(r"^..\s+\"?\.agent(/[^/]+)?/monitor/")
        new_lines = set(a_porc.splitlines()) - set(b_porc.splitlines())
        for line in sorted(new_lines):
            if _monitor_re.match(line):
                continue
            changes.append(f"working tree: {line.strip()}")
    b_hash, a_hash = before.get("task_hash"), after.get("task_hash")
    if b_hash and a_hash and b_hash != a_hash:
        rel: Path | str = task_file
        try:
            rel = task_file.relative_to(project_path)
        except (ValueError, AttributeError):
            pass
        changes.append(f"task.md content changed ({rel})")
    return changes


def _tamper_banner(changes: list[str]) -> str:
    """Loud banner naming what a judge mutated during a review run."""
    bar = "!" * 60
    lines = [
        bar,
        "!! TAMPER DETECTED — a judge modified the repo during review !!",
        bar,
        "Judges are read-only evaluators; these changes are NOT trustworthy work:",
    ]
    lines += [f"  - {c}" for c in changes]
    lines += [
        "Do NOT ingest this review into task.md. Inspect and restore:",
        "  git status && git diff    # then: git checkout -- <path> / rm <new file>",
        bar,
    ]
    return "\n".join(lines)


def print_usage():
    from tasks.template import usage_text
    print(usage_text())


def _gate_bounce(task_id: str, task_file, action: str) -> bool:
    """If `task_file` has open (unchecked) gates, print a steering message and
    return True (the caller should abort). Returns False when all gates are
    checked. The `--force` decision is the caller's — this only reports.
    """
    from tasks.core import _extract_head_position
    head = _extract_head_position(task_file)
    if head == "(all gates checked)":
        return False
    try:
        open_count = sum(
            1 for ln in task_file.read_text(encoding="utf-8").splitlines()
            if ln.strip().startswith("- [ ]")
        )
    except OSError:
        open_count = 0
    print(
        f"Blocked: task {task_id} has {open_count} open gate(s) — {action} needs them finalized.",
        file=sys.stderr,
    )
    print(f"  Next open gate: {head}", file=sys.stderr)
    print(
        "  Finish them (check the boxes in task.md), then retry — or override with --force.",
        file=sys.stderr,
    )
    return True


def main():
    # Force utf-8 on Windows where the default console encoding (cp1252) chokes on → and emoji.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help", "help"):
        print_usage()
        return

    _gc_dead_sessions(find_project_root())

    cmd = args[0]
    cmd_args = args[1:]

    if cmd == "work":
        if not cmd_args:
            print("Error: 'work' requires a task number or 'done'", file=sys.stderr)
            print("Usage: tasks work <number> | tasks work done", file=sys.stderr)
            sys.exit(1)

        task_num = cmd_args[0]
        if task_num != "done" and task_num.isdigit():
            task_num = task_num.zfill(3)
        force = any(a in ("--force", "-f") for a in cmd_args[1:])
        # --stale-panel-ok: the NARROW exit from the F18 irreversible freshness
        # gate — suppresses only that gate, only with a --reason, and the
        # reason is recorded in the receipt's freshness clause. Deliberately
        # not folded into --force: batch-5 field data shows agents take the
        # cheapest sanctioned exit under friction, and the cheap exit must not
        # be whole-policy bypass.
        stale_panel_ok = "--stale-panel-ok" in cmd_args[1:]
        # --reason "why": required for any forced close so the escape hatch leaves
        # a trace (the 046 fix). Stored in the verification receipt. Shared by
        # --stale-panel-ok; when BOTH --force and --stale-panel-ok appear, the
        # reason is attributed to force (the stronger hatch — judge F5).
        reason = None
        for _i, _a in enumerate(cmd_args):
            if _a == "--reason" and _i + 1 < len(cmd_args):
                reason = cmd_args[_i + 1]
                break
        project_path = find_project_root()

        # Handle 'tasks work done' - deactivate current task and set Status in task.md
        if task_num == "done":
            agent_dir = resolve_agent_dir(project_path)
            session_id = resolve_session_id()
            session_state = agent_dir / "sessions" / session_id / "current_state"

            # Find the active task from session state file
            prev_task = session_state.read_text(encoding="utf-8").strip() if session_state.exists() else None

            if prev_task:
                # Set ## Status to done in task.md
                tasks_dir = agent_dir / "tasks"
                matches = list(tasks_dir.glob(f"{prev_task}-*/task.md"))
                if matches:
                    task_file = matches[0]
                    if not force and _gate_bounce(prev_task, task_file, "closing this task"):
                        sys.exit(1)
                    # Belt to gate-bounce's suspenders (#09): head-position can be
                    # fooled by a tricky line while the honest, line-anchored COUNT
                    # still shows open gates — the 71/74-yet-"all-checked" symptom.
                    # Refuse on the count too, so no single parser's blind spot can
                    # close a task with open gates.
                    if not force:
                        from tasks.core import _gate_counts
                        try:
                            _chk, _tot = _gate_counts(task_file.read_text(encoding="utf-8"))
                        except OSError:
                            _chk, _tot = 0, 0
                        if _tot and _chk < _tot:
                            print(f"Blocked: task {prev_task} shows {_chk}/{_tot} gates "
                                  f"checked — {_tot - _chk} still open by the line-anchored "
                                  "count. Finish them, or override with --force --reason.",
                                  file=sys.stderr)
                            sys.exit(1)
                    # Evidence contract + consequence gate (P1 / P2). Close is no
                    # longer "write the string done": run the project's declared
                    # verify contract, record a receipt, and refuse to close on a
                    # failing verify or an unreviewed high-consequence change —
                    # unless forced with a recorded reason.
                    from tasks.core import (
                        close_decision, extract_risk, format_verify_receipt,
                        has_panel_impl_evidence, has_review_evidence,
                        resolve_panel_required, resolve_verify_commands,
                        resolve_verify_timeout,
                    )
                    risk = extract_risk(task_file)
                    commands = resolve_verify_commands(project_path, risk)
                    entries = []
                    verify_failed = False
                    if commands:
                        mv = _merge_verify_module()
                        if mv is None:
                            print("Error: cannot load the verify runner "
                                  "(skills/merge/merge-verify.py missing).", file=sys.stderr)
                            sys.exit(1)
                        _vt = resolve_verify_timeout(project_path)
                        print(f"Verifying close ({len(commands)} declared command(s), risk={risk})...",
                              flush=True)
                        for label, cmd in commands:
                            rc, output = mv.run_command_capture(
                                cmd, str(project_path), timeout_secs=_vt)
                            entries.append((label, cmd, rc, output))
                            if rc != 0:
                                verify_failed = True
                            print(f"  [{'PASS' if rc == 0 else f'FAIL exit {rc}'}] {cmd.splitlines()[0]}",
                                  flush=True)
                    else:
                        print(f"  (no verify contract declared — NOTHING verified at close; risk={risk})",
                              file=sys.stderr, flush=True)

                    # Owner policy: panel_required_for makes the evidence bar
                    # PANEL-grade (all available judges, quorum PASS) — for the
                    # configured risk classes, or "all". Otherwise single-judge
                    # impl evidence suffices as before.
                    _panel_req = resolve_panel_required(project_path, risk)
                    _evidence = (has_panel_impl_evidence(task_file) if _panel_req
                                 else has_review_evidence(task_file, impl_only=True))

                    # F18: panel freshness — computed BEFORE the close is
                    # earned, recorded in the receipt for every close with an
                    # impl round, and GATING for irreversible closes that rest
                    # on panel evidence (design-1.5.6.md; blind judge
                    # conditional-PASS, all conditions built).
                    from tasks.core import (
                        freshness_gate_decision, parse_judge_rounds,
                        tree_state_fingerprint,
                    )
                    _jm = task_file.parent / "judge.md"
                    _rounds = []
                    if _jm.exists():
                        try:
                            _rounds = parse_judge_rounds(
                                _jm.read_text(encoding="utf-8", errors="replace"))
                        except OSError:
                            _rounds = []
                    _impl = next((r for r in _rounds if r["mode"] == "impl"), None)
                    # evidence_carries = the panel evidence that would satisfy
                    # THIS close: rounds[0] impl + PASS (judge C3 — a FAIL
                    # round or a replan on top falls through to the
                    # panel-evidence block, never a double-block).
                    _carries = bool(_panel_req and _rounds
                                    and _rounds[0]["mode"] == "impl"
                                    and _rounds[0]["verdict"] == "PASS")
                    _now_fp = tree_state_fingerprint(project_path) if _impl else ""
                    _freshness = None
                    if _impl is not None:
                        if not _impl["tree_state"]:
                            # Judge F4: a missing stamp must leave a RECORD
                            # when panel evidence carries the close — silence
                            # here would be the one zero-record bypass.
                            if _carries:
                                _freshness = {"verdict": "NO-STAMP"}
                        elif _now_fp:
                            _stale = _now_fp != _impl["tree_state"]
                            _freshness = {
                                "verdict": "STALE" if _stale else "FRESH",
                                "round_fp": _impl["tree_state"],
                                "now_fp": _now_fp,
                                "accepted_reason": (
                                    reason if (_stale and stale_panel_ok
                                               and not force) else None),
                            }
                    _f_allowed, _f_reason = freshness_gate_decision(
                        risk=risk, panel_required=_panel_req,
                        evidence_carries=_carries,
                        round_fp=(_impl["tree_state"] if _impl else ""),
                        now_fp=_now_fp, force=force,
                        stale_ok=stale_panel_ok, stale_reason=reason,
                    )
                    if not _f_allowed:
                        print(f"\nBlocked: cannot close task {prev_task} — {_f_reason}",
                              file=sys.stderr, flush=True)
                        sys.exit(1)

                    allowed, block_reason = close_decision(
                        risk=risk, verify_declared=bool(commands),
                        verify_failed=verify_failed,
                        # impl-grade only: a plan-phase review cannot vouch for
                        # what was BUILT.
                        has_review_evidence=_evidence,
                        force=force, reason=reason,
                        panel_required=_panel_req,
                    )
                    if not allowed:
                        print(f"\nBlocked: cannot close task {prev_task} — {block_reason}",
                              file=sys.stderr, flush=True)
                        sys.exit(1)

                    # F14 blind-judge Finding 3: `unclassified` is not in
                    # HIGH_CONSEQUENCE, so the risk-keyed review bar was never
                    # evaluated — say so loudly instead of failing open in
                    # silence. A warning, not a block: every pre-1.5.0 task is
                    # unclassified, and panel-always projects already hold
                    # "all" closes to panel evidence regardless of risk.
                    if risk == "unclassified":
                        print("⚠ closing with ## Risk unclassified — the "
                              "risk-keyed review requirement was NOT evaluated "
                              "for this close. Set ## Risk to exactly one word "
                              "(reversible / irreversible / assertive) at the "
                              "Risk gate.", file=sys.stderr, flush=True)

                    # Close is earned — record the receipt (ONE section, newest
                    # entry first — a reopened task must not accrete duplicate
                    # headings that section parsers read as current), then set done.
                    import subprocess
                    from tasks.core import _set_status, upsert_task_section
                    try:
                        _head = subprocess.run(
                            ["git", "rev-parse", "HEAD"], cwd=project_path,
                            capture_output=True, text=True).stdout.strip()
                    except (OSError, subprocess.SubprocessError):
                        _head = ""
                    # Dirty-tree honesty (StrataDB F6): closing before committing
                    # is the normal flow, so say so in the receipt and out loud —
                    # a crash between close and commit silently loses "done" work.
                    _dirty = 0
                    try:
                        _porcelain = subprocess.run(
                            ["git", "status", "--porcelain"], cwd=project_path,
                            capture_output=True, text=True).stdout
                        _dirty = len([ln for ln in _porcelain.splitlines() if ln.strip()])
                    except (OSError, subprocess.SubprocessError):
                        pass
                    receipt = format_verify_receipt(
                        entries, _head, risk, reason=(reason if force else None),
                        dirty_files=_dirty, freshness=_freshness)
                    upsert_task_section(task_file, "Verification Receipt", receipt)
                    _set_status(task_file, "done")
                    if _dirty:
                        print(f"⚠ {_dirty} modified/untracked file(s) — this close's "
                              "receipt describes UNCOMMITTED work. Commit before ending "
                              "the session, or a crash loses 'done' work silently.",
                              flush=True)
                    # Panel freshness console note (1.5.3 advisory, F18-reworked):
                    # the receipt clause above is now the durable record; the
                    # note keeps the mismatch visible in the session where the
                    # close happened. Uses the already-computed freshness —
                    # re-parsing here could disagree with what the receipt says.
                    if _freshness and _freshness.get("verdict") == "STALE":
                        print("note: the code state changed after the newest "
                              "impl panel (tree-state mismatch) — if code was "
                              "edited post-review, consider re-running "
                              "`tasks panel-review <N> --mode impl`.",
                              flush=True)
                # Remove session dirs that reference this task.
                # PLAYBOOK_SESSION_ID is not set when called from Bash tool, so scan all sessions.
                # Intentional partial delete: only sessions pointing at prev_task are removed;
                # sessions for other tasks are left intact.
                sessions_dir = agent_dir / "sessions"  # agent_dir already resolved above
                if sessions_dir.exists():
                    for sf in sessions_dir.glob("*/current_state"):
                        try:
                            if sf.read_text(encoding="utf-8").strip() == prev_task:
                                shutil.rmtree(sf.parent, ignore_errors=True)
                        except OSError:
                            pass
                print(f"Task {prev_task} done.")

                # P9: surface this task's OPEN parked items at close so they are
                # not swallowed. Advisory (does not block) — resolve each by
                # promoting (`tasks new` then mark `[promoted → NNN]`), dismissing
                # (`[dismissed: reason]`), or leaving open deliberately.
                from tasks.core import open_parked_items, retro_proposal
                try:
                    _still_open = open_parked_items(task_file.read_text(encoding="utf-8"))
                except OSError:
                    _still_open = []
                if _still_open:
                    print(f"\n⚠ {len(_still_open)} open parked item(s) from this task — "
                          "resolve before they rot (`tasks parked` to review):")
                    for _p in _still_open:
                        print(f"    - {_p}")
                    print("  Promote (`tasks new …` + mark `[promoted → NNN]`), "
                          "dismiss (`[dismissed: reason]`), or re-park deliberately.")

                # P4: the learning loop finally gets a trigger — propose a retro
                # once enough tasks have closed since the last one.
                _retro = retro_proposal(project_path)
                if _retro:
                    print(f"\n💡 {_retro}")
            else:
                print("No active task.")
            print("Code edits blocked until: tasks work <N>")
            return

        # Resume a BLOCKED task (#08): `tasks work <N>` is the "I am picking this
        # up" verb. Clear the block FIRST — flip status back to in_progress — so
        # normal activation below sees an ordinary in_progress task (a blocked
        # task is skipped by _find_active_task, so it must be cleared here).
        _resume_matches = list(
            (resolve_agent_dir(project_path) / "tasks").glob(f"{task_num}-*/task.md"))
        if _resume_matches:
            from tasks.core import _is_blocked, resume_blocked_task
            if _is_blocked(_resume_matches[0]):
                resume_blocked_task(_resume_matches[0])
                print(f"Resuming task {task_num} (was blocked — decision made).")

        # Verify task exists
        # _extract_head_position is imported here, not further down where the
        # auto-close branch uses it: both live in this same function, so a
        # later function-scope import leaves the name unbound for the
        # re-adoption arm below (UnboundLocalError, not NameError).
        from tasks.core import _find_active_task, _extract_head_position
        task_file = _find_active_task(project_path, task_num)
        if not task_file:
            tasks_dir = resolve_agent_dir(project_path) / "tasks"
            matches = list(tasks_dir.glob(f"{task_num}-*/task.md"))
            if matches:
                from tasks.core import _is_done
                tf = matches[0]
                done = _is_done(tf)
                if done:
                    # Reopen: reset Status to in_progress so activation can proceed.
                    lines = tf.read_text(encoding="utf-8").splitlines(keepends=True)
                    for i, line in enumerate(lines):
                        if line.strip() == "## Status" and i + 1 < len(lines):
                            lines[i + 1] = "in_progress\n"
                            tf.write_text("".join(lines), encoding="utf-8")
                            break
                    print(f"Note: task {task_num} was marked done — reopening.")
                    task_file = tf
                    # Fall through to activation below
                elif "<!-- stub:" in tf.read_text(encoding="utf-8"):
                    # Stub — allow activation, expansion happens below
                    task_file = tf
                elif _extract_head_position(tf) == "(all gates checked)":
                    # Re-adopt: every gate is checked but the task was never
                    # closed. Reachable whenever the session pointer is lost
                    # while a finished-but-open task is active — a lost pointer
                    # used to make this state UNRECOVERABLE through the CLI:
                    # `work done` reads the pointer (absent → "No active task",
                    # never touches ## Status), and this branch refused the task
                    # because _find_active_task only returns tasks with open
                    # gates. The only sanctioned writer of ## Status needed a
                    # pointer, and the only way to get a pointer was refused, so
                    # the field report's author had to hand-write the file
                    # (task 027). Status is deliberately NOT rewritten here —
                    # activation alone restores the pointer, and `work done`
                    # remains the thing that closes the task.
                    print(f"Note: task {task_num} has all gates checked but is "
                          f"not closed — re-adopting; run 'tasks work done' to close it.")
                    task_file = tf
                else:
                    print(f"Task {task_num} has no open gates.", file=sys.stderr)
                    sys.exit(1)
            else:
                print(f"Task {task_num} not found", file=sys.stderr)
                sys.exit(1)

        # Auto-close previous task if all gates are checked
        agent_dir = resolve_agent_dir(project_path)
        agent_dir.mkdir(parents=True, exist_ok=True)
        session_id = resolve_session_id()
        session_dir = agent_dir / "sessions" / session_id
        session_state = session_dir / "current_state"
        prev_task = None
        if session_state.exists():
            prev_task = session_state.read_text(encoding="utf-8").strip()
        if prev_task and prev_task != task_num:
            from tasks.core import _extract_head_position, _extract_status
            prev_matches = list((agent_dir / "tasks").glob(f"{prev_task}-*/task.md"))
            if prev_matches:
                prev_file = prev_matches[0]
                prev_status = _extract_status(prev_file)
                prev_head = _extract_head_position(prev_file)
                if prev_head == "(all gates checked)" and not prev_status.startswith("done"):
                    # F14 blind-judge Finding 1 (the class, not a light-only
                    # patch): this branch used to write `done` DIRECTLY — no
                    # risk check, no review evidence, no verify contract, no
                    # receipt. A policy-free second close path defeats the
                    # whole 1.5.0 evidence contract, and `work done` is
                    # supposed to be the ONLY thing that closes a task (the
                    # 1.4.7 principle). The switch now bounces to the real
                    # close; --force switches away leaving the task honestly
                    # open, never silently done.
                    if force:
                        print(f"--force: switching away from task {prev_task} "
                              "(all gates checked, NOT closed — left open; "
                              "close it with `tasks work done` later).")
                    else:
                        print(f"Task {prev_task} has all gates checked but is not closed.\n"
                              f"Close it properly first: tasks work done   "
                              "(runs the verify contract + close policy)\n"
                              f"Or switch anyway: tasks work {task_num} --force   "
                              f"(leaves {prev_task} open)", file=sys.stderr)
                        sys.exit(1)
                elif not prev_status.startswith("done") and not force:
                    # prev task still has open gates — don't silently abandon it.
                    _gate_bounce(prev_task, prev_file, f"switching to task {task_num}")
                    sys.exit(1)
                elif not prev_status.startswith("done"):
                    print(f"--force: switching away from task {prev_task} with open gates (left in_progress).")

        # Write task number to per-session current_state
        session_dir.mkdir(parents=True, exist_ok=True)
        session_state.write_text(f"{task_num}\n", encoding="utf-8")

        # Session GC runs in _gc_dead_sessions() at the CLI entry point — and
        # ALSO in scripts/session-start-hook, which sweeps the same directory at
        # every SessionStart (including `compact`). This comment used to name
        # only the Python one, which is part of how the hook's contradictory
        # mtime-only policy went unnoticed until task 027.
        #
        # NOTE: only ACTIVATION writes `current_state` — this line, the freehand
        # orchestrator arm, and prepare-merge's pointer rewrite. Nothing refreshes
        # it while the session works, so its mtime means "when the task was
        # activated" and is never a liveness signal. Deleting sessions by that
        # mtime is what task 027 fixed; don't reintroduce it.

        # Expand stubs on activation
        task_content = task_file.read_text(encoding="utf-8")
        import re as _stub_re
        stub_match = _stub_re.search(r'<!-- stub:(\w+) -->', task_content)
        if stub_match:
            stub_type = stub_match.group(1)
            # Extract user's Intent and Why sections before expanding
            def _extract_section(content, heading):
                pattern = rf'^## {heading}\n(.*?)(?=\n## |\Z)'
                m = _stub_re.search(pattern, content, _stub_re.MULTILINE | _stub_re.DOTALL)
                return m.group(1).strip() if m else ""

            user_intent = _extract_section(task_content, "Intent")
            user_why = _extract_section(task_content, "Why")
            user_refs = _extract_section(task_content, "References")

            # Render full template
            from tasks.template import render_template
            task_num_int = int(task_num)
            title = task_file.parent.name.split("-", 1)[1].replace("-", " ").title()
            full_content = render_template(num=task_num_int, title=title, task_type=stub_type)

            # F3: Append playbook role template (same as create_task)
            from tasks.core import _load_playbook
            role_template = _load_playbook(stub_type, project_path)
            if role_template:
                full_content += "\n" + role_template + "\n"

            # Inject preserved user content
            if user_intent:
                # F2: Try both placeholder variants (build + quick)
                for placeholder in [
                    "(what we want to achieve \u2014 the outcome, not the activity)",
                    "(one line \u2014 what to do and how to verify)",
                ]:
                    if placeholder in full_content:
                        full_content = full_content.replace(placeholder, user_intent)
                        break
            if user_why:
                full_content = full_content.replace(
                    "(why this matters now \u2014 urgency, context, what breaks if delayed)",
                    user_why,
                )
            # F1: Inject preserved references
            if user_refs and "(optional)" not in user_refs.lower():
                # Replace the default References content
                full_content = _stub_re.sub(
                    r'(## References\n).*?(?=\n---)',
                    f'## References\n{user_refs}',
                    full_content,
                    count=1,
                    flags=_stub_re.DOTALL,
                )

            # F8: standing gates land at expansion (a stub has no gates until
            # now) — same helper create_task uses, same LAST-gates guarantee.
            from tasks.core import append_standing_gates, load_config
            full_content, _sg_issues = append_standing_gates(
                full_content, load_config(project_path), task_num_int)
            for _msg in _sg_issues:
                print(f"[playbook] standing_gates: {_msg}", file=sys.stderr)

            task_file.write_text(full_content, encoding="utf-8")
            # Re-read for chat injection and display
            task_content = full_content
            print(f"Expanded stub to full {stub_type} template.")

        # Workflow rules — deferred from bootstrap to task activation
        from tasks.template import workflow_briefing
        print("=== WORKFLOW ===")
        print(workflow_briefing())
        print()

        # Capture recent chat messages into task.md
        recent_chat = _capture_recent_chat(project_path)
        if recent_chat:
            _inject_chat_into_task(task_file, recent_chat)
            print(f"Captured {len(recent_chat)} recent chat message(s) into References.")

        # Print the full task file
        print(task_file.read_text(encoding="utf-8").rstrip())

        # F21 (batch-6 finding): the parked lifecycle taught its markers only
        # at CLOSE (own-task items). The consumption moment — a new task picks
        # up an EARLIER task's parked item — had no nudge, so task 012 consumed
        # 010's parked guard and 010's entry still reads open. One line, at
        # activation, only when parked debt exists.
        from tasks.core import open_parked_items as _opi
        _parked_elsewhere = 0
        try:
            for _tf in sorted((agent_dir / "tasks").glob("*/task.md")):
                if _tf == task_file:
                    continue
                _parked_elsewhere += len(_opi(_tf.read_text(encoding="utf-8")))
        except OSError:
            pass
        if _parked_elsewhere:
            print(f"\nnote: {_parked_elsewhere} open parked item(s) in earlier "
                  "tasks (`tasks parked` to list). If THIS task picks one up, "
                  f"mark the source entry `[promoted → {task_num}]` so the "
                  "lifecycle shows it consumed.", flush=True)


    elif cmd == "new":
        # Parse --stub flag — position-independent, so a trailing
        # `tasks new feature name --stub` can't silently become Intent text
        # (gauntlet-155 wart: the flag was only honored in first position).
        is_stub = "--stub" in cmd_args
        if is_stub:
            cmd_args = [a for a in cmd_args if a != "--stub"]

        if len(cmd_args) < 2:
            print("Error: 'new' requires a type and a name", file=sys.stderr)
            print("Usage: tasks new [--stub] <type> <name> [intent...]", file=sys.stderr)
            from tasks.core import list_all_types
            all_types = list_all_types(find_project_root())
            print(f"Types: {', '.join(all_types)}", file=sys.stderr)
            sys.exit(1)

        task_type = cmd_args[0]
        from tasks.core import list_all_types, _find_custom_playbook
        project_path_for_check = find_project_root()
        is_custom = _find_custom_playbook(project_path_for_check, task_type) is not None
        if task_type not in PLAYBOOKS and task_type not in ("quick", "light") and not is_custom:
            all_types = list_all_types(project_path_for_check)
            print(f"Error: unknown type '{task_type}'", file=sys.stderr)
            print(f"Types: {', '.join(all_types)}", file=sys.stderr)
            sys.exit(1)

        # args[1] = name, args[2:] = optional intent text
        task_name = cmd_args[1]
        intent_text = " ".join(cmd_args[2:]) if len(cmd_args) > 2 else None
        project_path = find_project_root()

        # Check if user included a task number prefix
        import re as _re
        from tasks.core import _next_task_number
        num_match = _re.match(r'^(\d{3})-(.+)$', task_name)
        if num_match:
            provided_num = int(num_match.group(1))
            tasks_dir = resolve_agent_dir(project_path) / "tasks"
            next_num = _next_task_number(tasks_dir)
            if provided_num == next_num:
                # Matches next number - strip it (user was explicit)
                task_name = num_match.group(2)
            else:
                print(f"Error: provided task number {provided_num:03d} doesn't match next number {next_num:03d}", file=sys.stderr)
                print(f"Usage: tasks new {task_type} {num_match.group(2)}", file=sys.stderr)
                sys.exit(1)
        task_file = create_task(project_path, task_name, task_type=task_type,
                               intent_text=intent_text, stub=is_stub)
        pattern_name = PLAYBOOKS.get(task_type, f"custom ({task_type})")

        import re
        task_num_match = re.match(r'^(\d+)-', task_file.parent.name)
        task_num = task_num_match.group(1) if task_num_match else "?"

        print(f"Created: {task_file.relative_to(project_path)}")
        if is_stub:
            print(f"Stub ({pattern_name}) — expand with: tasks work {task_num}")
        elif task_type not in ("quick", "light"):
            print(f"Pattern: {pattern_name}")
            print(f"Next: fill in task.md gates, then ask user to run: tasks work {task_num}")
        else:
            print(f"Next: fill in task.md gates, then ask user to run: tasks work {task_num}")
        print()

        # quick/light skip the full playbook-guide dump — shedding that
        # ceremony is their reason to exist (light still routes review by
        # risk; see the template's Risk Routing gate).
        if task_type not in ("quick", "light"):
            # Print full playbook so agent has workflow guidance inline
            playbook_path = _find_playbook_skill(project_path)
            if playbook_path:
                playbook_file = Path(playbook_path)
                if playbook_file.exists():
                    print("=== PLAYBOOK (task.md design guide) ===")
                    print("Use this to improve your task.md: select patterns and gates as appropriate,")
                    print("or invent new ones. This is a starting point — expand as needed.")
                    print()
                    content = playbook_file.read_text(encoding="utf-8")
                    # Strip sections not relevant to task design
                    for marker in ["## Mind Map", "> Evidence base:"]:
                        idx = content.find(marker)
                        if idx > 0:
                            content = content[:idx]
                    print(content.rstrip())
                    print()
                    print(f"Now fill in {task_file.relative_to(project_path)} — design a good task.md.")

    elif cmd == "init":
        # Parse provider-specific init flags (additive on top of normal init)
        provider = None
        install_provider_hooks = False
        remaining_init_args = []
        i = 0
        while i < len(cmd_args):
            if cmd_args[i] == "--provider" and i + 1 < len(cmd_args):
                provider = cmd_args[i + 1]
                i += 2
            elif cmd_args[i] == "--hooks":
                install_provider_hooks = True
                i += 1
            else:
                remaining_init_args.append(cmd_args[i])
                i += 1
        cmd_args = remaining_init_args

        # Target directory: argument or cwd
        target = Path(cmd_args[0]).resolve() if cmd_args else Path.cwd()
        if not target.exists():
            print(f"Error: directory not found: {target}", file=sys.stderr)
            sys.exit(1)

        title = target.name.replace("-", " ").replace("_", " ").title()
        print(f"Initializing project: {target.name}")

        # Refuse on the fresh-clone shape rather than mint a phantom root lane.
        require_lane_marker(target, "tasks init")
        # Create .agent/tasks/ (or .agent/<user>/tasks/ in multi-user mode)
        tasks_dir = resolve_agent_dir(target) / "tasks"
        existed = tasks_dir.exists()
        tasks_dir.mkdir(parents=True, exist_ok=True)
        print(f"  {tasks_dir.relative_to(target)}  {'exists' if existed else 'created'}")

        # Create MIND_MAP.md
        mind_map = target / "MIND_MAP.md"
        if not mind_map.exists():
            mind_map.write_text(f"""# {title}

## Architecture

(describe your project architecture here)
""", encoding="utf-8")
            print("  MIND_MAP.md    created")
        else:
            print("  MIND_MAP.md    exists")

        # Create CLAUDE.md
        claude_md = target / "CLAUDE.md"
        if not claude_md.exists():
            from tasks.template import claude_md as claude_md_template
            claude_md.write_text(claude_md_template(title), encoding="utf-8")
            print("  CLAUDE.md      created")
        else:
            print("  CLAUDE.md      exists")

        # Check for duplicate hook registrations
        settings_file = target / ".claude" / "settings.json"
        if settings_file.exists():
            import json
            try:
                settings = json.loads(settings_file.read_text(encoding="utf-8"))
                if "hooks" in settings:
                    hook_events = list(settings["hooks"].keys())
                    print(f"  ⚠ .claude/settings.json has local hook registrations: {', '.join(hook_events)}")
                    print(f"    These may duplicate plugin hooks (hooks/hooks.json) — causing double writes.")
                    print(f"    Fix: remove the 'hooks' key from .claude/settings.json")
            except (json.JSONDecodeError, KeyError):
                pass

        # Check for stale .claude/hooks/ directory
        local_hooks = target / ".claude" / "hooks"
        if local_hooks.is_dir():
            hook_files = [f.name for f in local_hooks.iterdir() if f.is_file()]
            if hook_files:
                print(f"  ⚠ .claude/hooks/ contains {len(hook_files)} hook scripts: {', '.join(hook_files)}")
                print(f"    These are stale copies — canonical hooks live in scripts/ (resolved via plugin).")
                print(f"    Fix: remove .claude/hooks/ directory")

        # --provider: install provider-specific bootstrap file (additive)
        if provider:
            _PROVIDER_MAP = {"codex": "CodexAdapter", "antigravity": "AntigravityAdapter", "pi": "PiAdapter", "grok": "GrokAdapter"}
            if provider not in _PROVIDER_MAP:
                print(f"Error: unknown provider '{provider}'. Choose: codex, antigravity, grok, pi", file=sys.stderr)
                sys.exit(1)
            import importlib
            adapter_cls_name = _PROVIDER_MAP[provider]
            mod = importlib.import_module(f"provider.adapters.{provider}")
            adapter_cls = getattr(mod, adapter_cls_name)
            bootstrap_file = {"codex": "AGENTS.md", "antigravity": "GEMINI.md", "pi": "AGENTS.md", "grok": "AGENTS.md"}[provider]
            bs_path = target / bootstrap_file
            already_existed = bs_path.exists()
            adapter = adapter_cls("init", target)
            adapter.install_bootstrap(target)
            print(f"  {bootstrap_file:<15}{'exists' if already_existed else 'created'}")
            # Grok: always install global enforcement hooks (task 020). On spaced
            # project paths, project/plugin hooks never schedule — the always-
            # trusted ~/.grok/hooks/playbook-enforcement.json is the only reliable
            # channel. --hooks remains required for other providers.
            if install_provider_hooks or provider == "grok":
                adapter.install_hooks(target)
                if provider == "grok" and not install_provider_hooks:
                    print("  grok hooks   auto-installed (required on Grok; pass --hooks to be explicit)")
        elif install_provider_hooks:
            print("Error: --hooks requires --provider codex, antigravity, grok, or pi", file=sys.stderr)
            sys.exit(1)

    elif cmd == "bootstrap":
        project_path = find_project_root()

        # Identity preamble
        from tasks.template import identity_preamble, mind_map_header
        print(identity_preamble())
        print()

        # Mind Map — full dump with navigation header
        mm_content = _load_mind_map(project_path)
        if mm_content:
            print("=== MIND MAP (MIND_MAP.md) ===")
            print(mind_map_header())
            print()
            print(mm_content.rstrip())
            print()

        # Pending tasks
        print("=== PENDING TASKS ===")
        list_tasks(project_path, pending_only=True)

        # Judge-pin nudge (task 012): covers projects that predate the models
        # maintenance loop. Presence check only — no probes at session start.
        if not (project_path / ".agent" / "models.json").exists():
            print()
            print("NOTE: no .agent/models.json — judge panel uses the plugin's shipped")
            print("defaults, which drift as providers retire models. Relay to the user:")
            print("pin per-machine judges via `tasks models check` + `tasks models select`.")

        # README drift nudge (task 017): maintainer-only — silently a no-op
        # outside a plugin source checkout / dogfood workspace. Advisory, so
        # bootstrap must never crash on it.
        try:
            from tasks.readme_drift import readme_drift
            _drift = readme_drift(project_path)
            if _drift:
                print()
                for _msg in _drift:
                    print(f"NOTE: {_msg}")
        except Exception:
            pass

        # CLI reference — shown last so mind map + tasks aren't buried
        from tasks.template import cli_reference
        print()
        print("=== CLI REFERENCE ===")
        print(cli_reference())

    elif cmd in ("list", "ls"):
        project_path = find_project_root()
        pending_only = "--pending" in cmd_args
        list_tasks(project_path, pending_only=pending_only)

    elif cmd == "panel-review":
        import subprocess
        from concurrent.futures import ProcessPoolExecutor, TimeoutError as FuturesTimeout

        # Parse flags
        review_mode = "plan"
        web_search = False
        timeout_flag = None  # --timeout HARD override (raw str); resolved from config below
        soft_timeout_flag = None  # --soft-timeout SOFT override (prompt wind-down)
        budget_flag = None   # --budget override (claude judges only)
        extra_prompt = ""
        no_mind_map = False
        bare = False
        models_flag = None  # --models CSV → explicit judge set for this run
        remaining_args = []
        i = 0
        while i < len(cmd_args):
            if cmd_args[i] == "--mode" and i + 1 < len(cmd_args):
                review_mode = cmd_args[i + 1]
                i += 2
            elif cmd_args[i] == "--models" and i + 1 < len(cmd_args):
                models_flag = [s.strip() for s in cmd_args[i + 1].split(",") if s.strip()]
                i += 2
            elif cmd_args[i] == "--web-search":
                web_search = True
                i += 1
            elif cmd_args[i] == "--timeout" and i + 1 < len(cmd_args):
                timeout_flag = cmd_args[i + 1]
                i += 2
            elif cmd_args[i] == "--soft-timeout" and i + 1 < len(cmd_args):
                soft_timeout_flag = cmd_args[i + 1]
                i += 2
            elif cmd_args[i] == "--budget" and i + 1 < len(cmd_args):
                budget_flag = cmd_args[i + 1]
                i += 2
            elif cmd_args[i] == "--prompt" and i + 1 < len(cmd_args):
                extra_prompt = cmd_args[i + 1]
                i += 2
            elif cmd_args[i] == "--no-mind-map":
                no_mind_map = True
                i += 1
            elif cmd_args[i] == "--bare":
                bare = True
                i += 1
            else:
                remaining_args.append(cmd_args[i])
                i += 1

        if review_mode not in ("plan", "impl"):
            print(f"Error: unknown mode '{review_mode}'", file=sys.stderr)
            sys.exit(1)

        task_num = remaining_args[0] if remaining_args else ""
        if task_num.isdigit():
            task_num = task_num.zfill(3)

        # Task number is optional; --prompt required when omitted
        if not task_num and not extra_prompt:
            print("Error: 'panel-review' requires a task number or --prompt", file=sys.stderr)
            print("Usage: tasks panel-review [<number>] [--mode plan|impl] [--models codex:gpt-5.5,agy,...] [--prompt \"...\"] [--no-mind-map] [--bare] [--web-search] [--timeout SECONDS] [--soft-timeout SECONDS] [--budget USD]", file=sys.stderr)
            sys.exit(1)

        project_path = find_project_root()
        # Review knobs — precedence: --flag > env var > .agent/config.json > default,
        # with config acting as a FLOOR on the hard timeout. Hard = hang-safety
        # kill; soft = the deadline the judge is told to self-regulate against.
        from tasks.core import (
            format_soft_hard_timeout_label,
            format_timeout_label,
            resolve_judge_budget,
            resolve_review_soft_timeout,
            resolve_review_timeout,
        )
        timeout_secs = resolve_review_timeout(project_path, timeout_flag)
        soft_timeout_secs = resolve_review_soft_timeout(
            project_path, hard_timeout_secs=timeout_secs, cli_value=soft_timeout_flag,
        )
        panel_budget = resolve_judge_budget(project_path, budget_flag)

        # Resolve task file if task number given
        task_file = None
        task_path = None
        if task_num:
            tasks_dir = resolve_agent_dir(project_path) / "tasks"
            matches = list(tasks_dir.glob(f"{task_num}-*/task.md"))
            if not matches:
                print(f"Task {task_num} not found", file=sys.stderr)
                sys.exit(1)
            task_file = matches[0]
            task_path = str(task_file.relative_to(project_path))
            # P6 nudge: mechanically-detectable issues shouldn't cost judge
            # tokens — and 'an audit ran once' is not freshness: the note also
            # fires when the newest receipt's commit is not HEAD.
            from tasks.audit import audit_freshness_note
            try:
                _panel_head = subprocess.run(
                    ["git", "rev-parse", "HEAD"], cwd=project_path,
                    capture_output=True, text=True).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                _panel_head = ""
            _audit_note = audit_freshness_note(
                task_file.read_text(encoding="utf-8"), _panel_head)
            if _audit_note:
                print(f"  note: {_audit_note}", file=sys.stderr, flush=True)

        from tasks.template import panel_plan_review_prompt, panel_impl_review_prompt

        # Build context PER TRANSPORT (1.5.3). stdin seats (claude/codex) have no
        # OS argv limit, so they get the high budget — an argv seat's ceiling
        # must not dictate what a stdin seat is allowed to read. Every truncation
        # is receipted (C3/P3) per transport, and a trimmed seat's PROMPT names
        # what it did not receive and where the full file lives.
        from tasks.core import (load_config, resolve_review_context_chars,
                                select_task_context)

        def _build_payload(budget: int) -> dict:
            parts, receipts = [], []
            trim_notice = ""
            if not bare:
                if not no_mind_map:
                    mm_content = _load_mind_map(project_path)
                    if mm_content:
                        parts.append(f"=== MIND_MAP.md ===\n{mm_content}")
                if task_file:
                    task_content = task_file.read_text(encoding="utf-8")
                    task_content, task_receipt = select_task_context(
                        task_content, budget // 2)
                    if task_receipt:
                        receipts.append(task_receipt)
                        # Aliased on purpose: other arms' local `import re`
                        # statements make bare `re` a local of main() for the
                        # WHOLE function, so this path (only reached when the
                        # task was actually trimmed) crashed UnboundLocal from
                        # 1.5.3 until the 1.5.9 judge caught it.
                        import re as _re
                        _dm = _re.search(r"· dropped: (.+?)(?: · WARNING|$)", task_receipt)
                        _dropped = (_dm.group(1)[:200] if _dm else "some sections")
                        trim_notice = (
                            f"your inline copy of {task_path} was TRIMMED to fit "
                            f"your transport (dropped sections: {_dropped}) — read "
                            f"{task_path} in the repo for the full trace.")
                    parts.append(f"=== {task_path} ===\n{task_content}")
                else:
                    # Taskless: include recent chat log as project context
                    chat_log = resolve_agent_dir(project_path) / "chat_log.md"
                    if chat_log.exists():
                        chat_content = chat_log.read_text(encoding="utf-8", errors="replace")
                        max_chat = budget // 2
                        if len(chat_content) > max_chat:
                            receipts.append(
                                f"chat_log.md {len(chat_content):,} → {max_chat:,} chars "
                                "(kept most recent tail)")
                            chat_content = "[... older chat elided ...]\n\n" + chat_content[-max_chat:]
                        parts.append(f"=== .agent/chat_log.md (recent) ===\n{chat_content}")
            sc = "\n\n".join(parts)
            if len(sc) > budget:
                receipts.append(
                    f"combined system context {len(sc):,} → {budget:,} chars "
                    "(head-clamped — mind map is large; raise headroom or use "
                    "--no-mind-map)")
                sc = sc[:budget] + "\n\n[... truncated ...]"
            return {"context": sc, "receipts": receipts, "trim_notice": trim_notice}

        _argv_budget = resolve_review_context_chars(project_path, stdin=False)
        _stdin_budget = resolve_review_context_chars(project_path, stdin=True)
        payloads = {"argv": _build_payload(_argv_budget)}
        payloads["stdin"] = (payloads["argv"] if _stdin_budget == _argv_budget
                             else _build_payload(_stdin_budget))
        for _tname in ("stdin", "argv"):
            if payloads[_tname]["receipts"]:
                print(f"  context[{_tname}]: " + " | ".join(payloads[_tname]["receipts"]),
                      file=sys.stderr, flush=True)

        # Judge execution L1: the project may declare commands safe to run in the
        # judge's read-only sandbox. Undeclared → clause absent, pre-1.5.3 prompt.
        _jv_raw = load_config(project_path).get("judge_verify")
        judge_verify_cmds = ([c for c in _jv_raw if isinstance(c, str) and c.strip()]
                             if isinstance(_jv_raw, list) else [])

        # Prompt strategy: bare/taskless → extra_prompt is full mission; with task → review prompt + optional steering
        if task_file:
            prompt_fn = panel_plan_review_prompt if review_mode == "plan" else panel_impl_review_prompt
            review_label = "plan review" if review_mode == "plan" else "impl review"
        else:
            prompt_fn = None
            review_label = "panel"

        # Output path: task dir when task given, agent_dir/ otherwise
        if task_file:
            judge_md = task_file.parent / "judge.md"
        else:
            agent_dir = resolve_agent_dir(project_path)
            agent_dir.mkdir(exist_ok=True)
            judge_md = agent_dir / "judge.md"

        # Discover available judges via adapter classes — each adapter declares
        # its own binary_name() and panel_variants(). Adding a new provider is
        # a one-line append to PANEL_ADAPTERS; no dispatch changes needed.
        from provider.adapters.claude import ClaudeAdapter
        from provider.adapters.codex import CodexAdapter
        from provider.adapters.antigravity import AntigravityAdapter
        from provider.adapters.pi import PiAdapter
        from provider.adapters.grok import GrokAdapter
        from provider.sandbox import load_judge_config, resolve_judge_spec
        PANEL_ADAPTERS = (ClaudeAdapter, CodexAdapter, AntigravityAdapter, GrokAdapter, PiAdapter)
        _JUDGE_ADAPTERS = {
            "claude": ClaudeAdapter, "codex": CodexAdapter,
            "agy": AntigravityAdapter, "pi": PiAdapter,
            "grok": GrokAdapter,
        }

        # Judge-set precedence: --models flag → models.json `panel` (shipped ⊕
        # project .agent/models.json) → legacy full fan-out (only if no config).
        if models_flag is not None:
            spec_names = models_flag
        else:
            spec_names = load_judge_config().get("panel") or None

        judges = []  # list of (adapter_cls, variant)
        if spec_names:
            skipped = []
            for nm in spec_names:
                try:
                    provider, variant = resolve_judge_spec(nm)
                except ValueError as e:
                    print(f"Error: {e}", file=sys.stderr)
                    sys.exit(1)
                cls = _JUDGE_ADAPTERS.get(provider)
                if cls is None:
                    print(f"Error: no adapter for provider '{provider}' (spec '{nm}')", file=sys.stderr)
                    sys.exit(1)
                if cls.is_available():
                    judges.append((cls, variant))
                else:
                    skipped.append(f"{nm} ({cls.binary_name()} not on PATH)")
            if skipped:
                print(f"  Skipped unavailable: {', '.join(skipped)}", flush=True)
        else:
            # No configured panel — legacy discovery (all providers × variants).
            for cls in PANEL_ADAPTERS:
                if cls.is_available():
                    for variant in cls.panel_variants():
                        judges.append((cls, variant))

        if not judges:
            print("Error: no available judges. Install a provider CLI, or name "
                  "reachable ones with --models (e.g. --models codex:gpt-5.5,agy).",
                  file=sys.stderr)
            sys.exit(1)

        display_target = task_path or "(promptless)"
        timeout_label = format_soft_hard_timeout_label(soft_timeout_secs, timeout_secs)
        hard_timeout_label = format_timeout_label(timeout_secs)
        print(
            f"Running panel {review_label} on {display_target} "
            f"({len(judges)} judges, timeout {timeout_label})...",
            flush=True,
        )

        def run_judge(judge_spec):
            adapter_cls, variant = judge_spec
            provider_name = adapter_cls.binary_name()
            label = f"{provider_name}:{variant}" if variant else provider_name
            # Per-transport payload (1.5.3): each seat gets the biggest context
            # its transport can carry; a trimmed seat's prompt says what was cut.
            _payload = payloads.get(adapter_cls.context_transport(), payloads["argv"])
            if prompt_fn:
                prompt = prompt_fn(
                    task_path,
                    inline_context=(provider_name != "claude"),
                    soft_timeout_secs=soft_timeout_secs,
                    hard_timeout_secs=timeout_secs,
                    trim_notice=_payload["trim_notice"],
                    judge_verify=judge_verify_cmds,
                )
                if extra_prompt:
                    prompt += f"\n\nAdditional steering from the user:\n{extra_prompt}"
            else:
                # Taskless / --bare: still prepend the soft-deadline steering when
                # there is a soft budget, so free-form panel prompts self-regulate too.
                from tasks.template import time_budget_instruction
                steer = time_budget_instruction(soft_timeout_secs, timeout_secs)
                prompt = (steer + "\n\n" + extra_prompt) if steer else extra_prompt

            # Single attempt only — never restart a long-running judge. The
            # hard timeout is hang safety alone (None = no kill at all).
            try:
                adapter = adapter_cls(session_id="judge", project_root=project_path)
                output = adapter.run_headless_judge(
                    prompt=prompt,
                    model=variant,
                    system_context=_payload["context"],
                    web_search=web_search,
                    timeout_secs=timeout_secs,
                    budget_usd=panel_budget,
                )
                return label, output
            except subprocess.TimeoutExpired as expired:
                # Keep whatever this seat had written. The marker stays FIRST so
                # the seat is still classified as failed and no reader mistakes a
                # truncated block for a finished review — but a seat that spent
                # the whole budget should still contribute what it found.
                raw = getattr(expired, "stdout", None) or getattr(expired, "output", None) or ""
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                raw = raw.strip()
                marker = f"(timed out after hard {hard_timeout_label})"
                if raw:
                    return label, (
                        f"{marker}\n\n**INCOMPLETE** — killed mid-response; the "
                        f"findings below may be cut off and reached no conclusion:"
                        f"\n\n{raw}"
                    )
                return label, marker
            except Exception as e:
                return label, f"(error: {e})"

        # Judge tamper guard (#1): judges are read-only evaluators, so snapshot
        # the repo before spawning and refuse to trust the run if the working
        # tree changed under them. On uncontained platforms (no seatbelt/bwrap,
        # or nested) project_writable=False was a no-op — this snapshot is then
        # the ONLY defense, so warn.
        from provider import sandbox as _sandbox_mod
        if not _sandbox_mod.containment_available():
            print("  ⚠ judges running UNCONTAINED (no usable OS sandbox here) — "
                  "the tamper guard is the only defense against repo mutation.",
                  file=sys.stderr, flush=True)
        _tamper_before = _snapshot_repo_state(project_path, task_file)

        # Run all judges in parallel
        import concurrent.futures
        results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(judges)) as executor:
            futures = {executor.submit(run_judge, j): j for j in judges}
            for future in concurrent.futures.as_completed(futures):
                label, output = future.result()
                results[label] = output
                print(f"  [{label}] done", flush=True)

        _tamper_changes = _detect_tamper(project_path, task_file, _tamper_before)

        # Classify each judge as succeeded vs failed — a failed judge must NOT
        # read as a clean empty review (T139) or a successful one. Shared
        # predicate (task 012) also catches claude's budget-exhaustion message,
        # which arrives as exit-0 stdout and previously counted as success.
        from tasks.models_check import budget_exceeded, judge_failed as _judge_failed

        failed = {lbl for lbl, out in results.items() if _judge_failed(out)}
        over_budget = {lbl for lbl in failed if budget_exceeded(results[lbl])}
        succeeded = len(results) - len(failed)

        # Verdict, not just a count (C4/P7). A panel is a gate: resolve the
        # quorum against the judges that actually launched, decide PASS/FAIL, and
        # exit non-zero below it (at the end, after all diagnostics print). The
        # tamper hard-stop below still wins — a mutated tree fails regardless of
        # how many judges succeeded.
        from tasks.core import resolve_panel_quorum
        panel_quorum = resolve_panel_quorum(project_path, len(results))
        panel_passed = succeeded >= panel_quorum
        verdict_reason = (
            f"{succeeded}/{len(results)} judges succeeded, quorum {panel_quorum}"
            + ("" if panel_passed else " — below quorum")
        )
        verdict_banner = (
            f"**PANEL VERDICT: {'PASS' if panel_passed else 'FAIL'}** — {verdict_reason}\n"
        )

        # Write judge.md (path already set above based on task_file presence)
        display_label = task_path or extra_prompt[:60]
        lines = [f"# Panel {review_label.title()} — {display_label}\n", verdict_banner]
        # Tamper banner rides directly under the round heading (#1) — the reading
        # agent must meet it before any finding, and the heading must stay FIRST
        # because judge.md stacks rounds by `# Panel …` headings (1.5.3). The
        # file is still written (paid verdicts are never discarded), but the run
        # exits non-zero below.
        if _tamper_changes:
            lines.insert(1, _tamper_banner(_tamper_changes) + "\n")
        lines.append(f"**Judges:** {succeeded}/{len(results)} succeeded | **Quorum:** {panel_quorum} | **Web search:** {'yes' if web_search else 'no'} | **Timeout:** {timeout_label}\n")
        # Context receipt (C3/P3), per transport (1.5.3): each seat saw what its
        # line says it saw — nothing was dropped without being named.
        _seats_by_transport = {"stdin": [], "argv": []}
        for _cls, _var in judges:
            _lbl = f"{_cls.binary_name()}:{_var}" if _var else _cls.binary_name()
            _seats_by_transport.setdefault(_cls.context_transport(), []).append(_lbl)
        for _tname in ("stdin", "argv"):
            _seats = _seats_by_transport.get(_tname) or []
            if not _seats:
                continue
            _r = payloads[_tname]["receipts"]
            _desc = " | ".join(_r) if _r else "full task.md + mind map delivered (no truncation)"
            lines.append(f"**Context[{_tname}: {', '.join(sorted(_seats))}]:** {_desc}\n")
        # Commit + tree-state stamps: name WHICH code state this panel reviewed.
        # The fingerprint (content-based, .agent-excluded) is what the close
        # gate's freshness advisory compares against — mtimes lie, content
        # doesn't.
        from tasks.core import tree_state_fingerprint
        try:
            _judged_head = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=project_path,
                capture_output=True, text=True).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            _judged_head = ""
        if _judged_head:
            lines.append(f"**Commit:** {_judged_head}\n")
        _fp = tree_state_fingerprint(project_path)
        if _fp:
            lines.append(f"**Tree-state:** {_fp}\n")
        if failed:
            lines.append(f"**⚠ Failed judges:** {', '.join(sorted(failed))} — see their blocks below for the exit code / stderr. NOT a clean empty review.\n")
        if over_budget:
            lines.append(f"**⚠ Budget-capped judges:** {', '.join(sorted(over_budget))} — hit the ${panel_budget} cap and produced no review. Raise `judge_budget_usd` in `.agent/config.json` or pass `--budget`.\n")
        lines.append("\n")
        # Triage frame (T124): prepend the pushback discipline AT THE TOP so
        # the reading agent meets the instruction BEFORE the per-judge
        # findings — primes the triage lens before the data is read.
        # The judges themselves never see this; it's bundled with their
        # outputs purely for the reading agent. Mirrors the in-task pushback
        # gate from template.judge_section / judge_impl_section, but for
        # panel reviews (where findings live in judge.md, not task.md) the
        # discipline rides with the data. Helper is unit-tested in tests/test_cli.py.
        lines.extend(_panel_triage_frame())
        for label in sorted(results.keys()):
            tag = "  [FAILED]" if label in failed else ""
            lines.append("═" * 60)
            lines.append(f"  JUDGE: {label}{tag}")
            lines.append("═" * 60 + "\n")
            lines.append(results[label].strip())
            lines.append("\n\n")
        # Stack, never clobber (1.5.3): a re-run panel must not destroy the
        # previous round's verdicts. Newest round first; the close gate reads
        # only the newest round's mode+verdict.
        from tasks.core import stack_judge_round
        stack_judge_round(judge_md, "\n".join(lines))
        summary = (f"\nPANEL {'PASS' if panel_passed else 'FAIL'}: {verdict_reason}"
                   f"\nSaved: {judge_md.relative_to(project_path)}")
        if failed:
            summary += f"; FAILED: {', '.join(sorted(failed))}"
        if over_budget:
            summary += (f"\nBudget notice: {', '.join(sorted(over_budget))} hit the "
                        f"${panel_budget} cap — raise judge_budget_usd in "
                        f".agent/config.json or pass --budget to re-run them.")
        print(summary, flush=True)

        # Tamper hard-stop (#1): a judge mutated the working tree. judge.md is
        # already written (with the banner on top) so verdicts aren't lost, but
        # the run exits non-zero and the operator must NOT ingest it into task.md.
        if _tamper_changes:
            print("\n" + _tamper_banner(_tamper_changes), file=sys.stderr, flush=True)
            sys.exit(1)

        # Hard stop on probe-confirmed dead pins (task 012). Pattern
        # classification alone is only a hint (failure tails can echo prompt
        # fragments containing the very same signatures); a live probe of the
        # exact failed spec is what triggers exit 1. judge.md is already
        # written above, so the review is never lost. Timeout/budget/other
        # failures keep the soft behavior (exit 0 fall-through).
        if failed:
            from tasks.models_check import (
                NEEDS_CLI_UPGRADE, apply_confirmed, check_pins,
                confirm_dead_specs, render_report,
            )
            label_provider = {}
            for adapter_cls, variant in judges:
                provider_name = adapter_cls.binary_name()
                lbl = f"{provider_name}:{variant}" if variant else provider_name
                label_provider[lbl] = (provider_name, variant)
            confirmed = confirm_dead_specs(
                {lbl: results[lbl] for lbl in failed}, label_provider)
            if confirmed:
                print("\nHARD STOP: judge pin(s) unavailable (probe-confirmed):", file=sys.stderr)
                for lbl in sorted(confirmed):
                    pv, detail = confirmed[lbl]
                    fix = ("upgrade the codex CLI (`codex update`)"
                           if pv == NEEDS_CLI_UPGRADE
                           else "re-select the panel (`tasks models select`)")
                    print(f"  {lbl}: {pv} — {detail} → {fix}", file=sys.stderr)
                print("\nCurrent availability:", file=sys.stderr)
                report = apply_confirmed(
                    check_pins(project_path, probe=False, extra_specs=sorted(confirmed)),
                    confirmed)
                print(render_report(report), file=sys.stderr)
                print("\nReview saved to judge.md but the panel is degraded — "
                      "decide how to proceed before re-running.", file=sys.stderr)
                sys.exit(1)

        # Quorum gate (C4/P7): below the required number of succeeding judges the
        # panel is a FAIL, not a report. judge.md leads with the same verdict and
        # is already written, so nothing is lost — but the run exits non-zero so a
        # caller (or the reviewing agent) cannot mistake a 4/7 panel for a pass.
        if not panel_passed:
            print(f"\nPANEL FAIL: {verdict_reason}. Raise the panel (fix/rerun the "
                  "failed seats) or set `panel_quorum` in .agent/config.json if this "
                  "bar is wrong for the project.", file=sys.stderr, flush=True)
            sys.exit(1)

    elif cmd == "models":
        # Model-availability discovery + panel selection (task 012).
        # `tasks models check [--no-probe]` audits every models.json pin;
        # `tasks models select [--no-probe]` interactively rewrites the panel.
        from tasks.models_check import cli_models
        sys.exit(cli_models(cmd_args, find_project_root()))

    elif cmd in ("plan-review", "impl-review", "judge"):
        # "judge" is a legacy alias — auto-detects mode from task status
        review_cmd = cmd
        if not cmd_args:
            print(f"Error: '{review_cmd}' requires a task number", file=sys.stderr)
            print(f"Usage: tasks {review_cmd} <number> [--backend codex|claude|agy|grok|pi] [--model <variant>] [--prompt \"...\"] [--timeout SECONDS] [--budget USD]  (default backend: models.json default_judge, ships codex; --budget is claude-only)", file=sys.stderr)
            sys.exit(1)

        import subprocess

        # Parse flags
        backend = None   # explicit --backend; else from models.json default_judge
        model = None     # explicit --model (variant within the backend)
        extra_prompt = ""
        timeout_flag = None   # --timeout N  HARD (overrides env / config / default)
        soft_timeout_flag = None  # --soft-timeout N  SOFT (prompt wind-down)
        budget_flag = None    # --budget N   (claude only; overrides env / config / default)
        remaining_args = []
        i = 0
        while i < len(cmd_args):
            if cmd_args[i] == "--backend" and i + 1 < len(cmd_args):
                backend = cmd_args[i + 1]
                i += 2
            elif cmd_args[i] == "--model" and i + 1 < len(cmd_args):
                model = cmd_args[i + 1]
                i += 2
            elif cmd_args[i] == "--prompt" and i + 1 < len(cmd_args):
                extra_prompt = cmd_args[i + 1]
                i += 2
            elif cmd_args[i] == "--timeout" and i + 1 < len(cmd_args):
                timeout_flag = cmd_args[i + 1]
                i += 2
            elif cmd_args[i] == "--soft-timeout" and i + 1 < len(cmd_args):
                soft_timeout_flag = cmd_args[i + 1]
                i += 2
            elif cmd_args[i] == "--budget" and i + 1 < len(cmd_args):
                budget_flag = cmd_args[i + 1]
                i += 2
            else:
                remaining_args.append(cmd_args[i])
                i += 1

        # No --backend → models.json default_judge (provider or provider:variant,
        # project-overridable; ships as "codex" so headless review avoids the
        # metered claude -p path by default). --model overrides the variant.
        if backend is None:
            from provider.sandbox import load_judge_config, resolve_judge_spec
            dj = load_judge_config().get("default_judge") or "claude"
            try:
                backend, dj_variant = resolve_judge_spec(dj)
            except ValueError:
                backend, dj_variant = dj, None
            if model is None:
                model = dj_variant

        # Accept friendlier aliases: "agy"/"gemini" → "antigravity", "qwen" → "pi"
        if backend in ("agy", "gemini"):
            backend = "antigravity"
        elif backend == "qwen":
            backend = "pi"
        if backend not in ("claude", "codex", "antigravity", "grok", "pi"):
            print(f"Error: unknown backend '{backend}'", file=sys.stderr)
            print("Supported: codex (default), claude, antigravity (alias: agy), grok, pi (alias: qwen)", file=sys.stderr)
            sys.exit(1)

        if not remaining_args:
            print(f"Error: '{review_cmd}' requires a task number", file=sys.stderr)
            sys.exit(1)

        task_num = remaining_args[0]
        if task_num.isdigit():
            task_num = task_num.zfill(3)
        project_path = find_project_root()
        # Review knobs — precedence: --flag > env var > .agent/config.json >
        # built-in default (resolvers live in tasks.core), with config as a
        # FLOOR on the hard timeout. Hard = hang-safety kill; soft = the
        # deadline the judge is told to wind down against.
        from tasks.core import (
            format_soft_hard_timeout_label,
            format_timeout_label,
            resolve_judge_budget,
            resolve_review_soft_timeout,
            resolve_review_timeout,
        )
        review_timeout = resolve_review_timeout(project_path, timeout_flag)
        review_soft_timeout = resolve_review_soft_timeout(
            project_path, hard_timeout_secs=review_timeout, cli_value=soft_timeout_flag,
        )
        review_budget = resolve_judge_budget(project_path, budget_flag)
        review_timeout_label = format_timeout_label(review_timeout)
        review_soft_hard_label = format_soft_hard_timeout_label(
            review_soft_timeout, review_timeout,
        )
        tasks_dir = resolve_agent_dir(project_path) / "tasks"
        matches = list(tasks_dir.glob(f"{task_num}-*/task.md"))
        if not matches:
            print(f"Task {task_num} not found", file=sys.stderr)
            sys.exit(1)

        task_file = matches[0]
        task_path = str(task_file.relative_to(project_path))

        from tasks.template import plan_review_prompt, impl_review_prompt

        # Build context: mind map + task content, transport-aware (1.5.3) —
        # stdin backends (claude/codex) get the high budget, argv backends the
        # byte-guarded one. Structure-aware and receipted (C3/P3).
        from tasks.core import (load_config, resolve_review_context_chars,
                                select_task_context)
        _sj_stdin = backend in ("claude", "codex")
        MAX_CONTEXT_CHARS = resolve_review_context_chars(project_path, stdin=_sj_stdin)
        context_parts = []
        context_receipts = []
        sj_trim_notice = ""
        mm_content = _load_mind_map(project_path)
        if mm_content:
            context_parts.append(f"=== MIND_MAP.md ===\n{mm_content}")
        task_content = task_file.read_text(encoding="utf-8")
        task_content, task_receipt = select_task_context(task_content, MAX_CONTEXT_CHARS // 2)
        if task_receipt:
            context_receipts.append(task_receipt)
            # Aliased for the same reason as the panel arm's trim-notice site:
            # bare `re` is an unbound main() local on this path (judge F1).
            import re as _re
            _dm = _re.search(r"· dropped: (.+?)(?: · WARNING|$)", task_receipt)
            sj_trim_notice = (
                f"your inline copy of {task_path} was TRIMMED "
                f"(dropped sections: {(_dm.group(1)[:200] if _dm else 'some sections')}) — "
                f"read {task_path} in the repo for the full trace.")
        context_parts.append(f"=== {task_path} ===\n{task_content}")
        system_context = "\n\n".join(context_parts)
        if len(system_context) > MAX_CONTEXT_CHARS:
            context_receipts.append(
                f"combined system context {len(system_context):,} → {MAX_CONTEXT_CHARS:,} "
                "chars (head-clamped)")
            system_context = system_context[:MAX_CONTEXT_CHARS] + "\n\n[... truncated for context budget ...]"
        if context_receipts:
            print("  context: " + " | ".join(context_receipts), file=sys.stderr, flush=True)
        _jv_raw = load_config(project_path).get("judge_verify")
        sj_judge_verify = ([c for c in _jv_raw if isinstance(c, str) and c.strip()]
                           if isinstance(_jv_raw, list) else [])

        # Determine mode: explicit from command, or auto-detect for legacy "judge"
        if review_cmd == "plan-review":
            review_mode = "plan"
        elif review_cmd == "impl-review":
            review_mode = "impl"
        else:  # legacy "judge" — auto-detect from status
            from tasks.core import _extract_status
            review_mode = "impl" if _extract_status(task_file).startswith("done") else "plan"

        _base_prompt_fn = plan_review_prompt if review_mode == "plan" else impl_review_prompt

        def prompt_fn(task_path_arg, inline_context=False):
            return _base_prompt_fn(
                task_path_arg,
                inline_context=inline_context,
                soft_timeout_secs=review_soft_timeout,
                hard_timeout_secs=review_timeout,
                trim_notice=sj_trim_notice,
                judge_verify=sj_judge_verify,
            )

        review_label = "plan review" if review_mode == "plan" else "impl review"

        def _bail_review_timeout(expired=None):
            # Only reachable when a finite HARD timeout is in force.
            #
            # Whatever the judge had already written is salvaged to a SEPARATE
            # `*.partial.log` rather than being dropped or overwriting the main
            # log. Both halves of that matter: a review killed at its ceiling has
            # usually produced most of its findings, and spending the full budget
            # to be handed nothing is the worst outcome; but a partial review must
            # never be mistaken for a complete one, nor replace a previous good
            # review. So: new file, explicit banner, still exit nonzero.
            partial = ""
            if expired is not None:
                raw = getattr(expired, "stdout", None) or getattr(expired, "output", None) or ""
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                partial = raw.strip()
            saved_note = ""
            if partial:
                partial_log = task_file.parent / (
                    _judge_log_name(backend).removesuffix(".log") + ".partial.log")
                partial_log.write_text(
                    f"# INCOMPLETE {review_label} — the judge was killed at the hard "
                    f"timeout ({review_timeout_label}) mid-response.\n"
                    f"# This is what it had written by then. It is NOT a finished "
                    f"review: findings may be cut off and it reached no conclusion.\n"
                    f"# The previous complete review, if any, is untouched in "
                    f"{_judge_log_name(backend)}.\n\n{partial}\n",
                    encoding="utf-8",
                )
                saved_note = (f" Partial output ({len(partial)} chars) saved to "
                              f"{partial_log.relative_to(project_path)}.")
            else:
                saved_note = " The judge produced no output before the kill."
            print(
                f"\n{review_label} hit hard timeout after {review_timeout_label} "
                f"({review_soft_hard_label}). Raise the hard kill with --timeout, "
                "PLAYBOOK_REVIEW_TIMEOUT_SECS, or .agent/config.json "
                "review_timeout_secs; move the soft deadline with --soft-timeout "
                "or review_soft_timeout_secs. Previous review log left untouched."
                + saved_note,
                file=sys.stderr, flush=True,
            )
            sys.exit(1)

        # Judge tamper guard (#1), same contract as the panel path: snapshot the
        # repo before spawning the single judge; warn if it will run uncontained.
        from provider import sandbox as _sandbox_mod
        if not _sandbox_mod.containment_available():
            print("  ⚠ judge running UNCONTAINED (no usable OS sandbox here) — "
                  "the tamper guard is the only defense against repo mutation.",
                  file=sys.stderr, flush=True)
        _tamper_before = _snapshot_repo_state(project_path, task_file)

        if backend == "claude":
            claude_bin = shutil.which("claude")
            if not claude_bin:
                print("Error: 'claude' not found on PATH", file=sys.stderr)
                sys.exit(1)

            prompt = prompt_fn(task_path)
            if extra_prompt:
                prompt += f"\n\nAdditional steering from the user:\n{extra_prompt}"
            env = os.environ.copy()
            env["CLAUDECODE"] = ""
            env.pop("CLAUDE_CODE_SSE_PORT", None)
            env.pop("CLAUDE_CODE_ENTRYPOINT", None)
            env["PLAYBOOK_SESSION_ID"] = "judge"

            # Bypass flag injected by provider.sandbox.run() — don't pass here.
            # The judge is a read-only evaluator sandboxed via provider.sandbox
            # (write containment via seatbelt/bwrap). PLAYBOOK_SESSION_ID=judge
            # above lets hooks identify judge sessions if needed.
            # --effort high for the same reason as the panel adapter (see
            # ClaudeAdapter.run_headless_judge): a judge is bought for its
            # reasoning. 'high' not 'max' — this bills the owner's own Claude
            # quota. Kept in step with the adapter so `--backend claude` and a
            # claude panel seat review at the same depth.
            claude_args = ["-p", "--effort", "high", "--max-budget-usd", review_budget]
            if model:
                from provider.adapters.claude import ClaudeAdapter
                claude_args += ["--model", ClaudeAdapter._MODEL_MAP.get(model, model)]
            # Windows: passing system_context as an argv element overflows the
            # Win32 command-line cap (32,767 chars → WinError 206). `claude -p`
            # with no positional prompt reads stdin, so pipe context+prompt
            # instead of putting them on argv. encoding="utf-8" keeps the pipe
            # (and stdout decode) off the cp1252 locale default on Windows.
            full_prompt = f"{system_context}\n\n---\n\n{prompt}"

            from provider import sandbox as _sandbox
            print(f"Running {review_label} (claude) on {task_path}...", flush=True)
            try:
                result = _sandbox.run(
                    "claude",
                    claude_args,
                    project_root=project_path,
                    project_writable=False,   # judge is read-only — cannot mutate repo/task.md
                    env=env,
                    input=full_prompt,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=review_timeout,
                )
            except subprocess.TimeoutExpired as _expired:
                _bail_review_timeout(_expired)

        elif backend == "codex":
            if not shutil.which("codex"):
                print("Error: 'codex' not found on PATH", file=sys.stderr)
                print("Install: https://github.com/openai/codex", file=sys.stderr)
                sys.exit(1)

            prompt = prompt_fn(task_path, inline_context=True)
            # Codex has no system prompt — inline context into the user prompt
            full_prompt = f"{system_context}\n\n---\n\n{prompt}"

            # Under the read-only judge sandbox (project_writable=False), codex
            # cannot write its `-o` transcript into the project tree. Point `-o`
            # at a temp file — system temp (/tmp, /var/folders) stays writable
            # under both seatbelt and bwrap — and copy it into the task dir from
            # the parent, after the tamper check (see the save block below).
            import tempfile as _tempfile
            _codex_log_fd, codex_log = _tempfile.mkstemp(suffix="-judge-codex.log")
            os.close(_codex_log_fd)
            codex_log = Path(codex_log)
            # Bypass flag (--dangerously-bypass-approvals-and-sandbox) inserted
            # after `exec` by provider.sandbox._compose_agent_argv.
            codex_args = ["exec"]
            if model:
                from provider.adapters.codex import _split_reasoning_effort
                model_id, effort = _split_reasoning_effort(model)
                codex_args += ["-m", model_id]
                if effort:
                    codex_args += ["-c", f"model_reasoning_effort={effort}"]
            codex_args += [
                "-s", "workspace-write",
                "--ephemeral",
                "-C", str(project_path),
                "-o", str(codex_log),
                "-",  # read prompt from stdin
            ]

            codex_env = os.environ.copy()
            codex_env["PLAYBOOK_SESSION_ID"] = "judge"

            from provider import sandbox as _sandbox
            print(f"Running {review_label} (codex) on {task_path}...", flush=True)
            try:
                result = _sandbox.run(
                    "codex", codex_args,
                    project_root=project_path,
                    project_writable=False,   # judge is read-only — cannot mutate repo/task.md
                    env=codex_env,
                    input=full_prompt,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=review_timeout,
                )
            except subprocess.TimeoutExpired as _expired:
                _bail_review_timeout(_expired)

        elif backend == "antigravity":  # agy
            if not shutil.which("agy"):
                print("Error: 'agy' not found on PATH", file=sys.stderr)
                sys.exit(1)

            prompt = prompt_fn(task_path, inline_context=True)
            full_prompt = f"{system_context}\n\n---\n\n{prompt}"
            if extra_prompt:
                full_prompt += f"\n\nAdditional steering from the user:\n{extra_prompt}"

            if model:
                print(f"  (note: agy has no model flag — ignoring --model {model}; uses agy's UI-selected model)", flush=True)
            # Prompt goes on STDIN, not argv: `agy --print` with no positional
            # prompt reads stdin (agy >=1.0.15). Windows caps the command line
            # at 32,767 chars (WinError 206), so full_prompt on argv overflows
            # it — same fix as the claude branch above and the adapter's
            # run_headless_judge. --print mode ignores cwd, needs --add-dir;
            # no -m/--model flag yet (uses whatever the agy UI has set).
            # Bypass (--dangerously-skip-permissions) prepended by sandbox.
            agy_args = [
                "--add-dir", str(project_path),
                "--print",
            ]
            # agy's own internal wait — keep it in step with the subprocess
            # timeout when finite; omit it entirely when unlimited so agy does
            # not kill a judge that is still writing.
            if review_timeout is not None:
                agy_args += ["--print-timeout", f"{review_timeout}s"]

            agy_env = os.environ.copy()
            agy_env["PLAYBOOK_SESSION_ID"] = "judge"

            from provider import sandbox as _sandbox
            print(f"Running {review_label} (agy) on {task_path}...", flush=True)
            try:
                result = _sandbox.run(
                    "agy", agy_args,
                    project_root=project_path,
                    project_writable=False,   # judge is read-only — cannot mutate repo/task.md
                    env=agy_env,
                    input=full_prompt,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=review_timeout,
                )
            except subprocess.TimeoutExpired as _expired:
                _bail_review_timeout(_expired)

        elif backend == "grok":
            if not shutil.which("grok"):
                print("Error: 'grok' not found on PATH", file=sys.stderr)
                sys.exit(1)

            prompt = prompt_fn(task_path, inline_context=True)
            if extra_prompt:
                prompt += f"\n\nAdditional steering from the user:\n{extra_prompt}"

            # Argv construction is delegated to the adapter — it owns the
            # dialect (prompt as `-p` value, model:effort split, context
            # inlined ahead of the prompt). Task-013 lesson: inline argv
            # copies drift from the adapter; don't make a fifth one.
            # Judge-only extra: grok's web tools are default-on — strip them.
            from provider.adapters.grok import GrokAdapter
            try:
                inv = GrokAdapter("judge", project_path).headless_argv(
                    prompt, model, context=system_context)
            except ValueError as e:  # bad model:effort spec — fail pre-spawn
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(1)
            grok_args = inv.argv + ["--disable-web-search"]

            # Windows caps the whole command line at 32,767 chars (WinError
            # 206); grok reads its prompt from argv (stdin is not a prompt
            # channel) — fail fast like the agy/pi arms.
            if os.name == "nt":
                payload = sum(len(a) + 1 for a in grok_args)
                if payload > 30_000:
                    print(f"Error: grok judge prompt+context is ~{payload} chars on argv; "
                          "Windows caps the command line at 32,767 chars and grok reads its "
                          "prompt from argv — shrink the context or use another backend.",
                          file=sys.stderr)
                    sys.exit(1)
            # POSIX per-element byte cap (#10) — the char budget can't bound argv bytes.
            from provider.argv_guard import argv_byte_error
            _argv_err = argv_byte_error(grok_args, "grok")
            if _argv_err:
                print(_argv_err, file=sys.stderr)
                sys.exit(1)

            grok_env = os.environ.copy()
            grok_env["PLAYBOOK_SESSION_ID"] = "judge"

            from provider import sandbox as _sandbox
            print(f"Running {review_label} (grok) on {task_path}...", flush=True)
            try:
                result = _sandbox.run(
                    "grok", grok_args,
                    project_root=project_path,
                    project_writable=False,   # judge is read-only — cannot mutate repo/task.md
                    env=grok_env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=review_timeout,
                )
            except subprocess.TimeoutExpired as _expired:
                _bail_review_timeout(_expired)
            except OSError as _e:
                # #10: E2BIG (argv too long) and kin were an uncaught traceback on
                # this path — a knowable pre-dispatch condition presented as a
                # crash. Turn it into a clean, actionable error.
                print(f"Error: grok judge dispatch failed ({_e}). Most likely the "
                      "prompt+context exceeded this platform's argv byte limit — "
                      "shrink the context or use a stdin-capable backend (claude/codex).",
                      file=sys.stderr)
                sys.exit(1)

        else:  # pi (local Qwen via oMLX)
            if not (shutil.which("pi") or shutil.which("omlx")):
                print("Error: neither 'pi' nor 'omlx' found on PATH", file=sys.stderr)
                print("Install: oMLX app (https://omlx.app/) or pi CLI", file=sys.stderr)
                sys.exit(1)

            prompt = prompt_fn(task_path, inline_context=True)

            # Pi has no system prompt convention — append-system-prompt threads
            # the system context. --no-context-files skips AGENTS.md/CLAUDE.md
            # auto-load so the judge isn't biased by project conventions.
            # --provider oss points at the local oMLX endpoint (127.0.0.1:8000).
            pi_args = [
                "-p", prompt,
                "--provider", "oss",
                "--no-context-files",
                "--append-system-prompt", system_context,
            ]
            if model:
                pi_args += ["--model", model]

            # Windows caps the whole command line at 32,767 chars (WinError 206);
            # pi reads its prompt AND context from argv only (no verified stdin
            # path), so fail fast with a clear message rather than a cryptic
            # spawn failure — mirrors the guard in provider/adapters/pi.py.
            if os.name == "nt":
                payload = sum(len(a) + 1 for a in pi_args)
                if payload > 30_000:
                    print(f"Error: pi judge prompt+context is ~{payload} chars on argv; "
                          "Windows caps the command line at 32,767 chars and pi reads its "
                          "prompt from argv only — shrink the context or use another backend.",
                          file=sys.stderr)
                    sys.exit(1)
            # POSIX per-element byte cap (#10).
            from provider.argv_guard import argv_byte_error
            _argv_err = argv_byte_error(pi_args, "pi")
            if _argv_err:
                print(_argv_err, file=sys.stderr)
                sys.exit(1)

            pi_env = os.environ.copy()
            pi_env["PLAYBOOK_SESSION_ID"] = "judge"

            from provider import sandbox as _sandbox
            print(f"Running {review_label} (pi) on {task_path}...", flush=True)
            try:
                result = _sandbox.run(
                    "pi", pi_args,
                    project_root=project_path,
                    project_writable=False,   # judge is read-only — cannot mutate repo/task.md
                    env=pi_env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=review_timeout,
                )
            except subprocess.TimeoutExpired as _expired:
                _bail_review_timeout(_expired)
            except OSError as _e:
                print(f"Error: pi judge dispatch failed ({_e}). Most likely the "
                      "prompt+context exceeded this platform's argv byte limit — "
                      "shrink the context or use a stdin-capable backend (claude/codex).",
                      file=sys.stderr)
                sys.exit(1)

        if result.stdout:
            print(result.stdout, end="", flush=True)
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr, flush=True)

        # Tamper check (#1): did the judge mutate the working tree? Computed here;
        # the log is still saved below (paid work preserved) but the run hard-stops
        # non-zero at the end so the operator won't ingest a tampered review.
        _tamper_changes = _detect_tamper(project_path, task_file, _tamper_before)

        # Save output — backend-specific log files
        judge_log = task_file.parent / _judge_log_name(backend)
        output = (result.stdout or "").strip()
        # Budget exhaustion arrives as exit-0 stdout (task 012 L3): detect it
        # BEFORE saving so it never overwrites a prior good review, tell the
        # user how to raise the cap, and exit nonzero — it's not a review.
        from tasks.models_check import budget_exceeded as _budget_exceeded
        from tasks.models_check import judge_failed as _judge_failed_str
        if _budget_exceeded(output):
            kept = (f"; kept previous {judge_log.relative_to(project_path)}"
                    if judge_log.exists() else "")
            print(f"\nJudge hit the ${review_budget} budget cap and produced no "
                  f"review{kept}. Raise judge_budget_usd in .agent/config.json "
                  f"or pass --budget.", flush=True)
            sys.exit(1)
        # Failure-marked output (e.g. claude's bad-model message: stdout WITH
        # exit 1) is not a review either — never let it overwrite a prior good
        # log (task 012 I1). The formatted string is what classification below
        # sees, so save/keep and hard-stop agree on what counts as a failure.
        _formatted_result = _sandbox.format_judge_output(result)
        # Set only on the success path below; stays None when the review failed,
        # so the write-back at the end cannot ingest a rejected run's output.
        saved_review_text = None
        if result.returncode != 0 and (not output or _judge_failed_str(_formatted_result)):
            if judge_log.exists():
                print(f"\nReview failed (exit {result.returncode}); kept previous {judge_log.relative_to(project_path)}", flush=True)
            else:
                print(f"\nReview failed (exit {result.returncode}); no output to save", flush=True)
        else:
            # Only codex writes its own log file (via `-o`); for it, stdout is a
            # fallback used only when that file is missing/empty. Every other
            # backend (claude/antigravity/grok/pi) MUST have stdout written here
            # — and OVERWRITTEN on each successful re-review, else a second run
            # prints "Saved" while silently keeping the stale log (task 014 I4).
            if backend == "codex":
                # codex wrote its clean final message to a temp file outside the
                # RO project; read it here (parent, post-tamper) and copy into the
                # task dir. stdout is the fallback when the temp file is empty.
                # Always overwrite on a successful review so a re-review can't
                # silently keep a stale log (task 014 I4).
                codex_out = ""
                try:
                    codex_out = codex_log.read_text(encoding="utf-8")
                except OSError:
                    pass
                try:
                    codex_log.unlink()
                except OSError:
                    pass
                saved_review_text = (
                    codex_out if codex_out.strip() else (result.stdout or ""))
            else:
                saved_review_text = result.stdout or ""
            # Durable context receipt (1.5.3): what THIS judge was shown rides
            # with its findings — a log without its delivery record is a verdict
            # with no chain of custody.
            _ctx_header = ("[context] "
                           + (" | ".join(context_receipts) if context_receipts
                              else "full task.md + mind map delivered (no truncation)")
                           + "\n\n")
            judge_log.write_text(_ctx_header + saved_review_text, encoding="utf-8")
            print(f"\nSaved: {judge_log.relative_to(project_path)}", flush=True)

        # Model-unavailable hard stop (task 012), same contract as the panel:
        # classify the FORMATTED result (both streams survive on nonzero exit
        # — codex 400s land on stderr, which stdout-only `output` misses),
        # then probe-confirm the exact spec before hard-stopping. Timeout
        # (handled above via _bail_review_timeout) and budget paths untouched.
        from tasks.models_check import (
            NEEDS_CLI_UPGRADE, apply_confirmed, check_pins, confirm_dead_specs,
            render_report,
        )
        _sj_provider = "agy" if backend == "antigravity" else backend
        _sj_spec = f"{_sj_provider}:{model}" if model else _sj_provider
        confirmed = confirm_dead_specs(
            {_sj_spec: _formatted_result}, {_sj_spec: (_sj_provider, model)})
        if confirmed:
            pv, detail = confirmed[_sj_spec]
            fix = ("upgrade the codex CLI (`codex update`)"
                   if pv == NEEDS_CLI_UPGRADE
                   else "re-select the panel (`tasks models select`)")
            print(f"\nHARD STOP: judge pin unavailable (probe-confirmed):\n"
                  f"  {_sj_spec}: {pv} — {detail} → {fix}\n\nCurrent availability:",
                  file=sys.stderr)
            report = apply_confirmed(
                check_pins(project_path, probe=False, extra_specs=[_sj_spec]),
                confirmed)
            print(render_report(report), file=sys.stderr)
            sys.exit(1)

        # Tamper hard-stop (#1): the single judge mutated the working tree. Log
        # is already saved above; exit non-zero with the loud banner so the
        # operator inspects/restores instead of trusting the review.
        if _tamper_changes:
            print("\n" + _tamper_banner(_tamper_changes), file=sys.stderr, flush=True)
            sys.exit(1)

        # Write the findings into task.md — LAST, deliberately. The judge is
        # sandboxed read-only and cannot do it itself (and must not be able to),
        # so the trusted parent does, exactly as the panel path does. This sits
        # after the budget, failure, model-unavailable and tamper checks because
        # every one of them can reject a run whose output still looks like a
        # review: writing any earlier would ingest findings the very next lines
        # declare untrustworthy. `saved_review_text` is the same content written
        # to the backend log, not raw stdout, so log and task.md never diverge.
        if result.returncode == 0 and saved_review_text and saved_review_text.strip():
            refusal = _write_review_findings(task_file, review_mode, saved_review_text)
            if refusal is None:
                print(f"Findings written to {task_file.relative_to(project_path)} "
                      f"(## {'Plan' if review_mode == 'plan' else 'Implementation'} Review)",
                      flush=True)
            else:
                # Exit non-zero: the review itself succeeded but its findings
                # were NOT delivered, and a caller that only checks the status
                # would otherwise treat an undelivered review as a clean one.
                print(f"\nCould not write findings into "
                      f"{task_file.relative_to(project_path)}: {refusal}\n"
                      f"They are saved in {judge_log.relative_to(project_path)} — "
                      f"paste them in by hand.", file=sys.stderr, flush=True)
                sys.exit(1)

        sys.exit(result.returncode)

    elif cmd == "context":
        if not cmd_args:
            print("Error: 'context' requires a task number", file=sys.stderr)
            print("Usage: tasks context <number>", file=sys.stderr)
            sys.exit(1)

        task_num = cmd_args[0]
        if task_num.isdigit():
            task_num = task_num.zfill(3)
        project_path = find_project_root()

        chat_log = resolve_agent_dir(project_path) / "chat_log.md"
        if not chat_log.exists():
            print("No .agent/chat_log.md found.", file=sys.stderr)
            sys.exit(1)

        import re
        open_tag = re.compile(r'^<!--\s*T' + re.escape(task_num) + r'\s*-->$')
        close_tag = re.compile(r'^<!--\s*/T' + re.escape(task_num) + r'\s*-->$')

        spans = []
        current_span = []
        inside = False
        for line in chat_log.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not inside and open_tag.match(stripped):
                inside = True
                continue
            elif inside and close_tag.match(stripped):
                spans.append("\n".join(current_span))
                current_span = []
                inside = False
                continue
            if inside:
                current_span.append(line)

        # Handle unclosed span at end of file
        if inside and current_span:
            spans.append("\n".join(current_span))

        if not spans:
            # F2 (untagged projects): `<!-- TNNN -->` spans are written only by
            # `tasks tag`, which nothing runs automatically — so this path was
            # blind for EVERY task on most projects while gate entries +
            # bash_history held everything needed to attribute messages. Fall
            # back to timestamp-window attribution (the same fallback `tasks
            # intent`'s chat layer uses); still fail loudly when nothing is
            # attributable. stdout stays pure messages — the provenance note
            # goes to stderr.
            fallback_msgs = []
            try:
                _n = int(task_num)
            except ValueError:
                _n = None
            if _n is not None:
                from tasks.retro import build_task_windows, extract_chatlog
                _bash_history = resolve_agent_dir(project_path) / "bash_history"
                _windows = build_task_windows(
                    chat_log, _bash_history if _bash_history.exists() else None)
                if _n in _windows:
                    fallback_msgs = [m for m in extract_chatlog(chat_log, _windows)
                                     if m.get("task") == _n]
            if not fallback_msgs:
                print(f"No attributed messages for task {task_num}.", file=sys.stderr)
                sys.exit(1)
            print(f"note: no <!-- T{task_num} --> tags in chat_log.md; messages "
                  "attributed via timestamp window (gate entries + bash_history). "
                  "Run `tasks tag` to persist attribution.", file=sys.stderr)
            _max_line = 200
            for m in fallback_msgs:
                text = " ".join(m["text"].split())
                if len(text) > _max_line:
                    text = text[:_max_line] + "..."
                print(f"[M{m['id']:03d}] {text}")
            # spans is empty: the tagged-span output loop below is a no-op.

        # Token-efficient output: strip markdown boilerplate, one line per message
        import re as _re
        max_line = 200
        msg_header = _re.compile(r'^\*\*\[(M\d+)\]\*\*.*')
        gate_header = _re.compile(r'^\*\*\[G\d+:\d+\]\*\*.*')
        for span in spans:
            msg_id = None
            msg_lines = []
            in_gate = False
            for line in span.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped == "---":
                    in_gate = False
                    continue
                if gate_header.match(stripped):
                    in_gate = True
                    continue
                if in_gate:
                    continue
                m = msg_header.match(stripped)
                if m:
                    # Flush previous message
                    if msg_id and msg_lines:
                        text = " ".join(msg_lines)
                        if len(text) > max_line:
                            text = text[:max_line] + "..."
                        print(f"[{msg_id}] {text}")
                    msg_id = m.group(1)
                    msg_lines = []
                else:
                    msg_lines.append(stripped)
            # Flush last message
            if msg_id and msg_lines:
                text = " ".join(msg_lines)
                if len(text) > max_line:
                    text = text[:max_line] + "..."
                print(f"[{msg_id}] {text}")

    elif cmd == "intent":
        # Vertical retro: 4 blind intent extractions over one task's layers.
        if not cmd_args:
            print("Error: 'intent' requires a task number", file=sys.stderr)
            print("Usage: tasks intent <number> [--chat-file P] [--base REF --head REF] "
                  "[--collect-only] [--timeout S]", file=sys.stderr)
            sys.exit(1)

        task_num = cmd_args[0]
        if task_num.isdigit():
            task_num = task_num.zfill(3)
        chat_file = base = head = None
        collect_only = False
        # None = not yet resolved; the real default comes from tasks.core so
        # `tasks intent` honours the same review knobs as plan/impl review
        # instead of pinning its own 300s. --timeout still overrides.
        timeout_secs = None
        i = 1
        while i < len(cmd_args):
            a = cmd_args[i]
            if a == "--chat-file" and i + 1 < len(cmd_args):
                chat_file = Path(cmd_args[i + 1]); i += 2
            elif a == "--base" and i + 1 < len(cmd_args):
                base = cmd_args[i + 1]; i += 2
            elif a == "--head" and i + 1 < len(cmd_args):
                head = cmd_args[i + 1]; i += 2
            elif a == "--collect-only":
                collect_only = True; i += 1
            elif a == "--timeout" and i + 1 < len(cmd_args):
                timeout_secs = int(cmd_args[i + 1]); i += 2
            else:
                print(f"Error: unknown option for intent: {a}", file=sys.stderr)
                sys.exit(1)

        if bool(base) != bool(head):
            print("Error: --base and --head must be given together (an explicit range)",
                  file=sys.stderr)
            sys.exit(1)

        from tasks.intent import (
            collect_all, run_extractions, make_default_runner,
            write_run, find_task_dir, new_run_id, last_intent_entry, LAYERS,
        )
        project_path = find_project_root()
        from tasks.core import format_timeout_label
        if timeout_secs is None:
            from tasks.core import resolve_review_timeout
            timeout_secs = resolve_review_timeout(project_path)
        agent_dir = resolve_agent_dir(project_path)
        task_dir = find_task_dir(agent_dir / "tasks", task_num)
        if task_dir is None:
            print(f"Error: no task {task_num} under {agent_dir / 'tasks'}", file=sys.stderr)
            sys.exit(1)

        slices = collect_all(project_path, agent_dir, task_dir, task_num,
                             chat_file=chat_file, base=base, head=head)
        print(f"Intent review — task {task_num} ({task_dir.name})")
        for layer in LAYERS:
            s = slices[layer]
            print(f"  {layer:7} {'✓' if s.available else '✗'}  {s.provenance}")
        avail = [l for l in LAYERS if slices[l].available]
        if not avail:
            print("Error: no available evidence on any layer — nothing to infer. "
                  "Pass --chat-file and/or --base/--head.", file=sys.stderr)
            sys.exit(1)

        run_id = new_run_id()
        if collect_only:
            from tasks.intent import build_prompt
            reports = {l: (build_prompt(slices[l]) if slices[l].available
                           else f"# Intent inferred from {l}\n\n_(no evidence — "
                                f"{slices[l].provenance})_\n") for l in LAYERS}
            print("\n(--collect-only: wrote prompts, skipped model calls)")
        else:
            print(f"\nRunning {len(avail)} blind extraction(s) "
                  f"(default judge, {format_timeout_label(timeout_secs)} each)...", flush=True)
            reports = run_extractions(slices, make_default_runner(
                project_path, timeout_secs=timeout_secs))

        run_dir = write_run(task_dir, slices, reports, run_id=run_id)
        rel = run_dir.relative_to(project_path)
        print(f"\nReports written: {rel}/")
        print(f"Grading sheet:   {rel}/review.md")
        prior = last_intent_entry(project_path / "INTENT.md", task_num)
        if prior:
            print("Prior validated intent exists — reconcile as a DELTA against INTENT.md.")
        print("\nNext: read review.md with the user, grade the seams, then append "
              "vetted intent to INTENT.md (the /intent command drives this).")

    elif cmd == "timeline":
        project_path = find_project_root()
        bash_history = resolve_agent_dir(project_path) / "bash_history"
        if not bash_history.exists():
            print("No .agent/bash_history found.", file=sys.stderr)
            sys.exit(1)

        import re
        # Match: timestamp | AGENT/SCRIPT | tasks work/new/done ...
        pattern = re.compile(
            r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| \w+ \| '
            r'(?:.*/)?(tasks (?:work|new) .+)$'
        )
        seen = set()
        for line in bash_history.read_text(encoding="utf-8", errors="replace").splitlines():
            m = pattern.match(line)
            if m:
                cmd = m.group(2)
                # Deduplicate AGENT+SCRIPT echoes (same command within 2 lines)
                if cmd not in seen:
                    seen.add(cmd)
                    print(f"{m.group(1)}  {cmd}")
                else:
                    seen.discard(cmd)

    elif cmd == "tagger":
        project_path = find_project_root()
        chat_log = resolve_agent_dir(project_path) / "chat_log.md"
        bash_history = resolve_agent_dir(project_path) / "bash_history"
        if not chat_log.exists():
            print("No .agent/chat_log.md found.", file=sys.stderr)
            sys.exit(1)
        if not bash_history.exists():
            print("No .agent/bash_history found.", file=sys.stderr)
            sys.exit(1)

        import re

        # 1. Parse messages from chat_log.md: (timestamp, msg_id, text)
        msg_header = re.compile(
            r'^\*\*\[(M\d+)\]\*\* \[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC\]'
        )
        gate_header = re.compile(r'^\*\*\[G\d+:\d+\]\*\*')
        entries = []  # (timestamp_str, sort_key, display_line)
        max_line = 200

        msg_id = None
        msg_ts = None
        msg_lines = []
        in_gate = False

        def flush_msg():
            if msg_id and msg_lines:
                text = " ".join(msg_lines)
                if len(text) > max_line:
                    text = text[:max_line] + "..."
                entries.append((msg_ts, 0, f"[{msg_id}] {text}"))

        for line in chat_log.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped == "---":
                in_gate = False
                continue
            if gate_header.match(stripped):
                in_gate = True
                continue
            if in_gate:
                continue
            m = msg_header.match(stripped)
            if m:
                flush_msg()
                msg_id = m.group(1)
                msg_ts = m.group(2)
                msg_lines = []
            elif stripped.startswith("<!--"):
                continue  # skip attribution tags / comments
            else:
                msg_lines.append(stripped)

        flush_msg()

        # 2. Parse task transitions from bash_history
        task_pattern = re.compile(
            r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| \w+ \| '
            r'(?:.*/)?(tasks (?:work|new) .+)$'
        )
        seen = set()
        for line in bash_history.read_text(encoding="utf-8", errors="replace").splitlines():
            m = task_pattern.match(line)
            if m:
                task_cmd = m.group(2)
                if task_cmd not in seen:
                    seen.add(task_cmd)
                    entries.append((m.group(1), 1, f"--- {task_cmd} ---"))
                else:
                    seen.discard(task_cmd)

        # 3. Sort by timestamp, then task transitions before messages (sort_key: 1 before 0)
        #    Actually: task transitions AFTER messages at same timestamp makes more sense
        #    But transitions should come BEFORE subsequent messages — sort_key 1 means
        #    transitions sort after messages at same second. That's fine: the transition
        #    happened between messages.
        entries.sort(key=lambda e: (e[0], e[1]))

        # 4. Output
        for _, _, display in entries:
            print(display)

    elif cmd == "tag":
        dry_run = "--dry-run" in cmd_args
        project_path = find_project_root()
        chat_log = resolve_agent_dir(project_path) / "chat_log.md"
        bash_history = resolve_agent_dir(project_path) / "bash_history"
        if not chat_log.exists():
            print("No .agent/chat_log.md found.", file=sys.stderr)
            sys.exit(1)
        if not bash_history.exists():
            print("No .agent/bash_history found.", file=sys.stderr)
            sys.exit(1)

        import re
        from bisect import bisect_right

        # 1. Build sorted task transition list from bash_history
        #    Each entry: (timestamp, active_task_or_None)
        task_pattern = re.compile(
            r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| \w+ \| '
            r'(?:.*/)?(tasks (?:work|new) .+)$'
        )
        work_re = re.compile(r'tasks work (\d+)')
        transitions = []  # [(timestamp, task_num_or_None)]
        seen = set()
        for line in bash_history.read_text(encoding="utf-8", errors="replace").splitlines():
            m = task_pattern.match(line)
            if m:
                task_cmd = m.group(2)
                if task_cmd not in seen:
                    seen.add(task_cmd)
                else:
                    seen.discard(task_cmd)
                    continue
                ts = m.group(1)
                if "work done" in task_cmd:
                    transitions.append((ts, None))
                else:
                    wm = work_re.search(task_cmd)
                    if wm:
                        transitions.append((ts, wm.group(1).zfill(3)))
        transitions.sort(key=lambda t: t[0])
        trans_times = [t[0] for t in transitions]

        def active_task_at(ts):
            """Return task number active at timestamp ts, or None."""
            idx = bisect_right(trans_times, ts) - 1
            if idx < 0:
                # F2 (first-task attribution): messages before the first
                # activation are the project seed — the mandate that produced
                # the first task. Attribute them to the first task ever
                # activated instead of dropping them.
                return next((t for _, t in transitions if t is not None), None)
            return transitions[idx][1]

        # 2. Scan chat_log.md, find message headers with timestamps,
        #    insert tags at task transition points
        msg_header = re.compile(
            r'^(\*\*\[(M\d+)\]\*\* \[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC\])'
        )
        # Also detect existing tags to avoid double-tagging
        existing_tag = re.compile(r'^<!--\s*/?T\d+\s*-->$')

        lines = chat_log.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        output = []
        current_tag = None  # currently open tag (task number)
        tags_inserted = 0

        for line in lines:
            stripped = line.strip()
            # Skip existing attribution tags (we'll rewrite them)
            if existing_tag.match(stripped):
                continue

            m = msg_header.match(stripped)
            if m:
                msg_id = m.group(2)
                msg_ts = m.group(3)
                task = active_task_at(msg_ts)

                if task != current_tag:
                    # Close previous tag if open
                    if current_tag is not None:
                        output.append(f"<!-- /T{current_tag} -->\n")
                        output.append("\n")
                        tags_inserted += 1
                    # Open new tag if task is active
                    if task is not None:
                        output.append(f"<!-- T{task} -->\n")
                        output.append("\n")
                        tags_inserted += 1
                    current_tag = task

            output.append(line)

        # Close final tag if still open
        if current_tag is not None:
            output.append(f"\n<!-- /T{current_tag} -->\n")
            tags_inserted += 1

        if dry_run:
            print(f"Would insert {tags_inserted} tags into chat_log.md")
            # Show first few transitions
            current_tag = None
            for line in output:
                stripped = line.strip()
                if existing_tag.match(stripped):
                    print(f"  {stripped}")
        else:
            chat_log.write_text("".join(output), encoding="utf-8")
            print(f"Inserted {tags_inserted} tags into chat_log.md")

    elif cmd == "retro":
        project_path = find_project_root()
        # Parse --since N flag
        since = 0
        i = 0
        while i < len(cmd_args):
            if cmd_args[i] == "--since" and i + 1 < len(cmd_args):
                try:
                    since = int(cmd_args[i + 1])
                except ValueError:
                    print(f"Error: --since requires a number", file=sys.stderr)
                    sys.exit(1)
                i += 2
            else:
                i += 1

        from tasks.retro import (
            extract_tasks, extract_chatlog, extract_mindmap,
            build_task_windows,
        )

        tasks_dir = resolve_agent_dir(project_path) / "tasks"
        chatlog_path = resolve_agent_dir(project_path) / "chat_log.md"
        bash_history_path = resolve_agent_dir(project_path) / "bash_history"
        mindmap_path = project_path / "MIND_MAP.md"

        # Extract data
        tasks = extract_tasks(tasks_dir, since=since)
        task_windows = build_task_windows(chatlog_path, bash_history_path)
        chatlog = extract_chatlog(chatlog_path, task_windows)
        mindmap = extract_mindmap(mindmap_path)

        if not tasks:
            print("No tasks found in window.", file=sys.stderr)
            sys.exit(1)

        # Run structural analysis passes
        from tasks.retro import (
            analyze_intent_health, analyze_garbage,
            generate_retro_task,
        )
        health = analyze_intent_health(tasks)
        gc = analyze_garbage(tasks)

        # Generate the retro task.md — a cognitive program
        retro_content = generate_retro_task(
            tasks=tasks, chatlog=chatlog, mindmap=mindmap,
            health=health, gc=gc,
        )

        # Create as a new task
        from tasks.core import _next_task_number, _slugify
        tasks_dir_path = resolve_agent_dir(project_path) / "tasks"
        task_num = _next_task_number(tasks_dir_path)
        first = tasks[0]["number"]
        last = tasks[-1]["number"]
        slug = f"retro-{first:03d}-{last:03d}"
        folder_name = f"{task_num:03d}-{slug}"
        task_dir = tasks_dir_path / folder_name
        task_dir.mkdir(parents=True)
        task_file = task_dir / "task.md"
        task_file.write_text(retro_content, encoding="utf-8")

        print(f"Created: {task_file.relative_to(project_path)}")
        print(f"Retro task T{task_num:03d} — {len(tasks)} tasks in window, "
              f"{len(chatlog)} chat messages, {len(mindmap)} mind map nodes")
        print(f"Next: tasks work {task_num}")

    elif cmd == "status":
        project_path = find_project_root()
        task_status(project_path)

    elif cmd == "audit":
        # P6: mechanical pre-panel sweeps — catch the stale/zombie/half-merged
        # stuff a grep can find before a judge spends a token on it. Records a
        # receipt into task.md and exits non-zero on real breakage so a review
        # can't proceed over a red audit.
        project_path = find_project_root()
        from tasks.audit import run_audit, format_audit_receipt
        # Optional task arg (else the active task) for the receipt destination.
        task_arg = next((a for a in cmd_args if a.isdigit()), None)
        agent_dir = resolve_agent_dir(project_path)
        task_file = None
        if task_arg:
            m = list((agent_dir / "tasks").glob(f"{task_arg.zfill(3)}-*/task.md"))
            task_file = m[0] if m else None
        else:
            sf = agent_dir / "sessions" / resolve_session_id() / "current_state"
            if sf.exists():
                active = sf.read_text(encoding="utf-8").strip()
                m = list((agent_dir / "tasks").glob(f"{active}-*/task.md"))
                task_file = m[0] if m else None

        print("Running pre-panel audit...", flush=True)
        audit = run_audit(project_path)
        for r in audit["results"]:
            n = len([ln for ln in r["output"].splitlines() if ln.strip()])
            tag = {"clean": "CLEAN", "findings": f"FINDINGS({n})", "error": "ERROR"}[r["status"]]
            print(f"  [{tag}] {r['name']} — {r['why']}", flush=True)
        if task_file:
            import subprocess as _sp
            from tasks.core import upsert_task_section
            try:
                _head = _sp.run(["git", "rev-parse", "HEAD"], cwd=project_path,
                                capture_output=True, text=True).stdout.strip()
            except (OSError, _sp.SubprocessError):
                _head = ""
            receipt = format_audit_receipt(audit, head_sha=_head)
            upsert_task_section(task_file, "Pre-Panel Audit", receipt)
            print(f"  Receipt recorded in {task_file.relative_to(project_path)}")
        print(f"\nAUDIT {'PASS' if audit['passed'] else 'FAIL'}", flush=True)
        if not audit["passed"]:
            print("  Fix the error-severity findings (or a broken sweep) before "
                  "reviewing — a red audit means mechanically-detectable issues remain.",
                  file=sys.stderr)
            sys.exit(1)

    elif cmd == "blocked":
        # #08: an honest state for "paused, waiting on the owner's decision" — so
        # the agent never fakes a checkbox or misuses freehand to end its turn.
        # Satisfies the Stop hook via the status marker (not gate text), records
        # the reason in task.md, shows as BLOCKED, and is cleared by `tasks work`.
        project_path = find_project_root()
        reason = " ".join(cmd_args).strip()
        if not reason:
            print('Error: a reason is required — tasks blocked "why you are paused '
                  'and what decision you need"', file=sys.stderr)
            sys.exit(1)
        agent_dir = resolve_agent_dir(project_path)
        session_id = resolve_session_id()
        state_file = agent_dir / "sessions" / session_id / "current_state"
        active = state_file.read_text(encoding="utf-8").strip() if state_file.exists() else None
        if not active:
            print("No active task to block. Activate one first: tasks work <N>",
                  file=sys.stderr)
            sys.exit(1)
        matches = list((agent_dir / "tasks").glob(f"{active}-*/task.md"))
        if not matches:
            print(f"Task {active} not found", file=sys.stderr)
            sys.exit(1)
        from tasks.core import set_task_blocked
        set_task_blocked(matches[0], reason)
        print(f"Task {active} marked BLOCKED — {' '.join(reason.split())}")
        print("You can end your turn; the Stop hook won't block a blocked task. "
              f"Resume with: tasks work {active}")

    elif cmd == "parked":
        # P9: the standing query that makes parked items un-swallowable. Lists
        # OPEN parked items across all tasks, oldest first; --all includes
        # resolved ones. Nothing else in the tool ever surfaced them again.
        project_path = find_project_root()
        show_all = "--all" in cmd_args
        from tasks.core import scan_parked
        items = scan_parked(project_path, open_only=not show_all)
        if not items:
            print("No parked items." if show_all else "No open parked items.")
        else:
            scope = "" if show_all else " open"
            print(f"{len(items)}{scope} parked item(s):")
            cur = None
            for it in items:
                if it["task"] != cur:
                    cur = it["task"]
                    print(f"\n  T{it['task']:03d} — {it['slug'].replace('-', ' ')}")
                tag = "" if it["status"] == "open" else f"  [{it['status']}]"
                print(f"    - {it['item']}{tag}")
            print("\nResolve each: promote (`tasks new …` then mark the bullet "
                  "`[promoted → NNN]`), dismiss (`[dismissed: reason]`), or leave "
                  "open deliberately.")

    elif cmd == "freehand":
        project_path = find_project_root()
        sub = cmd_args[0] if cmd_args else None

        if sub == "log":
            # Extract chat_log messages from freehand-start to now into task.md
            agent_dir = resolve_agent_dir(project_path)
            state_file = _state_file(project_path)
            if not state_file.exists():
                print("Error: no active task", file=sys.stderr)
                sys.exit(1)
            task_num = state_file.read_text(encoding="utf-8").strip()
            tasks_dir = agent_dir / "tasks"
            matches = list(tasks_dir.glob(f"{task_num}-*/task.md"))
            if not matches:
                print(f"Error: task {task_num} not found", file=sys.stderr)
                sys.exit(1)
            task_file = matches[0]
            task_text = task_file.read_text(encoding="utf-8")

            # Find the freehand-start marker
            import re
            # Use findall + take last — supports multiple freehand blocks in one task
            all_markers = re.findall(r'<!-- freehand-start: (.+?) -->', task_text)
            marker_match = all_markers[-1] if all_markers else None
            if not marker_match:
                print("Error: no freehand-start marker found in task.md", file=sys.stderr)
                sys.exit(1)

            # Parse the start timestamp
            from datetime import datetime, timezone
            start_str = marker_match.strip()
            try:
                start_ts = datetime.fromisoformat(start_str)
                if start_ts.tzinfo is None:
                    start_ts = start_ts.replace(tzinfo=timezone.utc)
            except ValueError:
                print(f"Error: cannot parse freehand-start timestamp: {start_str}", file=sys.stderr)
                sys.exit(1)

            # Read chat_log.md and extract messages in the span
            chat_log = agent_dir / "chat_log.md"
            if not chat_log.exists():
                print("Error: .agent/chat_log.md not found", file=sys.stderr)
                sys.exit(1)

            log_text = chat_log.read_text(encoding="utf-8", errors="replace")
            # Parse message blocks: **[MNNN]** [YYYY-MM-DD HH:MM:SS UTC]
            msg_pattern = re.compile(
                r'^(\*\*\[M\d+\]\*\* \[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC\].*)',
                re.MULTILINE
            )
            # Split log into message blocks by the --- separator
            blocks = log_text.split("\n---\n")
            extracted = []
            for block in blocks:
                m = msg_pattern.search(block)
                if m:
                    ts_str = m.group(2)
                    try:
                        msg_ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    except ValueError:
                        continue
                    if msg_ts >= start_ts:
                        extracted.append(block.strip())

            if not extracted:
                print("No chat_log messages found in freehand span.")
                return

            # Insert extracted messages into task.md below the Freehand log gate
            log_gate_pattern = re.compile(r'^(- \[ \] Freehand log\b.*)', re.MULTILINE)
            log_gate_match = log_gate_pattern.search(task_text)
            if not log_gate_match:
                print("Error: no '- [ ] Freehand log' gate found in task.md", file=sys.stderr)
                sys.exit(1)

            insert_pos = log_gate_match.end()
            log_content = "\n\n" + "\n\n---\n\n".join(extracted) + "\n"
            new_text = task_text[:insert_pos] + log_content + task_text[insert_pos:]
            task_file.write_text(new_text, encoding="utf-8")
            print(f"Inserted {len(extracted)} chat_log messages into task.md")
            return

        # Main freehand command: insert Freehand block into active task
        state_file = _state_file(project_path)
        agent_dir = resolve_agent_dir(project_path)

        if state_file.exists():
            task_num = state_file.read_text(encoding="utf-8").strip()
        else:
            task_num = None

        if not task_num:
            # Orchestrator mode: create a minimal freehand task (no Design Phase)
            print("No active task — creating freehand session...")
            from tasks.core import _next_task_number, _slugify
            tasks_dir = agent_dir / "tasks"
            task_num_int = _next_task_number(tasks_dir)
            task_num = f"{task_num_int:03d}"
            slug = _slugify("freehand")
            task_dir = tasks_dir / f"{task_num}-{slug}"
            task_dir.mkdir(parents=True, exist_ok=True)
            task_file = task_dir / "task.md"
            # Write minimal template — Freehand gate is first unchecked gate
            from datetime import datetime, timezone
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            task_file.write_text(
                f"# {task_num} - Freehand\n\n"
                f"## Status\nin_progress\n\n"
                f"## Intent\n(freehand session — intent determined during work)\n\n"
                f"## Work Plan\n\n"
                f"### Freehand\n"
                f"<!-- freehand-start: {now_iso} -->\n"
                f"- [ ] Freehand\n"
                f"- [ ] Freehand log — run `.claude/bin/tasks freehand log` to capture chat_log messages, "
                f"then retro-add checked gates for work done\n"
                f"- [ ] Rewrite this freehand work into normal task gates inside this task so the final trace reads like ordinary tracked work\n"
                f"- [ ] Rename this task folder and header to match what was actually done, then check this gate last\n",
                encoding="utf-8",
            )
            # Activate it
            session_id = resolve_session_id()
            session_dir = agent_dir / "sessions" / session_id
            session_dir.mkdir(parents=True, exist_ok=True)
            (session_dir / "current_state").write_text(f"{task_num}\n", encoding="utf-8")
            print(f"Created and activated task {task_num}")
        else:
            # Work mode: insert freehand block into current task
            tasks_dir = agent_dir / "tasks"
            matches = list(tasks_dir.glob(f"{task_num}-*/task.md"))
            if not matches:
                print(f"Error: task {task_num} not found", file=sys.stderr)
                sys.exit(1)
            task_file = matches[0]

            from datetime import datetime, timezone
            task_text = task_file.read_text(encoding="utf-8")
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            freehand_block = (
                f"\n### Freehand\n"
                f"<!-- freehand-start: {now_iso} -->\n"
                f"- [ ] Freehand\n"
                f"- [ ] Freehand log — run `.claude/bin/tasks freehand log` to capture chat_log messages, "
                f"then retro-add checked gates for work done\n"
                f"- [ ] Rewrite this freehand work into normal task gates inside this task so the final trace reads like ordinary tracked work\n"
                f"- [ ] Rename this task folder and header to match what was actually done, then check this gate last\n"
            )

            # Find Work Plan section and insert before first unchecked gate there
            import re
            work_plan_match = re.search(r'^## Work Plan\b', task_text, re.MULTILINE)
            if work_plan_match:
                after_wp = task_text[work_plan_match.start():]
                gate_match = re.search(r'^- \[ \]', after_wp, re.MULTILINE)
                if gate_match:
                    insert_pos = work_plan_match.start() + gate_match.start()
                else:
                    sep_match = re.search(r'\n---\n', after_wp)
                    if sep_match:
                        insert_pos = work_plan_match.start() + sep_match.start()
                    else:
                        insert_pos = len(task_text)
            else:
                insert_pos = len(task_text)

            new_text = task_text[:insert_pos] + freehand_block + "\n" + task_text[insert_pos:]
            task_file.write_text(new_text, encoding="utf-8")
            print(f"Freehand block inserted in task {task_num}")
        print(f"Freehand mode active. Agent: wait for user instructions. Close only when user says done.")

    elif cmd == "doctor":
        project_path = find_project_root()
        passed = 0
        failed = 0
        warned = 0

        def iter_hook_commands(node):
            if isinstance(node, dict):
                command = node.get("command")
                if isinstance(command, str):
                    yield command
                for value in node.values():
                    yield from iter_hook_commands(value)
            elif isinstance(node, list):
                for item in node:
                    yield from iter_hook_commands(item)

        def check(name: str, ok: bool, detail: str = ""):
            nonlocal passed, failed
            status = "PASS" if ok else "FAIL"
            msg = f"  [{status}] {name}"
            if detail:
                msg += f" — {detail}"
            print(msg)
            if ok:
                passed += 1
            else:
                failed += 1

        def warn(name: str, detail: str = ""):
            # Non-fatal advisory: surfaced but never counts as a failed check.
            nonlocal warned
            msg = f"  [WARN] {name}"
            if detail:
                msg += f" — {detail}"
            print(msg)
            warned += 1

        print("tasks doctor\n")

        # 1. Project structure
        agent_tasks = resolve_agent_dir(project_path) / "tasks"
        check("project: tasks/ exists", agent_tasks.exists())
        claude_md = project_path / "CLAUDE.md"
        check("project: CLAUDE.md exists", claude_md.exists())
        mind_map = project_path / "MIND_MAP.md"
        check("project: MIND_MAP.md exists", mind_map.exists())

        # 1b. Optional per-install config (.agent/config.json). Advisory only:
        # a missing/malformed file or bad value falls back to defaults at runtime,
        # so these are warnings, not failures.
        import json as _json
        cfg_path = project_path / ".agent" / "config.json"
        if cfg_path.exists():
            try:
                _cfg = _json.loads(cfg_path.read_text(encoding="utf-8", errors="replace"))
            except (ValueError, OSError) as e:
                # "defaults used" is true for the review knobs but NOT for
                # merge_verify: an unparseable config makes the merge skill
                # BLOCK, so say so when the file was trying to declare one.
                _extra = ""
                try:
                    if "merge_verify" in cfg_path.read_text(encoding="utf-8", errors="replace"):
                        _extra = ("; the merge skill will BLOCK on this rather "
                                  "than skip its verify step")
                except OSError:
                    pass
                warn("config: .agent/config.json parses",
                     f"invalid JSON ({e}); defaults used{_extra}")
                _cfg = None
            if isinstance(_cfg, dict):
                # Validate through the runtime's own parser and report the
                # runtime's own defaults, so doctor can never call a value
                # clean that the runtime ignores, nor name a fallback that
                # is no longer the fallback.
                from tasks.core import DEFAULT_JUDGE_BUDGET_USD as _DEF_BUDGET
                from tasks.core import DEFAULT_REVIEW_SOFT_TIMEOUT_SECS as _DEF_SOFT
                from tasks.core import DEFAULT_REVIEW_TIMEOUT_SECS as _DEF_HARD
                from tasks.core import _parse_timeout as _pt
                _jb = _cfg.get("judge_budget_usd")
                if _jb is not None:
                    try:
                        _ok = float(_jb) >= 0
                    except (TypeError, ValueError):
                        _ok = False
                    if not _ok:
                        warn("config: judge_budget_usd", f"{_jb!r} not a non-negative number; default ${_DEF_BUDGET} used")
                _rt = _cfg.get("review_timeout_secs")
                if _rt is not None:
                    # 0 / "unlimited" = no hard kill; a positive int = hang safety.
                    try:
                        _pt(_rt)
                        _ok = True
                    except (TypeError, ValueError):
                        _ok = False
                    if not _ok:
                        warn("config: review_timeout_secs",
                             f'{_rt!r} not a positive integer or an unlimited form '
                             f'(0/"unlimited"); default {_DEF_HARD}s used')
                _st = _cfg.get("review_soft_timeout_secs")
                if _st is not None:
                    try:
                        _pt(_st)
                        _ok = True
                    except (TypeError, ValueError):
                        _ok = False
                    if not _ok:
                        warn("config: review_soft_timeout_secs",
                             f'{_st!r} not a positive integer or an unlimited form '
                             f'(0/"unlimited"); default {_DEF_SOFT}s used')
                # merge_verify — the post-merge soundness command the merge skill
                # runs (skills/merge/merge-verify.py). Advisory here, but worth
                # surfacing early: at merge time an unusable declaration BLOCKS
                # the merge's verify step rather than being ignored, so a typo
                # found by doctor is a typo found cheaply.
                for _m in _merge_verify_issues(_cfg):
                    warn("config: merge_verify", _m)
                for _m in _merge_verify_untracked(project_path, _cfg):
                    warn("config: merge_verify", _m)
            elif _cfg is not None:
                warn("config: .agent/config.json shape", "top-level value is not a JSON object; ignored")

        # 1c. Judge pins (.agent/models.json + shipped panel) — advisory only.
        # Cheap checks: adapter presence + codex cache/effort validation; NO
        # live probes in doctor (that's `tasks models check`).
        try:
            from tasks.models_check import bad_pins, check_pins
            models_path = project_path / ".agent" / "models.json"
            if not models_path.exists():
                warn("models: .agent/models.json", "absent — shipped panel used; "
                     "create with `tasks models select`")
            _report = check_pins(project_path, probe=False)
            for _e in bad_pins(_report):
                warn(f"models: pin '{_e['spec']}'", f"{_e['verdict']} — {_e['detail']}; "
                     f"refresh with `tasks models select`")
        except Exception as e:  # doctor must never crash on an advisory check
            warn("models: pin check ran", f"skipped ({e})")

        # 1d. README drift (task 017) — maintainer-only advisory. Silently a
        # no-op outside a plugin source checkout / dogfood workspace.
        try:
            from tasks.readme_drift import readme_drift
            for _msg in readme_drift(project_path):
                warn("readme: audit drift", _msg)
        except Exception as e:  # doctor must never crash on an advisory check
            warn("readme: drift check ran", f"skipped ({e})")

        # 1e. Gate-logging health across ALL lanes (bug report #4). state-echo
        # writes `**[G<task>:…]**` per gate transition into each lane's chat_log;
        # if those stop while tasks keep completing, retro attribution silently
        # degrades. Scan every lane — NOT just resolve_agent_dir's current one —
        # because the reported case is one dev running doctor while a PEER's lane
        # is the broken one (task 018 panel T7). Advisory; never crashes doctor.
        try:
            from tasks.gate_logging import done_task_numbers, gate_logging_gap
            from tasks.core import _agent_lanes
            for lane_user, lane_rel in _agent_lanes(project_path):
                chat_log = project_path / lane_rel / "chat_log.md"
                if not chat_log.is_file():
                    continue
                text = chat_log.read_text(encoding="utf-8", errors="replace")
                done = done_task_numbers(project_path / lane_rel / "tasks")
                gap = gate_logging_gap(text, done)
                if gap:
                    label = lane_user or "(root)"
                    warn(f"gate-logging: lane '{label}'", gap)
        except Exception as e:  # advisory — doctor must never crash here
            warn("gate-logging: lane scan ran", f"skipped ({e})")

        # 1f. Hook command quoting (task 019 / field bug AloVet 2026-07-20).
        # Every hooks.json `command` was quote-wrapped, which grok resolves as
        # a literal path -> command-not-found -> all six hooks fail-open. Scan
        # the copies the host actually loads (CLAUDE_PLUGIN_ROOT, the copy next
        # to this module, the workspace source tree, and grok's own ~/.grok
        # copies), not just the source tree — a clean checkout is not proof the
        # running install is clean. Missing copies are silently skipped.
        # Advisory; never crashes doctor.
        try:
            from tasks.hooks_check import hooks_check_report
            for _label, _detail in hooks_check_report(project_path):
                warn(_label, _detail)
        except Exception as e:  # advisory — doctor must never crash here
            warn("hooks: command-quoting check ran", f"skipped ({e})")

        # 1g. Grok always-trusted global enforcement file (task 020).
        # Absolute script pins go stale on upgrade/move → fail-open. Also flag
        # a missing file when AGENTS.md exists (Grok bootstrap present).
        try:
            from tasks.hooks_check import grok_enforcement_report, grok_enforcement_issues
            agents_md = project_path / "AGENTS.md"
            issues = grok_enforcement_issues()
            # Only warn "missing" when the project looks Grok-bootstrapped;
            # always warn on stale/broken paths if the file exists.
            if issues:
                missing_only = all(i.startswith("missing ") for i in issues)
                if not missing_only or agents_md.is_file():
                    for _label, _detail in grok_enforcement_report():
                        warn(_label, _detail)
        except Exception as e:  # advisory — doctor must never crash here
            warn("hooks: grok enforcement check ran", f"skipped ({e})")

        # 2. Unicode
        stdout_enc = getattr(sys.stdout, "encoding", "unknown") or "unknown"
        check("unicode: stdout encoding", "utf" in stdout_enc.lower(), stdout_enc)

        # 3. Dead session dirs left by crashed sessions.
        #
        # Uses _session_is_dead, the same predicate _gc_dead_sessions deletes by,
        # so doctor cannot report a session the GC would keep (or vice versa).
        # It used to flag any pointer older than 24h with no liveness check and
        # no self-exclusion, which after task 027 is exactly the false-positive
        # class this task removed: a live session on a multi-day task is the
        # NORMAL case, not a fault to report.
        #
        # In practice this now reports only what the GC could NOT reclaim —
        # _gc_dead_sessions runs at the CLI entry point, so by the time doctor
        # looks, every deletable dead dir is already gone. A non-empty list
        # therefore means the sweep is being blocked (permissions, read-only
        # mount), which is worth surfacing precisely because the sweep itself
        # is deliberately silent about failures (fail-open).
        agent_dir = resolve_agent_dir(project_path)
        stale = []
        sessions_dir = agent_dir / "sessions"
        if sessions_dir.exists():
            cutoff = time.time() - 86400
            own_session = _own_session_id()
            for session_dir in sorted(sessions_dir.iterdir()):
                if session_dir.is_symlink() or not session_dir.is_dir():
                    continue
                if _session_is_dead(session_dir, own_session, cutoff):
                    stale.append(session_dir.name)
        check("session: no dead session dirs", len(stale) == 0,
              f"dead: {', '.join(stale)}" if stale else "clean")

        # 4. Hooks — check .claude/hooks/ (installed) or src/hooks/ (dev repo)
        hooks_dirs = [project_path / "scripts", project_path / ".claude" / "hooks", project_path / "src" / "hooks"]
        # On a plugin install the hook scripts live at ${CLAUDE_PLUGIN_ROOT}/scripts
        # (wired via the plugin's hooks.json), not in the project tree. Resolve
        # that dir too so doctor doesn't false-negative "missing" on every
        # plugin install even though the gates demonstrably fire.
        #
        # F16 (batch-4): resolve the RUNNING code's own scripts dir — the same
        # tree the version check reads (block 5, task 010) and the copy the
        # daily `tasks` wrapper resolved to. Without it, doctor hunted
        # ~/.claude/plugins by mtime and could inspect a DIFFERENT install than
        # the one executing: 4 FAIL (hooks "missing", truncation, resolver)
        # while every hook demonstrably enforced all session. The home glob
        # stays only as a last resort for layouts where the module has no
        # sibling scripts/ (dev src/ checkouts).
        _plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
        _resolved_install = False
        if _plugin_root and (Path(_plugin_root) / "scripts").is_dir():
            hooks_dirs.append(Path(_plugin_root) / "scripts")
            _resolved_install = True
        _own_scripts = Path(__file__).resolve().parent.parent / "scripts"
        if _own_scripts.is_dir():
            hooks_dirs.append(_own_scripts)
            _resolved_install = True
        if not _resolved_install:
            _plugins_home = Path.home() / ".claude" / "plugins"
            if _plugins_home.exists():
                _found = sorted(_plugins_home.glob("**/playbook/scripts"),
                                key=lambda p: p.stat().st_mtime, reverse=True)
                if _found:
                    hooks_dirs.append(_found[0])
        for hook_name in ["state-echo-hook", "task-gate-hook"]:
            found = False
            for hooks_dir in hooks_dirs:
                hook_path = hooks_dir / hook_name
                if hook_path.exists():
                    executable = os.access(hook_path, os.X_OK)
                    check(f"hooks: {hook_name}", executable,
                          f"found at {hooks_dir.name}/" + ("" if executable else " but not executable"))
                    found = True
                    break
            if not found:
                check(f"hooks: {hook_name}", False, "missing")

        # 4b. Check ~/.claude/settings.json for stale hook entries pointing to nonexistent paths
        user_settings = Path.home() / ".claude" / "settings.json"
        stale_hooks = []
        if user_settings.exists():
            import json as _json
            try:
                settings = _json.loads(user_settings.read_text(encoding="utf-8"))
                for cmd in iter_hook_commands(settings.get("hooks", {})):
                    for token in cmd.split():
                        p = Path(token)
                        if p.suffix in (".sh", "") and len(p.parts) > 2 and not p.exists():
                            stale_hooks.append(str(p))
            except (ValueError, KeyError):
                pass
        check("hooks: no stale entries in ~/.claude/settings.json",
              len(stale_hooks) == 0,
              f"stale paths: {', '.join(stale_hooks[:3])}" if stale_hooks else "clean")

        # 5. Plugin version — read the RUNNING code's own manifest (same tree as
        # this module), not a global glob: with several cached plugin versions
        # the glob's [0] is readdir-order nondeterministic (task 010). Dev
        # layout (src/tasks/) has no sibling manifest -> sorted glob fallback.
        from tasks.core import VERSION as code_version
        installed_version = None
        own_manifest = Path(__file__).resolve().parent.parent / ".claude-plugin" / "plugin.json"
        if own_manifest.is_file():
            plugin_json_paths = [own_manifest]
        else:
            plugin_json_paths = sorted(Path.home().glob(".claude/plugins/**/playbook/.claude-plugin/plugin.json"))
        if plugin_json_paths:
            import json as _json2
            try:
                pdata = _json2.loads(plugin_json_paths[0].read_text(encoding="utf-8"))
                installed_version = pdata.get("version", "unknown")
            except (ValueError, OSError):
                installed_version = "unreadable"
        if installed_version:
            version_ok = installed_version == code_version
            check("plugin: version matches code", version_ok,
                  f"installed={installed_version}, code={code_version}" + ("" if version_ok else " — run /upgrade"))
        else:
            check("plugin: installed", False, "no plugin found")

        # 6. Python version
        import platform
        py_ver = platform.python_version()
        major, minor = sys.version_info[:2]
        check("python: version >= 3.8", major >= 3 and minor >= 8, py_ver)

        # 7. write_text encoding (check installed plugin scripts)
        import re as _re
        import inspect
        cli_src = Path(inspect.getfile(sys.modules[__name__]))
        core_src = cli_src.parent / "core.py"
        unencoded = 0
        for src_file in [cli_src, core_src]:
            if src_file.exists():
                content = src_file.read_text(encoding="utf-8")
                # Find all write_text/read_text calls (may span multiple lines)
                for m in _re.finditer(r'\.(write_text|read_text)\(', content):
                    # Find the matching closing paren
                    start = m.end()
                    depth = 1
                    pos = start
                    while pos < len(content) and depth > 0:
                        if content[pos] == '(':
                            depth += 1
                        elif content[pos] == ')':
                            depth -= 1
                        pos += 1
                    call_body = content[start:pos]
                    if "encoding=" not in call_body:
                        unencoded += 1
        check("encoding: write_text/read_text have encoding=", unencoded == 0,
              f"{unencoded} unencoded calls" if unencoded else "all encoded")

        # 8. Gate echo truncation
        has_truncation = False
        for hd in hooks_dirs:
            echo_hook = hd / "state-echo-hook"
            if echo_hook.exists():
                hook_content = echo_hook.read_text(encoding="utf-8")
                has_truncation = "cut -c" in hook_content or "GATE_TEXT_STORE" in hook_content
                break
        check("hooks: gate text truncation", has_truncation,
              "prevents recursive duplication" if has_truncation else "gate text may grow unbounded")

        # 9. Session-id resolver consistency (split-brain regression guard).
        # Python and bash must produce identical session_ids without PLAYBOOK_SESSION_ID,
        # otherwise hooks and CLI look in different .agent/sessions/ directories.
        gate_lib = None
        for hd in hooks_dirs + [project_path / "scripts"]:
            cand = hd / "gate-echo-lib.sh"
            if cand.exists():
                gate_lib = cand
                break
        if gate_lib and (sys.platform == "win32" or os.name == "nt"):
            # Windows: the process-walk is skipped by both resolvers (disjoint
            # MSYS vs native PID namespaces, see find_agent_root_pid). Two
            # assertions: (1) the env-set path honors PLAYBOOK_SESSION_ID;
            # (2) the env-UNSET path returns the shared constant
            # 'pid-win-fallback' and gate-echo-lib.sh carries the same literal
            # — that constant is the only thing preventing split-brain when the
            # env var doesn't propagate. We deliberately don't shell out to
            # bash: MSYS path resolution is unreliable when bash.exe is spawned
            # from native Python, which would produce a spurious MISMATCH; the
            # static literal check covers the bash side instead.
            probe = "pid-doctor-probe"
            saved = os.environ.get("PLAYBOOK_SESSION_ID")
            os.environ["PLAYBOOK_SESSION_ID"] = probe
            try:
                py_sid = resolve_session_id()
            finally:
                if saved is None:
                    os.environ.pop("PLAYBOOK_SESSION_ID", None)
                else:
                    os.environ["PLAYBOOK_SESSION_ID"] = saved
            check("session-id: Python ≡ bash resolver", py_sid == probe,
                  "env-authoritative on Windows (ancestor scan skipped)"
                  if py_sid == probe else f"Python ignored PLAYBOOK_SESSION_ID: {py_sid!r}")
            saved = os.environ.pop("PLAYBOOK_SESSION_ID", None)
            try:
                py_fallback = resolve_session_id()
            finally:
                if saved is not None:
                    os.environ["PLAYBOOK_SESSION_ID"] = saved
            bash_has_const = "pid-win-fallback" in gate_lib.read_text(
                encoding="utf-8", errors="replace")
            fallback_ok = py_fallback == "pid-win-fallback" and bash_has_const
            check("session-id: env-unset fallback converges", fallback_ok,
                  "both resolvers use constant 'pid-win-fallback'"
                  if fallback_ok else
                  f"Python fallback {py_fallback!r}; bash literal present: {bash_has_const}"
                  " — split-brain risk when PLAYBOOK_SESSION_ID is unset")
        elif gate_lib:
            import subprocess as _sub
            from tasks.core import find_agent_root_pid
            saved = os.environ.pop("PLAYBOOK_SESSION_ID", None)
            try:
                find_agent_root_pid.cache_clear()
                py_sid = resolve_session_id()
                env = {k: v for k, v in os.environ.items() if k != "PLAYBOOK_SESSION_ID"}
                r = _sub.run(["bash", "-c", f"source '{gate_lib.as_posix()}' && resolve_session_id"],
                             capture_output=True, text=True, env=env, timeout=5)
                bash_sid = r.stdout.strip()
            finally:
                if saved is not None:
                    os.environ["PLAYBOOK_SESSION_ID"] = saved
            agree = py_sid == bash_sid and py_sid.startswith("pid-")
            detail = f"both → {py_sid}" if agree else f"MISMATCH py={py_sid!r} bash={bash_sid!r}"
            check("session-id: Python ≡ bash resolver", agree, detail)
        else:
            check("session-id: Python ≡ bash resolver", False, "gate-echo-lib.sh not found")

        # Summary
        total = passed + failed
        summary = f"\n{passed}/{total} checks passed"
        if failed:
            summary += f" ({failed} failed)"
        if warned:
            summary += f" ({warned} warning{'s' if warned != 1 else ''})"
        print(summary)

    elif cmd == "merge-doctor":
        from tasks.merge_prep import cmd_merge_doctor
        cmd_merge_doctor(cmd_args)

    elif cmd == "mindmap-sync":
        from tasks.mindmap import cmd_mindmap_sync
        cmd_mindmap_sync(cmd_args)

    elif cmd == "log":
        # tasks log [N] [--width W]
        # Compact one-line-per-message view of chat_log.md (no gate cruft).
        # N: show only the last N messages (default: all).
        # --width: crop each message body to W chars (default 500).
        import re
        cmd_args = sys.argv[2:]
        last_n = None
        width = 500
        i = 0
        while i < len(cmd_args):
            a = cmd_args[i]
            if a == "--width" and i + 1 < len(cmd_args):
                width = max(10, int(cmd_args[i + 1])); i += 2
            elif a.isdigit():
                last_n = int(a); i += 1
            else:
                i += 1
        project_path = find_project_root()
        chat_log = resolve_agent_dir(project_path) / "chat_log.md"
        if not chat_log.exists():
            print("Error: .agent/chat_log.md not found", file=sys.stderr)
            sys.exit(1)
        text = chat_log.read_text(encoding="utf-8", errors="replace")
        blocks = text.split("\n---\n")
        lines = []
        for block in blocks:
            # Entry header format grew a ` (provider/pid)` suffix with multi-provider
            # tagging (commit 0fca4b0), e.g. `**[M12]** [… UTC] `HOST` (claude/pid-9)`.
            # The suffix must be OPTIONAL (legacy entries lack it, and requiring it
            # made `tasks log` silently print nothing — bug report #5b) and CAPTURED
            # (its provider token is the real agent; the backticked field is now just
            # `HOST`). Prefer the suffix provider; fall back to the backticked name.
            m = re.match(
                r'\*\*(\[M\d+\])\*\* \[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}):\d{2} UTC\] '
                r'`(\w+)`(?:\s*\(([^)/]+)/[^)]*\))?\s*\n+(.*)',
                block.strip(), re.DOTALL
            )
            if m:
                mid, ts, role, provider, body = m.groups()
                agent = provider or role
                body = " ".join(body.split())
                if len(body) > width:
                    body = body[:width - 1] + "…"
                lines.append(f"{mid} {ts} {agent:<6} {body}")
        if last_n is not None:
            lines = lines[-last_n:]
        for line in lines:
            print(line)

    elif cmd == "prepare-merge":
        from tasks.merge_prep import cmd_prepare_merge
        cmd_prepare_merge(cmd_args)

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print_usage()
        sys.exit(1)


if __name__ == "__main__":
    main()
