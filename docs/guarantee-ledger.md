# Guarantee ledger

`guarantee-ledger.json` is the Phase 1 inventory of externally meaningful
Playbook guarantees. It is evidence metadata, not a second implementation of
runtime policy: every entry points to the production owner and to concrete
evidence (or says that evidence is missing).

The stable ID is the review key. `failure_consequence` uses Critical, High,
Medium, or Low. `claim_kind` separates deterministic mechanisms from semantic
outcome claims. Evidence uses only the seven classifications mandated by the
stabilization plan. `boundary` records what the cited proof actually crosses;
a mock or direct function test remains `unit`, regardless of its test name.
`negative_control` is either `null` or an exact `{path, reference}` binding to
a tracked adverse test/scenario. A boolean assertion is not evidence, and a
control that repeats its own proof is not a control: the validator rejects a
`negative_control` whose `{path, reference}` equals the proof it hangs on.
Python references use an AST-resolved `Class.method` or top-level function;
shell references use `scenario:ID`; document/manual references use
`section:Exact heading`. Two guarantees may not carry the same statement, and
one guarantee may not cite the same proof target twice.

`owner` is an array of exact `{path, reference}` bindings, so the
path-to-symbol relationship is never ambiguous. The reference resolves
mechanically against the owning file:

| Reference | Applies to | Resolved by |
|---|---|---|
| `symbol:name` / `symbol:Class.name` | `.py` | AST — module-level functions, classes, methods, and constants |
| `function:name` | shell scripts | the `name() {` definition |
| `case-arm:pattern` | shell scripts | a `pattern)` arm inside a `case` block |
| `section:Exact heading` | `.md`, `.template` | a unique Markdown heading |
| `pointer:/a/b` | `.json` | an RFC 6901 JSON pointer |
| `whole-file` | anything with no finer named seam | tracked-path existence only |

`whole-file` is refused wherever a finer seam mechanically exists: for a Python
module that defines any module-level name, and for a Markdown file that has any
heading. It stays available for shell scripts and other files whose guarantee
lives in the script body rather than in an incidental helper. A nonexistent,
non-unique, or mismatched owner reference is a validation error, not a comment.

Statuses are deliberately non-interchangeable:

- `verified_by_current_executable_evidence`: current executable evidence crosses
  the boundary claimed by the entry. For mechanical Critical/High entries the
  validator additionally requires an executable integration proof with a
  targeted negative control.
- `partially_evidenced`: some relevant evidence exists, but a provider,
  platform, install, containment, or semantic boundary remains unproved.
- `unverified`: the claim is identified but current evidence does not establish
  it.
- `missing_evidence`: no qualifying current proof exists.
- `known_violation`: current reproduced behavior **contradicts** the stated guarantee.
  This is not a gap in evidence — it is evidence of a defect, and it is deliberately
  not interchangeable with the three statuses above.
- `not_applicable`: the named guarantee does not apply to the recorded cells.

A `known_violation` entry keeps the *intended* public guarantee as its statement — the
promise is not quietly rewritten to match the bug — and carries a
`violation_reproduction` record that no other status may have:

| Field | Meaning | Enforced as |
|---|---|---|
| `type` | reproduction evidence class | executable unit/integration, live-platform, or manual |
| `path` | tracked file holding the reproduction | must be a tracked path |
| `reference` | exact selector | resolved like any proof (`Class.method`, `scenario:ID`, `section:…`) |
| `invocation` | the command or bounded condition that reproduces it | non-empty |
| `observed` | what actually happened | non-empty |
| `platforms` | where it is *reproduced* (not merely suspected) | platform enum, unique |
| `artifacts` | the tracked release artifacts implicated | each must be tracked |
| `phase` | where the runtime correction is scheduled | 2–11, and must appear in `follow_up_phases` |

A Critical or High `known_violation` must schedule Phase 2 and must be reproduced by
executable or live-platform evidence — manual judgment alone is not enough to assert a
defect against a release artifact. The reproduction may not cite the same
`{path, reference}` as one of the entry's own proofs: a single target cannot both prove
a guarantee and reproduce its violation.

`platforms` and `artifacts` answer two different questions and are held to two different
standards. `platforms` is where the failure was actually **executed**; a platform or shell
whose failure is inferred from source but not run is recorded in the limitations and in
`artifacts`, never in `platforms`. `artifacts` is every shipped, tracked file **implicated**
by the defect, which includes a sibling artifact carrying structurally equivalent code that
no available host can execute — under-listing it would hide half the fix from whoever
performs the correction.

**What the validator cannot check.** The validator enforces that `platforms` are members of
the platform enum and that every `artifacts` entry is tracked. It cannot verify that a
listed platform was genuinely reproduced or that a listed artifact is genuinely implicated:
a reproduction that falsely claims `["linux", "macos", "windows-git-bash"]` validates
clean. Both fields are author claims backed by the recorded `invocation` and `observed`,
not machine-checked facts. Schema-validity is therefore not verification, and a reviewer
must read the reproduction rather than trust the green exit code.

Every non-green entry names its limitation and follow-up phase. Required live
provider/platform cells are separately enumerated and must be scheduled for
Phase 8. Semantic claims must provide a controlled protocol, at least two
repeated samples, and a bounded conclusion; a deterministic mechanism test may
not be substituted for agent-quality judgment.

Validate and print the coverage/missing-evidence report with:

```bash
python3.10 scripts/guarantee_ledger.py --summary
```

The stdlib-only validator strictly loads both JSON files with duplicate-member
rejection, validates `ledger_version` as a real canonical `YYYY-MM-DD` date,
checks required fields, IDs, enums, minimum sizes and uniqueness, tracked
owner/proof/artifact paths, mechanically resolved owner bindings, exact evidence
targets, distinct adverse controls, the `known_violation` reproduction contract,
Phase 8 live scheduling, and the Critical/High
integration-plus-negative-control gate. The CLI and the tests use this same load
and validation path.

**Measured mutation coverage.** The counting convention is one *diagnostic arm*
per `errors.append(<message>)` statement plus one per element of a
`return [<message>, ...]` list in `scripts/guarantee_ledger.py`, derived from the
module's AST rather than counted by hand. On that convention the validator has
**114 diagnostic arms** (105 distinct message templates, 8 of which are emitted
from more than one arm). **113 of the 114 are demonstrated red-first**: the arm is
neutralised in a `/tmp` copy of the validator whose `ROOT` is pinned to this
worktree — the pinned unmutated copy is confirmed green first — and the named test
in `tests/test_guarantee_ledger.py` is observed to fail. Removal of any single one
of those 113 is red. Attribution is exclusive for 75 of them — the failing test
fires for that arm alone — while the remaining 38 are caught by a test that also
covers sibling arms, chiefly `test_every_material_schema_constraint_is_enforced`,
one method whose subTests sweep all 55 material schema constraints at once.

**The one arm with no red-first demonstration, and why.** The third
`ledger_version` arm — the `parsed_version.isoformat() != ledger_version`
round-trip check — is **unreachable by construction**, so no input can distinguish
its presence. Its two guards leave it nothing to catch: the `\d{4}-\d{2}-\d{2}`
fullmatch admits only a 4-2-2 digit layout, and `datetime.date.fromisoformat`
rejects every such string it cannot reproduce exactly (non-ASCII digits included,
on the declared Python 3.10 floor).
`test_ledger_version_round_trip_arm_is_unreachable_by_construction` pins that
claim mechanically over 60,002 structured candidates, so this paragraph fails as a
test if the arm ever becomes reachable. The contract it nominally guards **is**
enforced, by the two arms that can fire: removing those two while *keeping* the
round-trip arm turns the module red. The arm is retained deliberately —
defence-in-depth against a laxer future `fromisoformat` — because deleting a
fail-safe to round a coverage figure up to 114/114 would weaken the validator to
flatter the number.

Two arms that look like duplicates are not. `_reference_errors` is **skipped
entirely** for proofs whose `type` is `missing` (they carry a null `path`), so the
blank-reference check inside `validate_ledger` is the **sole** enforcer for a blank
`reference` on a `missing`-type proof — not a redundant copy of the identical check
in `_reference_errors`. Removing it alone is a real fail-open, and
`test_missing_type_proof_with_a_blank_reference_rejected` is the control that
catches that removal.

The JSON Schema is the portable shape documentation; this repository does not
bundle or claim a general JSON Schema implementation. Instead, the validator
mechanically compares the schema's material fields, enums, and constraints with
its custom contract before validating the ledger, and
`test_schema_constraint_inventory_is_complete` walks the schema for all eleven
material keyword classes it inspects — `const`, `enum`, `pattern`, `format`,
`minItems`, `maxItems`, `uniqueItems`, `minLength`, `maxLength`, `minimum`, and
`maximum` — and fails if any occurrence of one has no enforcing mutation test.
Those eleven are the classes the inventory *looks for*, not a claim about what
the schema currently contains: the present schema has 55 material constraints
drawn from nine of them, while `maxItems` and `maxLength` do not occur in it at
all today, so both are watched for rather than currently enforced. Adding a
constraint to the schema without an enforcing rule is therefore a test failure,
not a silent fail-open.
Repository-aware rules that JSON Schema cannot establish—Git tracking,
AST/shell/section/pointer resolution, control distinctness, and evidence
sufficiency—remain explicit custom checks. A schema/validator mismatch is a
validation error, not an informational warning.

`--summary` counts known violations separately from missing/partial evidence and prints
each one's reproduction, invocation, implicated artifacts, and correction phase. They are
also included in the Critical/High non-green total, because a contradicted guarantee is
not green.

What the ledger does **not** claim: a `verified_by_current_executable_evidence`
entry means the retained statement is established by the exact proofs bound to
it on Linux, with the recorded boundary and limitations. It does not mean the
guarantee holds on every platform or provider, and it is not a statement about
Playbook as a whole. Every clause that current evidence does not reach is
either split into its own entry or recorded as a non-green gap with its
follow-up phase.
