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


if __name__ == "__main__":
    unittest.main(verbosity=2)
