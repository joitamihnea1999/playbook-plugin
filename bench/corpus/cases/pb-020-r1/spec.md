# 020 - C1 Tasks Handoff
## Status
pending

> **Before filling this in:** run `.claude/bin/tasks work <N>` to activate this task. Hooks won't enforce until activated.

## Risk
assertive

> Code is git-revertable, but this task ships a new user-facing command + docs/usage/CLI-reference claims about how handoff behaves (a claim about the world), and it's a new subsystem. Instrument that would reveal the claims false: the red-first behavior tests (section content, blocked transition, bootstrap surfacing, resume-consumes, negative control) + test_cli_dispatch pinning the command reaches its arm. Assertive → impl panel required at close (panel_required_for includes assertive).

> **Set this at the Structure gate** to one of: `reversible` / `irreversible` / `assertive`.
> - `reversible` — the question is whether the WORLD reverts, not the diff: touches only code/config that `git revert` fully undoes. A change that deletes/migrates persisted data, alters an on-disk format, rotates a secret, rewrites history, or publishes/asserts a claim about the world is NOT reversible even when its diff reverts cleanly. Normal bar.
> - `irreversible` — deletes/migrates data, rotates a secret, rewrites history, or publishes. Needs a named rollback plan + explicit confirmation, and cannot light-close.
> - `assertive` — changes a **claim about the world** (docs, a calibration, a measurement, a "verified accurate"). Reviewed for the claim AND its instrument regardless of diff size — a docs-only diff can be the most review-worthy thing a task produces. Cannot light-close.
> - Leaving it `unclassified` is **not** the cheap way out: an unset gate cannot be evaluated, so the close is held to the same bar as `assertive`/`irreversible` (review evidence, or `--force --reason`). One word here is the whole cost.

## Intent
turn the proven manual session-handoff pattern into a tasks handoff command: writes a ## Handoff section (mechanical ~80% from state playbook keeps), prints agent instructions for the ~20% judgment, blocks with reason handoff; bootstrap surfaces an unconsumed handoff; resume via tasks work <N> consumes it

## Why
(why this matters now — urgency, context, what breaks if delayed)

## References
- [x] Context: node [2] (task lifecycle — `tasks blocked` is the honest pause; task.md IS the execution trace, survives context compaction), node [7] (lifecycle.py — cmd_blocked/cmd_work/resume path), node [20] (project_setup — cmd_bootstrap). Building blocks verified: core.py `set_task_blocked`/`resume_blocked_task`/`_is_blocked`/`_extract_status`/`_extract_head_position`/`_gate_counts`/`_iter_task_dirs`; `tasks work <N>` already calls `resume_blocked_task` when `_is_blocked` (lifecycle.py:530) — so resume-consumes-handoff is free. C2's `_code_roots` (just shipped) gives the nested code-root list for the handoff's per-root git summary.
- Playbook: playbook/Build
- Note: Don't hardcode task numbers in plans — `.claude/bin/tasks new` auto-increments.

### Recent Chat (auto-captured at activation — review and remove unrelated)

**[M150]** [2026-08-24 19:16:41]
You are Session B of the 1.5.38 batch in this playbook-managed workspace. Session A shipped A1/A2/A3 to branch fix/1.5.38-batch in the nested playbook-plugin/ checkout (HEAD 7edeaaa, pushed; tasks 008...

**[M151]** [2026-08-24 19:53:13]
<task-notification> <task-id>bfjrgkqq0</task-id> <tool-use-id>toolu_01YL7msdAaGGz3faWBTmeqZ1</tool-use-id> <output-file>/tmp/claude-1000/-home-mihnea-Documents-Workspace-playbook-plugin-dev/0c041f51-2...

**[M152]** [2026-08-24 20:28:16]
continue the 1.5.38 Session B batch: process B3 impl panel result, then B4-B6

**[M153]** [2026-08-24 20:44:25]
<task-notification> <task-id>b6xchi7n9</task-id> <tool-use-id>toolu_01Xag8zqUzUkLbspPbhX5sQj</tool-use-id> <output-file>/tmp/claude-1000/-home-mihnea-Documents-Workspace-playbook-plugin-dev/0c041f51-2...

**[M154]** [2026-08-24 21:03:52]
<task-notification> <task-id>bw0p3866g</task-id> <tool-use-id>toolu_014aEuRhXfqaw1Q7FgJ1Lv6i</tool-use-id> <output-file>/tmp/claude-1000/-home-mihnea-Documents-Workspace-playbook-plugin-dev/0c041f51-2...

**[M155]** [2026-08-24 21:13:17]
how much untill you finish? do you already have a final report to give me?

**[M156]** [2026-08-24 21:22:52]
<task-notification> <task-id>b4vfx0d8q</task-id> <tool-use-id>toolu_01MfK5NvKPw974GExA1UHwWZ</tool-use-id> <output-file>/tmp/claude-1000/-home-mihnea-Documents-Workspace-playbook-plugin-dev/0c041f51-2...

**[M157]** [2026-08-24 21:26:08]
it's output in the end: Ran 1 shell command 018 closed. Let me push and monitor CI to confirm Windows finally goes green: Ran 1 shell command CI monitor running (b4vfx0d8q) for the 018 push. Windows l...

**[M158]** [2026-08-24 21:26:31]
Done. All four CI lanes are green on the final run — the batch is complete. 1.5.38 Session B — FINAL report ✅ Branch fix/1.5.38-batch (nested playbook-plugin/). Not merged, version not bumped, as inst...

**[M159]** [2026-08-24 21:27:54]
You are Session C, the final session of the 1.5.38 batch in this playbook-managed workspace. Branch fix/1.5.38-batch in the nested playbook-plugin/ checkout carries A1-A3 + B1-B6 (HEAD 37a4e8d, pushed...

---

## Design Phase

> **Write a 1-sentence answer for each gate.** A bare checkmark means you skipped it.
> Complete these gates before writing the work plan.
> (The `/playbook` skill has workflow patterns if you need a reference.)

### Chat Log Research
- [x] Review the "Recent Chat" messages captured in References (auto-injected at `tasks work`). Remove unrelated ones. Pull key user quotes, constraints, and context into Intent/Why above. The user's actual words are the ground truth for Intent. — M159 is the Session-C batch brief carrying C1's owner-ratified design (2026-08-24). The pattern is proven 3× in this project's own history (workspace tasks 006, 010 resumed losslessly from written handoffs). Intent captures the ratified constraints.

### Understand
- [x] Restate the request in my own words. What does the user actually want? — a `tasks handoff` command that (1) writes a `## Handoff` section into the ACTIVE task with the mechanical ~80% playbook already knows (project + each code_root branch/HEAD/dirty; checked/unchecked gates + next unchecked; latest verification receipt line; a generated timestamp); (2) prints instructions telling the AGENT to append the ~20% judgment; (3) blocks the task with reason "handoff" (reuse `tasks blocked`); (4) `tasks bootstrap` surfaces an unconsumed handoff prominently; (5) resuming via `tasks work <N>` consumes it (section stays as history).
- [x] Critique: Am I solving the stated problem or a different one I find more interesting? — solving exactly the stated command; NOT adding a daemon, network, or auto-capture of the ~20% (that stays the agent's job by design).
- [x] What would "done" look like? How will we know the task succeeded? — red-first tests green for: section content correctness, blocked-state transition (reason handoff), bootstrap surfacing (names task + "resume with tasks work <N>"), resume flow (status→in_progress, section persists), and a negative control (bootstrap quiet with no handoff). Plus docs + a dispatch entry pinned by test_cli_dispatch.
- [x] What are you assuming about the existing code/architecture that you haven't verified? — verified: `tasks work <N>` already calls resume_blocked_task on a blocked task (so resume auto-consumes); set_task_blocked upserts `## Blocked` idempotently; COMMANDS tuple + dispatch chain are pinned by test_cli_dispatch (must add `handoff` to both). To verify: exact bootstrap insertion point + degrade-gracefully when git/state/receipts absent.
- [x] What is OUT of scope for this task? — auto-generating the ~20% judgment; a daemon/watcher; network/remote sync; changing `tasks blocked`/`tasks work` semantics (only reuse them); multi-handoff conflict UI (newest wins).

### Structure
- [x] What kind of work is this? (build / investigate / evaluate / decide / combination?) If combination, what's the sequence? If >15 gates or uncertain approach, pick a checkpoint where you pause and reassess direction before continuing. — build: a new command (cmd_handoff) + a core section-builder + bootstrap surfacing + dispatch wiring + docs. Sequence: red tests → cmd_handoff + builder → bootstrap surfacing → dispatch + usage → docs → impl panel. Contained; no mid-checkpoint needed.
- [x] **Set `## Risk`** (reversible / irreversible / assertive). Ask: if this claim/change were WRONG, what would show it? An `assertive` task (changes a claim about the world — docs, a calibration, a measurement) must name the instrument that would reveal the claim false. An `irreversible` task must name its rollback plan. These cannot be light-closed for being small. — set to `assertive` above; instrument = the red-first behavior tests + test_cli_dispatch; assertive → impl panel at close.

### Reflection Gates
- [x] Wrote task-specific check questions (Bad: "is this working?" Good: "Does the output include the progress counter?" — the answer should require evidence, not just yes/no) — Q1: does `## Handoff` contain project branch/HEAD/dirty + each code_root's + gate counts + next-unchecked + latest receipt line + timestamp? Q2: after handoff, is status `blocked` with reason `handoff` and NO gate touched? Q3: does bootstrap print the task name + "resume with tasks work <N>" for the newest unconsumed handoff? Q4: after `tasks work <N>`, is status `in_progress` and does `## Handoff` still exist (history)? Q5: with no blocked-handoff task, does bootstrap stay silent about handoffs? Q6: does handoff degrade gracefully with no git / no receipts / no code_roots?
- [x] Test strategy: what are you testing and how? (point tests for specific behavior, property tests via `hypothesis` for invariants on transformations/parsers/arithmetic) — a new `tests/test_handoff.py` (stdlib unittest, subprocess through the real `python3 -m tasks.cli` for the end-to-end flow + direct calls to the section builder for content): section content, blocked transition, bootstrap surfacing, resume-consumes, negative control, graceful degrade. Deterministic; no hypothesis needed.
- [x] Before the riskiest step: what would make you stop and reconsider? — riskiest = the bootstrap surfacing false-positive/negative. If bootstrap surfaces a CONSUMED handoff (resumed task) or misses an unconsumed one, STOP. Keying on status==blocked AND block-reason=="handoff" (resume flips status → consumed) is the invariant; the negative control + resume test guard it.
- [x] If judging quality before building: is the gap worth closing? — yes; owner-ratified, and the manual pattern is proven 3× in-project. Codifying it removes a lossy manual step.

### Verify
- [x] Review the work plan. If a likely growth point exists, add it to the plan now. — growth point: the section-builder's graceful-degrade branches (git/receipt/roots absent) — captured in W2. Plan covers red→builder→writer→command→dispatch→bootstrap→docs.
- [x] Does the work plan include moments where you stop and question your approach — not just execute? — yes: W1 confirms RED (unknown command) before impl; W6's STOP-condition on bootstrap false-positive/negative (consumed vs unconsumed).
- [x] Checkpoint: Would a fresh agent understand this task and execute it well? — yes; W1-W9 name exact functions (build_handoff_section, find_unconsumed_handoff, cmd_handoff) and the block-reason invariant.
- [x] The work plan below has the right granularity (not too coarse, not micro-steps) — 9 gates, one concern each; appropriate for a new command spanning core+lifecycle+cli+bootstrap+docs.

## Work Plan

> For each work section: what could go wrong? How will you know it worked? (specific check, not "looks good")
> Standard feature: 6-8 work gates + tests. Large tasks work fine — if >15 gates, add a mid-point checkpoint to reassess direction.

- [ ] W1 (RED): write `tests/test_handoff.py`
- [ ] W2 (GREEN): add `build_handoff_section(project_path, task_file)` to `core.py`
- [ ] W3 (GREEN): add `upsert` of the `## Handoff` section (idempotent replace, blockquote-safe like set_task_blocked)
- [ ] W4 (GREEN): `cmd_handoff` in `lifecycle.py`
- [ ] W5 (GREEN): dispatch
- [ ] W6 (GREEN): bootstrap surfacing
- [ ] W7 (GREEN): docs
- [ ] W8: full verify green (`cd playbook-plugin && python3 scripts/verify`); suite count moved; test_cli_dispatch green.
- [ ] W9: impl panel (assertive close); triage; then close + commit nested + push + CI + workspace task record.

---

## Pre-review
- [ ] All tests pass
- [ ] No debug artifacts
- [ ] MIND_MAP.md: update the OWNING subsystem node **in place**

