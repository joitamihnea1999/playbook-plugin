#!/usr/bin/env python3
"""`tasks detect-verify` — deterministic full-verify-command detection (1.5.19).

The interactive /playbook:init used to have the agent free-form-guess a project's
verify command; this pins the replacement's behavior across the common stacks and
its edge cases. Hermetic: builds throwaway project trees, never executes tooling.

Run: python3 -m unittest tests.test_verify_detect
"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "plugins/playbook"))
from tasks.verify_detect import cli_detect_verify, detect_verify  # noqa: E402


def _mk(files: dict) -> Path:
    d = Path(tempfile.mkdtemp())
    for name, content in files.items():
        p = d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return d


class DetectVerify(unittest.TestCase):
    def test_python_full_stack(self):
        d = _mk({"pyproject.toml": "[tool.pytest.ini_options]\n[tool.mypy]\n[tool.ruff]\n",
                 "tests/test_x.py": ""})
        self.assertEqual(detect_verify(d)["command"],
                         "python3 -m pytest && mypy . && ruff check .")

    def test_python_pyright_and_flake8_variants(self):
        d = _mk({"pyproject.toml": "[tool.pyright]\n", "pytest.ini": "",
                 "setup.cfg": "[flake8]\n"})
        cmd = detect_verify(d)["command"]
        self.assertIn("python3 -m pytest", cmd)
        self.assertIn("pyright", cmd)
        self.assertIn("flake8", cmd)
        self.assertNotIn("mypy", cmd)  # pyright OR mypy, not both

    def test_node_scripts(self):
        d = _mk({"package.json": json.dumps(
            {"scripts": {"test": "jest", "lint": "eslint .", "typecheck": "tsc --noEmit"}})})
        self.assertEqual(detect_verify(d)["command"],
                         "npm run typecheck && npm test && npm run lint")

    def test_node_missing_scripts_key_is_safe(self):
        d = _mk({"package.json": json.dumps({"name": "x"})})
        self.assertEqual(detect_verify(d)["command"], "")

    def test_node_malformed_json_is_safe(self):
        d = _mk({"package.json": "{ not json"})
        self.assertEqual(detect_verify(d)["command"], "")  # no crash, no command

    def test_node_non_object_json_does_not_crash(self):
        # 1.5.20 BUG-1: a valid-but-non-object package.json (list/string/number/
        # bool) must not AttributeError on `.get` — the module never raises.
        for content in ("[1,2,3]", '"hello"', "42", "true", "null"):
            with self.subTest(content=content):
                d = _mk({"package.json": content})
                self.assertEqual(detect_verify(d)["command"], "")

    def test_makefile_immediate_assignment_is_not_a_target(self):
        # 1.5.20 BUG-2: `test := build/out` is a variable, not a `test:` target,
        # so it must NOT yield a `make test` that fails at verify time.
        for var in ("test := build/out\n", "check ::= foo\n", "lint := x\n"):
            with self.subTest(var=var.strip()):
                self.assertEqual(detect_verify(_mk({"Makefile": var}))["command"], "")

    def test_rust_and_go(self):
        self.assertEqual(detect_verify(_mk({"Cargo.toml": "[package]\n"}))["command"],
                         "cargo test && cargo clippy -- -D warnings")
        self.assertEqual(detect_verify(_mk({"go.mod": "module x\n"}))["command"],
                         "go test ./... && go vet ./...")

    def test_makefile_is_fallback_only(self):
        # A Makefile target is offered only when nothing else was detected, so a
        # `make test` never doubles up a detected pytest.
        d = _mk({"Makefile": "check:\n\tpytest\n",
                 "pyproject.toml": "[tool.pytest.ini_options]\n", "tests/t.py": ""})
        cmd = detect_verify(d)["command"]
        self.assertIn("python3 -m pytest", cmd)
        self.assertNotIn("make", cmd)

    def test_makefile_used_when_alone(self):
        d = _mk({"Makefile": "check:\n\tstuff\nlint:\n\tstuff\n"})
        self.assertEqual(detect_verify(_mk({"Makefile": "check:\n\tx\n"}))["command"], "make check")
        self.assertEqual(detect_verify(d)["command"], "make check && make lint")

    def test_makefile_variable_line_is_not_a_target(self):
        # `VAR = value` must not be read as a target.
        d = _mk({"Makefile": "CC = gcc\ntest:\n\tstuff\n"})
        self.assertEqual(detect_verify(d)["command"], "make test")

    def test_empty_project_has_a_note_not_a_command(self):
        r = detect_verify(_mk({}))
        self.assertEqual(r["command"], "")
        self.assertTrue(r["notes"])

    def test_monorepo_polyglot_dedups_and_chains(self):
        d = _mk({"go.mod": "module x\n", "Cargo.toml": "[package]\n"})
        cmd = detect_verify(d)["command"]
        self.assertEqual(cmd.count("&&"), 3)  # 2 go + 2 cargo = 4 cmds, 3 joiners

    def test_cli_json(self):
        d = _mk({"go.mod": "module x\n"})
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli_detect_verify(["--json"], d)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue())["command"], "go test ./... && go vet ./...")

    def test_cli_rejects_unknown_flag(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli_detect_verify(["--nope"], Path("/tmp")), 2)


class DetectsThisProjectsShape(unittest.TestCase):
    """Task 096 (PLAN S10, from 079): `tasks detect-verify` found no toolchain for
    this very workspace — its suite runs through `scripts/verify` inside a
    `code_roots` checkout, and bare stdlib unittest was always read as pytest."""

    PY = "#!/usr/bin/env python3\nprint('verify')\n"

    def test_scripts_verify_is_the_only_component_for_its_root(self):
        d = _mk({"scripts/verify": self.PY, "tests/test_x.py": "import unittest\n",
                 "package.json": json.dumps({"scripts": {"test": "jest"}}), "Makefile": "test:\n"})
        r = detect_verify(d)
        self.assertEqual(r["command"], "python3 scripts/verify")
        self.assertEqual(len(r["components"]), 1)

    def test_scripts_verify_shell_shebang(self):
        d = _mk({"scripts/verify": "#!/usr/bin/env bash\nset -e\n"})
        self.assertEqual(detect_verify(d)["command"], "bash scripts/verify")

    TC = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        pass\n"

    def test_bare_unittest_suite_on_positive_evidence(self):
        d = _mk({"tests/test_a.py": self.TC,
                 "tests/test_b.py": "from unittest import mock, TestCase\n\n\nclass U(TestCase):\n    pass\n"})
        self.assertEqual(detect_verify(d)["command"], "python3 -m unittest discover -s tests")

    def test_mixed_or_unproven_suites_stay_pytest(self):
        for files in (
            {"tests/test_a.py": "import unittest\n", "tests/test_b.py": "import pytest\n"},
            {"tests/test_a.py": "import unittest\n", "tests/test_b.py": "def test_x():\n    assert 1\n"},
            {"tests/test_a.py": "import unittest\n", "tests/conftest.py": ""},
            {"tests/test_a.py": "import unittest\n", "pytest.ini": ""},
            # impl r1 (grok Critical): `unittest.mock` is not a unittest suite —
            # `unittest discover` ran 0 tests on this tree and exited 0
            {"tests/test_a.py": "from unittest.mock import patch\n\n\ndef test_x():\n    assert patch\n"},
            # impl r1 (codex ×2): a module-level pytest function beside a TestCase
            {"tests/test_a.py": self.TC + "\n\ndef test_bare():\n    assert 0\n"},
            # impl r1 (grok): a conftest.py below tests/ is pytest evidence
            {"tests/unit/test_a.py": self.TC, "tests/unit/conftest.py": ""},
            # impl r2 (codex ×2): discover never enters a subdirectory without
            # __init__.py — the nested test would silently not run
            {"tests/test_a.py": self.TC, "tests/unit/test_b.py": self.TC},
            # impl r2 (codex ×2, grok): a plain pytest `class Test…` beside a TestCase
            {"tests/test_a.py": self.TC + "\n\nclass TestPlain:\n    def test_x(self):\n        assert 0\n"},
            # impl r2 (grok): pytest's other default file pattern
            {"tests/test_a.py": self.TC, "tests/widget_test.py": "def test_fails():\n    assert False\n"},
        ):
            with self.subTest(files=sorted(files)):
                self.assertEqual(detect_verify(_mk(files))["command"], "python3 -m pytest")

    def test_nested_unittest_package_is_still_unittest(self):
        # control for the r2 rule: a nested dir WITH __init__.py is discoverable
        d = _mk({"tests/test_a.py": self.TC, "tests/unit/__init__.py": "",
                 "tests/unit/test_b.py": self.TC})
        self.assertEqual(detect_verify(d)["command"], "python3 -m unittest discover -s tests")

    def test_suggested_unittest_command_runs_every_detected_test(self):
        # end to end (impl r2, codex-high): the suggestion must not pass by
        # running fewer tests than the tree holds
        import subprocess
        import sys as _sys
        body = ("import unittest\n\n\nclass T(unittest.TestCase):\n"
                "    def test_one(self):\n        pass\n\n    def test_two(self):\n        pass\n")
        d = _mk({"tests/test_a.py": body, "tests/unit/__init__.py": "", "tests/unit/test_b.py": body})
        self.assertEqual(detect_verify(d)["command"], "python3 -m unittest discover -s tests")
        r = subprocess.run([_sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                           cwd=d, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Ran 4 tests", r.stderr)

    def test_this_workspace_shape_exact(self):
        # outer project: no toolchain of its own, one code_root holding scripts/verify
        d = _mk({".agent/config.json": json.dumps({"code_roots": ["playbook-plugin"]}),
                 "playbook-plugin/scripts/verify": self.PY,
                 "playbook-plugin/tests/test_x.py": "import unittest\n"})
        self.assertEqual(detect_verify(d)["command"],
                         "(cd playbook-plugin && python3 scripts/verify)")

    def test_code_root_with_shell_metacharacters_is_quoted(self):
        d = _mk({".agent/config.json": json.dumps({"code_roots": ["a b;x"]}),
                 "a b;x/scripts/verify": self.PY})
        self.assertEqual(detect_verify(d)["command"], "(cd 'a b;x' && python3 scripts/verify)")

    def test_traversal_code_root_is_ignored(self):
        outside = _mk({"scripts/verify": self.PY})
        d = _mk({".agent/config.json": json.dumps(
            {"code_roots": [f"../{outside.name}", str(outside)]})})
        self.assertEqual(detect_verify(d)["command"], "")

    def test_symlinked_code_root_resolving_outside_is_ignored(self):
        import os
        outside = _mk({"scripts/verify": self.PY})
        d = _mk({".agent/config.json": json.dumps({"code_roots": ["link"]})})
        try:
            os.symlink(outside, d / "link", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable (unprivileged Windows)")
        self.assertEqual(detect_verify(d)["command"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
