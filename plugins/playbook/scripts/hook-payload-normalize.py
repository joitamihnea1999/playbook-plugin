#!/usr/bin/env python3
"""Normalize provider hook-payload dialects to the Claude Code schema.

The playbook bash hooks parse Claude Code's snake_case payloads
(tool_name/tool_input/prompt/...). Grok Build delivers camelCase payloads
with partially different tool vocabulary and tool_input keys — captured
live from grok 0.2.99 headless sessions (task 014):

  top-level:  toolName / toolInput / hookEventName / sessionId (camelCase)
  tool names: Edit arrives as "StrReplace", Bash as "Shell"
              (Write and Read already use their Claude names)
  input keys: file_path arrives as "path", content as "contents"
              (old_string / new_string / command already match Claude)
  prompt:     the user prompt is wrapped in <user_query>...</user_query>

This script reads ONE JSON payload on stdin and writes the normalized
payload to stdout. It is invoked once per hook run, right after
`INPUT=$(cat)`, so every downstream parse site in the hook reads the
normalized form — one normalizer instead of N per-site fallbacks
(task 014 plan-panel F-D).

Safety contract:
  - Claude payloads pass through BYTE-IDENTICALLY: a payload with no
    camelCase dialect marker (`hookEventName`/`toolName`/`toolInput`/
    `sessionId`) is echoed back exactly as received — never re-parsed,
    re-serialized, tool-renamed, or prompt-unwrapped. This makes the
    "claude unchanged" guarantee literal, not merely semantic (task 014
    impl-panel I2: unconditional transforms could rewrite a claude prompt
    that itself contained `<user_query>` tags, and re-serialization with
    json.dumps' default spaced separators broke chat-log-hook's jq-less
    `"prompt":"..."` grep fallback).
  - Any error (non-JSON stdin, unexpected shape) → the raw input is
    echoed back verbatim, so the hooks' own per-site `|| echo ""`
    fallbacks apply exactly as before this shim existed.

Cursor-compat note: Cursor's camelCase dialect shares the top-level key
shape, so this normalizer is deliberately not grok-branded.
"""
import json
import os
import re
import sys

# Claude never emits these tool names; grok 0.2.99 does (captured live).
_TOOL_NAMES = {
    "StrReplace": "Edit",
    "Shell": "Bash",
    # Grok Build current names (docs + 2026-07 live sessions): Edit/Write alias
    # to search_replace, Bash to run_terminal_command; `write` is a distinct
    # create-file tool that must still map to Write for task-gate Guard 0/1.
    "search_replace": "Edit",
    "write": "Write",
    "run_terminal_command": "Bash",
}

# Remap these even WITHOUT camelCase dialect markers (hybrid hosts, panel 020).
# StrReplace/Shell stay dialect-gated so a pure snake_case Claude payload that
# happens to mention those strings is still byte-identical (task 014 contract).
_TOOL_NAMES_ALWAYS = frozenset(
    {"search_replace", "write", "run_terminal_command"}
)

# grok-native tool_input keys → Claude keys. Applied additively (grok key
# kept, Claude key added) and only when the Claude key is absent.
_INPUT_KEYS = {"path": "file_path", "contents": "content"}

_TOP_KEYS = {
    "hookEventName": "hook_event_name",
    "sessionId": "session_id",
    "toolName": "tool_name",
    "toolInput": "tool_input",
    "workspaceRoot": "workspace_root",
    "transcriptPath": "transcript_path",
}

# Presence of ANY of these top-level camelCase keys marks a grok/Cursor-dialect
# payload. Absent → treat as a native claude payload and pass through untouched
# (the byte-identity contract). Claude never emits camelCase top-level keys.
_DIALECT_MARKERS = ("hookEventName", "toolName", "toolInput", "sessionId")

# grok wraps the UserPromptSubmit prompt; unwrap only when the wrapper spans
# the whole value (a Claude prompt merely MENTIONING the tag is untouched).
_USER_QUERY_RE = re.compile(
    r"^\s*<user_query>\s*(.*?)\s*</user_query>\s*$", re.DOTALL
)


def is_foreign_dialect(payload) -> bool:
    """True iff the payload carries a camelCase dialect marker (grok/Cursor).

    A native claude payload has none of these, so normalize() leaves it
    entirely alone — the transforms below only ever run on foreign dialects.
    """
    return isinstance(payload, dict) and any(k in payload for k in _DIALECT_MARKERS)


def has_foreign_tool_name(payload) -> bool:
    """True if tool_name is a Grok name that must remap without dialect markers.

    Hybrid hosts may send write/search_replace/run_terminal_command with
    snake_case keys only; without remap, Guard 1 fail-opens (panel 020).
    StrReplace/Shell remain dialect-marker-gated (byte-identity for Claude).
    """
    if not isinstance(payload, dict):
        return False
    tn = payload.get("tool_name")
    return isinstance(tn, str) and tn in _TOOL_NAMES_ALWAYS


def needs_normalize(payload) -> bool:
    return is_foreign_dialect(payload) or has_foreign_tool_name(payload)


def normalize(payload):
    if not needs_normalize(payload):
        return payload
    out = dict(payload)
    for camel, snake in _TOP_KEYS.items():
        if camel in out and snake not in out:
            out[snake] = out[camel]
    tool_name = out.get("tool_name")
    if isinstance(tool_name, str) and tool_name in _TOOL_NAMES:
        out["tool_name"] = _TOOL_NAMES[tool_name]
    tool_input = out.get("tool_input")
    if isinstance(tool_input, dict):
        tool_input = dict(tool_input)
        for theirs, ours in _INPUT_KEYS.items():
            if theirs in tool_input and ours not in tool_input:
                tool_input[ours] = tool_input[theirs]
        out["tool_input"] = tool_input
    prompt = out.get("prompt")
    if isinstance(prompt, str):
        m = _USER_QUERY_RE.match(prompt)
        if m:
            out["prompt"] = m.group(1)
    return out


# --- fused field extraction (--emit-fields) ---------------------------------
#
# The hooks used to spend one python3 process per field: normalize, then
# tool_name, then file_path, then os.path.normpath — four interpreter starts
# (~14 ms each, bare) parsing the same JSON three times, on EVERY tool call.
# `--emit-fields` does all of it in the one process that was already being
# spawned for normalization.
#
# Wire format: NUL-delimited records, because a command or a path may contain
# newlines and a line-based protocol would silently truncate it. bash reads
# them with `IFS= read -r -d ''`. The leading sentinel lets the hook tell "the
# fused path ran" from "python is missing / the script died", and fall back to
# the original per-field extraction in that case rather than treating an empty
# tool_name as fact — a gate that cannot read its payload must not fail open.
FIELDS_SENTINEL = "pb-fields-v2"


def _wire_safe(value: str) -> str:
    """Return a field that bash can receive without changing the wire frame.

    Truncate at the first NUL — the wire delimiter must not appear in a field.

    JSON encodes a literal NUL as \\u0000, so a payload can smuggle the delimiter
    into a field value. Emitted naively, `file_path` = "/tmp/a.py\\u0000/.agent/x"
    becomes TWO records: the path slot gets "/tmp/a.py" and "/.agent/x" slides
    into the normpath slot — which the gate matches against its `*/.agent/*`
    exemption and ALLOWS, while every later record (including the payload) ends
    up misaligned. Measured before this guard: the gate returned 0 on an edit to
    real code. A fail-open in the enforcing gate, which the doctrine forbids.

    Truncation rather than rejection because it is also the FAITHFUL reading: a
    path carrying an embedded NUL, had it reached any syscall, would be
    "/tmp/a.py". So the frame stays intact and the path is judged as the code file
    it actually names — blocked, not exempted.

    Lone UTF-16 surrogates are legal JSON escapes but are not UTF-8 encodable.
    Replace them with U+FFFD before stdout encoding.  Otherwise the producer
    dies after parsing a valid payload and the enforcing consumer is forced
    onto its slower recovery path.  U+FFFD matches the host-facing effective
    filename on runtimes which repair an unpaired surrogate during UTF-8
    conversion, and (critically) preserves the real suffix for classification.
    """
    if not isinstance(value, str):
        return value
    value = value.split("\0", 1)[0]
    return "".join(
        "\ufffd" if 0xD800 <= ord(ch) <= 0xDFFF else ch
        for ch in value
    )


def extract_fields(payload):
    """(tool_name, path, normpath_of_path, command, transcript_path).

    `path` is tool-specific. NotebookEdit acts on notebook_path even if a
    foreign/over-specified payload also carries file_path; every other editing
    tool acts on file_path. A decoy management file must not exempt the real
    notebook target.
    normpath is applied unconditionally, so an absent path yields "." exactly as
    `os.path.normpath('')` did at the original call site.

    transcript_path carries the state-echo hook's sanitization with it: a value
    containing a newline is dropped rather than passed on, because that file is
    written as the monitor's one-line pointer and a second line there is a
    pointer-file injection.
    """
    ti = payload.get("tool_input") if isinstance(payload, dict) else None
    ti = ti if isinstance(ti, dict) else {}
    tool_name = payload.get("tool_name", "") if isinstance(payload, dict) else ""
    path = (ti.get("notebook_path", "") if tool_name == "NotebookEdit"
            else ti.get("file_path", ""))
    command = ti.get("command", "")
    transcript = payload.get("transcript_path", "") if isinstance(payload, dict) else ""
    if not (isinstance(transcript, str) and transcript
            and "\n" not in transcript and "\r" not in transcript):
        transcript = ""
    # Every field is truncated at the first NUL before it reaches the wire, so a
    # \u0000 in the payload can never shift the frame (see _wire_safe). normpath is
    # taken on the TRUNCATED path — judging the untruncated one would defeat it.
    tool_name = _wire_safe(tool_name) if isinstance(tool_name, str) else ""
    path = _wire_safe(path) if isinstance(path, str) else ""
    command = _wire_safe(command) if isinstance(command, str) else ""
    return (
        tool_name,
        path,
        # normpath re-introduces `\` on Windows; the gate matches this field
        # against `*/.agent/*` (forward-slash), so a native-separator value would
        # break the exemption. Re-normalize to `/`. No-op on POSIX (os.sep=="/").
        os.path.normpath(path).replace(os.sep, "/"),
        command,
        _wire_safe(transcript),
    )


def emit_fields(raw):
    """Write the sentinel, the four fields, then the payload — all NUL-delimited.

    The payload record honours the same byte-identity contract as the plain
    mode: a native claude payload is echoed back exactly as received.
    """
    try:
        payload = json.loads(raw)
    except Exception:
        # A malformed payload is not a successful fused read.  Emit no valid
        # sentinel so the enforcing hook takes its recovery path and blocks if
        # the raw payload still cannot be decoded.  State-echo is advisory and
        # simply retains its old best-effort behavior on the same path.
        out = ["pb-fields-error-v2", "", "", "", "", "", raw]
    else:
        fields = extract_fields(normalize(payload))
        body = raw if not needs_normalize(payload) else json.dumps(
            normalize(payload), separators=(",", ":"))
        out = [FIELDS_SENTINEL, *fields, body]
    sys.stdout.write("\0".join(out) + "\0")


def main():
    # This is an ENFORCING hook. On Windows, stdin/stdout default to the console
    # codepage (cp1252), so reading a UTF-8 payload or writing a field that holds
    # a non-ASCII char (e.g. the U+FFFD _wire_safe emits for a lone surrogate)
    # raises UnicodeEncodeError — the producer dies mid-frame and the consumer is
    # forced onto its slower recovery path. Force UTF-8, matching tasks/cli.py.
    # newline="" disables newline TRANSLATION on the same streams: Windows
    # stdout otherwise rewrites every '\n' in a field to '\r\n' (and stdin folds
    # '\r\n' to '\n' on read), so the NUL-framed wire delivered altered bytes to
    # the enforcing bash consumer and the byte-identity payload echo was not
    # byte-identical. Newlines are DATA on this wire — only NUL delimits.
    # Encoding is a no-op on POSIX, where the streams are already UTF-8.
    for _stream in (sys.stdin, sys.stdout):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace", newline="")
    raw = sys.stdin.read()
    if "--emit-fields" in sys.argv[1:]:
        emit_fields(raw)
        return
    try:
        payload = json.loads(raw)
    except Exception:
        sys.stdout.write(raw)  # non-JSON → verbatim (hooks' own fallbacks apply)
        return
    if not needs_normalize(payload):
        sys.stdout.write(raw)  # native claude → BYTE-IDENTICAL passthrough
        return
    # Foreign dialect / foreign tool names: emit the normalized payload.
    # Compact separators keep the output shape claude-native (no space after
    # ':'/',') so downstream grep fallbacks that expect the compact form still match.
    sys.stdout.write(json.dumps(normalize(payload), separators=(",", ":")))

if __name__ == "__main__":
    main()
