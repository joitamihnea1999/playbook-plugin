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
| `seat`        | string        | the judge spec as `model:effort` (e.g. `claude:opus:high`, `codex:gpt-5.6-terra:medium`) |
| `task`        | string        | task number (`"042"`) or `"-"` for a taskless / `--prompt` review  |
| `round`       | int           | review iteration (see the round note below); `0` = unknown         |
| `duration_ms` | int, optional | wall time of the judge subprocess in milliseconds (absent if unknown) |
| `status`      | string        | `ok` \| `fail` \| `timeout` \| `dnf` (did-not-finish / spawn error)|
| `usage`       | object        | token usage — see the usage note below                            |

`seat` carries reasoning effort so spend can be attributed by `model:effort`
(the owner's ask). codex and grok already encode effort in the model variant
(`gpt-5.6-terra:medium`, `grok-4.6:high`); the claude judge runs at a fixed
`--effort high` not present in its variant, so `:high` is appended for claude
seats. A codex/grok seat configured WITHOUT an explicit effort in its variant is
recorded without one (the ambient CLI default is not resolved) — the record
reflects what was specified, never a guessed effort.

`status` is `dnf` (did-not-finish) for any judge output that begins with
`(error: …)` — this covers BOTH a pre-spawn resolution error (a missing CLI, an
adapter raise) and a judge that spawned and then crashed. A `dnf` record
therefore does NOT imply tokens were spent; it means "no usable review," with
`usage` unknown.

All numeric fields (`round`, `duration_ms`, and the usage token counts) are
magnitude-capped (to 15 digits) and all string fields are byte-capped with
control characters stripped, so **real** records stay well under the PIPE_BUF
atomic-write bound. This is a best-effort bound, not an absolute one: a
pathological caller could still exceed it (e.g. a field stuffed with `"`/`\`,
which JSON escapes to two bytes each) — an accepted bound, never a correctness
risk, because the write never affects any decision. The journal file is opened
non-blocking and without following symlinks, its `journal/` parent is lstat-
checked, and the fd is confirmed to be a regular file before any write, so a
hostile-tree swap of the journal path (or its parent dir) to a FIFO/symlink is
skipped rather than hanging or escaping the lane.

On a **panel**, one record lands per seat, all sharing the same `round`.

Example:

```json
{"ts":"2026-09-01T16:41:19Z","session_id":"pid-123","hook":"review","decision":"record","reason":"review spend","kind":"panel","seat":"claude:opus:high","task":"042","round":3,"duration_ms":48210,"status":"ok","usage":{"status":"unknown"}}
```

### The `usage` field — honest bounds

`usage` is always present and always exactly one of two shapes: the explicit
marker `{"status":"unknown"}`, or `{"status":"known","in":<int>,"out":<int>}`.
`append_review` **normalizes** it to that fixed schema — an arbitrary caller dict
is never copied verbatim (that would blow the PIPE_BUF bound), and a non-int
token count degrades to `unknown`. **Numbers are never fabricated.**

Where the numbers come from (task 056): the codex and grok judge paths ask the
CLI for **structured output** — `codex exec --json` (JSONL; the LAST
`turn.completed` event carries `usage.input_tokens` / `usage.output_tokens`) and
`grok --output-format json` (one object with the review under `text` and
`usage.input_tokens` / `usage.output_tokens`). The adapter parses the usage from
that raw stdout and returns the review PROSE as a `str` subclass that *carries* the
usage (`provider.usage.JudgeOutput`), so judge.md / judge-*.log hold prose while the
spend record holds the CLI's own numbers — copied, never derived. `in` is the
vendor's reported `input_tokens` **as reported** (codex's includes cached input;
grok's is its billed input) — recorded per vendor, not normalized across them;
cached/reasoning/cost fields are not recorded (fixed schema). A codex/grok seat is
`known` on success and also when the CLI emitted a usage frame before exiting
nonzero (tokens were spent either way), and on a timeout when the CLI had already
emitted a complete usage frame before the kill (the partial stdout is parsed strictly);
it is `unknown` on a timeout with no complete frame, on spawn errors,
and whenever the stdout is not the expected envelope — a non-int, bool or negative
count is `unknown`, never clamped into a number.

The **claude** judge still runs in plain-text mode, so its seats are `unknown` by
design. **Usage comes ONLY from the adapter-carried value** (`JudgeOutput.usage`,
attached by an adapter that itself requested structured output). Judge TEXT is
never parsed for usage: a review is prose, and prose — even a seat whose entire
output happens to be a JSON usage envelope — can never poison the field. Enabling
structured output for claude would be a deliberate adapter change that attaches
the carrier (the shared parser already knows claude's `usage` shape), not an
accident. Failure shapes fail closed: JSON-looking stdout that is not the
recognized envelope, a codex event stream with a stray line or without a terminal
`turn.completed`, a codex `error`/`turn.failed` event, or a grok `stopReason`
other than `end_turn` all produce a `(FAILED — …)` result (partial text kept as a
diagnostic, usage still recorded when a frame exists) — never a clean seat.
A reader should still treat `unknown` as an ordinary value (claude seats,
failures), never assume tokens are present.

One disclosed trade-off: grok's json mode emits its single object at the END, so
a grok seat killed at the hard timeout salvages no partial prose (its status is
`timeout` either way); codex's JSONL partial stdout is salvaged as the last
completed message text, not protocol frames.

### The `round` field — best-effort

`round` counts existing panel rounds + 1, summed across BOTH `judge.md` and its
overflow sibling `judge-archive.md` (judge.md retains only the newest 5 rounds
and archives the rest, so counting judge.md alone would cap the round at 6). For
a **panel** it is the iteration this panel becomes; for **single** and
**tail-cert** it is an approximate spend-correlation hint (those do not add a
`judge.md` round), not an exact iteration index. `0` means it could not be
determined.
