"""Judge token usage from a CLI's STRUCTURED output (task 056). Stdlib only.

Two judge CLIs report per-call token counts when asked for structured output:

  codex exec --json            JSONL events; the LAST ``turn.completed`` line carries
                               ``usage.input_tokens`` / ``usage.output_tokens``; the
                               review text is the LAST completed ``agent_message`` item
                               (codex's own ``-o`` semantics: the final message).
  grok --output-format json    ONE JSON object: ``text`` + ``usage.input_tokens`` /
                               ``usage.output_tokens`` (claude's ``--output-format json``
                               uses this same ``usage`` shape — enabling it for claude is
                               a deliberate future decision: it would flip ``known`` on).

Shapes were captured from live probes (codex-cli 0.153.4, grok 1.0.13, 2026-09-09)
and are pinned VERBATIM in tests/test_judge_usage.py. Numbers are COPIED from the
CLI's JSON — never derived, estimated or clamped here: a non-int, bool or negative
count is ``None`` (the journal writes ``{"status":"unknown"}``). Free-form review
prose never parses as one of these envelopes, so a judge that quotes a usage-shaped
string cannot poison the field (task 042's anchored-envelope rule, kept).

``in`` is the vendor's reported ``input_tokens`` AS REPORTED (codex's includes
cached input; grok's is the billed input) — recorded, not normalized across vendors.
"""
from __future__ import annotations

import json
from typing import Optional


class JudgeOutput(str):
    """A judge's review text that CARRIES its usage. It is a plain ``str`` for every
    existing caller and test double (``run_headless_judge`` keeps returning str);
    ``tasks.review._parse_judge_usage`` honours ``.usage`` first. Any str operation
    yields a plain str (usage None) — timeout/error strings never carry a number."""
    usage: Optional[dict]

    def __new__(cls, text: str, usage: Optional[dict] = None):
        obj = super().__new__(cls, text)
        obj.usage = usage if _valid_known(usage) else None
        return obj


def _is_count(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _valid_known(usage) -> bool:
    return (isinstance(usage, dict) and usage.get("status") == "known"
            and _is_count(usage.get("in")) and _is_count(usage.get("out")))


def _usage_from_obj(usage) -> Optional[dict]:
    """``{"input_tokens":N,"output_tokens":N}`` → known usage, else None."""
    if not isinstance(usage, dict):
        return None
    _in, _out = usage.get("input_tokens"), usage.get("output_tokens")
    if _is_count(_in) and _is_count(_out):
        return {"status": "known", "in": _in, "out": _out}
    return None


def _json_object(raw: str) -> Optional[dict]:
    s = raw.strip()
    if not (s.startswith("{") and s.endswith("}")):
        return None
    try:
        obj = json.loads(s)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _jsonl_events(raw: str, *, lenient_tail: bool = False) -> Optional[list]:
    """Parse codex ``--json`` JSONL: every non-blank line must be a JSON object with
    a ``type`` — otherwise this is not the codex envelope (None). ``lenient_tail``
    (salvage only) ignores ONE incomplete trailing line — the shape a hard-timeout
    kill leaves behind; the usage/extraction parse stays strict."""
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    events = []
    for i, line in enumerate(lines):
        try:
            ev = json.loads(line)
        except (ValueError, TypeError):
            if lenient_tail and i == len(lines) - 1 and events:
                break
            return None
        if not isinstance(ev, dict) or not isinstance(ev.get("type"), str):
            return None
        events.append(ev)
    return events or None


def codex_protocol_detected(raw: str) -> bool:
    """True when the FIRST non-blank stdout line is a codex event object — i.e.
    the CLI did emit the ``--json`` protocol. A protocol stream the strict parser
    then rejects is MALFORMED structured output, not prose: it must fail the
    seat, never be returned verbatim as a "review" (impl round 1, opus/codex)."""
    for ln in (raw or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            ev = json.loads(ln)
        except (ValueError, TypeError):
            return False
        return isinstance(ev, dict) and isinstance(ev.get("type"), str)
    return False


def extract_codex(raw: str, *, lenient_tail: bool = False) -> "Optional[tuple[str, Optional[dict], list[str]]]":
    """codex ``exec --json`` stdout → ``(review_text, usage, error_messages)``;
    None when the stdout is not that envelope (e.g. plain prose from a CLI that
    ignored the flag). ``review_text`` is the LAST completed ``agent_message``
    (``""`` when none — e.g. a failed turn); ``usage`` from the LAST
    ``turn.completed`` — the last one WINS even when invalid (a malformed final
    frame yields None, never an earlier stale count); ``error_messages`` from
    ``error`` / ``turn.failed`` events and ``item.completed`` items of type
    ``error``."""
    events = _jsonl_events(raw or "", lenient_tail=lenient_tail)
    if events is None:
        return None
    text, usage, errors = "", None, []
    completed = False
    for ev in events:
        t = ev.get("type")
        if t == "item.completed":
            item = ev.get("item")
            if isinstance(item, dict):
                if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                    text = item["text"]
                # item-level `error` items are DIAGNOSTICS (codex emits non-fatal
                # app-server-lag notices before a good message — round 2); only
                # `error` / `turn.failed` EVENTS are fatal.
        elif t == "turn.completed":
            completed = True
            usage = _usage_from_obj(ev.get("usage"))   # last wins, None if invalid
        elif t == "error" and isinstance(ev.get("message"), str):
            errors.append(ev["message"])
        elif t == "turn.failed":
            err = ev.get("error")
            if isinstance(err, dict) and isinstance(err.get("message"), str):
                errors.append(err["message"])
    if not completed and not errors and not lenient_tail:
        # A complete prefix that never reached turn.completed is not a finished
        # review (round 2, codex-medium); the salvage path (lenient_tail) is
        # exactly the place this shape is legitimate.
        errors.append("incomplete codex event stream: no turn.completed")
    return text, usage, errors


def extract_grok(raw: str) -> "Optional[tuple[str, Optional[dict], list[str]]]":
    """grok ``--output-format json`` stdout → ``(review_text, usage, error_messages)``;
    None when the stdout is not one JSON object. A ``{"type":"error","message":…}``
    object (grok's failure shape) yields ``""`` text + the message."""
    obj = _json_object(raw or "")
    if obj is None:
        return None
    if obj.get("type") == "error":
        msg = obj.get("message")
        return "", None, [msg if isinstance(msg, str) else json.dumps(obj)[:500]]
    text = obj.get("text")
    if not isinstance(text, str):
        return None          # some other JSON object — not grok's envelope
    errors = []
    stop = obj.get("stopReason")
    if isinstance(stop, str) and stop != "end_turn":
        errors.append(f"grok stopped: {stop}")   # max_tokens / refusal / cancelled … (round 2)
    return text, _usage_from_obj(obj.get("usage")), errors


def parse_usage(raw) -> Optional[dict]:
    """Best-effort token usage from a CLI's structured stdout — the ONE parser.
    Recognizes (a) a single JSON object carrying ``usage.input_tokens`` /
    ``usage.output_tokens`` (grok json; claude's shape) and (b) codex JSONL whose
    LAST ``turn.completed`` carries them. Anything else → None. Never fabricates."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    obj = _json_object(raw)
    if obj is not None:
        return _usage_from_obj(obj.get("usage"))
    got = extract_codex(raw)
    if got is not None:
        return got[1]
    return None


def judge_output_from_result(result, extract, format_judge_output) -> JudgeOutput:
    """Turn a judge subprocess result into the review text the callers expect,
    CARRYING the usage parsed from the CLI's structured stdout. One rule for both
    adapters (task 056, plan-panel + impl-round-1 convergent findings):

      * rc != 0  → `format_judge_output(result)` unchanged (`(FAILED — exit N)` +
                   labeled tails, so failure signatures stay scannable) — but the
                   usage frame, if the CLI emitted one before failing, is still
                   recorded: the tokens were spent.
      * empty    → `(no output)` (the T139 rule, via format_judge_output).
      * stdout is genuinely NON-JSON prose (a CLI that ignored the flag) →
                   returned verbatim, usage None — legacy behaviour.
      * JSON-looking stdout that is not the recognized envelope (a truncated
                   object, another schema, a codex protocol stream with a stray
                   line or a missing terminal `turn.completed`) → `(FAILED — …)`:
                   structured output was requested, so garbage never becomes a
                   "review" (round 2).
      * recognized envelope WITH error events → `(FAILED — …reported an error: …)`
                   even when a message text exists (kept after the marker as a
                   diagnostic); usage carried.
      * recognized envelope WITHOUT review text → `(FAILED — … no review text)`.
      * recognized envelope WITH review text and no errors → that text, usage carried.

    Every failure marker starts with `(FAILED — ` so `judge_failed` fails the seat
    AND `_judge_status` journals `fail` (tokens spent) on the panel, single and
    tail-cert paths alike — never the no-cost `dnf`, never a clean PASS.
    """
    raw = result.stdout or ""
    got = extract(raw) if raw.strip() else None
    usage = got[1] if got else None
    if result.returncode != 0:
        return JudgeOutput(format_judge_output(result), usage=usage)
    if not raw.strip():
        return JudgeOutput(format_judge_output(result))          # "(no output)"
    if got is None:
        head = raw.lstrip()[:1]
        if head in ("{", "[") or (extract is extract_codex and codex_protocol_detected(raw)):
            # Structured output was REQUESTED: JSON-looking stdout that is not the
            # recognized envelope (truncated object, a different schema, a stray
            # line inside the event stream) fails closed — never a verbatim
            # "review" (round 2, convergent). Only genuinely non-JSON prose
            # (a CLI that ignored the flag) keeps the verbatim legacy path.
            return JudgeOutput("(FAILED — malformed or unrecognized structured judge output: "
                               f"{raw.strip()[:300]!r})")
        return JudgeOutput(format_judge_output(result))          # verbatim prose
    text, _usage, errors = got
    detail = ("; ".join(e.strip() for e in errors if e.strip()))[:800]
    if errors:
        msg = f"(FAILED — the judge CLI reported an error: {detail})"
        if text.strip():
            msg += f"\n\n[partial text before the failure]\n{text.strip()}"
        return JudgeOutput(msg, usage=usage)
    if text.strip():
        return JudgeOutput(text.strip(), usage=usage)
    return JudgeOutput("(FAILED — structured judge output carried no review text)", usage=usage)


def salvage_text(provider: str, raw: str) -> str:
    """What to persist from a judge killed at the hard timeout. For codex the
    partial stdout is JSONL frames; hand the operator the last COMPLETED
    ``agent_message`` when there is one (readable partial findings), else the raw
    frames. grok's json mode emits its one object at the end, so a timed-out grok
    seat has nothing structured to salvage — raw (usually empty) is returned as
    today. Other providers pass through."""
    text = (raw or "").strip()
    if provider == "codex" and text:
        got = extract_codex(text, lenient_tail=True)   # a kill leaves one cut frame
        if got is not None and got[0].strip():
            return got[0].strip()
    return text
