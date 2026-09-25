"""Detect a project's full verify command (1.5.19).

`tasks work done` runs the declared `verify` command; if none is set, close
loudly refuses to claim it verified. `/playbook:init` used to have the AGENT
free-form-inspect the project and guess a command — non-deterministic and
untested. This module makes that inspection a deterministic, testable primitive:
look at what toolchains are actually present and assemble a command that runs
ALL of them (typecheck AND tests AND lint), chained with ` && `.

The goal is "everything runs" — the assembled command is a STARTING POINT the
user confirms/corrects at init time (a missed tool is exactly what the confirm
step catches); it is never silently authoritative. Stdlib only; reads small
config files and never executes the command it composes. Its ONE execution is
a `python3 -m pytest --version` probe (owner decision, task 098), run only for
a Python `tests/` tree with no pytest config, to decide between pytest and
unittest: pytest is suggested where the project CONFIGURES it (no probe) or
where the probe finds it installed, never otherwise.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _has(root: Path, *names: str) -> bool:
    return any((root / n).exists() for n in names)


def _pyproject_has(root: Path, needle: str) -> bool:
    return needle in _read(root / "pyproject.toml")


def _component(tool: str, cmd: str, reason: str) -> dict:
    return {"tool": tool, "cmd": cmd, "reason": reason}


_IMPORTS_PYTEST = re.compile(r"^\s*(import|from)\s+pytest\b", re.M)
# a module-level pytest-style test function: unittest discover never runs it
_BARE_TEST_FUNC = re.compile(r"^(?:async\s+)?def\s+test", re.M)
# a `class Test…` and its bases
_TEST_CLASS = re.compile(r"^\s*class\s+Test\w*\s*(?:\(([^)]*)\))?\s*:", re.M)
# the base names that make a class a TestCase, compared exactly (task 098)
_TESTCASE_BASES = frozenset({"TestCase", "unittest.TestCase",
                             "IsolatedAsyncioTestCase", "unittest.IsolatedAsyncioTestCase"})
# a package `load_tests` hook: any mention of the name (def, assignment, import)
_LOAD_TESTS = re.compile(r"\bload_tests\b")
_STAR_IMPORT = re.compile(r"^\s*from\s+\S+\s+import\s+\*", re.M)
# a module-level alias named like a test (`test_x = …`, `TestX = …`)
_TEST_ALIAS = re.compile(r"^(?:test|Test)\w*\s*(?::[^=\n]*)?=(?!=)", re.M)
# Directories the outside-`tests/` walk never enters: hidden (.git, .venv, …)
# and vendored/installed trees whose tests are not the project's.
_SKIP_DIRS = frozenset({"node_modules", "venv", "env", "site-packages", "__pycache__",
                        "build", "dist"})
_WALK_CAP = 20000   # files the outside-`tests/` walk reads before it stops looking
_SEEN_CAP = 8       # observations listed in the note


def _is_testcase_base(bases) -> bool:
    return any(b.strip() in _TESTCASE_BASES for b in (bases or "").split(","))


def _pytest_available() -> bool:
    """`python3 -m pytest --version` succeeds (owner decision, task 098: the one
    execution this module makes). Any error, timeout or missing interpreter is
    False — the answer then is unittest, never a pytest that is not there."""
    exe = shutil.which("python3") or sys.executable
    try:
        r = subprocess.run([exe, "-m", "pytest", "--version"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    return r.returncode == 0


def _test_files_outside_tests(root: Path) -> "list[str]":
    """`test_*.py` / `*_test.py` outside `<root>/tests` (hidden and vendored
    directories not walked, at most _WALK_CAP files read)."""
    tests = os.path.normcase(os.path.abspath(root / "tests"))
    out: "list[str]" = []
    seen = 0
    try:
        for cur, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs
                             if not d.startswith(".") and d not in _SKIP_DIRS
                             and os.path.normcase(os.path.abspath(os.path.join(cur, d))) != tests)
            for f in sorted(files):
                seen += 1
                if seen > _WALK_CAP:
                    return out
                if f.endswith(".py") and (f.startswith("test_") or f.endswith("_test.py")):
                    out.append(Path(os.path.relpath(os.path.join(cur, f), root)).as_posix())
    except OSError:
        pass
    return out


def _discover_observations(root: Path) -> "list[str]":
    """Shapes in this tree that `unittest discover -s tests` can skip WITHOUT
    failing. A text scan — an observation for the human to confirm at init, NOT
    a completeness check (task 098: three rounds of a single-judge review each
    found new shapes a scan can miss in a dynamic language)."""
    tests = root / "tests"
    seen: "list[str]" = []
    try:
        for init in sorted(tests.rglob("__init__.py")):
            text = _read(init)
            rel = init.relative_to(root).as_posix()
            if _LOAD_TESTS.search(text):
                seen.append(f"`{rel}` binds load_tests (discover stops recursing there)")
            elif _STAR_IMPORT.search(text):
                seen.append(f"`{rel}` has an `import *` (it may bind load_tests)")
        for extra in ("conftest.py", "*_test.py"):
            for f in sorted(tests.rglob(extra)):
                seen.append(f"`{f.relative_to(root).as_posix()}` is a pytest-only file")
        for f in sorted(tests.rglob("test_*.py")):
            rel = f.relative_to(root).as_posix()
            d = f.parent
            while d != tests:
                if not (d / "__init__.py").is_file():
                    seen.append(f"`{d.relative_to(root).as_posix()}/` is not a package (discover skips it)")
                    break
                d = d.parent
            text = _read(f)
            if _IMPORTS_PYTEST.search(text):
                seen.append(f"`{rel}` imports pytest")
            if _BARE_TEST_FUNC.search(text):
                seen.append(f"`{rel}` has a module-level `def test…`")
            if _TEST_ALIAS.search(text):
                seen.append(f"`{rel}` has a module-level test alias (`test… =` / `Test… =`)")
            if any(not _is_testcase_base(m.group(1)) for m in _TEST_CLASS.finditer(text)):
                seen.append(f"`{rel}` has a `class Test…` whose base is not exactly TestCase "
                            "(indirect or plain)")
    except OSError:
        seen.append("the tests/ tree could not be read in full")
    seen += [f"`{p}` is a test file outside tests/" for p in _test_files_outside_tests(root)]
    uniq = list(dict.fromkeys(seen))
    if len(uniq) > _SEEN_CAP:
        uniq = uniq[:_SEEN_CAP] + [f"… and {len(uniq) - _SEEN_CAP} more"]
    return uniq


def _unittest_note(root: Path, prefix: str = "") -> str:
    seen = _discover_observations(root)
    found = ("Seen in this tree: " + "; ".join(seen) + ".") if seen else \
        "None of these shapes was seen by a text scan — that is not a proof."
    return (f"{prefix}`python3 -m unittest discover -s tests` is suggested because pytest is not "
            "installed here. It is a STARTING POINT, not a complete runner: discover can skip "
            "tests without failing — a package `__init__.py` that binds `load_tests` (by def, "
            "assignment, import or `import *`), module-level aliases (`test_x = …`, "
            "`TestX = …`), test files outside `tests/`, `class Test…` with an indirect base, "
            "bare `def test…` functions, test directories without `__init__.py`. Confirm at "
            "init that its `Ran N tests` matches the suite. " + found)


def _python_components(root: Path, notes: "Optional[list[str]]" = None) -> list[dict]:
    out: list[dict] = []
    # tests — pytest where the project has it (config, or it is installed); it
    # runs unittest TestCases too. Otherwise unittest discover, with its limits
    # stated (owner decision, task 098). Never a pytest that is not installed.
    pytest_cfg = (_pyproject_has(root, "[tool.pytest") or _has(root, "pytest.ini", "tox.ini")
                  or "[tool:pytest]" in _read(root / "setup.cfg"))
    if pytest_cfg:
        out.append(_component("pytest", "python3 -m pytest", "pytest config"))
    elif (root / "tests").is_dir():
        if _pytest_available():
            out.append(_component("pytest", "python3 -m pytest",
                                  "pytest is installed; it runs unittest TestCases too"))
        else:
            out.append(_component("unittest", "python3 -m unittest discover -s tests",
                                  "tests/ and no pytest installed — see the note"))
            if notes is not None:
                notes.append(_unittest_note(root))
    # type check (mypy or pyright — not both)
    if _pyproject_has(root, "[tool.mypy") or _has(root, "mypy.ini", ".mypy.ini"):
        out.append(_component("mypy", "mypy .", "mypy config"))
    elif _pyproject_has(root, "[tool.pyright") or _has(root, "pyrightconfig.json"):
        out.append(_component("pyright", "pyright", "pyright config"))
    # lint (ruff or flake8)
    if _pyproject_has(root, "[tool.ruff") or _has(root, "ruff.toml", ".ruff.toml"):
        out.append(_component("ruff", "ruff check .", "ruff config"))
    elif _has(root, ".flake8") or "[flake8]" in _read(root / "setup.cfg"):
        out.append(_component("flake8", "flake8", "flake8 config"))
    return out


def _node_components(root: Path) -> list[dict]:
    pkg = root / "package.json"
    if not pkg.is_file():
        return []
    try:
        parsed = json.loads(_read(pkg))
    except ValueError:
        parsed = None
    # A valid-but-non-object package.json (a bare list/string/number/bool) must
    # not crash `.get` — honor the module's "never raises" contract.
    scripts = parsed.get("scripts") if isinstance(parsed, dict) else None
    if not isinstance(scripts, dict):
        scripts = {}
    out: list[dict] = []
    # Prefer the project's own declared scripts (they encode the intended checks).
    for name in ("typecheck", "type-check", "tsc"):
        if name in scripts:
            out.append(_component(f"npm:{name}", f"npm run {name}", "package.json script"))
            break
    if "test" in scripts:
        out.append(_component("npm:test", "npm test", "package.json test script"))
    for name in ("lint", "eslint"):
        if name in scripts:
            out.append(_component(f"npm:{name}", f"npm run {name}", "package.json script"))
            break
    return out


def _rust_components(root: Path) -> list[dict]:
    if not (root / "Cargo.toml").is_file():
        return []
    return [
        _component("cargo-test", "cargo test", "Cargo.toml"),
        _component("cargo-clippy", "cargo clippy -- -D warnings", "Cargo.toml (clippy)"),
    ]


def _go_components(root: Path) -> list[dict]:
    if not (root / "go.mod").is_file():
        return []
    return [
        _component("go-test", "go test ./...", "go.mod"),
        _component("go-vet", "go vet ./...", "go.mod"),
    ]


def _make_targets(root: Path) -> set[str]:
    targets: set[str] = set()
    for name in ("Makefile", "makefile", "GNUmakefile"):
        text = _read(root / name)
        if not text:
            continue
        for line in text.splitlines():
            # A target line is `name:` (or `name: deps`) at column 0 — not a
            # recipe (tab-indented), not a variable assignment. Reject both
            # `name = …` (`=` in the name) and `name := …`/`name ::= …` (an `=`
            # immediately after the colon), so a `test := build/out` variable is
            # not mistaken for a `test:` target (which would suggest a `make
            # test` that fails at verify time).
            if not (line[:1].isalpha() and ":" in line):
                continue
            name, _, rest = line.partition(":")
            if "=" in name or rest.lstrip(":").startswith("="):
                continue
            targets.add(name.strip())
    return targets


def _make_components(root: Path, already: bool) -> list[dict]:
    # Only offer a Make target when nothing else was detected OR the target
    # name is unambiguous — a `make test` alongside detected pytest would double
    # up. Keep it as a fallback for Makefile-driven projects.
    if already:
        return []
    targets = _make_targets(root)
    out: list[dict] = []
    for t in ("check", "test", "lint"):
        if t in targets:
            out.append(_component(f"make:{t}", f"make {t}", f"Makefile `{t}` target"))
    return out


def _entrypoint_component(root: Path) -> Optional[dict]:
    """Task 096: a `scripts/verify` file is the project's own declared "run
    everything" entrypoint, so it is the ONLY component for its root."""
    path = root / "scripts" / "verify"
    if not path.is_file():
        return None
    lines = _read(path).splitlines()
    shebang = lines[0] if lines and lines[0].startswith("#!") else ""
    if "python" in shebang:
        cmd = "python3 scripts/verify"
    elif re.search(r"\b(bash|sh)\b", shebang):
        cmd = "bash scripts/verify"
    else:
        cmd = "scripts/verify"
    return _component("scripts/verify", cmd, "the project's own verify entrypoint")


def _root_components(root: Path, notes: "Optional[list[str]]" = None) -> list[dict]:
    entry = _entrypoint_component(root)
    if entry is not None:
        return [entry]
    components: list[dict] = []
    components += _python_components(root, notes)
    components += _node_components(root)
    components += _rust_components(root)
    components += _go_components(root)
    components += _make_components(root, already=bool(components))
    return components


def _code_root_dirs(root: Path) -> list[str]:
    """The project's `code_roots` (nested checkouts, `.agent/config.json`) that
    are real directories inside the project. Validation is the fingerprint's:
    `_code_roots` rejects absolute paths, `..` and junk (loudly), and a root whose
    RESOLVED path leaves the project (a symlink) is skipped, as core.py does."""
    try:
        cfg = json.loads(_read(root / ".agent" / "config.json") or "{}")
    except ValueError:
        return []
    if not isinstance(cfg, dict) or not cfg.get("code_roots"):
        return []
    try:
        from tasks.core import _code_roots
        rels = _code_roots(cfg)
    except Exception as exc:
        print(f"[playbook] detect-verify: code_roots not inspected ({type(exc).__name__}: {exc})",
              file=sys.stderr)
        return []
    out: list[str] = []
    try:
        proj = root.resolve()
    except (OSError, RuntimeError, ValueError):
        return []
    for rel in rels:
        cand = root / rel
        try:
            res = cand.resolve()
            inside = res != proj and proj in res.parents
        except (OSError, RuntimeError, ValueError):
            inside = False
        if inside and cand.is_dir():
            out.append(rel)
    return out


def detect_verify(project_root: Optional[Path] = None) -> dict:
    """Return {"command": str, "components": [ {tool, cmd, reason} ], "notes":[]}.

    `command` is the ` && `-joined assembly of every detected check, or "" when
    nothing was found. Never raises; an unreadable file is treated as absent.
    Each `code_roots` checkout is inspected too; its components run as
    `(cd <root> && <cmd>)` — verify runs in a real bash script, so the subshell
    keeps each root's cwd to itself (task 096).
    """
    root = Path(project_root) if project_root is not None else Path.cwd()
    notes: list[str] = []
    components = _root_components(root, notes)
    _outer_count = len(components)
    _roots_used: list[str] = []
    for rel in _code_root_dirs(root):
        _root_notes: list[str] = []
        _found = _root_components(root / rel, _root_notes)
        if _found:
            _roots_used.append(rel)
        for c in _found:
            comp = _component(f"{rel}:{c['tool']}",
                              f"(cd {shlex.quote(rel)} && {c['cmd']})",
                              f"code_roots `{rel}`: {c['reason']}")
            comp["_root"], comp["_raw"] = rel, c["cmd"]
            components.append(comp)
        notes += [f"code_roots `{rel}`: {n}" for n in _root_notes]
    _single_root = _roots_used[0] if (_outer_count == 0 and len(_roots_used) == 1) else None

    # De-dup by cmd while preserving order.
    seen: set[str] = set()
    unique = [c for c in components if not (c["cmd"] in seen or seen.add(c["cmd"]))]

    if not unique:
        notes.append("No known toolchain detected — set `verify` in .agent/config.json "
                     "by hand (a command that typechecks, tests, and lints everything).")
    command = " && ".join(c["cmd"] for c in unique)
    # One code root and nothing from the outer root: `cd <root> && a && b` —
    # the form this workspace declares (task 098); nothing follows, so the cwd
    # change cannot leak. Several roots keep one `(cd … && …)` subshell each.
    if unique and _single_root is not None and all(c.get("_root") == _single_root for c in unique):
        command = f"cd {shlex.quote(_single_root)} && " + " && ".join(c["_raw"] for c in unique)
    for c in unique:
        c.pop("_root", None)
        c.pop("_raw", None)
    return {"command": command, "components": unique, "notes": notes}


def render_verify(report: dict) -> str:
    lines = ["=== Detected verify command (confirm it runs EVERYTHING before using) ==="]
    if report["command"]:
        lines.append(f"\n  {report['command']}\n")
        lines.append("Components:")
        for c in report["components"]:
            lines.append(f"  - {c['cmd']:<32} ({c['reason']})")
    for n in report["notes"]:
        lines.append(f"\n{n}")
    return "\n".join(lines)


def cli_detect_verify(cmd_args: list[str], project_root: Path) -> int:
    """`tasks detect-verify [--json]` — print a suggested full-verify command."""
    as_json = False
    for a in cmd_args:
        if a == "--json":
            as_json = True
        else:
            print(f"Error: unknown detect-verify flag '{a}'", file=sys.stderr)
            return 2
    report = detect_verify(project_root)
    print(json.dumps(report, indent=2) if as_json else render_verify(report))
    return 0
