"""Judge token usage from the CLIs' STRUCTURED output (task 056).

Fixtures are VERBATIM captures (2026-09-09) of `codex exec --json` (codex-cli
0.153.4) and `grok --output-format json` (grok 1.0.13) — the success run and a
bad-model failure for each. The parser recognizes exactly these real envelopes;
numbers are copied from the CLI's own JSON, never derived. Anything else →
None (`unknown` in the journal). Stdlib unittest.
"""
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN = _HERE.parent / "plugins" / "playbook"
sys.path.insert(0, str(PLUGIN))

from provider.usage import (  # noqa: E402
    JudgeOutput, extract_codex, extract_grok, parse_usage, salvage_text,
)
from tasks.review import _parse_judge_usage  # noqa: E402

CODEX_OK = '{"type":"thread.started","thread_id":"01a084d2-c8e7-7e73-b9bb-76d927599274"}\n{"type":"turn.started"}\n{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"OK"}}\n{"type":"turn.completed","usage":{"input_tokens":13080,"cached_input_tokens":10624,"cache_write_input_tokens":0,"output_tokens":5,"reasoning_output_tokens":0}}\n'

CODEX_BAD = '{"type":"thread.started","thread_id":"01a08588-e878-73a3-bd8b-d03dbf317ded"}\n{"type":"item.completed","item":{"id":"item_0","type":"error","message":"Model metadata for `no-such-model-xyz` not found. Defaulting to fallback metadata; this can degrade performance and cause issues."}}\n{"type":"turn.started"}\n{"type":"error","message":"{\\"type\\":\\"error\\",\\"status\\":400,\\"error\\":{\\"type\\":\\"invalid_request_error\\",\\"message\\":\\"The \'no-such-model-xyz\' model is not supported when using Codex with a ChatGPT account.\\"}}"}\n{"type":"turn.failed","error":{"message":"{\\"type\\":\\"error\\",\\"status\\":400,\\"error\\":{\\"type\\":\\"invalid_request_error\\",\\"message\\":\\"The \'no-such-model-xyz\' model is not supported when using Codex with a ChatGPT account.\\"}}"}}\n'

GROK_OK = '{\n  "text": "OK",\n  "stopReason": "end_turn",\n  "sessionId": "01a084d2-e833-71a2-bf9e-b7923b1d2582",\n  "requestId": "abc8a071-454d-4fe0-84ee-464955990a49",\n  "thought": "The user wants me to reply with a single word: OK. That\'s a simple request.",\n  "usage": {\n    "input_tokens": 13443,\n    "cache_read_input_tokens": 1408,\n    "cache_creation_input_tokens": 0,\n    "output_tokens": 26,\n    "reasoning_tokens": 21,\n    "total_tokens": 14877\n  },\n  "num_turns": 1,\n  "total_cost_usd": 0.00471682,\n  "total_cost_usd_ticks": 47168200,\n  "modelUsage": {\n    "grok-4.6-build": {\n      "inputTokens": 13443,\n      "outputTokens": 26,\n      "cacheReadInputTokens": 1408,\n      "cacheCreationInputTokens": 0,\n      "modelCalls": 1,\n      "costUSD": 0.00471682\n    }\n  }\n}\n'

GROK_BAD = '{"type":"error","message":"Couldn\'t set model \'no-such-model-xyz\': Invalid params: \\"unknown model id\\". Run \'grok models\' to see available models."}\n'


class ParseUsage(unittest.TestCase):
    def test_codex_jsonl_turn_completed(self):
        self.assertEqual(parse_usage(CODEX_OK), {"status": "known", "in": 13080, "out": 5})

    def test_grok_json_object(self):
        self.assertEqual(parse_usage(GROK_OK), {"status": "known", "in": 13443, "out": 26})

    def test_failures_carry_no_usage(self):
        self.assertIsNone(parse_usage(CODEX_BAD))
        self.assertIsNone(parse_usage(GROK_BAD))

    def test_prose_quote_is_never_fabricated(self):
        prose = ('The judge output was {"usage":{"input_tokens":1,"output_tokens":2}} and\n'
                 '{"type":"turn.completed","usage":{"input_tokens":9,"output_tokens":9}} quoted.\n'
                 "Finding 1: fine.")
        self.assertIsNone(parse_usage(prose))

    def test_partial_jsonl_without_turn_completed(self):
        partial = "\n".join(CODEX_OK.splitlines()[:3]) + "\n"
        self.assertIsNone(parse_usage(partial))

    def test_last_turn_completed_wins(self):
        two = CODEX_OK + '{"type":"turn.completed","usage":{"input_tokens":7,"output_tokens":3}}\n'
        self.assertEqual(parse_usage(two), {"status": "known", "in": 7, "out": 3})

    def test_non_int_bool_negative_are_unknown(self):
        for bad in ('{"usage":{"input_tokens":"13","output_tokens":5}}',
                    '{"usage":{"input_tokens":true,"output_tokens":5}}',
                    '{"usage":{"input_tokens":-1,"output_tokens":5}}',
                    '{"usage":{"input_tokens":1.5,"output_tokens":5}}',
                    '{"usage":{"input_tokens":1}}', '{"usage":[]}', "", None, "{}"):
            self.assertIsNone(parse_usage(bad), repr(bad))


class ExtractCodexRound1(unittest.TestCase):
    """Impl round-1 panel (task 056): failure shapes of the codex envelope."""

    def test_error_event_after_a_message_is_still_an_error(self):
        mixed = CODEX_OK.replace(
            '{"type":"turn.completed"',
            '{"type":"turn.failed","error":{"message":"rate limited"}}\n{"type":"turn.completed"')
        text, usage, errors = extract_codex(mixed)
        self.assertEqual(text, "OK")
        self.assertEqual(errors, ["rate limited"])

    def test_malformed_final_turn_completed_forgets_the_earlier_usage(self):
        two = CODEX_OK + '{"type":"turn.completed","usage":{"input_tokens":-1,"output_tokens":3}}\n'
        self.assertIsNone(extract_codex(two)[1])
        self.assertIsNone(parse_usage(two))

    def test_protocol_detected_but_one_bad_line_is_not_prose(self):
        from provider.usage import codex_protocol_detected
        bad = CODEX_OK.replace('{"type":"turn.started"}', 'warning: something on stdout')
        self.assertIsNone(extract_codex(bad), "strict parse must reject")
        self.assertTrue(codex_protocol_detected(bad))
        self.assertFalse(codex_protocol_detected("1. **Finding** — prose"))

    def test_salvage_tolerates_one_truncated_trailing_line(self):
        truncated = "\n".join(CODEX_OK.splitlines()[:3]) + '\n{"type":"turn.compl'
        self.assertEqual(salvage_text("codex", truncated), "OK")


class ExtractCodex(unittest.TestCase):
    def test_success(self):
        text, usage, errors = extract_codex(CODEX_OK)
        self.assertEqual(text, "OK")
        self.assertEqual(usage, {"status": "known", "in": 13080, "out": 5})
        self.assertEqual(errors, [])

    def test_last_agent_message_wins(self):
        two = ('{"type":"item.completed","item":{"id":"i0","type":"agent_message","text":"thinking aloud"}}\n'
               + CODEX_OK)
        text, _u, _e = extract_codex(two)
        self.assertEqual(text, "OK")

    def test_bad_model_run_is_recognized_with_errors_and_no_text(self):
        text, usage, errors = extract_codex(CODEX_BAD)
        self.assertEqual(text, "")
        self.assertIsNone(usage)
        self.assertTrue(errors and any("not supported" in e for e in errors), errors)

    def test_prose_is_unrecognized(self):
        self.assertIsNone(extract_codex("1. **Finding** — looks fine.\n"))
        self.assertIsNone(extract_codex(""))


class ExtractGrok(unittest.TestCase):
    def test_success(self):
        text, usage, errors = extract_grok(GROK_OK)
        self.assertEqual(text, "OK")
        self.assertEqual(usage, {"status": "known", "in": 13443, "out": 26})
        self.assertEqual(errors, [])

    def test_bad_model_is_recognized_error(self):
        text, usage, errors = extract_grok(GROK_BAD)
        self.assertEqual(text, "")
        self.assertIsNone(usage)
        self.assertTrue(errors and "unknown model id" in errors[0], errors)

    def test_prose_is_unrecognized(self):
        self.assertIsNone(extract_grok("Findings: none.\n"))
        self.assertIsNone(extract_grok('["not", "an", "object"]'))

    def test_a_json_object_that_is_not_the_envelope_is_unrecognized(self):
        # Round-1 panel (grok): only `type: error` or a string `text` marks the envelope.
        self.assertIsNone(extract_grok('{"ok": true}'))
        self.assertIsNone(extract_grok('{"usage": {"input_tokens": 1, "output_tokens": 2}}'))


class SalvageText(unittest.TestCase):
    """A judge killed at the hard timeout leaves PARTIAL stdout; the salvage must
    hand the operator prose, not protocol frames (plan panel, codex #3)."""

    def test_codex_partial_jsonl_yields_the_completed_message(self):
        partial = "\n".join(CODEX_OK.splitlines()[:3]) + "\n"     # no turn.completed
        self.assertEqual(salvage_text("codex", partial), "OK")

    def test_codex_frames_without_a_message_keep_the_raw_frames(self):
        partial = "\n".join(CODEX_OK.splitlines()[:2]) + "\n"
        self.assertEqual(salvage_text("codex", partial), partial.strip())

    def test_prose_and_other_providers_pass_through(self):
        self.assertEqual(salvage_text("codex", "1. partial finding"), "1. partial finding")
        self.assertEqual(salvage_text("grok", '{"text": "cut'), '{"text": "cut')
        self.assertEqual(salvage_text("claude", "x\n"), "x")


class JudgeOutputCarrier(unittest.TestCase):
    def test_is_a_str_and_carries_usage(self):
        out = JudgeOutput("1. fine", usage={"status": "known", "in": 1, "out": 2})
        self.assertIsInstance(out, str)
        self.assertEqual(out, "1. fine")
        self.assertEqual(out.usage, {"status": "known", "in": 1, "out": 2})
        self.assertIsNone(JudgeOutput("x").usage)
        self.assertEqual(out.strip(), "1. fine")   # str ops still work (yield plain str)

    def test_review_parser_honours_the_carried_usage(self):
        out = JudgeOutput("prose review", usage={"status": "known", "in": 13080, "out": 5})
        self.assertEqual(_parse_judge_usage(out), {"status": "known", "in": 13080, "out": 5})
        # a carried usage that is not a valid known shape is ignored, not copied
        self.assertIsNone(_parse_judge_usage(JudgeOutput("x", usage={"status": "known", "in": -1, "out": 2})))
        self.assertIsNone(_parse_judge_usage(JudgeOutput("x", usage={"in": 1, "out": 2})))
        # Plain strings NEVER carry usage (round-1 panel, codex-sol high): a
        # plain-text judge that emits only a JSON usage envelope must not record
        # `known` — only an adapter that requested structured output attaches it.
        self.assertIsNone(_parse_judge_usage("prose review"))
        self.assertIsNone(_parse_judge_usage(GROK_OK))
        self.assertIsNone(_parse_judge_usage(CODEX_OK))
        self.assertIsNone(_parse_judge_usage('{"usage":{"input_tokens":1,"output_tokens":2}}'))


import subprocess  # noqa: E402
import tempfile  # noqa: E402
from unittest import mock  # noqa: E402

from provider import sandbox  # noqa: E402
from provider.adapters.codex import CodexAdapter  # noqa: E402
from provider.adapters.grok import GrokAdapter  # noqa: E402


def _cp(stdout, rc=0, stderr=""):
    return subprocess.CompletedProcess(args=["x"], returncode=rc, stdout=stdout, stderr=stderr)


class _AdapterBase(unittest.TestCase):
    ADAPTER = None
    BIN = ""
    FLAG = ()
    OK = ""
    BAD = ""
    OK_USAGE = None

    def setUp(self):
        if self.ADAPTER is None:
            self.skipTest("base")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.adapter = self.ADAPTER(session_id="judge", project_root=Path(self._tmp.name))
        self._which = mock.patch("shutil.which", lambda name: f"/usr/bin/{name}")
        self._which.start()
        self.addCleanup(self._which.stop)

    def _run(self, result):
        calls = []

        def fake_run(binary, args, **kw):
            calls.append((binary, list(args)))
            return result
        with mock.patch.object(sandbox, "run", fake_run):
            out = self.adapter.run_headless_judge(
                prompt="Review this.", model=None, system_context="ctx",
                web_search=False, timeout_secs=30, budget_usd="1")
        return out, calls

    def test_judge_argv_requests_structured_output_but_plain_argv_does_not(self):
        out, calls = self._run(_cp(self.OK))
        self.assertEqual(len(calls), 1)
        argv = calls[0][1]
        for i, tok in enumerate(self.FLAG):
            self.assertIn(tok, argv, argv)
        # Order pin: the flag pair is contiguous.
        i = argv.index(self.FLAG[0])
        self.assertEqual(argv[i:i + len(self.FLAG)], list(self.FLAG), argv)
        plain = self.adapter.headless_argv("p", None, context="c").argv
        self.assertNotIn(self.FLAG[0], plain, "the plain/subagent argv must be unchanged")

    def test_success_returns_prose_carrying_usage(self):
        out, _ = self._run(_cp(self.OK))
        self.assertIsInstance(out, str)
        self.assertEqual(out, "OK")
        self.assertEqual(getattr(out, "usage", None), self.OK_USAGE)
        self.assertEqual(_parse_judge_usage(out), self.OK_USAGE)

    def test_nonzero_exit_keeps_the_failure_marker(self):
        out, _ = self._run(_cp(self.BAD, rc=1, stderr="boom"))
        self.assertTrue(out.startswith("(FAILED — exit 1)"), out)
        self.assertIsNone(getattr(out, "usage", None))
        from tasks.models_check import judge_failed
        self.assertTrue(judge_failed(out))

    def test_nonzero_exit_still_records_a_usage_frame_when_present(self):
        out, _ = self._run(_cp(self.OK, rc=1, stderr="killed late"))
        self.assertTrue(out.startswith("(FAILED — exit 1)"), out)
        self.assertEqual(getattr(out, "usage", None), self.OK_USAGE)

    def test_structured_without_review_text_is_an_error_not_a_pass(self):
        out, _ = self._run(_cp(self.BAD, rc=0))
        self.assertTrue(out.startswith("(FAILED — "), out)
        from tasks.models_check import judge_failed
        self.assertTrue(judge_failed(out))
        from tasks.review import _judge_status
        self.assertEqual(_judge_status(out), "fail", "tokens were spent: fail, never dnf")

    def test_unrecognized_stdout_is_returned_verbatim(self):
        out, _ = self._run(_cp("1. **Finding** — plain prose, flag ignored.\n"))
        # format_judge_output's rc-0 rule: stdout VERBATIM (legacy behaviour kept).
        self.assertEqual(out, "1. **Finding** — plain prose, flag ignored.\n")
        self.assertIsNone(getattr(out, "usage", None))

    def test_empty_stdout_is_no_output(self):
        out, _ = self._run(_cp(""))
        self.assertEqual(out, "(no output)")


class CodexJudge(_AdapterBase):
    ADAPTER = CodexAdapter
    FLAG = ("--json",)
    OK = CODEX_OK
    BAD = CODEX_BAD
    OK_USAGE = {"status": "known", "in": 13080, "out": 5}

    def test_message_plus_failed_turn_on_exit_0_is_a_failed_review(self):
        mixed = CODEX_OK.replace(
            '{"type":"turn.completed"',
            '{"type":"turn.failed","error":{"message":"rate limited"}}\n{"type":"turn.completed"')
        out, _ = self._run(_cp(mixed, rc=0))
        self.assertTrue(out.startswith("(FAILED — "), out)
        self.assertIn("rate limited", out)
        self.assertIn("OK", out, "the salvaged text stays as a diagnostic")
        self.assertEqual(getattr(out, "usage", None), self.OK_USAGE)

    def test_protocol_stream_with_a_bad_line_on_exit_0_is_a_failed_review(self):
        bad = CODEX_OK.replace('{"type":"turn.started"}', 'warning: something on stdout')
        out, _ = self._run(_cp(bad, rc=0))
        self.assertTrue(out.startswith("(FAILED — "), out)
        self.assertIsNone(getattr(out, "usage", None))

    def test_json_flag_precedes_the_stdin_dash(self):
        _out, calls = self._run(_cp(self.OK))
        argv = calls[0][1]
        self.assertEqual(argv[-1], "-")
        self.assertLess(argv.index("--json"), len(argv) - 1)


class GrokJudge(_AdapterBase):
    ADAPTER = GrokAdapter
    FLAG = ("--output-format", "json")
    OK = GROK_OK
    BAD = GROK_BAD
    OK_USAGE = {"status": "known", "in": 13443, "out": 26}

    def test_non_envelope_json_object_is_verbatim(self):
        out, _ = self._run(_cp('{"ok": true}\n'))
        self.assertEqual(out, '{"ok": true}\n')
        self.assertIsNone(getattr(out, "usage", None))

    def test_stream_argv_keeps_streaming_json(self):
        argv = self.adapter.headless_argv("p", None, stream=True).argv
        self.assertIn("streaming-json", argv)
        self.assertNotIn("json", [a for a in argv if a == "json"])


if __name__ == "__main__":
    unittest.main()
