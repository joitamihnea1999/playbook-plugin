# Enforcement journal — record shapes

Playbook appends a best-effort, **log-only** journal of what its enforcement and
review machinery did, one JSON object per line, to the lane-resolved
`.agent/<lane>/journal/enforcement.jsonl` (root lane:
`.agent/journal/enforcement.jsonl`). It exists so an external reader — e.g.
**playbook-lens** (a separate repo) — can attribute enforcement decisions and
review spend after the fact.

**Hard contract (every writer):** a journal write failure NEVER changes or
breaks any decision or review. Each record is a single `O_APPEND` write of one
line, with no `fsync` and no locking; POSIX makes an append of at most
`PIPE_BUF` bytes atomic against concurrent appenders, so lines from separate
processes (parallel panel seats, multi-lane) do not interleave while a record
stays under that bound (≥512 bytes everywhere; 4096 on Linux). String fields are
byte-capped so real records stay well under 512 bytes. The journal is only ever
created inside an already-existing playbook-managed lane — `.agent` itself is
never minted as a side effect.

Writers live in `scripts/pb_journal.py` (`append`, `append_review`) and its bash
twin `scripts/gate-echo-lib.sh::journal_enforcement`. Guarantees:
`PB-ENFORCEMENT-JOURNAL` (enforcement decisions) and `PB-REVIEW-SPEND-JOURNAL`
(review spend).

## Common envelope

Every record carries these keys:

| key          | type   | meaning                                                        |
|--------------|--------|---------------------------------------------------------------|
| `ts`         | string | UTC timestamp, `YYYY-MM-DDTHH:MM:SSZ`                          |
| `session_id` | string | emitting session id (may be empty)                            |
| `hook`       | string | which surface emitted it (see below)                          |
| `decision`   | string | one of `allow` \| `block` \| `record`                         |
| `reason`     | string | short fixed reason string                                     |

A reader MUST tolerate unknown future keys and unknown `hook`/`decision` values
rather than erroring — this is a log, and the repo's format is the source of
truth.

## Family 1 — enforcement decisions (`decision` = `allow` / `block` / `record`)

Emitted by the gate/guard/stop/batch hooks and by the close path. `hook` is one
of `task-gate`, `command-guard`, `stop`, `gate-batch-check`, or `close`.
`task-gate` writes `allow` or `block`; `command-guard`/`stop`/`gate-batch-check`
write `block`; the **close** path writes a `record` (`hook="close"`,
`reason="verify contract"`) logging the verify bar a close ran. Optional fields:
`tool`, `path`, `command` (the command head, capped). See
`PB-ENFORCEMENT-JOURNAL`.

## Family 2 — review spend (`hook` = `review`, `decision` = `record`)

Emitted by the review runner (`tasks/review.py`) for **every judge invocation** —
each panel seat, the single judge, and the tail-cert judge — via
`pb_journal.append_review`, on its completion or timeout. The one exception is a
**tamper hard-stop**: if a read-only judge mutated the working tree, the review
emits its tamper banner and stops, recording **no** spend — the journal write
must never precede that banner (a hostile-tree hang inside it could suppress the
banner, the "operation-before-banner" class the tamper guard is built around),
so every path emits only past the tamper check. `reason` is `"review spend"`.
Additional fields:

| key           | type          | meaning                                                            |
|---------------|---------------|--------------------------------------------------------------------|
| `kind`        | string        | `panel` \| `single` \| `tail-cert`                                 |
| `seat`        | string        | the judge spec, `model` or `model:effort` (e.g. `claude:opus`)     |
| `task`        | string        | task number (`"042"`) or `"-"` for a taskless / `--prompt` review  |
| `round`       | int           | review iteration (see the round note below); `0` = unknown         |
| `duration_ms` | int, optional | wall time of the judge subprocess in milliseconds (absent if unknown) |
| `status`      | string        | `ok` \| `fail` \| `timeout` \| `dnf` (did-not-finish / spawn error)|
| `usage`       | object        | token usage — see the usage note below                            |

On a **panel**, one record lands per seat, all sharing the same `round`.

Example:

```json
{"ts":"2026-09-01T16:41:19Z","session_id":"pid-123","hook":"review","decision":"record","reason":"review spend","kind":"panel","seat":"claude:opus","task":"042","round":3,"duration_ms":48210,"status":"ok","usage":{"status":"unknown"}}
```

### The `usage` field — honest bounds

`usage` is always present and always exactly one of two shapes: the explicit
marker `{"status":"unknown"}`, or `{"status":"known","in":<int>,"out":<int>}`.
`append_review` **normalizes** it to that fixed schema — an arbitrary caller dict
is never copied verbatim (that would blow the PIPE_BUF bound), and a non-int
token count degrades to `unknown`. **Numbers are never fabricated.**

In practice `usage` is `unknown` almost always: the claude judge runs in
plain-text mode (no `--output-format json`), and codex/grok do not surface
per-call tokens on this path. The parser is **anchored to a structured
envelope** — it recognizes claude's real JSON usage shape
(`{"usage":{"input_tokens":…,"output_tokens":…}}`) only when the judge's ENTIRE
output parses as that one JSON object. This is deliberate: a bare substring
search would let a judge that merely *quotes* a usage-shaped string in its prose
poison the field with a fabricated number. Free-form review prose never parses as
a single JSON object, so it can never trip it. Populating real token counts would
require a future switch to structured judge output — a reader should treat
`unknown` as the common case and `known` as a bonus, never assume tokens are
present.

### The `round` field — best-effort

`round` counts existing panel rounds + 1, summed across BOTH `judge.md` and its
overflow sibling `judge-archive.md` (judge.md retains only the newest 5 rounds
and archives the rest, so counting judge.md alone would cap the round at 6). For
a **panel** it is the iteration this panel becomes; for **single** and
**tail-cert** it is an approximate spend-correlation hint (those do not add a
`judge.md` round), not an exact iteration index. `0` means it could not be
determined.
