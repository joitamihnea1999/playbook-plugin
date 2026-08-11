"""Composable template components for task.md files.

Each methodology clause is a function returning a markdown string.
Templates are rendered by composing components in order.

Usage:
    from tasks.template import render_template
    content = render_template(num=1, title="My Task", task_type="feature")
"""
from __future__ import annotations

from tasks.core import PLAYBOOKS


# ---------------------------------------------------------------------------
# Components — each returns a markdown string
# ---------------------------------------------------------------------------

def header(num: int, title: str) -> str:
    return f"# {num:03d} - {title}"


def sticker() -> str:
    return """\
> **Gate discipline:** One gate \u2192 do work \u2192 check box \u2192 next gate.
> Never batch. Never backfill. The document IS the execution trace.
> **Closing a gate:** check the box, append your outcome. Never replace the original text.
> Design Phase = orientation (one gate, brief answer). Work Plan = real work (one gate, full effort).
> If you see the same gate 5+ times in the hook echo, you're drifting \u2014 STOP and update."""


def status() -> str:
    return """\
## Status
pending

> **Before filling this in:** run `.claude/bin/tasks work <N>` to activate this task. Hooks won't enforce until activated.

## Risk
unclassified

> **Set this at the Structure gate** to one of: `reversible` / `irreversible` / `assertive`.
> - `reversible` — `git revert` undoes it completely. Normal bar.
> - `irreversible` — deletes/migrates data, rotates a secret, rewrites history, or publishes. Needs a named rollback plan + explicit confirmation, and cannot light-close.
> - `assertive` — changes a **claim about the world** (docs, a calibration, a measurement, a "verified accurate"). Reviewed for the claim AND its instrument regardless of diff size — a docs-only diff can be the most review-worthy thing a task produces. Cannot light-close."""


def intent_why_refs(playbook: str) -> str:
    return f"""\
## Intent
(what we want to achieve \u2014 the outcome, not the activity)

## Why
(why this matters now \u2014 urgency, context, what breaks if delayed)

## References
- [ ] Context: `grep -Ein "keyword1|keyword2" MIND_MAP.md` \u2192 paste relevant excerpts below
- Playbook: {playbook}
- Note: Don't hardcode task numbers in plans \u2014 `.claude/bin/tasks new` auto-increments.

---"""


def design_phase_intro() -> str:
    return """\
## Design Phase

> **Write a 1-sentence answer for each gate.** A bare checkmark means you skipped it.
> Complete these gates before writing the work plan.
> (The `/playbook` skill has workflow patterns if you need a reference.)"""


def chat_log_research() -> str:
    return """\
### Chat Log Research
- [ ] Review the "Recent Chat" messages captured in References (auto-injected at `tasks work`). Remove unrelated ones. Pull key user quotes, constraints, and context into Intent/Why above. The user's actual words are the ground truth for Intent."""


def understand() -> str:
    return """\
### Understand
- [ ] Restate the request in my own words. What does the user actually want?
- [ ] Critique: Am I solving the stated problem or a different one I find more interesting?
- [ ] What would "done" look like? How will we know the task succeeded?
- [ ] What are you assuming about the existing code/architecture that you haven't verified?
- [ ] What is OUT of scope for this task?"""


def structure() -> str:
    return """\
### Structure
- [ ] What kind of work is this? (build / investigate / evaluate / decide / combination?) If combination, what's the sequence? If >15 gates or uncertain approach, pick a checkpoint where you pause and reassess direction before continuing.
- [ ] **Set `## Risk`** (reversible / irreversible / assertive). Ask: if this claim/change were WRONG, what would show it? An `assertive` task (changes a claim about the world — docs, a calibration, a measurement) must name the instrument that would reveal the claim false. An `irreversible` task must name its rollback plan. These cannot be light-closed for being small."""


def reflection_gates() -> str:
    return """\
### Reflection Gates
- [ ] Wrote task-specific check questions (Bad: "is this working?" Good: "Does the output include the progress counter?" \u2014 the answer should require evidence, not just yes/no)
- [ ] Test strategy: what are you testing and how? (point tests for specific behavior, property tests via `hypothesis` for invariants on transformations/parsers/arithmetic)
- [ ] Before the riskiest step: what would make you stop and reconsider?
- [ ] If judging quality before building: is the gap worth closing?"""



def verify() -> str:
    return """\
### Verify
- [ ] Review the work plan. If a likely growth point exists, add it to the plan now.
- [ ] Does the work plan include moments where you stop and question your approach \u2014 not just execute?
- [ ] Checkpoint: Would a fresh agent understand this task and execute it well?
- [ ] The work plan below has the right granularity (not too coarse, not micro-steps)"""


def design_phase() -> str:
    """Compose all design phase subsections."""
    parts = [
        design_phase_intro(),
        chat_log_research(),
        understand(),
        structure(),
        reflection_gates(),
        verify(),
    ]
    return "\n\n".join(parts)


def judge_section() -> str:
    return """\
## Plan Review
- [ ] Run `.claude/bin/tasks plan-review <N>` — wait for it to finish (it writes the judge's findings into this file; the judge itself is sandboxed read-only and will NOT touch your gates). Re-read this file to see its findings below, then address valid concerns by revising Work Plan gates yourself. **Justify lens:** does every work gate trace up to something in Intent/Design? Are there gates that justify nothing above them (scope creep)? Intent claims with no gate to satisfy them (gaps)?
- [ ] **Triage plan-review findings: judge = opinion, not gospel.** For each finding, document accept (with rationale) / park (with rationale) / reject (with rationale). Push back where you have concrete evidence — you live with the outcomes, the reviewer doesn't. Verify file:line claims before applying — single-judge reviews can cite wrong locations.
- [ ] *(Optional)* Run `.claude/bin/tasks panel-review <N>` for multi-model panel (writes to judge.md, not this file). Add `--prompt "..."` to append extra steering (e.g. focus area, constraint). Read judge.md with user, accept/reject findings, apply selected advice to Work Plan.

(plan review findings appear here)

---"""


def work_plan() -> str:
    return """\
## Work Plan

> For each work section: what could go wrong? How will you know it worked? (specific check, not "looks good")
> Standard feature: 6-8 work gates + tests. Large tasks work fine — if >15 gates, add a mid-point checkpoint to reassess direction.

(write work gates here)

---"""


def judge_impl_section() -> str:
    return """\
## Implementation Review
- [ ] Run `.claude/bin/tasks impl-review <N>` — wait for it to finish (it writes the judge's findings into this file; the judge itself is sandboxed read-only). Re-read findings. **Satisfy lens:** does every Intent claim trace down through code to tests? Where does the chain break?
- [ ] **Triage impl-review findings: judge = opinion, not gospel.** For each finding, document accept (with rationale) / park (with rationale) / reject (with rationale). Push back where you have concrete evidence — you live with the outcomes, the reviewer doesn't. Verify file:line claims before applying — single-judge reviews can cite wrong locations.
- [ ] *(Optional)* Run `.claude/bin/tasks panel-review <N> --mode impl` for multi-model panel review. Add `--prompt "..."` to append extra steering.

(implementation review findings appear here)

---"""


def debrief() -> str:
    return """\
## Debrief
- [ ] Freehand — work is done, stay for discussion with user. Remove this gate during Design Phase if running headless or task doesn't need debrief."""


def pre_review() -> str:
    return """\
## Pre-review
- [ ] All tests pass
- [ ] No debug artifacts
- [ ] MIND_MAP.md: update the OWNING subsystem node **in place** — a NEW node only for a genuinely new subsystem, never one node per task"""


def parked() -> str:
    return """\
## Parked
(Findings or ideas that emerged during work but are out of scope. Describe each with enough context for a future task to pick it up.)

---"""


def _intent_check(task_path: str) -> str:
    """Extract task number and return intent-check instruction for judge prompts."""
    import re as _re
    _tn = _re.search(r'[/\\](\d{3})-', task_path)
    task_number = _tn.group(1) if _tn else None
    if task_number:
        return (
            # Unconditional: the old form said "if .agent/chat_log.md exists",
            # which is false on a multi-user repo (the log lives in the lane),
            # so judges were told to skip the user's own words. `tasks context`
            # resolves the lane itself and is harmless when there is no log.
            f"Run `tasks context {task_number}` to see the user's original messages. "
            "Check whether the task addresses what the user actually asked for, not just the agent's interpretation. "
        )
    return ""


def _depth_budget_clause(soft_timeout_secs: "int | None") -> str:
    """The parenthetical inside the depth instruction.

    It has to agree with `time_budget_instruction`: with no soft deadline
    configured that function emits nothing, so telling the judge to reason
    "within the soft time budget" would point at a budget the prompt never
    states. Same instruction either way — reason until the claims are grounded,
    don't pad — but only the first form names a deadline.
    """
    if soft_timeout_secs is None:
        return (
            "reason until the claims are code-grounded; depth is how hard you "
            "think, not how much you write — do not pad"
        )
    return (
        "reason until the claims are code-grounded within the soft time budget; "
        "don't pad or exhaust the budget for its own sake — the soft deadline is "
        "the target, the hard kill is hang safety only"
    )


def time_budget_instruction(
    soft_timeout_secs: "int | None" = None,
    hard_timeout_secs: "int | None" = None,
) -> str:
    """Prompt paragraph: soft wind-down + hard hang-safety.

    Soft = finish the current thought, then answer; do not open a new trail.
    Hard = process kill, only if truly stuck. Returns "" when no soft budget is
    configured — soft is the steering signal, so with no soft deadline the judge
    gets no time paragraph at all rather than a paragraph about a kill it should
    never be planning around.
    """
    if soft_timeout_secs is None:
        return ""
    from tasks.core import human_duration

    soft = human_duration(soft_timeout_secs)
    if hard_timeout_secs is None:
        hard_clause = (
            "There is no hard process kill configured — still honor the soft "
            "deadline so you do not burn unbounded tokens."
        )
    else:
        hard = human_duration(hard_timeout_secs)
        hard_clause = (
            f"A hard process kill exists only as hang safety at ~{hard} — "
            f"never plan to use it; it is not a target."
        )
    return (
        f"TIME BUDGET — soft deadline ~{soft}. {hard_clause} "
        "Self-regulate so you always produce a complete written findings response: "
        "(1) Prefer fewer deep, high-value probes over exhaustive exploration — "
        "do not pad reasoning for its own sake or open low-value side trails. "
        "(2) When you approach the soft deadline (or have used most of it): "
        "finish the SINGLE thought process you are currently in — the current "
        "hypothesis, file trail, or scenario only. Do not abandon mid-idea if "
        "finishing that one thought is what makes the finding sound. "
        "(3) After that current thought is finished, write your final findings "
        "from everything you have already grounded. "
        "(4) Do NOT start a new hypothesis, new file trail, or new investigation "
        "branch after the soft deadline. "
        "(5) A shorter fully-grounded report beats a longer incomplete one. "
    )


def plan_review_prompt(
    task_path: str,
    inline_context: bool = False,
    *,
    soft_timeout_secs: "int | None" = None,
    hard_timeout_secs: "int | None" = None,
) -> str:
    """Return the blind judge prompt for plan review (before implementation)."""
    context_location = "provided below" if inline_context else "provided in your system prompt"
    intent_check = _intent_check(task_path)
    time_budget = time_budget_instruction(soft_timeout_secs, hard_timeout_secs)
    depth_budget = _depth_budget_clause(soft_timeout_secs)

    return (
        "You are a senior engineer reviewing a PLAN — no code has been written yet. "
        f"The MIND_MAP.md and task.md are {context_location}. "
        "Read the source files referenced in the plan to understand existing patterns. "
        f"{intent_check}"
        f"{time_budget}"
        "Work the problem deeply before you write anything — spend substantial reasoning effort on the analysis, not on a long report "
        f"({depth_budget}). "
        "Where you have file access, read the relevant source and its callers/callees (don't judge from names alone) and trace, end-to-end, the data and control flow the plan would touch. "
        "Form several independent hypotheses about where this plan will fail or fall short, and for each, try to construct a concrete scenario that breaks it — keep the ones that hold up, discard the rest. "
        "Where you have file access, verify each claim against the code before committing to it; otherwise ground it in the inline task.md / mind-map context — do not treat lack of file access as a reason to drop everything. Drop only claims you cannot ground either way. "
        "All of that reasoning stays internal — your written output stays terse per the finding-format rule below. Depth of thinking, brevity of report. "
        "Then critique the plan through six lenses: "
        "(1) Intent alignment — will this approach actually fulfill the stated Intent? What's missing or underspecified? "
        "(2) Failure modes — what will go wrong that isn't addressed? Construct a concrete failing scenario. "
        "(3) Hostile sequences — for every state-changing flow the plan touches, walk: two concurrent requests (identical or conflicting); "
        "the same logical event delivered twice under DISTINCT ids; events arriving reordered; the external call succeeding while the local "
        "transaction rolls back; a crash after commit but before any post-commit step; a lost response causing the caller to retry. "
        "For each applicable sequence, name the invariant or mechanism the plan relies on AND the planned test that will prove it — "
        "an unaddressed applicable sequence is a finding. If the change touches no shared or persisted state, say so in one line and move on. "
        "(4) Test coverage — does the test plan cover the failure modes above? For pure-function code, does it identify invariants (idempotency, bounds, round-trip) worth property-testing? "
        "(5) Simplify — is anything over-engineered? What can be dropped? "
        "(6) Prove it — cite file:line evidence for claims about existing code. No hand-waving. "
        "Be specific and adversarial — your job is to find problems, not approve. "
        "Max 5 findings, Critical and Important only — drop Minor. "
        "Then, as your LAST line, report whether the cap bound you: "
        "`CAP: 5/5 reported, more remain` if you had to drop findings to fit, or "
        "`CAP: <k>/5 reported, exhausted` if you reported everything you found — "
        "so the reader can tell convergence (nothing left) from saturation (more remain). "
        "Each finding: cite file:line, 1-2 sentences stating the problem, 1 sentence stating the fix. No elaboration. "
        "DO NOT edit any files — you are sandboxed read-only and the attempt will "
        "fail. Output your findings to stdout only; the parent process writes them "
        "into the task's '## Plan Review' section, and the agent that owns the task "
        "triages them and revises its own Work Plan gates."
    )


def impl_review_prompt(
    task_path: str,
    inline_context: bool = False,
    *,
    soft_timeout_secs: "int | None" = None,
    hard_timeout_secs: "int | None" = None,
) -> str:
    """Return the blind judge prompt for implementation review (after code is written)."""
    context_location = "provided below" if inline_context else "provided in your system prompt"
    intent_check = _intent_check(task_path)
    time_budget = time_budget_instruction(soft_timeout_secs, hard_timeout_secs)
    depth_budget = _depth_budget_clause(soft_timeout_secs)

    return (
        "You are a senior engineer reviewing a COMPLETED implementation. "
        f"The MIND_MAP.md and task.md are {context_location}. "
        "Read the source files changed by this task (look at the Work Plan gates for paths). "
        f"{intent_check}"
        f"{time_budget}"
        "Work the problem deeply before you write anything — spend substantial reasoning effort on the analysis, not on a long report "
        f"({depth_budget}). "
        "Where you have file access, read the changed source and its callers/callees (don't judge from names alone) and trace the data and control flow end-to-end. "
        "Form several independent hypotheses about how this code could be wrong — bugs, edge cases, races, security — and for each, try to construct a concrete input or sequence that triggers it; keep the ones that hold up, discard the rest. "
        "For any test claim, check the test would actually fail if the behavior regressed. Where you have file access, verify each claim against the code before committing to it; otherwise ground it in the inline task.md / mind-map context — do not treat lack of file access as a reason to drop everything. Drop only claims you cannot ground either way. "
        "All of that reasoning stays internal — your written output stays terse per the finding-format rule below. Depth of thinking, brevity of report. "
        "Review through six lenses: "
        "(1) Simplify — what's unnecessary or over-engineered? What can be removed? "
        "(2) Self-critique — does the code actually fulfill the stated Intent? What would a skeptic say? "
        "(3) Bug scan — find actual bugs, edge cases, race conditions, or security issues. "
        "(4) Hostile sequences — for every state-changing flow this change touches, walk: two concurrent requests (identical or conflicting); "
        "the same logical event delivered twice under DISTINCT ids; events arriving reordered; the external call succeeding while the local "
        "transaction rolls back; a crash after commit but before any post-commit step; a lost response causing the caller to retry. "
        "For each applicable sequence, trace the persisted state and external effects, then cite the test that proves it safe or raise a "
        "finding demanding one. If the change touches no shared or persisted state, say so in one line and move on. "
        "(5) Test quality — do the tests verify Intent claims or just confirm the implementation? For pure-function code (parsers, formatters, transformations), are there untested invariants that property tests would catch? "
        "(6) Prove it works — cite file:line evidence showing correctness, or construct a concrete scenario showing failure. "
        "Be specific and adversarial — your job is to find problems, not approve. "
        "Max 5 findings, Critical and Important only — drop Minor. "
        "Then, as your LAST line, report whether the cap bound you: "
        "`CAP: 5/5 reported, more remain` if you had to drop findings to fit, or "
        "`CAP: <k>/5 reported, exhausted` if you reported everything you found — "
        "so the reader can tell convergence (nothing left) from saturation (more remain). "
        "Each finding: cite file:line, 1-2 sentences stating the problem, 1 sentence stating the fix. No elaboration. "
        "DO NOT edit any files — you are sandboxed read-only and the attempt will "
        "fail. Output your findings to stdout only; the parent process writes them "
        "into the task's '## Implementation Review' section."
    )


def panel_plan_review_prompt(
    task_path: str,
    inline_context: bool = False,
    *,
    soft_timeout_secs: "int | None" = None,
    hard_timeout_secs: "int | None" = None,
) -> str:
    """Panel judge prompt for plan review — writes to stdout, never edits task.md."""
    context_location = "provided below" if inline_context else "provided in your system prompt"
    intent_check = _intent_check(task_path)
    time_budget = time_budget_instruction(soft_timeout_secs, hard_timeout_secs)
    depth_budget = _depth_budget_clause(soft_timeout_secs)

    return (
        "You are a senior engineer reviewing a PLAN — no code has been written yet. "
        f"The MIND_MAP.md and task.md are {context_location}. "
        "Read the source files referenced in the plan to understand existing patterns. "
        f"{intent_check}"
        f"{time_budget}"
        "Work the problem deeply before you write anything — spend substantial reasoning effort on the analysis, not on a long report "
        f"({depth_budget}). "
        "Where you have file access, read the relevant source and its callers/callees (don't judge from names alone) and trace, end-to-end, the data and control flow the plan would touch. "
        "Form several independent hypotheses about where this plan will fail or fall short, and for each, try to construct a concrete scenario that breaks it — keep the ones that hold up, discard the rest. "
        "Where you have file access, verify each claim against the code before committing to it; otherwise ground it in the inline task.md / mind-map context — do not treat lack of file access as a reason to drop everything. Drop only claims you cannot ground either way. "
        "All of that reasoning stays internal — your written output stays terse per the finding-format rule below. Depth of thinking, brevity of report. "
        "Then critique the plan through six lenses: "
        "(1) Intent alignment — will this approach actually fulfill the stated Intent? What's missing or underspecified? "
        "(2) Failure modes — what will go wrong that isn't addressed? Construct a concrete failing scenario. "
        "(3) Hostile sequences — for every state-changing flow the plan touches, walk: two concurrent requests (identical or conflicting); "
        "the same logical event delivered twice under DISTINCT ids; events arriving reordered; the external call succeeding while the local "
        "transaction rolls back; a crash after commit but before any post-commit step; a lost response causing the caller to retry. "
        "For each applicable sequence, name the invariant or mechanism the plan relies on AND the planned test that will prove it — "
        "an unaddressed applicable sequence is a finding. If the change touches no shared or persisted state, say so in one line and move on. "
        "(4) Test coverage — does the test plan cover the failure modes above? For pure-function code, does it identify invariants (idempotency, bounds, round-trip) worth property-testing? "
        "(5) Simplify — is anything over-engineered? What can be dropped? "
        "(6) Prove it — cite file:line evidence for claims about existing code. No hand-waving. "
        "Be specific and adversarial — your job is to find problems, not approve. "
        "Max 5 findings, Critical and Important only — drop Minor. "
        "Then, as your LAST line, report whether the cap bound you: "
        "`CAP: 5/5 reported, more remain` if you had to drop findings to fit, or "
        "`CAP: <k>/5 reported, exhausted` if you reported everything you found — "
        "so the reader can tell convergence (nothing left) from saturation (more remain). "
        "Each finding: cite file:line, 1-2 sentences stating the problem, 1 sentence stating the fix. No elaboration. "
        "Note: your findings will be triaged by the reading agent — they will verify file:line claims before applying, push back on speculative concerns, and require concrete evidence. Self-flag any claim you cannot defend with code citation. The reading agent lives with the outcomes; you do not. "
        "DO NOT edit any files. Output your findings to stdout only."
    )


def panel_impl_review_prompt(
    task_path: str,
    inline_context: bool = False,
    *,
    soft_timeout_secs: "int | None" = None,
    hard_timeout_secs: "int | None" = None,
) -> str:
    """Panel judge prompt for impl review — writes to stdout, never edits task.md."""
    context_location = "provided below" if inline_context else "provided in your system prompt"
    intent_check = _intent_check(task_path)
    time_budget = time_budget_instruction(soft_timeout_secs, hard_timeout_secs)
    depth_budget = _depth_budget_clause(soft_timeout_secs)

    return (
        "You are a senior engineer reviewing a COMPLETED implementation. "
        f"The MIND_MAP.md and task.md are {context_location}. "
        "Read the source files changed by this task (look at the Work Plan gates for paths). "
        f"{intent_check}"
        f"{time_budget}"
        "Work the problem deeply before you write anything — spend substantial reasoning effort on the analysis, not on a long report "
        f"({depth_budget}). "
        "Where you have file access, read the changed source and its callers/callees (don't judge from names alone) and trace the data and control flow end-to-end. "
        "Form several independent hypotheses about how this code could be wrong — bugs, edge cases, races, security — and for each, try to construct a concrete input or sequence that triggers it; keep the ones that hold up, discard the rest. "
        "For any test claim, check the test would actually fail if the behavior regressed. Where you have file access, verify each claim against the code before committing to it; otherwise ground it in the inline task.md / mind-map context — do not treat lack of file access as a reason to drop everything. Drop only claims you cannot ground either way. "
        "All of that reasoning stays internal — your written output stays terse per the finding-format rule below. Depth of thinking, brevity of report. "
        "Review through six lenses: "
        "(1) Simplify — what's unnecessary or over-engineered? What can be removed? "
        "(2) Self-critique — does the code actually fulfill the stated Intent? What would a skeptic say? "
        "(3) Bug scan — find actual bugs, edge cases, race conditions, or security issues. "
        "(4) Hostile sequences — for every state-changing flow this change touches, walk: two concurrent requests (identical or conflicting); "
        "the same logical event delivered twice under DISTINCT ids; events arriving reordered; the external call succeeding while the local "
        "transaction rolls back; a crash after commit but before any post-commit step; a lost response causing the caller to retry. "
        "For each applicable sequence, trace the persisted state and external effects, then cite the test that proves it safe or raise a "
        "finding demanding one. If the change touches no shared or persisted state, say so in one line and move on. "
        "(5) Test quality — do the tests verify Intent claims or just confirm the implementation? For pure-function code (parsers, formatters, transformations), are there untested invariants that property tests would catch? "
        "(6) Prove it works — cite file:line evidence showing correctness, or construct a concrete scenario showing failure. "
        "Be specific and adversarial — your job is to find problems, not approve. "
        "Max 5 findings, Critical and Important only — drop Minor. "
        "Then, as your LAST line, report whether the cap bound you: "
        "`CAP: 5/5 reported, more remain` if you had to drop findings to fit, or "
        "`CAP: <k>/5 reported, exhausted` if you reported everything you found — "
        "so the reader can tell convergence (nothing left) from saturation (more remain). "
        "Each finding: cite file:line, 1-2 sentences stating the problem, 1 sentence stating the fix. No elaboration. "
        "Note: your findings will be triaged by the reading agent — they will verify file:line claims before applying, push back on speculative concerns, and require concrete evidence. Self-flag any claim you cannot defend with code citation. The reading agent lives with the outcomes; you do not. "
        "DO NOT edit any files. Output your findings to stdout only."
    )


# Legacy alias for backward compatibility
def judge_prompt(
    task_path: str,
    inline_context: bool = False,
    mode: str = "plan",
    *,
    soft_timeout_secs: "int | None" = None,
    hard_timeout_secs: "int | None" = None,
) -> str:
    """Deprecated: use plan_review_prompt() or impl_review_prompt() instead."""
    kwargs = dict(
        soft_timeout_secs=soft_timeout_secs,
        hard_timeout_secs=hard_timeout_secs,
    )
    if mode == "impl":
        return impl_review_prompt(task_path, inline_context, **kwargs)
    return plan_review_prompt(task_path, inline_context, **kwargs)


def design_phase_light() -> str:
    """Lightweight design phase for Fix tasks — just restate and define done."""
    return "## Design Phase\n\n" + chat_log_research() + "\n\n" + """\
### Fix Orientation
- [ ] What exactly is broken or needs cleaning up?
- [ ] What does "fixed" look like? (specific grep, test, or behavior)
- [ ] What adjacent code could this break?
- [ ] Test strategy: point tests, or also property tests (`hypothesis`) if fixing a parser/formatter/transformation?"""


def work_plan_fix() -> str:
    """Fix-specific work plan — locate, fix, verify pairs."""
    return """\
## Work Plan

> Fix/Verify pairs. What could this break?

- [ ] Fix: (what to change)
- [ ] Verify: (grep/test that confirms the fix)
- [ ] Side effects: anything else that changed? Adjacent code still works?

---"""


def design_phase_investigate() -> str:
    """Investigate-oriented design phase — hypothesis-first."""
    return "## Design Phase\n\n" + chat_log_research() + "\n\n" + """\
### Investigation Orientation
- [ ] What's the question or hypothesis? State it before looking.
- [ ] What evidence would change your mind?
- [ ] When do you stop? (convergence criteria: N rounds with no new position, or specific answer found)
- [ ] Test strategy: if findings lead to code changes, point tests or also property tests (`hypothesis`) for invariants?"""


def work_plan_investigate() -> str:
    """Investigate-specific work plan — round structure."""
    return """\
## Work Plan

> Rounds: hypothesis → test → result → checkpoint. Stop when converging.

### Round 1: [focus]
- **Hypothesis:** (before testing)
- **Test:** (what to check)
- **Result:** (what happened)
- [ ] Checkpoint: converging or scattering? New hypothesis needed?

### Round 2: [focus]
- **Hypothesis:** (refined from Round 1)
- **Test:** (what to check)
- **Result:** (what happened)
- [ ] Checkpoint: converging or scattering?

### Synthesis
- [ ] What did you learn? Key findings with evidence.
- [ ] What remains unknown? What would a follow-up task investigate?

---"""


def design_phase_evaluate() -> str:
    """Evaluate-oriented design phase — define lenses and scope."""
    return "## Design Phase\n\n" + chat_log_research() + "\n\n" + """\
### Evaluation Orientation
- [ ] What are you evaluating, and against what criteria?
- [ ] Define lenses (2-4 dimensions to assess consistently across all items)
- [ ] How many items? If >5, plan a midpoint checkpoint.
- [ ] Are you assessing or fixing? Keep them separate — assess first.
- [ ] Test strategy: if evaluation leads to fixes, point tests or also property tests (`hypothesis`) for invariants?"""


def work_plan_evaluate() -> str:
    """Evaluate-specific work plan — lenses, per-item, verdict."""
    return """\
## Work Plan

> Apply lenses consistently. Assess first, decide action after.

### Lenses
| Lens | What it measures |
|------|-----------------|
| (lens 1) | (description) |
| (lens 2) | (description) |

### Assessment
- [ ] Item 1: (apply all lenses)
- [ ] Item 2: (apply all lenses)
- [ ] Midpoint checkpoint: patterns emerging? Abort early or continue?

### Verdict
- [ ] Overall assessment: PASS / PARTIAL / FAIL
- [ ] Gaps found: cosmetic or material?
- [ ] Sufficiency: is the current state good enough, or do gaps justify action?

---"""


def standing_orders() -> str:
    return """\
## Standing Orders
- **Expand dynamically**: When you discover something you'll need to do, write new gates immediately \u2014 don't wait until you get there.
- **Steer openly**: If your direction changes, edit your open (unchecked) gates to reflect reality. The plan is alive, not a contract.
- **Never defer awareness**: The moment you realize work exists, capture it. Forgetting is the failure mode, not having too many gates."""


# ---------------------------------------------------------------------------
# CLAUDE.md init template
# ---------------------------------------------------------------------------

def claude_md(title: str) -> str:
    """Generate CLAUDE.md content for `tasks init`."""
    return f"""\
# {title}

## Start Here

```bash
.claude/bin/tasks bootstrap          # loads mind map, skills, pending tasks
```

Then **ask the user** what they want to work on. Don't autonomously pick a task.

## CLI

```bash
.claude/bin/tasks work <number>              # activate task, hook starts tracking
.claude/bin/tasks work done [--force]        # finish; bounces if gates still open (--force overrides)
.claude/bin/tasks new <type> <name> [intent] # create task — intent fills ## Intent
.claude/bin/tasks new --stub <type> <name> [intent] # stub — expands on tasks work
.claude/bin/tasks plan-review <number>       # blind plan review by independent agent
.claude/bin/tasks impl-review <number>       # blind implementation review by independent agent
.claude/bin/tasks list [--pending]           # task overview
.claude/bin/tasks status                     # current gate position
.claude/bin/tasks bootstrap                  # orientation: mind map + skills + pending
.claude/bin/sandbox --prompt "..." [--agent claude|codex|agy|pi] [--bare]  # run a contained headless subagent; `--help` for flags
```

## Don't

- Create task directories manually — always `.claude/bin/tasks new`
- Edit session state files directly (under `.agent/`, or `.agent/<user>/` in a multi-user repo) — use `.claude/bin/tasks work <N>` / `.claude/bin/tasks work done`
- Edit `## Status` in task.md directly — use `.claude/bin/tasks work done`
- Skip task.md checkboxes — they're your observable progress
- Start coding without an active task — blocked by hook until `.claude/bin/tasks work <N>`
- Use EnterPlanMode or plan files — use `.claude/bin/tasks new <type> <name>` instead, the task.md IS the plan
"""


# ---------------------------------------------------------------------------
# Bootstrap briefing
# ---------------------------------------------------------------------------

def identity_preamble() -> str:
    """One-line framing shown at the top of bootstrap."""
    return "You are a coding assistant working with a task management harness."


def mind_map_header() -> str:
    """Navigation header shown before full mind map at bootstrap."""
    return (
        "Project knowledge graph. Nodes cross-reference with [N] IDs.\n"
        "Full map below — drill into a node: grep '^\\[N\\]' MIND_MAP.md\n"
        "Format spec: /mindmap skill"
    )



def workflow_briefing() -> str:
    """Workflow rules shown at task activation (tasks work <N>)."""
    return """\
- One gate at a time: read gate → do work → check box → next gate
- Pattern templates in task.md ARE the work plan — fill them in, don't skip"""


def cli_reference() -> str:
    """CLI quick reference shown at bootstrap."""
    return """\
Tasks CLI:
  Workflow:
    tasks work <N>             activate task
    tasks work done            deactivate
    tasks freehand             user-driven mode (no gate pressure)
  Create:
    tasks new <type> <name> [intent]   create task (intent fills ## Intent)
    tasks new --stub <type> <name> [intent]   stub (expands on work)
  Review:
    tasks plan-review <N>      blind plan review
    tasks impl-review <N>      blind impl review
    tasks panel-review [<N>]   multi-model judge panel; task optional — use --prompt alone for any question, --bare to strip all context
    tasks models check         audit models.json judge pins against live availability (--no-probe skips claude probes)
    tasks models select        interactively refresh the panel in .agent/models.json
  Analysis:
    tasks retro [--since N]    project retrospective
    tasks global-retro-collect --since DATE ROOT [ROOT...]   collect cross-VM retro archive
    tasks context <N>          extract chat messages for a task
    tasks doctor               harness health check
  Info:
    tasks list [--pending]     show tasks
    tasks status               current gate position"""


def agents_md_template() -> str:
    """AGENTS.md content for Codex projects.

    Codex auto-loads AGENTS.md from the repo root (baked into its base
    instructions).  This file teaches the agent the Playbook workflow.
    Embed cli_reference() literally — current at install time.  To refresh
    after a Playbook upgrade: delete AGENTS.md, then re-run
    `tasks init --provider codex`.
    """
    return """\
# Playbook Workflow

This project uses the **Playbook task harness**.  Follow these rules on every
session — they govern how you work, not what you build.

## Start of Session

Run this first, before anything else:

    .claude/bin/tasks bootstrap

It prints the project mind map, pending tasks, and the full CLI reference.
Read it.  Then ask the user what to work on, or pick the highest-priority
pending task.

## Before Editing Code

You **must** activate a task before touching any code file:

    .claude/bin/tasks work <N>      # e.g. tasks work 042

This sets the active task.  Without it, edits are blocked.

## Working Through a Task

- Read the task.md that `tasks work` prints.
- Work **one gate at a time**: read the gate → do the work → check the box
  (append your outcome on the same line) → move to the next gate.
- Never skip gates.  Never batch-close multiple gates in one edit.
- If you discover new work, add new gates to task.md immediately.

## End of Task

    .claude/bin/tasks work done

This deactivates the task and marks it done.  Run it when all gates are
checked — not before.

## CLI Reference

{cli_ref}

## Do Not

- Edit session state files directly (under `.agent/`, or `.agent/<user>/` in a multi-user repo) — use `tasks work` / `tasks work done`.
- Create task directories manually — use `tasks new`.
- Close multiple gates in a single edit.
- Start coding without an active task.
""".format(cli_ref=cli_reference())


def antigravity_md_template() -> str:
    """GEMINI.md content for Antigravity CLI (`agy`) projects.

    agy reads GEMINI.md from project cwd (mirrors the user-level `~/.gemini/GEMINI.md`
    convention). Hook enforcement works when `tasks init --provider antigravity --hooks`
    is run: this installs a global agy plugin via `agy plugin install` that wires
    PreToolUse / PostToolUse / UserPromptSubmit / Stop hooks to the Playbook scripts.

    Model selection: agy v1.0.2 has no -m CLI flag — set the model from the agy UI
    (~/.gemini/antigravity/user_settings.pb). When upstream ships -m, panel-review
    will switch from a single judge to per-model judges automatically.
    """
    return """\
# Playbook Workflow

This project uses the **Playbook task harness**.  Hooks are installed globally
via `agy plugin install` when you run `tasks init --provider antigravity --hooks`.
Without that step the file is advisory.

## Start of Session

Run this first:

    .claude/bin/tasks bootstrap

It prints the project mind map, pending tasks, and the full CLI reference.

## Before Editing Code

Activate a task:

    .claude/bin/tasks work <N>

## Working Through a Task

Work one gate at a time.  Check each gate box before moving to the next.
Never skip.  Never batch.

## End of Task

    .claude/bin/tasks work done

## CLI Reference

{cli_ref}

## Do Not

- Edit session state files directly (under `.agent/`, or `.agent/<user>/` in a multi-user repo) — use `tasks work` / `tasks work done`.
- Create task directories manually — use `tasks new`.
- Close multiple gates in a single edit.
- Start coding without an active task.
""".format(cli_ref=cli_reference())


# ---------------------------------------------------------------------------
# CLI usage
# ---------------------------------------------------------------------------

def usage_text() -> str:
    """Usage text for `tasks --help`."""
    types = ", ".join(sorted(set(PLAYBOOKS.keys()) | {"quick"}))
    return f"""\
Usage: tasks <command> [args]

Commands:
  work <number>       Set active task (e.g. tasks work 058)
  work done [--force --reason "why"]  Finish task; runs the verify contract and
                      records a receipt; a failing verify or an unreviewed
                      assertive/irreversible task blocks (--force needs --reason)
  audit [<N>]         Run mechanical pre-panel sweeps (conflict markers, merge
                      artifacts, stale markers, + project sweeps); receipt to task.md
  parked [--all]      List open parked items across tasks (--all: incl. resolved)
  blocked "<reason>"  Pause the active task awaiting the owner's decision — an
                      honest state (not a faked checkbox); resume with work <N>
  freehand            User-driven mode (no gate pressure)
  new <type> <name> [intent]   Create task (intent pre-fills ## Intent)
  new --stub <type> <name> [intent]   Create stub (expands on work)
  list [--pending]    List all tasks with status
  status              Show head position for active tasks
  plan-review <N>     Run blind plan review
  impl-review <N>     Run blind implementation review
  panel-review [<N>]  Multi-model judge panel
                      --prompt "..."     add steering (appended to review prompt, or full mission if no task)
                      --no-mind-map      strip mind map from context
                      --bare             no context at all; --prompt is the entire prompt
  models check        Audit models.json judge pins against live availability (--no-probe: skip claude probes)
  models select       Interactively refresh the panel in .agent/models.json
  retro [--since N]   Project retrospective
  global-retro-collect --since DATE [--machine NAME] [--out DIR] [--format zip|tgz] ROOT [ROOT...]
                      Collect Playbook artifacts for a global retro archive
  context <N>         Extract chat messages for a task
  log [N] [--width W]  Compact one-line-per-message chat log (last N, body cropped to W; default all/500)
  prepare-merge [--target <branch>] [--dry-run]
                      Renumber tasks, re-sequence chat_log, report MIND_MAP collisions
                      so the branch merges cleanly into target (default: main)
  doctor              Harness health check
  bootstrap           Load mind map + skills + pending tasks
  init                Create CLAUDE.md for this project

Sandboxed subagents (separate CLI):
  .claude/bin/sandbox --prompt "..." [--agent claude|codex|agy|pi] [--bare] [--stream]
                      Run a contained headless agent (write-containment); `--help` for flags

Task types: {types}

Examples:
  tasks work 058
  tasks new feature add-auth
  tasks new build my-task Build extraction layer for retro command
  tasks new --stub research token-bug Investigate auth token refresh
  tasks plan-review 001
  tasks panel-review 001 --prompt "focus on the title-detection approach"
  tasks panel-review --prompt "which of these two designs is simpler?" --no-mind-map
  tasks panel-review --bare --prompt "read ideas.txt and pick the best story idea"
  tasks global-retro-collect --since 2026-03-14 ~/Code /data --out /tmp
  tasks list --pending"""


# ---------------------------------------------------------------------------
# Composition
def sticker_quick() -> str:
    return """\
> **Gate discipline:** One gate \u2192 do work \u2192 check box \u2192 next gate.
> Never batch. Never backfill. The document IS the execution trace."""


def render_stub_template(num: int, title: str, intent_text: str = "",
                         task_type: str | None = None) -> str:
    """Minimal stub for GTD capture. No gates, expands on `tasks work <N>`."""
    type_tag = task_type or "feature"
    parts = [
        header(num, title),
        f"<!-- stub:{type_tag} -->",
        status(),
        f"## Intent\n{intent_text}" if intent_text else "## Intent\n(fill in before expanding)",
        "## Why\n(fill in before expanding)",
        "## References\n(optional)",
    ]
    return "\n\n".join(parts) + "\n"


def render_quick_template(num: int, title: str) -> str:
    """Minimal task.md for sub-hour fixes and small work. ~3 gates, no ceremony."""
    parts = [
        header(num, title),
        sticker_quick(),
        status(),
        "## Intent\n(one line — what to do and how to verify)",
        "---",
        "## Work\n- [ ] Do the work\n- [ ] Test: verify it worked\n- [ ] Cleanup: mind map, commit",
        "## Parked\n(out of scope discoveries)",
    ]
    return "\n\n".join(parts) + "\n"


# ---------------------------------------------------------------------------

def render_template(num: int, title: str, task_type: str | None = None) -> str:
    """Compose all components into a complete task.md template.

    Args:
        num: Task number (will be zero-padded to 3 digits)
        title: Task title (will be title-cased in header)
        task_type: Optional task type for playbook reference

    Returns:
        Complete task.md content as a string
    """
    # Quick template — standalone, no PLAYBOOKS lookup
    if task_type == "quick":
        return render_quick_template(num, title)

    pattern_name = PLAYBOOKS.get(task_type) if task_type else None
    playbook_ref = f"playbook/{pattern_name}" if pattern_name else "(none)"

    # --- Eval mode: read template flags from PLAYBOOK_EVAL_CONFIG ---
    import os as _os
    _eval_cfg = {}
    _eval_config_path = _os.environ.get("PLAYBOOK_EVAL_CONFIG", "")
    if _eval_config_path:
        try:
            import json as _json
            _eval_cfg = _json.loads(open(_eval_config_path).read())
        except Exception:
            pass

    # Common parts shared by all variants
    common_start = [
        header(num, title),
    ]
    if _eval_cfg.get("sticker", "on") != "off":
        common_start.append(sticker())
    common_start += [
        status(),
        intent_why_refs(playbook_ref),
    ]
    common_end = []
    if _eval_cfg.get("debrief", "on") != "off":
        common_end.append(debrief())
    common_end += [
        pre_review(),
        parked(),
        standing_orders(),
    ]

    if pattern_name == "Fix":
        middle = [
            design_phase_light(),
            work_plan_fix(),
        ]
    elif pattern_name == "Investigate":
        middle = [
            design_phase_investigate(),
            work_plan_investigate(),
        ]
    elif pattern_name == "Evaluate":
        middle = [
            design_phase_evaluate(),
            work_plan_evaluate(),
        ]
    else:
        # Build (default) — full ceremony
        middle = []
        if _eval_cfg.get("design_phase", "on") != "off":
            middle.append(design_phase())
        if _eval_cfg.get("judge", "on") != "off":
            middle.append(judge_section())
        middle.append(work_plan())
        if _eval_cfg.get("judge", "on") != "off":
            middle.append(judge_impl_section())

    parts = common_start + middle + common_end
    return "\n\n".join(parts) + "\n"
