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
config files, never executes anything.
"""
from __future__ import annotations

import json
import re
import shlex
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
_IMPORTS_UNITTEST = re.compile(r"^\s*(import\s+unittest\b|from\s+unittest\b)", re.M)


def _unittest_only(root: Path) -> bool:
    """Task 096: a bare stdlib-unittest suite (this very project) was read as
    pytest. Unittest only on POSITIVE evidence — every `tests/test_*.py` imports
    unittest and none imports pytest, and no `conftest.py`: a pytest test
    function needs no import at all, so "no pytest import" proves nothing."""
    tests = root / "tests"
    if not tests.is_dir() or _has(root, "conftest.py") or (tests / "conftest.py").exists():
        return False
    try:
        files = sorted(tests.rglob("test_*.py"))
    except OSError:
        return False
    if not files:
        return False
    for f in files:
        text = _read(f)
        if _IMPORTS_PYTEST.search(text) or not _IMPORTS_UNITTEST.search(text):
            return False
    return True


def _python_components(root: Path) -> list[dict]:
    out: list[dict] = []
    # tests
    pytest_cfg = (_pyproject_has(root, "[tool.pytest") or _has(root, "pytest.ini", "tox.ini")
                  or "[tool:pytest]" in _read(root / "setup.cfg"))
    if not pytest_cfg and _unittest_only(root):
        out.append(_component("unittest", "python3 -m unittest discover -s tests",
                              "every tests/test_*.py imports unittest, none pytest"))
    elif pytest_cfg or (root / "tests").is_dir():
        out.append(_component("pytest", "python3 -m pytest", "pytest config / tests dir"))
    elif _has(root, "pyproject.toml", "setup.py", "setup.cfg") and _has(root, "tests"):
        out.append(_component("pytest", "python3 -m pytest", "python project with tests/"))
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


def _root_components(root: Path) -> list[dict]:
    entry = _entrypoint_component(root)
    if entry is not None:
        return [entry]
    components: list[dict] = []
    components += _python_components(root)
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
    except Exception:
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
    components = _root_components(root)
    for rel in _code_root_dirs(root):
        for c in _root_components(root / rel):
            components.append(_component(f"{rel}:{c['tool']}",
                                         f"(cd {shlex.quote(rel)} && {c['cmd']})",
                                         f"code_roots `{rel}`: {c['reason']}"))

    # De-dup by cmd while preserving order.
    seen: set[str] = set()
    unique = [c for c in components if not (c["cmd"] in seen or seen.add(c["cmd"]))]

    notes: list[str] = []
    if not unique:
        notes.append("No known toolchain detected — set `verify` in .agent/config.json "
                     "by hand (a command that typechecks, tests, and lints everything).")
    command = " && ".join(c["cmd"] for c in unique)
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
