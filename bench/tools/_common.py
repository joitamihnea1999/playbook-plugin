"""Shared helpers for the corpus-builder tools (read-only workspace access)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_BENCH_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = _BENCH_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_PLUGIN_ROOT = REPO_ROOT / "plugins" / "playbook"
if str(_PLUGIN_ROOT) not in sys.path:          # bench.lib.package imports tasks.template
    sys.path.insert(0, str(_PLUGIN_ROOT))

DEFAULT_CASES_DIR = _BENCH_ROOT / "corpus" / "cases"
DEFAULT_CORPUS_DIR = _BENCH_ROOT / "corpus"
PROMPT_BUDGET_CHARS = 90_000          # plan §3.4: every case renders under this (grok argv)


class ToolError(Exception):
    """Operator-facing failure; the message is printed and mapped to an exit code."""


def utf8_stdio() -> None:
    """Windows lesson (plan §8): a cp1252 console truncates the output of a tool
    that prints an arbitrary task title."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def git(repo: Path, *args: str, check: bool = True) -> str:
    """Run git in `repo`; stdout as text. Never writes (callers pass read-only verbs)."""
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise ToolError(f"git {' '.join(args[:3])} failed in {repo}: {proc.stderr.strip()}")
    return proc.stdout


def git_ok(repo: Path, *args: str) -> bool:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    return proc.returncode == 0


def resolve_commit(repo: Path, ref: str) -> str:
    """Full 40-hex sha of `ref`, or ToolError when it does not name a commit."""
    proc = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    sha = proc.stdout.strip()
    if proc.returncode != 0 or len(sha) != 40:
        raise ToolError(f"{ref!r} does not resolve to a commit in {repo}")
    return sha


def task_number(task: str) -> str:
    try:
        return f"{int(task):03d}"
    except ValueError:
        raise ToolError(f"--task must be a number, got {task!r}") from None


def find_task_dir(workspace: Path, task: str) -> Path:
    """`<workspace>/.agent/tasks/<NNN>-*` — exactly one match required."""
    num = task_number(task)
    tasks_root = Path(workspace) / ".agent" / "tasks"
    if not tasks_root.is_dir():
        raise ToolError(f"no .agent/tasks under {workspace}")
    hits = sorted(p for p in tasks_root.iterdir() if p.is_dir() and p.name.startswith(num + "-"))
    if len(hits) != 1:
        raise ToolError(f"task {num}: expected one dir under {tasks_root}, found {[p.name for p in hits]}")
    return hits[0]


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def parse_source_repos(specs: list, default_playbook: bool = True) -> dict:
    """`NAME=PATH` list → {name: Path}. `playbook-plugin` defaults to this repo."""
    out = {}
    if default_playbook:
        out["playbook-plugin"] = REPO_ROOT
    for spec in specs or []:
        name, sep, path = spec.partition("=")
        if not sep or not name.strip() or not path.strip():
            raise ToolError(f"--source-repo expects NAME=PATH, got {spec!r}")
        out[name.strip()] = Path(path.strip()).expanduser()
    return out


def short(sha: str, n: int = 10) -> str:
    return (sha or "")[:n]
