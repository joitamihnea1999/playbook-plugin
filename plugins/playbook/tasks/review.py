"""Judge orchestration: the `panel-review` arm, the single-judge
`plan-review` / `impl-review` / `judge` arm, and the review machinery both
share.

Boundary: everything about SPAWNING judges, guarding them, and delivering
their output — per-transport context assembly (via mindmap's `_load_mind_map`
and core's `select_task_context`), the tamper trio
(`_snapshot_repo_state` / `_detect_tamper` / `_tamper_banner` — the #1 guard;
judges are read-only evaluators), the triage frame the reading agent meets
before findings, sentinel-delimited findings write-back
(`_write_review_findings` — the trusted parent writes, never the judge), the
per-backend log-name contract, quorum verdicts, timeout salvage, and the
model-unavailable hard stops. The CLOSE path is not here — it consumes
judge.md via tasks.core parsers. Imports stdlib + tasks.core + tasks.shared +
tasks.mindmap + tasks.template/models_check/audit + provider.*; never a
command module (design-1.5.9.md §4).
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path
from tasks.atomic import atomic_write
from tasks.core import resolve_agent_dir
from tasks.mindmap import _load_mind_map
from tasks.shared import find_project_root


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
    # it. Package primitive: same-dir temp + fsync + os.replace, cleaning up the
    # temp itself on failure. Keep this writer's soft contract of returning an
    # error string (not raising) on an I/O failure.
    try:
        atomic_write(task_file, new_text)
    except OSError as e:
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


def cmd_panel_review(cmd_args):
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


def cmd_single_review(cmd, cmd_args):
    # "judge" is a legacy alias — auto-detects mode from task status
    review_cmd = cmd
    if not cmd_args:
        print(f"Error: '{review_cmd}' requires a task number", file=sys.stderr)
        print(f"Usage: tasks {review_cmd} <number> [--backend claude|codex|agy|grok|pi] [--model <variant>] [--prompt \"...\"] [--timeout SECONDS] [--budget USD]  (default backend: models.json default_judge, ships \"opus\" (claude); --budget is claude-only)", file=sys.stderr)
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
    # project-overridable; since 1.5.12 ships as "opus" — the all-Claude default,
    # so a Claude-first user's review doesn't exercise the codex/grok adapter
    # drift). --model overrides the variant.
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
            atomic_write(
                partial_log,
                f"# INCOMPLETE {review_label} — the judge was killed at the hard "
                f"timeout ({review_timeout_label}) mid-response.\n"
                f"# This is what it had written by then. It is NOT a finished "
                f"review: findings may be cut off and it reached no conclusion.\n"
                f"# The previous complete review, if any, is untouched in "
                f"{_judge_log_name(backend)}.\n\n{partial}\n",
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
        atomic_write(judge_log, _ctx_header + saved_review_text)
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
