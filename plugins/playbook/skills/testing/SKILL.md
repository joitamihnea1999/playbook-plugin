---
name: testing
description: >
  Derive and prioritize tests from project specifications, direct human
  corrections, architecture risk, and transferable cross-project testing
  culture, then compare them with the implemented suite. Use when planning tests
  for a change, preserving a regression, judging whether tests prove the real
  contract, mining chat_log.md for recurring human pain, reusing established
  testing lessons, or running a broader confidence audit.
---

# Testing

Tests are what let a playbook agent run unsupervised without going wrong: the
better the suite, the longer the leash. The task template puts a test gate after
every work gate for exactly this reason — **this skill is the method for filling
those gates well.** Treat tests as durable perception for the next agent, and
measure whether the suite covers what the project *promises*, what the human has
*learned through use*, and what the architecture can *foresee* — not just what
was easy to assert.

## Set the effort level

Use the smallest investigation that can change the testing decision.

- **Focused change or regression:** read the current task/spec, the targeted
  relevant chat, the affected architecture, and the nearby tests. Usually
  recommend 1-3 tests.
- **Feature or subsystem review:** sample its authoritative specs, relevant human
  history, architecture boundaries, and test clusters. Recommend at most 5.
- **Full historical audit:** scan the wider chat log and suite only when
  explicitly requested or when the consequence justifies the cost.

Do not turn every preference into a test, or inventory tests without a decision
they inform.

## Freeze three pre-suite ledgers

Derive candidate needs **before** deeply inspecting test bodies. Keep the sources
separate so existing coverage does not reshape the requirements in hindsight.

### 1. Specification ledger

Read authoritative task intent and acceptance criteria (the task's `## Intent` and
Design gates), public API/CLI contracts, schemas, state models, README/design
docs, and stated compatibility, lifecycle, error, persistence, or security
guarantees.

Derive explicit requirements and their logical consequences: invalid inputs,
boundaries, state transitions, round trips, failure behavior, preservation,
idempotency, permissions, retries, concurrency, and migration behavior where the
contract implies them.

### 2. Human-signal ledger

When `.agent/chat_log.md` exists, build a compact view of the relevant human
messages — `tasks context <N>` pulls the span attributed to a task, and
`tasks log` gives the one-line-per-message timeline. Search for corrections,
surprises, repeated questions, unsafe effects, missing work, regressions, and
success claims that did not actually accomplish the goal. Expand adjacent raw
context before interpreting a signal, and cite message IDs.

Distinguish **direct human evidence** from agent summaries, generated prompts,
task prose, and inference. Do not attribute generated `HOST` content to the human
without verification.

For each material signal ask:

- What was the human trying to accomplish?
- What betrayed the expectation or required another intervention?
- What must remain true?
- What call, write, transition, disclosure, substitution, or silent recovery
  would prove recurrence?

Keep only falsifiable, consequential, or recurring issues. Label an unresolved
preference `AMBIGUOUS_INTENT` rather than inventing a contract.

### 3. Architecture-risk ledger

Inspect the implementation boundaries for risks neither the specification nor the
human history names: interruption windows, partial writes, stale caches,
malformed external responses, identity/provenance loss, lifecycle transitions,
privilege boundaries, resource limits, and platform/provider drift.

Use this pass to anticipate failures, not to rewrite the two frozen ledgers.

Use this compact schema for all three:

| Evidence | Situation | Invariant | Forbidden evidence | Owner boundary | Candidate proof |
|---|---|---|---|---|---|

## Challenge with shared culture

After freezing all three local ledgers, read [culture.md](culture.md) completely.
Use its cross-project lessons to challenge omissions and recognize failure shapes
this project has not yet named.

Do not rewrite the frozen ledgers or import a lesson as a requirement on its own.
For every relevant lesson, cite both the cultural practice and the local
specification, human signal, or architecture evidence that makes it applicable.
Mark it `ADOPT`, `PARK`, or `REJECT` with the failure prevented, the proof
boundary, the limits, and the cost. Carry adopted candidates into the suite
comparison as a separate culture-transfer source.

## Compare with the actual suite

Inspect the test configuration and collection rules, representative assertion
bodies, fixtures, CI lanes, and the production path each test exercises. Classify
every candidate:

- `STRONG`: proves the invariant at an adequate boundary.
- `PARTIAL`: covers some logic but misses a condition or forbidden effect.
- `WRONG_BOUNDARY`: passes below or beside the layer that owns the risk.
- `MISSING`: has no meaningful executable evidence.
- `EXTERNAL/MANUAL`: the honest authority is live or human-owned.
- `AMBIGUOUS_INTENT`: the evidence does not support a stable requirement.

Perform the reverse diff: keep valuable tests that none of the ledgers predicted —
they may encode proactive architecture, ecosystem contracts, or historical
failures absent from the sampled human evidence.

Do not treat a test-shaped file as executed without checking the runner. Do not
present skipped, historical, optional, untracked, or manual evidence as routine
current confidence.

## Design the proof

For each material gap:

1. Preserve the smallest realistic specification example or betrayal.
2. Assert **forbidden effects**, not only messages or return values.
3. Identify whether the component observes, advises, enforces, or performs the
   risky action.
4. Exercise the production entry point — conversions, configuration, thresholds,
   assembly — when they own the risk.
5. Choose the cheapest boundary that can disprove the claim: pure/property,
   state-machine, integration, fault-injection, packaged/live, or human judgment.
6. Label confidence honestly: `executable-open`, `controlled`, `owner-boundary`,
   `historical`, `live-current`, or `manual`.
7. For a consequential regression, temporarily introduce the named defect, require
   the test to fail **for the intended reason**, then revert it. Targeted
   sensitivity calibration — not mutation volume. (This is the same discipline the
   playbook's own suite follows: every guard has a negative control.)

Structural validity does not prove semantic usefulness. Layer mechanical
invariants with named positives/hard negatives, metamorphic checks, provenance,
and small blinded or human evaluation when *meaning* is the claim.

## Prioritize and report

Rank gaps qualitatively by consequence, recurrence/exposure, probability the test
detects the failure, and implementation-plus-maintenance cost. Recommend no more
than five confidence upgrades.

Report specification coverage and human-signal coverage **independently**. Flag
conflicts rather than silently choosing: human experience may reveal a missing or
changed specification, while an old workaround may not deserve permanent contract
status.

For each recommendation provide:

| Source | Betrayal/risk | Classification | Proposed proof | Confidence gained | Cost/limits |
|---|---|---|---|---|---|

If the user asked only for an assessment, do not implement the tests. The goal is
not "more tests" — it is enough executable evidence that the next agent can act
without making the human diagnose the same failure again.
