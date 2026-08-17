#!/usr/bin/env python3
"""Point tests for `tasks environment` — the advisory tool-recommendation report
(1.5.15). All detection surfaces (PATH lookups, platform, ~/.claude, project
config) are monkeypatched so the test is hermetic and launches nothing.

Pure stdlib unittest (honors the stdlib-only runtime invariant).
Run: python3 tests/test_environment.py   (or: python3 -m unittest ...)
"""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

# The runtime tree is plugins/playbook/ (dispatcher sets PYTHONPATH there).
_HERE = Path(__file__).resolve().parent
import sys  # noqa: E402
sys.path.insert(0, str(_HERE.parent / "plugins/playbook"))

from tasks import environment as env  # noqa: E402


def _which(installed):
    """A shutil.which stub: present iff the binary name is in `installed`."""
    return lambda name: ("/usr/bin/" + name) if name in installed else None


class ProviderItemsTest(unittest.TestCase):
    def _items(self, installed):
        with mock.patch.object(env.shutil, "which", side_effect=_which(installed)):
            return {i["name"]: i for i in env._provider_items()}

    def test_all_four_vendors_reported(self):
        items = self._items(set())
        self.assertEqual(
            set(items),
            {"agent CLI: codex", "agent CLI: agy", "agent CLI: grok", "agent CLI: pi"})

    def test_installed_vendor_is_ok_with_no_hint(self):
        items = self._items({"codex"})
        self.assertTrue(items["agent CLI: codex"]["present"])
        self.assertEqual(items["agent CLI: codex"]["severity"], env.SEV_OK)
        self.assertEqual(items["agent CLI: codex"]["hint"], "")

    def test_missing_vendor_recommended_with_hint(self):
        items = self._items(set())
        codex = items["agent CLI: codex"]
        self.assertFalse(codex["present"])
        self.assertEqual(codex["severity"], env.SEV_RECOMMENDED)
        self.assertIn("npm install -g @openai/codex", codex["hint"])  # concrete hint
        # a vendor without a confident command points at its docs instead
        self.assertIn("docs", items["agent CLI: agy"]["hint"])


class SandboxItemTest(unittest.TestCase):
    def test_linux_needs_bwrap(self):
        with mock.patch.object(env.platform, "system", return_value="Linux"), \
                mock.patch.object(env.shutil, "which", side_effect=_which(set())):
            item = env._sandbox_item()
        self.assertEqual(item["category"], "sandbox")
        self.assertFalse(item["present"])
        self.assertIn("bubblewrap", item["hint"])

    def test_linux_with_bwrap_is_ok(self):
        with mock.patch.object(env.platform, "system", return_value="Linux"), \
                mock.patch.object(env.shutil, "which", side_effect=_which({"bwrap"})):
            item = env._sandbox_item()
        self.assertTrue(item["present"])

    def test_darwin_uses_seatbelt_probe(self):
        with mock.patch.object(env.platform, "system", return_value="Darwin"), \
                mock.patch("provider.sandbox._seatbelt_usable", return_value=True):
            item = env._sandbox_item()
        self.assertTrue(item["present"])
        self.assertIn("seatbelt", item["name"])

    def test_unknown_os_has_no_primitive(self):
        with mock.patch.object(env.platform, "system", return_value="Plan9"):
            item = env._sandbox_item()
        self.assertFalse(item["present"])
        self.assertIn("Plan9", item["name"])


class CommandWordsTest(unittest.TestCase):
    def test_python_dash_m_yields_the_interpreter(self):
        self.assertEqual(env._command_words("python3 -m pytest"), ["python3"])

    def test_chained_and_piped_segments(self):
        self.assertEqual(
            env._command_words("mypy . && pytest -q | tee log ; ruff check ."),
            ["mypy", "pytest", "tee", "ruff"])

    def test_leading_env_assignment_skipped(self):
        self.assertEqual(env._command_words("FOO=bar BAZ=1 pytest"), ["pytest"])

    # ── 1.5.16 hardening: no invented tokens on real-world verify strings ──────
    def test_subshell_does_not_invent_paren_tokens(self):
        # was ['(cd', 'pytest)'] — the close-paren even made an installed tool
        # (grep) look absent.
        self.assertEqual(env._command_words("(cd sub && pytest)"), ["pytest"])
        self.assertEqual(env._command_words("(cd sub && grep -r x .)"), ["grep"])

    def test_operator_inside_quotes_is_not_split(self):
        self.assertEqual(env._command_words('grep "a|b" .'), ["grep"])

    def test_bash_dash_c_body_is_not_parsed(self):
        # was ['bash', 'ruff"'] — the && inside the quoted -c arg got split.
        self.assertEqual(env._command_words('bash -c "pytest && ruff"'), ["bash"])

    def test_shell_keywords_are_not_reported_as_tools(self):
        self.assertEqual(env._command_words("if pytest; then echo ok; fi"), ["pytest"])
        self.assertEqual(env._command_words("for f in a b; do pytest; done"), ["pytest"])
        self.assertEqual(env._command_words("while pytest; do echo x; done"), ["pytest"])

    def test_redirection_target_is_not_a_command(self):
        self.assertEqual(env._command_words("pytest > log.txt"), ["pytest"])

    def test_env_prefix_reaches_real_command(self):
        # was ['env'] (a false negative — pytest never checked).
        self.assertEqual(env._command_words("env FOO=1 pytest"), ["pytest"])
        self.assertEqual(env._command_words("sudo pytest"), ["pytest"])

    def test_unbalanced_quotes_yield_nothing_not_a_crash(self):
        self.assertEqual(env._command_words('pytest "unterminated'), [])


class VerifyItemsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name)
        (self.project / ".agent").mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, verify):
        (self.project / ".agent" / "config.json").write_text(
            json.dumps({"verify": verify}), encoding="utf-8")

    def _items(self, installed):
        with mock.patch.object(env.shutil, "which", side_effect=_which(installed)):
            return env._verify_items(self.project)

    def test_no_config_no_items(self):
        self.assertEqual(env._verify_items(self.project), [])

    def test_missing_tool_is_a_warning(self):
        self._write("pytest && ruff check .")
        items = {i["name"]: i for i in self._items({"pytest"})}
        self.assertEqual(items["verify tool: pytest"]["severity"], env.SEV_OK)
        self.assertFalse(items["verify tool: ruff"]["present"])
        self.assertEqual(items["verify tool: ruff"]["severity"], env.SEV_WARNING)

    def test_risk_keyed_dict_is_flattened(self):
        self._write({"_always": ["pytest"], "assertive": ["mypy ."]})
        names = {i["name"] for i in self._items(set())}
        self.assertIn("verify tool: pytest", names)
        self.assertIn("verify tool: mypy", names)

    def test_python_m_module_not_falsely_flagged(self):
        self._write("python3 -m pytest")
        items = {i["name"]: i for i in self._items({"python3"})}
        # only python3 is a command word; the module `pytest` is not a PATH check
        self.assertEqual(list(items), ["verify tool: python3"])
        self.assertTrue(items["verify tool: python3"]["present"])

    def test_dedup_across_commands(self):
        self._write("pytest tests/a && pytest tests/b")
        self.assertEqual(len([i for i in self._items(set())
                              if i["name"] == "verify tool: pytest"]), 1)


class LoggingItemTest(unittest.TestCase):
    def _item(self, *, file_present, bash_env):
        tmp = tempfile.TemporaryDirectory()
        home = Path(tmp.name)
        (home / ".claude").mkdir()
        if file_present:
            (home / ".claude" / "bash-log.sh").write_text("# stub", encoding="utf-8")
        settings = {}
        if bash_env is not None:
            settings = {"env": {"BASH_ENV": bash_env}}
        (home / ".claude" / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        with mock.patch.object(env.Path, "home", return_value=home):
            item = env._logging_item()
        tmp.cleanup()
        return item

    def test_wired_is_ok(self):
        item = self._item(file_present=True, bash_env="/home/u/.claude/bash-log.sh")
        self.assertTrue(item["present"])

    def test_file_present_but_not_wired(self):
        item = self._item(file_present=True, bash_env=None)
        self.assertFalse(item["present"])
        self.assertIn("playbook:init", item["hint"])

    def test_wired_but_file_absent(self):
        item = self._item(file_present=False, bash_env="/home/u/.claude/bash-log.sh")
        self.assertFalse(item["present"])


class ReportAndCliTest(unittest.TestCase):
    def _report(self, installed, system="Linux"):
        with mock.patch.object(env.shutil, "which", side_effect=_which(installed)), \
                mock.patch.object(env.platform, "system", return_value=system), \
                mock.patch.object(env.Path, "home", return_value=Path("/nonexistent-home")):
            return env.environment_report(None)

    def test_report_has_all_categories_and_platform(self):
        report = self._report({"bwrap", "codex", "grok"})
        self.assertEqual(report["platform"], "Linux")
        cats = {i["category"] for i in report["items"]}
        self.assertIn("provider", cats)
        self.assertIn("sandbox", cats)
        self.assertIn("logging", cats)

    def test_suggestions_are_only_absent_items(self):
        report = self._report({"bwrap", "codex", "grok", "agy", "pi"})
        sug = env.suggestions(report)
        self.assertTrue(all(not i["present"] for i in sug))
        # everything present except logging (home is nonexistent) → 1 suggestion
        self.assertEqual([i["category"] for i in sug], ["logging"])

    def test_render_suggest_only_hides_ok(self):
        report = self._report({"bwrap", "codex", "grok", "agy", "pi"})
        text = env.render_environment(report, show_ok=False)
        self.assertNotIn("✓", text)
        self.assertIn("Environment recommendations", text)

    def test_cli_json_is_valid(self):
        with mock.patch.object(env.shutil, "which", side_effect=_which(set())), \
                mock.patch.object(env.platform, "system", return_value="Linux"), \
                mock.patch.object(env.Path, "home", return_value=Path("/nonexistent-home")):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = env.cli_environment(["--json"], Path("/tmp"))
        self.assertEqual(rc, 0)
        parsed = json.loads(buf.getvalue())
        self.assertIn("items", parsed)

    def test_cli_rejects_unknown_flag(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(env.cli_environment(["--bogus"], Path("/tmp")), 2)

    def test_report_never_raises_when_home_undeterminable(self):
        # HOME unset + no passwd entry (container as arbitrary UID): Path.home()
        # raises RuntimeError. environment_report must still return, and the
        # bare `tasks environment` CLI must not crash.
        with mock.patch.object(env.shutil, "which", side_effect=_which(set())), \
                mock.patch.object(env.platform, "system", return_value="Linux"), \
                mock.patch.object(env.Path, "home",
                                  side_effect=RuntimeError("Could not determine home directory.")):
            report = env.environment_report(None)  # must not raise
            self.assertIn("items", report)
            log = [i for i in report["items"] if i["category"] == "logging"][0]
            self.assertFalse(log["present"])
            with redirect_stdout(io.StringIO()):
                self.assertEqual(env.cli_environment([], Path("/tmp")), 0)  # no crash

    def test_json_suggest_only_filters_the_json(self):
        report = self._report({"bwrap", "codex", "grok", "agy", "pi"})  # only logging absent
        with mock.patch.object(env, "environment_report", return_value=report):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = env.cli_environment(["--json", "--suggest-only"], Path("/tmp"))
        self.assertEqual(rc, 0)
        parsed = json.loads(buf.getvalue())
        self.assertTrue(all(not i["present"] for i in parsed["items"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
