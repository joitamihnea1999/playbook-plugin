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
        from tasks.project_setup import cmd_init
        cmd_init(cmd_args)

    elif cmd == "bootstrap":
        from tasks.project_setup import cmd_bootstrap
        cmd_bootstrap(cmd_args)

    elif cmd in ("list", "ls"):
        project_path = find_project_root()
        pending_only = "--pending" in cmd_args
        list_tasks(project_path, pending_only=pending_only)

    elif cmd == "panel-review":
        from tasks.review import cmd_panel_review
        cmd_panel_review(cmd_args)

    elif cmd == "models":
        # Model-availability discovery + panel selection (task 012).
        # `tasks models check [--no-probe]` audits every models.json pin;
        # `tasks models select [--no-probe]` interactively rewrites the panel.
        from tasks.models_check import cli_models
        sys.exit(cli_models(cmd_args, find_project_root()))

    elif cmd in ("plan-review", "impl-review", "judge"):
        from tasks.review import cmd_single_review
        cmd_single_review(cmd, cmd_args)

    elif cmd == "context":
        from tasks.history import cmd_context
        cmd_context(cmd_args)

    elif cmd == "intent":
        from tasks.history import cmd_intent
        cmd_intent(cmd_args)

    elif cmd == "timeline":
        from tasks.history import cmd_timeline
        cmd_timeline(cmd_args)

    elif cmd == "tagger":
        from tasks.history import cmd_tagger
        cmd_tagger(cmd_args)

    elif cmd == "tag":
        from tasks.history import cmd_tag
        cmd_tag(cmd_args)

    elif cmd == "retro":
        from tasks.history import cmd_retro
        cmd_retro(cmd_args)

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
        from tasks.history import cmd_log
        cmd_log(cmd_args)

    elif cmd == "prepare-merge":
        from tasks.merge_prep import cmd_prepare_merge
        cmd_prepare_merge(cmd_args)

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print_usage()
        sys.exit(1)


if __name__ == "__main__":
    main()
