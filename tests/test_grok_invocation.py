#!/usr/bin/env python3
"""Point tests for the Grok Build (`grok`) provider integration (task 014).

Covers the adapter invocation shape, the payload-dialect normalizer, and the
models_check grok arms. All flag/dialect facts were captured live from grok
0.2.99 (see provider/adapters/grok.py docstring).

Pure stdlib unittest. Run: python3 tests/test_grok_invocation.py
"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_PLUGIN = _HERE.parent / "plugins/playbook"
sys.path.insert(0, str(_PLUGIN))

from provider.adapters.grok import GrokAdapter, _split_reasoning_effort  # noqa: E402
from tasks import models_check as mc  # noqa: E402


class GrokInvocationTest(unittest.TestCase):
    def setUp(self):
        self.a = GrokAdapter(session_id="judge", project_root=Path("/tmp/proj"))

    def test_prompt_is_p_value_not_stdin(self):
        inv = self.a.headless_argv("REVIEW", "grok-build", context="CTX")
        self.assertIsNone(inv.stdin)  # grok has no stdin prompt channel
        i = inv.argv.index("-p")
        self.assertEqual(inv.argv[i + 1], "CTX\n\n---\n\nREVIEW")
        self.assertIn("-m", inv.argv)
        self.assertEqual(inv.argv[inv.argv.index("-m") + 1], "grok-build")

    def test_bare_no_context_prompt_only(self):
        inv = self.a.headless_argv("JUST THE PROMPT", None, context="", bare=True)
        self.assertEqual(inv.argv, ["-p", "JUST THE PROMPT"])

    def test_model_effort_suffix_maps_to_reasoning_effort(self):
        inv = self.a.headless_argv("R", "grok-build:high", context="")
        self.assertEqual(inv.argv[inv.argv.index("-m") + 1], "grok-build")
        self.assertEqual(inv.argv[inv.argv.index("--reasoning-effort") + 1], "high")

    def test_split_reasoning_effort(self):
        self.assertEqual(_split_reasoning_effort("grok-build"), ("grok-build", None))
        self.assertEqual(_split_reasoning_effort("grok-build:medium"), ("grok-build", "medium"))
        with self.assertRaises(ValueError):
            _split_reasoning_effort("grok-build:turbo")
        with self.assertRaises(ValueError):
            _split_reasoning_effort(":high")

    def test_web_search_off_appends_disable_flag(self):
        captured = {}

        def fake_run(binary, args, **kw):
            captured["args"] = args
            captured["input"] = kw.get("input")
            return subprocess.CompletedProcess(args, 0, stdout="BODY", stderr="")

        with mock.patch("shutil.which", return_value="/usr/bin/grok"), \
                mock.patch("provider.sandbox.run", side_effect=fake_run), \
                mock.patch("provider.sandbox.format_judge_output", side_effect=lambda r: r.stdout):
            self.a.run_headless_judge("R", "grok-build", "CTX", web_search=False, timeout_secs=90, budget_usd="10")
        self.assertIn("--disable-web-search", captured["args"])
        self.assertIsNone(captured["input"])  # never stdin

    def test_web_search_on_omits_disable_flag(self):
        def fake_run(binary, args, **kw):
            self.assertNotIn("--disable-web-search", args)
            return subprocess.CompletedProcess(args, 0, stdout="BODY", stderr="")

        with mock.patch("shutil.which", return_value="/usr/bin/grok"), \
                mock.patch("provider.sandbox.run", side_effect=fake_run), \
                mock.patch("provider.sandbox.format_judge_output", side_effect=lambda r: r.stdout):
            self.a.run_headless_judge("R", "grok-build", "CTX", web_search=True, timeout_secs=90, budget_usd="10")

    def test_windows_argv_guard(self):
        with mock.patch("shutil.which", return_value="/usr/bin/grok"), \
                mock.patch.object(os, "name", "nt"):
            out = self.a.run_headless_judge(
                "P", "grok-build", "X" * 40000, web_search=False, timeout_secs=60, budget_usd="10")
        self.assertTrue(out.startswith("(error: grok judge prompt+context is ~"))
        self.assertIn("Windows caps the command line", out)

    def test_panel_variants_and_identity(self):
        self.assertEqual(GrokAdapter.binary_name(), "grok")
        self.assertEqual(GrokAdapter.panel_variants(), ["grok-build"])


class GrokPayloadNormalizerTest(unittest.TestCase):
    """The shared hook-payload shim: grok camelCase/renamed dialect → claude."""

    NORM = str(_PLUGIN / "scripts" / "hook-payload-normalize.py")

    def norm(self, payload):
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        out = subprocess.run([sys.executable, self.NORM], input=raw,
                             capture_output=True, text=True).stdout
        return out

    def test_claude_payload_byte_identical(self):
        # A native (snake_case) claude payload must round-trip BYTE-for-byte —
        # no re-serialization, so downstream grep fallbacks keep matching.
        raw = '{"hook_event_name": "PreToolUse", "tool_name": "Edit", "tool_input": {"file_path": "/x/y.py"}}'
        self.assertEqual(self.norm(raw), raw)

    def test_claude_prompt_with_user_query_tags_untouched(self):
        # A claude user whose message IS <user_query>...</user_query> must not
        # be rewritten (no dialect marker → passthrough).
        raw = '{"prompt": "<user_query>\\nhi\\n</user_query>"}'
        self.assertEqual(self.norm(raw), raw)

    def test_claude_tool_named_shell_not_remapped(self):
        # Hypothetical claude payload with tool_name "Shell" stays "Shell"
        # (no camelCase marker → no vocabulary remap).
        raw = '{"tool_name": "Shell", "tool_input": {"command": "ls"}}'
        self.assertEqual(self.norm(raw), raw)

    def test_grok_output_is_compact_json(self):
        # Transformed grok output uses compact separators (no space after :,)
        out = self.norm({"toolName": "Shell", "toolInput": {"command": "ls"}})
        self.assertNotIn('": ', out)
        self.assertNotIn('", "', out)

    def test_grok_strreplace_becomes_edit(self):
        out = json.loads(self.norm({
            "toolName": "StrReplace",
            "toolInput": {"path": "/x/h.py", "old_string": "a", "new_string": "b"}}))
        self.assertEqual(out["tool_name"], "Edit")
        self.assertEqual(out["tool_input"]["file_path"], "/x/h.py")
        self.assertEqual(out["tool_input"]["old_string"], "a")

    def test_grok_shell_becomes_bash(self):
        out = json.loads(self.norm({"toolName": "Shell", "toolInput": {"command": "ls"}}))
        self.assertEqual(out["tool_name"], "Bash")
        self.assertEqual(out["tool_input"]["command"], "ls")

    def test_grok_write_keys(self):
        out = json.loads(self.norm({"toolName": "Write",
                                    "toolInput": {"path": "/x/n.py", "contents": "print(1)"}}))
        self.assertEqual(out["tool_name"], "Write")
        self.assertEqual(out["tool_input"]["file_path"], "/x/n.py")
        self.assertEqual(out["tool_input"]["content"], "print(1)")

    def test_prompt_unwrap(self):
        out = json.loads(self.norm({"hookEventName": "user_prompt_submit",
                                    "prompt": "<user_query>\nfix the bug\n</user_query>"}))
        self.assertEqual(out["prompt"], "fix the bug")

    def test_prompt_tag_mention_untouched(self):
        out = json.loads(self.norm({"prompt": "see <user_query> in the docs"}))
        self.assertEqual(out["prompt"], "see <user_query> in the docs")

    def test_non_json_passthrough(self):
        self.assertEqual(self.norm("not json at all"), "not json at all")


class GrokModelsCheckTest(unittest.TestCase):
    def test_parse_grok_models(self):
        real = ("You are logged in with grok.com.\n\n"
                "Default model: grok-composer-2.5-fast\n\n"
                "Available models:\n"
                "  * grok-composer-2.5-fast (default)\n"
                "  - grok-build\n")
        self.assertEqual(mc.parse_grok_models(real),
                         ["grok-composer-2.5-fast", "grok-build"])

    def test_parse_grok_models_empty_and_prose(self):
        self.assertEqual(mc.parse_grok_models("no bullets here\njust prose"), [])

    def test_classify_failure_grok_model_gone(self):
        failed = ('(FAILED — exit 1)\nCouldn\'t set model \'bogus\': Invalid params: '
                  '"unknown model id". Run \'grok models\' to see available models.')
        self.assertEqual(mc.classify_failure(failed), mc.MODEL_UNAVAILABLE)

    def test_classify_failure_grok_needs_both_fragments(self):
        # A passing review that merely quotes ONE fragment must NOT classify.
        review = 'The doc says: Couldn\'t set model when the id is wrong.'
        self.assertEqual(mc.classify_failure(review), mc.OTHER)

    def test_confirm_dead_specs_grok_arm(self):
        failed = ('(FAILED — exit 1)\nCouldn\'t set model \'g\': Invalid params: '
                  '"unknown model id".')
        res = mc.confirm_dead_specs(
            {"grok:g": failed}, {"grok:g": ("grok", "g")},
            probe_grok=lambda m: (mc.GONE, f"{m} gone"))
        self.assertEqual(res, {"grok:g": (mc.GONE, "g gone")})

    def test_adapter_classes_has_grok(self):
        self.assertIn("grok", mc._adapter_classes())


if __name__ == "__main__":
    unittest.main(verbosity=2)
