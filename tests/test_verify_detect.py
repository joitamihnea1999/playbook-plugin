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
from tasks import verify_detect as vd  # noqa: E402
from unittest import mock  # noqa: E402


class _NoPytest(unittest.TestCase):
    """Task 098: the detector probes `python3 -m pytest --version`. Every test
    pins the answer, so no result depends on what this machine has installed."""
    PYTEST_INSTALLED = False

    def setUp(self):
        patcher = mock.patch.object(vd, "_pytest_available",
                                    return_value=self.PYTEST_INSTALLED)
        self.probe = patcher.start()
        self.addCleanup(patcher.stop)


def _mk(files: dict) -> Path:
    d = Path(tempfile.mkdtemp())
    for name, content in files.items():
        p = d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return d


class DetectVerify(_NoPytest):
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


class DetectsThisProjectsShape(_NoPytest):
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
    UD = "python3 -m unittest discover -s tests"

    def _note(self, r):
        notes = [n for n in r["notes"] if "unittest discover -s tests` is suggested" in n]
        self.assertEqual(len(notes), 1, r["notes"])
        return notes[0]

    # ── task 098 (owner): pytest where the project has it, else unittest + its limits ──
    def test_pytest_config_suggests_pytest_without_probing(self):
        r = detect_verify(_mk({"pytest.ini": "", "tests/test_a.py": self.TC}))
        self.assertEqual(r["command"], "python3 -m pytest")
        self.probe.assert_not_called()

    def test_installed_pytest_is_suggested_it_runs_testcases_too(self):
        self.probe.return_value = True
        r = detect_verify(_mk({"tests/test_a.py": self.TC}))
        self.assertEqual(r["command"], "python3 -m pytest")
        self.assertFalse(any("unittest discover" in n for n in r["notes"]))

    def test_no_pytest_means_unittest_never_an_absent_pytest(self):
        r = detect_verify(_mk({"tests/test_a.py": self.TC}))
        self.assertEqual(r["command"], self.UD)
        note = self._note(r)
        # the limits are stated whatever the tree holds — not a completeness claim
        for shape in ("load_tests", "module-level aliases", "outside `tests/`",
                      "indirect base", "STARTING POINT", "Ran N tests"):
            self.assertIn(shape, note)
        self.assertIn("None of these shapes was seen by a scan", note)

    def test_the_probe_is_the_one_execution(self):
        # `python3 -m pytest --version`, output discarded; a failure means False
        calls = []

        def fake_run(argv, **kw):
            calls.append((argv, kw))
            return mock.Mock(returncode=1)

        with mock.patch.object(vd.subprocess, "run", side_effect=fake_run):
            self.assertFalse(_REAL_PROBE(Path('.')))
        self.assertEqual(calls[0][0][1:], ["-m", "pytest", "--version"])
        self.assertIs(calls[0][1]["stdout"], vd.subprocess.DEVNULL)
        with mock.patch.object(vd.subprocess, "run", side_effect=OSError("no python3")):
            self.assertFalse(_REAL_PROBE(Path('.')))

    # ── task 098 impl panel round 1 ──
    def test_probe_runs_python3_in_the_root_it_decides_for(self):
        # opus: no sys.executable fallback (the suggestion says python3);
        # codex-high: cwd = the root whose command will run there
        calls = []

        def fake_run(argv, **kw):
            calls.append((argv, kw))
            return mock.Mock(returncode=0)

        root = _mk({})
        with mock.patch.object(vd.shutil, "which", return_value="/usr/bin/python3"), \
                mock.patch.object(vd.subprocess, "run", side_effect=fake_run):
            self.assertTrue(_REAL_PROBE(root))
        self.assertEqual(calls[0][0], ["/usr/bin/python3", "-m", "pytest", "--version"])
        self.assertEqual(calls[0][1]["cwd"], str(root))
        with mock.patch.object(vd.shutil, "which", return_value=None), \
                mock.patch.object(vd.subprocess, "run", side_effect=fake_run):
            self.assertFalse(_REAL_PROBE(root))      # no python3 on PATH → no pytest claim
        self.assertEqual(len(calls), 1)

    def test_each_python_root_is_probed_in_its_own_directory(self):
        # sonnet: "one execution" was false with several roots — it is one per root
        d = _mk({".agent/config.json": json.dumps({"code_roots": ["a", "b"]}),
                 "a/tests/test_x.py": self.TC, "b/tests/test_y.py": self.TC})
        detect_verify(d)
        self.assertEqual(sorted(Path(c.args[0]).name for c in self.probe.call_args_list), ["a", "b"])

    def test_a_tox_ini_without_pytest_is_not_pytest_config(self):
        # codex ×2, grok: a bare [tox] made an uninstalled pytest the suggestion
        r = detect_verify(_mk({"tox.ini": "[tox]\nenvlist = py310\n", "tests/test_a.py": self.TC}))
        self.assertEqual(r["command"], self.UD)
        r = detect_verify(_mk({"tox.ini": "[pytest]\naddopts = -q\n", "tests/test_a.py": self.TC}))
        self.assertEqual(r["command"], "python3 -m pytest")

    def test_a_comment_or_string_naming_load_tests_is_not_a_binding(self):
        # grok: `# do not define load_tests` was reported as a hook
        d = _mk({"tests/test_a.py": self.TC,
                 "tests/pkg/__init__.py": "# do not define load_tests here\nNOTE = 'load_tests'\n",
                 "tests/pkg/test_b.py": self.TC})
        self.assertNotIn("binds load_tests", self._note(detect_verify(d)))

    def test_a_file_outside_discovers_pattern_is_reported_as_such(self):
        # grok: tests/widget_test.py (a TestCase!) is never loaded — discover ran 0 tests
        import subprocess
        import sys as _sys
        d = _mk({"tests/widget_test.py": self.TC.replace("pass", "assert False")})
        note = self._note(detect_verify(d))
        self.assertIn("`tests/widget_test.py` does not match discover's `test*.py` pattern", note)
        self.assertIn("files not matching `test*.py`", note)     # the standing limits say it too
        r = subprocess.run([_sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                           cwd=d, capture_output=True, text=True, timeout=120)
        self.assertIn("Ran 0 tests", r.stderr)                    # the skip is real

    # ── task 098 impl panel round 2 (the last D6 round) ──
    def test_config_markers_count_only_as_real_section_headers(self):
        # sonnet, codex ×2, grok: a comment or value naming the section made an
        # uninstalled pytest the suggestion, with no probe
        not_config = [
            {"tox.ini": "[tox]\n# [pytest] is not used here\ndescription = no [pytest] here\n"},
            {"tox.ini": "[tool:pytest]\naddopts = -q\n"},      # pytest does not read this in tox.ini
            {"pyproject.toml": "[project]\nname = 'x'\n# see [tool.pytest.ini_options] upstream\n"},
            {"setup.cfg": "[metadata]\ndescription = mentions [tool:pytest]\n"},
            # D6-amended single judge, pass 1: an indented header is an ini
            # continuation line, and a header-shaped line inside a string is not a table
            {"tox.ini": "[tox]\nenvlist = py310\n  [pytest]\n"},
            {"setup.cfg": "[metadata]\n  [tool:pytest]\n"},
            {"pyproject.toml": '[project]\ndescription = """\n[tool.pytest.ini_options]\n"""\n'},
            # single judge, pass 2: an empty `[tool.pytest]` table is no pytest config
            {"pyproject.toml": "[tool.pytest]\n# nothing here\n"},
        ]
        for files in not_config:
            with self.subTest(files=sorted(files)):
                r = detect_verify(_mk({**files, "tests/test_a.py": self.TC}))
                self.assertEqual(r["command"], self.UD)
        for files in ({"tox.ini": "[tox]\n\n[pytest]\naddopts = -q\n"},
                      {"pyproject.toml": "[tool.pytest.ini_options]\naddopts = '-q'\n"},
                      {"setup.cfg": "[tool:pytest]\naddopts = -q\n"},
                      {"pytest.ini": ""}):
            with self.subTest(files=sorted(files)):
                r = detect_verify(_mk({**files, "tests/test_a.py": self.TC}))
                self.assertEqual(r["command"], "python3 -m pytest")

    def test_load_tests_false_bindings_are_not_reported(self):
        # grok: an indented def, a line inside a string, an `import … as other`
        for init in ("class X:\n    def load_tests(self):\n        pass\n",
                     '"""\nload_tests = something\n"""\n',
                     "from .support import load_tests as _lt\n",
                     # D6-amended single judge, pass 1: a plain import binds its FIRST name
                     "import tests.pkg.sub.load_tests\n",
                     "import os, sys.load_tests\n",
                     # single judge, pass 2: a backslash-continued string literal
                     'x = "\\\nload_tests = 1"\n'):
            with self.subTest(init=init.splitlines()[0]):
                d = _mk({"tests/test_a.py": self.TC, "tests/pkg/__init__.py": init,
                         "tests/pkg/test_b.py": self.TC})
                self.assertNotIn("binds load_tests", self._note(detect_verify(d)))

    def test_shapes_inside_a_docstring_are_not_reported(self):
        # single judge, pass 2: the scan must see code, not text (now via ast)
        doc = '"""Example:\n\nfrom helper import *\ndef test_x(): pass\ntest_y = 1\nclass TestZ: pass\n"""\n'
        d = _mk({"tests/test_a.py": doc + self.TC, "tests/pkg/__init__.py": doc,
                 "tests/pkg/test_b.py": self.TC})
        note = self._note(detect_verify(d))
        for shape in ("has an `import *`", "has a module-level `def test", "has a module-level test alias",
                      "whose base is not exactly", "binds load_tests"):
            self.assertNotIn(shape, note)

    def test_an_unparseable_test_file_is_reported(self):
        note = self._note(detect_verify(_mk({"tests/test_a.py": self.TC,
                                             "tests/test_bad.py": "def (:\n"})))
        self.assertIn("`tests/test_bad.py` could not be parsed", note)

    def test_real_load_tests_imports_still_count(self):
        for init in ("from .support import load_tests\n", "import helpers as load_tests\n",
                     "from .support import (\n    load_tests,\n)\n"):
            with self.subTest(init=init.splitlines()[0]):
                d = _mk({"tests/test_a.py": self.TC, "tests/pkg/__init__.py": init,
                         "tests/pkg/test_b.py": self.TC})
                self.assertIn("binds load_tests", self._note(detect_verify(d)))

    def test_outside_files_matching_discovers_pattern_are_reported(self):
        # grok: `test.py`, `src/testfoo.py` match test*.py yet sit outside tests/
        for extra in ({"test.py": "import unittest\n"}, {"src/testfoo.py": "import unittest\n"}):
            with self.subTest(extra=sorted(extra)):
                note = self._note(detect_verify(_mk({"tests/test_a.py": self.TC, **extra})))
                self.assertIn(f"`{next(iter(extra))}` is a test file outside tests/", note)

    def test_a_walk_that_stops_early_says_so(self):
        # grok: past the cap the walk returned silently and the note claimed none seen
        d = _mk({"tests/test_a.py": self.TC, "a.txt": "", "b.txt": "", "src/test_hidden.py": ""})
        with mock.patch.object(vd, "_WALK_CAP", 2):
            note = self._note(detect_verify(d))
        self.assertIn("stopped after 2 files", note)
        self.assertNotIn("None of these shapes was seen", note)

    def test_suggested_unittest_command_runs_every_test_on_a_clean_tree(self):
        import subprocess
        import sys as _sys
        body = ("import unittest\n\n\nclass T(unittest.TestCase):\n"
                "    def test_one(self):\n        pass\n\n    def test_two(self):\n        pass\n")
        d = _mk({"tests/test_a.py": body, "tests/unit/__init__.py": "", "tests/unit/test_b.py": body})
        self.assertEqual(detect_verify(d)["command"], self.UD)
        r = subprocess.run([_sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                           cwd=d, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Ran 4 tests", r.stderr)

    # The shapes the 096/098 reviews found discover can skip. They are no longer
    # a claim of coverage: the detector REPORTS them in the note (owner, task 098).
    def test_each_skip_shape_is_reported_in_the_note(self):
        cases = [
            ("unittest.mock only, a bare pytest function",
             {"tests/test_a.py": "from unittest.mock import patch\n\n\ndef test_x():\n    assert patch\n"},
             "module-level `def test"),
            ("a plain pytest class beside a TestCase",
             {"tests/test_a.py": self.TC + "\n\nclass TestPlain:\n    def test_x(self):\n        assert 0\n"},
             "whose base is not exactly TestCase"),
            ("a base that only CONTAINS the word (pass 1)",
             {"tests/test_a.py": self.TC + "\n\nclass BaseTestCase:\n    pass\n\n\n"
                                           "class TestHidden(BaseTestCase):\n    def test_h(self):\n        assert 0\n"},
             "whose base is not exactly TestCase"),
            ("load_tests by def (pass 1)",
             {"tests/test_a.py": self.TC, "tests/pkg/__init__.py": "def load_tests(l, t, p):\n    return t\n",
              "tests/pkg/test_b.py": self.TC},
             "`tests/pkg/__init__.py` binds load_tests"),
            ("load_tests by import (pass 2)",
             {"tests/test_a.py": self.TC, "tests/pkg/__init__.py": "from .support import load_tests\n",
              "tests/pkg/test_b.py": self.TC},
             "`tests/pkg/__init__.py` binds load_tests"),
            ("load_tests by assignment (pass 2)",
             {"tests/test_a.py": self.TC, "tests/pkg/__init__.py": "load_tests = _load\n",
              "tests/pkg/test_b.py": self.TC},
             "`tests/pkg/__init__.py` binds load_tests"),
            ("import * in a package (pass 3)",
             {"tests/test_a.py": self.TC, "tests/pkg/__init__.py": "from .support import *\n",
              "tests/pkg/test_b.py": self.TC},
             "`tests/pkg/__init__.py` has an `import *`"),
            ("module-level aliases (pass 3)",
             {"tests/test_a.py": self.TC + "\n\ndef _f():\n    assert 0\n\n\ntest_fail = _f\nTestHidden = T\n"},
             "module-level test alias"),
            ("a root-level test file (pass 1)",
             {"tests/test_a.py": self.TC, "widget_test.py": "def test_f():\n    assert 0\n"},
             "`widget_test.py` is a test file outside tests/"),
            ("a test file below the root, outside tests/ (pass 2)",
             {"tests/test_a.py": self.TC, "src/foo_test.py": "def test_f():\n    assert 0\n"},
             "`src/foo_test.py` is a test file outside tests/"),
            ("a test subdirectory that is not a package",
             {"tests/test_a.py": self.TC, "tests/unit/test_b.py": self.TC},
             "`tests/unit/` is not a package"),
            ("pytest's own files under tests/",
             {"tests/test_a.py": self.TC, "tests/unit/conftest.py": ""},
             "is a pytest-only file"),
        ]
        for label, files, expect in cases:
            with self.subTest(label):
                r = detect_verify(_mk(files))
                self.assertEqual(r["command"], self.UD)          # never an absent pytest
                self.assertIn(expect, self._note(r))

    def test_hidden_and_vendored_dirs_are_not_observed(self):
        for extra in ({".venv/lib/site-packages/x/test_x.py": "def test_x(): pass\n"},
                      {"node_modules/pkg/test_y.py": "def test_y(): pass\n"},
                      {".git/hooks/test_z.py": "def test_z(): pass\n"}):
            with self.subTest(extra=sorted(extra)):
                note = self._note(detect_verify(_mk({"tests/test_a.py": self.TC, **extra})))
                self.assertIn("None of these shapes was seen", note)

    def test_exact_testcase_bases_are_not_reported(self):
        body = ("import unittest\nfrom unittest import TestCase\n\n\n"
                "class TestA(unittest.TestCase):\n    def test_a(self):\n        pass\n\n\n"
                "class TestB(TestCase):\n    def test_b(self):\n        pass\n")
        note = self._note(detect_verify(_mk({"tests/test_a.py": body, "tests/pkg/__init__.py": "",
                                             "tests/pkg/test_b.py": self.TC})))
        self.assertIn("None of these shapes was seen", note)

    def test_this_workspace_shape_exact(self):
        # outer project: no toolchain of its own, one code_root holding scripts/verify
        d = _mk({".agent/config.json": json.dumps({"code_roots": ["playbook-plugin"]}),
                 "playbook-plugin/scripts/verify": self.PY,
                 "playbook-plugin/tests/test_x.py": "import unittest\n"})
        # task 098: one code root and nothing at the outer root → the plain form,
        # exactly what this workspace declares as its verify contract
        self.assertEqual(detect_verify(d)["command"],
                         "cd playbook-plugin && python3 scripts/verify")

    def test_several_code_roots_keep_one_subshell_each(self):
        PY = self.PY
        d = _mk({".agent/config.json": json.dumps({"code_roots": ["a", "b"]}),
                 "a/scripts/verify": PY, "b/scripts/verify": PY})
        self.assertEqual(detect_verify(d)["command"],
                         "(cd a && python3 scripts/verify) && (cd b && python3 scripts/verify)")

    def test_code_root_with_shell_metacharacters_is_quoted(self):
        d = _mk({".agent/config.json": json.dumps({"code_roots": ["a b;x"]}),
                 "a b;x/scripts/verify": self.PY})
        self.assertEqual(detect_verify(d)["command"], "cd 'a b;x' && python3 scripts/verify")

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


_REAL_PROBE = vd._pytest_available   # captured at import, before any test patches it


if __name__ == "__main__":
    unittest.main(verbosity=2)
