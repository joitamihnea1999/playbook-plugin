"""Project-root and per-user lane resolution for the provider layer (task 022).

Playbook namespaces agent runtime under `.agent/<user>/` when a
`.agent/current_user` marker is present, so that several humans (or
workstations) can share one repo without trampling each other's session state.
Anything that reads or writes runtime state must resolve the lane rather than
hardcode the root `.agent/`, or the writer and the reader end up in different
directories — the split-brain this module exists to prevent.

Why this lives in `provider/` instead of importing `tasks.core`
---------------------------------------------------------------
The Codex hook scripts bootstrap their imports by putting `scripts/lib/` on
`sys.path` (see `_bootstrap_imports` in `scripts/codex-*-hook`). In that
context `import tasks.core` does NOT reach the canonical `tasks/` package — it
reaches the `scripts/lib/tasks/` mirror, which is 600+ lines diverged and dead
(see mind map [7], [32]). Executing dead code on the enforcement path would be
bad on its own; it would also block the already-parked deletion of that mirror.
`provider/` is byte-mirrored into `scripts/lib/provider/`, so a resolver that
lives here is correct from both import roots.

The validation contract is intentionally identical to `tasks/core.py`
`_validate_username` and `scripts/gate-echo-lib.sh` `resolve_agent_dir`; a
shared test-vector table in `tests/test_provider_multiuser.py` asserts all three
implementations agree, so the copies cannot drift silently.

Error contract — the one deliberate difference
----------------------------------------------
`tasks/core.py` raises `SystemExit` on a malformed marker, which is right for a
CLI. Here it would be wrong: the Codex hooks catch `Exception` (not
`BaseException`) and translate failures into a per-event fail-open/fail-closed
decision. A `SystemExit` would sail past those handlers and exit 1 — bypassing
the deny channel on PreToolUse and bricking otherwise-fine tool calls
elsewhere. So this module raises `InvalidUserMarkerError`, a plain `ValueError`
subclass that the existing handlers already catch.
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = [
    "InvalidUserMarkerError",
    "find_project_root",
    "lanes_without_marker",
    "resolve_agent_dir",
    "validate_username",
]

# Same pattern as tasks/core.py:_USERNAME_RE and the case-globs in
# gate-echo-lib.sh:resolve_agent_dir.
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")


class InvalidUserMarkerError(ValueError):
    """`.agent/current_user` exists but does not name a usable directory."""


def validate_username(name: str) -> str:
    """Return `name` unchanged, or raise InvalidUserMarkerError.

    Rejects: empty, `.`, `..`, anything containing a slash, anything not
    starting with a letter or digit, and anything outside
    [A-Za-z0-9][A-Za-z0-9_.-]* — i.e. exactly what tasks/core.py rejects.
    """
    if not name or name in (".", "..") or "/" in name or not _USERNAME_RE.match(name):
        raise InvalidUserMarkerError(
            f".agent/current_user contains invalid username {name!r}. "
            "Must be non-empty, start with a letter or digit, and contain only "
            "letters, digits, hyphens, underscores, and dots (no spaces or slashes)."
        )
    return name


def resolve_agent_dir(project_root: Path) -> Path:
    """Return the agent state root for this project.

    Multi-user mode: `.agent/current_user` exists → `.agent/<username>/`
    Legacy mode:     `.agent/current_user` absent  → `.agent/`

    An unreadable marker is NOT treated as absent — silently answering the root
    lane there is how state ends up split across two directories. Only a marker
    that genuinely isn't there means "legacy layout".
    """
    marker = project_root / ".agent" / "current_user"
    try:
        raw = marker.read_text(encoding="utf-8")
    except FileNotFoundError:
        return project_root / ".agent"
    except NotADirectoryError:
        # `.agent` is a regular file — not a playbook layout at all.
        return project_root / ".agent"
    except OSError as exc:
        raise InvalidUserMarkerError(
            f"cannot read {marker}: {exc}"
        ) from exc
    return project_root / ".agent" / validate_username(raw.strip())


def lanes_without_marker(project_root: Path) -> list[str]:
    """Lane names present when the repo is in the "fresh clone" shape, else [].

    `.agent/current_user` is gitignored install-local, so a clone of a
    multi-user repo arrives with lanes and no marker. `resolve_agent_dir` then
    answers the root `.agent/`, and whatever writes next mints a phantom lane
    beside the real ones. Every state-CREATING surface must consult this first.

    Deliberately narrow, matching `require_lane_marker` in gate-echo-lib.sh:
    a marker, or an existing root `.agent/tasks/` (a legitimate mixed layout
    where root is itself a lane), or no lanes at all ⇒ empty list.

    Read-only surfaces must not use this to refuse; they should degrade.
    """
    agent = project_root / ".agent"
    if (agent / "current_user").exists():
        return []
    if (agent / "tasks").is_dir():
        return []
    if not agent.is_dir():
        return []
    try:
        return sorted(
            child.name for child in agent.iterdir()
            if child.is_dir() and (child / "tasks").is_dir()
        )
    except OSError:
        return []


def fresh_clone_message(project_root: Path, lanes: list[str], context: str) -> str:
    """The one message every surface prints for the fresh-clone shape."""
    return (
        f"Error: {context} found per-user playbook lanes but no "
        f".agent/current_user marker.\n\n"
        f"  Project: {project_root}\n"
        f"  Lane(s): {', '.join(lanes)}\n\n"
        "The marker is gitignored install-local, so a fresh clone never receives it.\n"
        "Without it every surface would fall back to the shared root .agent/, creating\n"
        "a phantom lane beside the real ones. Pick your lane first:\n\n"
        f"    echo '<your-username>' > \"{project_root}/.agent/current_user\"\n"
    )


def find_project_root(start: Path | None = None) -> Path | None:
    """Walk up from `start` (default: cwd) for the nearest playbook project.

    Matches BOTH layouts — legacy `.agent/tasks/` and multi-user
    `.agent/<user>/tasks/`. Returns None when no project is found; callers
    decide whether that is fatal.

    Mirrors `find_project_root` in `tasks/cli.py` and `gate-echo-lib.sh`.
    """
    here = (start or Path.cwd()).resolve()
    for candidate in [here, *here.parents]:
        agent = candidate / ".agent"
        if (agent / "tasks").is_dir():
            return candidate
        if agent.is_dir():
            try:
                for sub in agent.iterdir():
                    if sub.is_dir() and (sub / "tasks").is_dir():
                        return candidate
            except OSError:
                # Unreadable .agent — keep walking rather than crash a hook.
                pass
    return None
