"""Environment recommendations — the tools that let playbook run smoothly and
optimally, and how to get the ones you're missing (1.5.15).

Advisory ONLY: nothing here ever fails a gate or changes `tasks doctor`'s exit
code. It answers "what would improve this setup, and how do I get it?" across
four categories:

  provider  extra agent CLIs (codex / agy / grok / pi) so the review panel can
            span vendors — a single-vendor panel is the thing playbook exists to
            avoid ("never trust one model; let them disagree").
  sandbox   the OS containment primitive `.claude/bin/sandbox` needs for its
            write-blast-radius guarantee (Linux bubblewrap / macOS seatbelt).
  verify    the command-word binaries the project's declared `verify` command
            calls but that are not on PATH — a missing one makes close fail.
  logging   the ~/.claude/bash-log.sh (BASH_ENV) wiring that feeds chat/task
            attribution.

Surfaced by `tasks environment [--json]`, an advisory section in `tasks doctor`,
and `/playbook:init`. Stdlib only.

Install hints DRIFT as tools rename/move — edit the values in `_PROVIDER_HINTS`
and `_bwrap_install_hint`; the report shape is stable. Where a concrete command
isn't reliably known, the hint points at the vendor's own install docs rather
than fabricating a package name that could send someone to the wrong place.
"""
from __future__ import annotations

import json
import platform
import re
import shutil
import sys
from pathlib import Path
from typing import Optional

SEV_OK = "ok"
SEV_RECOMMENDED = "recommended"
SEV_WARNING = "warning"

# (why it helps, concrete install command or "" when the vendor's docs are the
# honest answer). These are non-Claude panel vendors — claude is the reference
# platform you're already running, so it is not listed.
_PROVIDER_HINTS: dict[str, tuple[str, str]] = {
    "codex": ("OpenAI Codex CLI — a strong non-Claude panel vendor",
              "npm install -g @openai/codex"),
    "agy":   ("Google Antigravity CLI (`agy`, the ex-Gemini agent) — panel vendor",
              ""),
    "grok":  ("xAI Grok CLI (`grok`) — panel vendor; restart Grok after install",
              ""),
    "pi":    ("the pi CLI — panel vendor for qwen / deepseek / oss models",
              ""),
}

# Shell words that are builtins/no-ops, never a "tool to install".
_SHELL_BUILTINS = {
    "cd", "echo", "true", "false", "set", "export", "unset", "test", "[", "[[",
    ":", "exit", "return", "pwd", "source", ".", "env", "then", "else", "fi",
    "do", "done", "wait", "time", "exec",
}


def _item(name: str, category: str, present: bool, severity: str,
          why: str, hint: str = "") -> dict:
    return {"name": name, "category": category, "present": present,
            "severity": severity if not present else SEV_OK,
            "why": why, "hint": hint if not present else ""}


# ── provider CLIs ─────────────────────────────────────────────────────────────

def _provider_items() -> list[dict]:
    items: list[dict] = []
    for name, (why, cmd) in _PROVIDER_HINTS.items():
        present = shutil.which(name) is not None
        hint = (f"e.g. `{cmd}`" if cmd
                else f"install the {name} CLI (see the vendor's docs)")
        items.append(_item(f"agent CLI: {name}", "provider", present,
                           SEV_RECOMMENDED, why, hint))
    return items


# ── sandbox containment ───────────────────────────────────────────────────────

def _bwrap_install_hint() -> str:
    return ("install bubblewrap — e.g. `apt install bubblewrap` (Debian/Ubuntu), "
            "`dnf install bubblewrap` (Fedora), `pacman -S bubblewrap` (Arch)")


def _sandbox_item() -> dict:
    system = platform.system()
    why = (".claude/bin/sandbox uses it for deny-write OS containment "
           "(blast-radius control when running --skip-permissions)")
    if system == "Darwin":
        try:
            from provider.sandbox import _seatbelt_usable
            present = _seatbelt_usable()
        except Exception:
            present = shutil.which("sandbox-exec") is not None
        hint = ("seatbelt (`sandbox-exec`) ships with macOS but could not apply a "
                "deny profile here — check SIP / that you are not already sandboxed")
        return _item("sandbox: seatbelt", "sandbox", present, SEV_RECOMMENDED, why, hint)
    if system == "Linux":
        present = shutil.which("bwrap") is not None
        return _item("sandbox: bubblewrap", "sandbox", present, SEV_RECOMMENDED,
                     why, _bwrap_install_hint())
    # Unknown OS: no containment primitive known.
    return _item(f"sandbox: containment ({system or 'unknown OS'})", "sandbox",
                 False, SEV_RECOMMENDED,
                 "no write-containment primitive is known for this OS — "
                 ".claude/bin/sandbox cannot guarantee blast-radius control",
                 "run the agent only where you trust the blast radius")


# ── verify-command tooling ────────────────────────────────────────────────────

def _extract_verify_commands(verify) -> list[str]:
    """Flatten a config.json `verify` value (string, list, or risk-keyed dict)
    into the list of command strings it would run."""
    if isinstance(verify, str):
        return [verify]
    if isinstance(verify, list):
        return [x for x in verify if isinstance(x, str)]
    if isinstance(verify, dict):
        out: list[str] = []
        for v in verify.values():
            if isinstance(v, str):
                out.append(v)
            elif isinstance(v, list):
                out.extend(x for x in v if isinstance(x, str))
        return out
    return []


def _command_words(command: str) -> list[str]:
    """The leading executable of each pipeline segment of a shell command.

    Best-effort: split on the shell control operators, skip leading `VAR=val`
    env assignments, take the first token. `python3 -m pytest` yields
    `python3` (which is on PATH), so we never false-warn on a stdlib module
    invocation — we only flag a genuinely-absent leading binary.
    """
    words: list[str] = []
    for seg in re.split(r"&&|\|\||;|\||\n", command):
        toks = seg.strip().split()
        i = 0
        while i < len(toks) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[i]):
            i += 1  # skip FOO=bar prefixes
        if i < len(toks):
            words.append(toks[i])
    return words


def _verify_items(project_root: Optional[Path]) -> list[dict]:
    if project_root is None:
        return []
    config = project_root / ".agent" / "config.json"
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    verify = data.get("verify") if isinstance(data, dict) else None
    if not verify:
        return []
    seen: set[str] = set()
    items: list[dict] = []
    for command in _extract_verify_commands(verify):
        for word in _command_words(command):
            if word in _SHELL_BUILTINS or "/" in word or word in seen:
                continue
            seen.add(word)
            present = shutil.which(word) is not None
            items.append(_item(
                f"verify tool: {word}", "verify", present, SEV_WARNING,
                "called by the project's verify command; a missing one makes "
                "`tasks work done` fail at the verify step",
                f"install `{word}` (the tool your verify command runs)"))
    return items


# ── shell command logging ─────────────────────────────────────────────────────

def _logging_item() -> dict:
    home = Path.home()
    bash_log = home / ".claude" / "bash-log.sh"
    settings = home / ".claude" / "settings.json"
    wired = False
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
        env = data.get("env") if isinstance(data, dict) else None
        be = env.get("BASH_ENV") if isinstance(env, dict) else None
        wired = bool(be) and be.replace("\\", "/").endswith("/.claude/bash-log.sh")
    except (OSError, ValueError):
        wired = False
    present = bash_log.is_file() and wired
    return _item("shell command logging (BASH_ENV)", "logging", present,
                 SEV_RECOMMENDED,
                 "logs each bash command to the chat log, which feeds task "
                 "attribution (`tasks log`) and retros",
                 "re-run `/playbook:init` — it deploys ~/.claude/bash-log.sh and "
                 "sets BASH_ENV in ~/.claude/settings.json")


# ── report + render ───────────────────────────────────────────────────────────

def environment_report(project_root: Optional[Path] = None) -> dict:
    """{"platform": str, "items": [ {name, category, present, severity, why,
    hint} ]}. Never raises — an unreadable surface degrades to "recommend it"."""
    items: list[dict] = []
    items.extend(_provider_items())
    items.append(_sandbox_item())
    items.extend(_verify_items(project_root))
    items.append(_logging_item())
    return {"platform": platform.system(), "items": items}


def suggestions(report: dict) -> list[dict]:
    """Only the items worth acting on — the absent/suboptimal ones."""
    return [i for i in report["items"] if not i["present"]]


_CATEGORY_ORDER = ["provider", "sandbox", "verify", "logging"]
_CATEGORY_TITLE = {
    "provider": "Agent CLIs (extra panel vendors)",
    "sandbox": "Sandbox containment",
    "verify": "Verify-command tooling",
    "logging": "Shell command logging",
}


def render_environment(report: dict, show_ok: bool = True) -> str:
    lines = ["=== Environment recommendations (advisory — none of this fails a gate) ==="]
    by_cat: dict[str, list[dict]] = {}
    for i in report["items"]:
        by_cat.setdefault(i["category"], []).append(i)
    any_line = False
    for cat in _CATEGORY_ORDER:
        cat_items = by_cat.get(cat, [])
        shown = cat_items if show_ok else [i for i in cat_items if not i["present"]]
        if not shown:
            continue
        lines.append(f"\n[{_CATEGORY_TITLE[cat]}]")
        for i in shown:
            any_line = True
            if i["present"]:
                lines.append(f"  ✓ {i['name']}")
            else:
                tag = "!" if i["severity"] == SEV_WARNING else "•"
                lines.append(f"  {tag} {i['name']} — {i['why']}")
                if i["hint"]:
                    lines.append(f"      → {i['hint']}")
    if not any_line:
        lines.append("\n  Everything recommended is present. Nothing to install.")
    return "\n".join(lines)


def cli_environment(cmd_args: list[str], project_root: Path) -> int:
    """`tasks environment [--json] [--suggest-only]`."""
    as_json = False
    show_ok = True
    for a in cmd_args:
        if a == "--json":
            as_json = True
        elif a == "--suggest-only":
            show_ok = False
        else:
            print(f"Error: unknown environment flag '{a}'", file=sys.stderr)
            return 2
    report = environment_report(project_root)
    if as_json:
        print(json.dumps(report, indent=2))
    else:
        print(render_environment(report, show_ok=show_ok))
    return 0
