#!/usr/bin/env python3
"""The fused hook field extraction (`hook-payload-normalize.py --emit-fields`).

Audit finding (1.5.31): the hooks spent one python3 process per field —
normalize, then tool_name, then file_path, then os.path.normpath — four
interpreter starts on EVERY tool call, parsing the same JSON three times.
`--emit-fields` does all of it in the one process already being spawned to
normalize, cutting the measured per-Bash-call hook cost roughly in half.

This is the wire-format spec. What it pins:

  * PARITY — the fused values equal what the old per-field `python3 -c`
    one-liners produced, for every payload shape the hooks see. A refactor that
    silently changes what the ENFORCING gate reads is the failure mode here, so
    the old expressions are re-evaluated in-process and compared row by row.
  * NUL framing — a command or path containing newlines must survive. A
    line-based protocol would truncate a multi-line command, and the gate would
    then judge a different command than the one about to run.
  * The SENTINEL — it separates "the fused read ran" from "python is missing /
    the script died". Without that distinction a hook would read an empty
    tool_name as fact and fail open, so the hooks fall back to per-field
    extraction whenever the sentinel is absent.
  * BYTE IDENTITY — the payload record still honours the task-014 contract: a
    native claude payload comes back exactly as received.

Run: python3 tests/test_fused_payload_fields.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
SCRIPTS = _HERE.parent / "plugins/playbook/scripts"
NORMALIZER = SCRIPTS / "hook-payload-normalize.py"

SENTINEL = "pb-fields-v2"
N_FIELDS = 7  # sentinel, tool_name, path, normpath, command, transcript, payload

# Every payload shape the three hooks actually meet.
PAYLOADS = {
    "claude edit": {"tool_name": "Edit",
                    "tool_input": {"file_path": "src/app.py",
                                   "old_string": "a", "new_string": "b"}},
    "claude write": {"tool_name": "Write",
                     "tool_input": {"file_path": ".agent/tasks/001-x/task.md",
                                    "content": "x"}},
    "claude bash": {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}},
    "notebook edit": {"tool_name": "NotebookEdit",
                      "tool_input": {"notebook_path": "nb.ipynb"}},
    "traversal path": {"tool_name": "Edit",
                       "tool_input": {"file_path": ".agent/../src/main.py"}},
    "no tool_input": {"tool_name": "Read"},
    "empty payload": {},
    "grok camelCase": {"toolName": "StrReplace",
                       "toolInput": {"path": "src/x.py"}},
    "grok shell": {"toolName": "Shell", "toolInput": {"command": "ls -la"}},
    "multiline command": {"tool_name": "Bash",
                          "tool_input": {"command": "set -e\nmake test\n"}},
    "unicode path": {"tool_name": "Edit",
                     "tool_input": {"file_path": "src/café/ação.py"}},
    "quotes in command": {"tool_name": "Bash",
                          "tool_input": {"command": "echo \"a'b\" $HOME `id`"}},
    "wrong types": {"tool_name": 42, "tool_input": {"file_path": None,
                                                    "command": ["a", "b"]}},
    "tool_input not a dict": {"tool_name": "Edit", "tool_input": "nope"},
    "transcript path": {"tool_name": "Read", "transcript_path": "/t/s.jsonl"},
    "transcript injection": {"tool_name": "Read",
                             "transcript_path": "/t/s.jsonl\nevil"},
}


def emit_fields(raw: str) -> "list[str]":
    r = subprocess.run([sys.executable, str(NORMALIZER), "--emit-fields"],
                       input=raw, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert out.endswith("\0"), "every record must be NUL-terminated"
    return out[:-1].split("\0")


def plain_normalize(raw: str) -> str:
    r = subprocess.run([sys.executable, str(NORMALIZER)],
                       input=raw, capture_output=True, text=True, timeout=60)
    return r.stdout


# The pre-fusion one-liners, verbatim from the hooks — the parity oracle.
def old_tool_name(raw):
    d = json.loads(raw)
    return d.get("tool_name", "")


def old_file_path(raw):
    return json.loads(raw).get("tool_input", {}).get("file_path", "")


def old_file_or_notebook(raw):
    d = json.loads(raw).get("tool_input", {})
    return d.get("file_path", "") or d.get("notebook_path", "")


def old_command(raw):
    return json.loads(raw).get("tool_input", {}).get("command", "")


def old_normpath(fp):
    return os.path.normpath(fp)


class WireFormat(unittest.TestCase):
    def test_every_payload_yields_the_full_record_set(self):
        for name, payload in PAYLOADS.items():
            with self.subTest(payload=name):
                fields = emit_fields(json.dumps(payload))
                self.assertEqual(len(fields), N_FIELDS,
                                 f"{name}: got {len(fields)} records")
                self.assertEqual(fields[0], SENTINEL)

    def test_non_json_still_emits_the_sentinel_and_the_raw_payload(self):
        """The hooks must be able to tell a malformed payload (fields empty, and
        that is the truth) from a dead script (no sentinel → fall back)."""
        fields = emit_fields("this is not json")
        self.assertEqual(fields[0], SENTINEL)
        self.assertEqual(fields[1:6], ["", "", "", "", ""])
        self.assertEqual(fields[6], "this is not json")

    def test_empty_stdin(self):
        fields = emit_fields("")
        self.assertEqual(fields[0], SENTINEL)
        self.assertEqual(fields[6], "")


class ParityWithThePerFieldExtraction(unittest.TestCase):
    """The values the gate reads must not change. This is the whole risk."""

    def test_tool_name_parity(self):
        for name, payload in PAYLOADS.items():
            with self.subTest(payload=name):
                raw = json.dumps(payload)
                fields = emit_fields(raw)
                normalized = plain_normalize(raw)
                expected = old_tool_name(normalized)
                self.assertEqual(fields[1], str(expected) if isinstance(expected, str) else "")

    def test_path_parity_matches_file_path_then_notebook_path(self):
        for name, payload in PAYLOADS.items():
            with self.subTest(payload=name):
                raw = json.dumps(payload)
                fields = emit_fields(raw)
                normalized = plain_normalize(raw)
                try:
                    expected = old_file_or_notebook(normalized)
                except Exception:
                    expected = ""
                expected = expected if isinstance(expected, str) else ""
                self.assertEqual(fields[2], expected)

    def test_normpath_parity_including_the_empty_case(self):
        """`os.path.normpath('')` is '.', and the old call site produced exactly
        that when no path was present. Preserved deliberately: the `.agent/`
        exemption case-match downstream depends on the resolved form."""
        for name, payload in PAYLOADS.items():
            with self.subTest(payload=name):
                fields = emit_fields(json.dumps(payload))
                self.assertEqual(fields[3], old_normpath(fields[2]))
        self.assertEqual(emit_fields(json.dumps({"tool_name": "Edit"}))[3], ".")

    def test_command_parity(self):
        for name, payload in PAYLOADS.items():
            with self.subTest(payload=name):
                raw = json.dumps(payload)
                fields = emit_fields(raw)
                normalized = plain_normalize(raw)
                try:
                    expected = old_command(normalized)
                except Exception:
                    expected = ""
                expected = expected if isinstance(expected, str) else ""
                self.assertEqual(fields[4], expected)

    def test_traversal_is_resolved_not_exempted(self):
        """The NEW-1 property: `.agent/../src/main.py` must resolve to a code
        path, or the gate exempts a write that lands on real code."""
        fields = emit_fields(json.dumps(PAYLOADS["traversal path"]))
        self.assertEqual(fields[3], os.path.join("src", "main.py"))
        self.assertNotIn(".agent", fields[3])


class NulFraming(unittest.TestCase):
    def test_multiline_command_survives_intact(self):
        fields = emit_fields(json.dumps(PAYLOADS["multiline command"]))
        self.assertEqual(fields[4], "set -e\nmake test\n")

    def test_unicode_path_survives_intact(self):
        fields = emit_fields(json.dumps(PAYLOADS["unicode path"]))
        self.assertEqual(fields[2], "src/café/ação.py")

    def test_shell_metacharacters_are_data_not_code(self):
        fields = emit_fields(json.dumps(PAYLOADS["quotes in command"]))
        self.assertEqual(fields[4], "echo \"a'b\" $HOME `id`")


class TranscriptSanitization(unittest.TestCase):
    def test_clean_transcript_path_passes(self):
        fields = emit_fields(json.dumps(PAYLOADS["transcript path"]))
        self.assertEqual(fields[5], "/t/s.jsonl")

    def test_newline_bearing_transcript_path_is_dropped(self):
        """It is written as a ONE-LINE pointer file for the monitor; a second
        line there is pointer-file injection. The sanitization moved into the
        fused extraction and must still hold."""
        fields = emit_fields(json.dumps(PAYLOADS["transcript injection"]))
        self.assertEqual(fields[5], "")

    def test_carriage_return_is_dropped_too(self):
        fields = emit_fields(json.dumps(
            {"tool_name": "Read", "transcript_path": "/t/s.jsonl\revil"}))
        self.assertEqual(fields[5], "")


class ByteIdentityContract(unittest.TestCase):
    def test_native_claude_payload_comes_back_verbatim(self):
        """Task 014's literal guarantee, now also on the fused path: no reparse,
        no re-serialization, no reordering."""
        raw = '{"tool_name":"Edit", "tool_input":  {"file_path":"x.py"} }'
        self.assertEqual(emit_fields(raw)[6], raw)

    def test_prompt_mentioning_the_grok_wrapper_is_untouched(self):
        raw = json.dumps({"tool_name": "Read",
                          "prompt": "<user_query>hi</user_query>"})
        self.assertEqual(emit_fields(raw)[6], raw)

    def test_foreign_dialect_is_normalized_in_the_payload_record(self):
        fields = emit_fields(json.dumps(PAYLOADS["grok camelCase"]))
        payload = json.loads(fields[6])
        self.assertEqual(payload["tool_name"], "Edit")
        self.assertEqual(payload["tool_input"]["file_path"], "src/x.py")


class HooksUseTheFusedPath(unittest.TestCase):
    """A protocol nobody speaks is dead code — pin the wiring, both sides."""

    HOOKS = ("task-gate-hook", "state-echo-hook")

    def test_hooks_request_the_fused_fields(self):
        for h in self.HOOKS:
            with self.subTest(hook=h):
                text = (SCRIPTS / h).read_text(encoding="utf-8")
                self.assertIn("--emit-fields", text)
                self.assertIn(SENTINEL, text,
                              f"{h} does not check the sentinel it depends on")

    def test_hooks_keep_the_per_field_fallback(self):
        """The fallback is the safety property, not leftovers: if the fused read
        did not run, the gate must still resolve tool_name the old way rather
        than treat an empty value as 'not a code edit'."""
        for h in self.HOOKS:
            with self.subTest(hook=h):
                text = (SCRIPTS / h).read_text(encoding="utf-8")
                self.assertIn("get('tool_name','')", text,
                              f"{h} dropped its per-field fallback")

    def test_command_guard_wrapper_is_a_single_interpreter(self):
        text = (SCRIPTS / "command-guard-hook").read_text(encoding="utf-8")
        self.assertNotIn("hook-payload-normalize.py", text,
                         "the guard wrapper still spawns a second interpreter")
        self.assertIn("exec python3", text)
        guard = (SCRIPTS / "command_guard.py").read_text(encoding="utf-8")
        self.assertIn("_normalize_payload", guard,
                      "the guard must normalize in-process now that the "
                      "wrapper no longer does")

    def test_sentinel_version_is_consistent_across_both_sides(self):
        producer = NORMALIZER.read_text(encoding="utf-8")
        self.assertIn(f'FIELDS_SENTINEL = "{SENTINEL}"', producer)


if __name__ == "__main__":
    unittest.main(verbosity=2)
