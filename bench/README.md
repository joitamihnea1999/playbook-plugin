# judgebench — measuring playbook judge seats on frozen review inputs

A **development tool** for `playbook-plugin` itself, beside `arena/`. It is
**never shipped** in `plugins/playbook/` — a `playbook:init` project never sees
it, it adds no `tasks` subcommand, no config key, no models.json field.

Spec: [`docs/plans/judge-benchmark-harness.md`](../docs/plans/judge-benchmark-harness.md)
(v1 = Tests A & B infrastructure). This README is the operator's quickstart.

## What it does

Runs N candidate judge configurations (`provider:model:effort`) against the
**same frozen historical review inputs** reconstructed from real playbook tasks
and scores them: valid findings, unique valid findings, severity, false
positives, resource use, latency. Raw measurements are persisted; composites
are computed at report time and labeled with their parameters.

```
python3 bench/judgebench.py corpus validate
python3 bench/judgebench.py corpus show [<case-id>]
python3 bench/judgebench.py run --cases all|id,id --candidates sol-med,sol-high --run-id A1 [--resume] (--fake | --live)
python3 bench/judgebench.py adjudicate <run-id>
python3 bench/judgebench.py report <run-id> [--md out.md] [--weights 8,3,1]
```

Exit codes: `0` ok · `1` completed-with-DNFs · `2` unusable (bad corpus/args).

## Safety notes (read before `--live`)

- **Real providers are opt-in.** `run` refuses unless exactly one of `--fake`
  (scripted runner, free) or `--live` (real CLIs, spends quota) is given. Tests
  never pass `--live`.
- Judges run in the existing provider sandbox, **read-only**, against a
  **temporary detached worktree** at the case's pre-review SHA — never the live
  workspace tree. The bench writes only under `bench/runs/<run-id>/` (gitignored).
- **No writes to any `.agent/`.** Spend records go to the run directory's own
  journal (`bench/runs/<run-id>/journal/enforcement.jsonl`, `kind="bench"`),
  never to a production journal.
- `--web-search` is never passed. Network is whatever production judges get
  (model APIs only).
- A live run can take hours (cases × candidates × up to 20 min). Launch it in
  the background; it is resumable with `--resume`.

## Honesty notes

- **Claude candidates cannot vary effort in v1.** The claude adapter hardcodes
  `--effort high` (`provider/adapters/claude.py`); a spec like `claude:opus:medium`
  would be recorded truthfully as what actually ran. codex and grok carry the
  effort inside the model variant and honor it.
- Token usage is `{"status":"unknown"}` unless a CLI reports it in a parseable
  form (`tasks.review._parse_judge_usage`) — numbers are never estimated.
- Dollar figures in a report are **secondary metadata** from `bench/lib/rates.py`
  (dated, non-authoritative), never a measurement.
- Runs are auditable and comparable, not deterministic: `manifest.json` is what
  makes two runs interpretable.

## Layout

```
bench/
  judgebench.py     CLI entrypoint
  lib/              cases · package · runner · records · scoring · report · rates
  corpus/           frozen case index + cases/<id>/{case.json,spec.md,diff.patch,truth.json,context/}
  runs/             gitignored; one dir per run
```

Tests live in `tests/test_judgebench_*.py` so `scripts/verify` runs them.

## Corpus build checklist (step 9 — content, not code)

Filled in when the corpus is built. Until then the corpus is empty and
`corpus validate` reports `0 cases`.
