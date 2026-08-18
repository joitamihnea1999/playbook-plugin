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


def _python_components(root: Path) -> list[dict]:
    out: list[dict] = []
    # tests
    if (_pyproject_has(root, "[tool.pytest") or _has(root, "pytest.ini", "tox.ini")
            or (root / "tests").is_dir()
            or "[tool:pytest]" in _read(root / "setup.cfg")):
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


def detect_verify(project_root: Optional[Path] = None) -> dict:
    """Return {"command": str, "components": [ {tool, cmd, reason} ], "notes":[]}.

    `command` is the ` && `-joined assembly of every detected check, or "" when
    nothing was found. Never raises; an unreadable file is treated as absent.
    """
    root = Path(project_root) if project_root is not None else Path.cwd()
    components: list[dict] = []
    components += _python_components(root)
    components += _node_components(root)
    components += _rust_components(root)
    components += _go_components(root)
    components += _make_components(root, already=bool(components))

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
