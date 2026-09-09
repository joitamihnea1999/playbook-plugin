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


def _jsonl_events(raw: str) -> Optional[list]:
    """Parse codex ``--json`` JSONL: every non-blank line must be a JSON object with
    a ``type`` — otherwise this is not the codex envelope (None)."""
    events = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (ValueError, TypeError):
            return None
        if not isinstance(ev, dict) or not isinstance(ev.get("type"), str):
            return None
        events.append(ev)
    return events or None


def extract_codex(raw: str) -> "Optional[tuple[str, Optional[dict], list[str]]]":
    """codex ``exec --json`` stdout → ``(review_text, usage, error_messages)``;
    None when the stdout is not that envelope (e.g. plain prose from a CLI that
    ignored the flag). ``review_text`` is the LAST completed ``agent_message``
    (``""`` when none — e.g. a failed turn); ``usage`` from the LAST
    ``turn.completed``; ``error_messages`` from ``error`` / ``turn.failed`` events
    and ``item.completed`` items of type ``error``."""
    events = _jsonl_events(raw or "")
    if events is None:
        return None
    text, usage, errors = "", None, []
    for ev in events:
        t = ev.get("type")
        if t == "item.completed":
            item = ev.get("item")
            if isinstance(item, dict):
                if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                    text = item["text"]
                elif item.get("type") == "error" and isinstance(item.get("message"), str):
                    errors.append(item["message"])
        elif t == "turn.completed":
            u = _usage_from_obj(ev.get("usage"))
            if u is not None:
                usage = u
        elif t == "error" and isinstance(ev.get("message"), str):
            errors.append(ev["message"])
        elif t == "turn.failed":
            err = ev.get("error")
            if isinstance(err, dict) and isinstance(err.get("message"), str):
                errors.append(err["message"])
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
    return (text if isinstance(text, str) else ""), _usage_from_obj(obj.get("usage")), []


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
    adapters (task 056, plan-panel convergent findings):

      * rc != 0  → `format_judge_output(result)` unchanged (`(FAILED — exit N)` +
                   labeled tails, so failure signatures stay scannable) — but the
                   usage frame, if the CLI emitted one before failing, is still
                   recorded: the tokens were spent.
      * empty    → `(no output)` (the T139 rule, via format_judge_output).
      * stdout is NOT the structured envelope (a CLI that ignored the flag, plain
                   prose) → returned verbatim, usage None — legacy behaviour.
      * recognized envelope WITH review text → that text, usage carried.
      * recognized envelope WITHOUT review text (an error event, a failed turn that
                   still exited 0, an empty `text`) → `(error: …)` naming the CLI's
                   messages, so `judge_failed` marks the seat failed and it can never
                   count toward a quorum as a clean review.
    """
    raw = result.stdout or ""
    got = extract(raw) if raw.strip() else None
    usage = got[1] if got else None
    if result.returncode != 0:
        return JudgeOutput(format_judge_output(result), usage=usage)
    if not raw.strip():
        return JudgeOutput(format_judge_output(result))          # "(no output)"
    if got is None:
        return JudgeOutput(format_judge_output(result))          # verbatim prose
    text, _usage, errors = got
    if text.strip():
        return JudgeOutput(text.strip(), usage=usage)
    detail = ("; ".join(e.strip() for e in errors if e.strip()))[:800]
    msg = "(error: structured judge output carried no review text"
    msg += f": {detail})" if detail else ")"
    return JudgeOutput(msg, usage=usage)


def salvage_text(provider: str, raw: str) -> str:
    """What to persist from a judge killed at the hard timeout. For codex the
    partial stdout is JSONL frames; hand the operator the last COMPLETED
    ``agent_message`` when there is one (readable partial findings), else the raw
    frames. grok's json mode emits its one object at the end, so a timed-out grok
    seat has nothing structured to salvage — raw (usually empty) is returned as
    today. Other providers pass through."""
    text = (raw or "").strip()
    if provider == "codex" and text:
        got = extract_codex(text)
        if got is not None and got[0].strip():
            return got[0].strip()
    return text
