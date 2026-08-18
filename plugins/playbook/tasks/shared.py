"""Small helpers shared by more than one command module.

Boundary: project-root discovery (`find_project_root`), THE session-liveness
policy and its GC sweep (`_own_session_id` / `_session_is_dead` /
`_gc_dead_sessions` — one policy, two consumers: the CLI entry sweep and
doctor's report; bash twin in scripts/session-start-hook), and loading the
merge skill's verify runner by path (`_merge_verify_module` and its two
config advisories — consumed by the close path's verify contract and by
doctor). Imports stdlib + tasks.core ONLY; never a command module. Command
modules import from here, one direction (design-1.5.9.md §4).
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from tasks.core import resolve_agent_dir, resolve_session_id


def find_project_root() -> Path:
    """Find project root by looking for the nearest .agent/tasks/ directory."""
    cwd = Path.cwd()

    for p in [cwd, *cwd.parents]:
        agent = p / ".agent"
        if (agent / "tasks").exists():
            return p
        # Multi-user layout: .agent/<user>/tasks/
        if agent.is_dir():
            for sub in agent.iterdir():
                if sub.is_dir() and (sub / "tasks").exists():
                    return p

    # Fall back to cwd (create_task will make .agent/tasks/)
    return cwd


def _own_session_id() -> str:
    """The session id to exclude from any sweep or staleness report.

    Prefers PLAYBOOK_SESSION_ID but falls back to resolve_session_id() rather
    than to "" — the env var does not always propagate (VSCode CLAUDE_ENV_FILE
    quirks, missing wrappers, subprocess loss), and on Windows resolve_session_id
    returns the constant `pid-win-fallback`, whose non-numeric suffix makes
    `int()`/`kill -0` fail. With an empty own-id, every CLI invocation would
    therefore classify the shared Windows session dir as dead and delete it
    (task 027).

    Delegates to resolve_session_id() so the env value is SANITIZED (a malformed
    PLAYBOOK_SESSION_ID is neutralized to the derived pid, not returned raw) —
    parity with the one-resolver contract. This id is only ever a comparison key
    for GC exclusion, never a path/delete component, so it was never a traversal
    vector; the change is for consistency, not a live fix.
    """
    return resolve_session_id()


def _session_is_dead(session_dir: Path, own_session: str, cutoff: float) -> bool:
    """THE session-liveness policy. One function, so consumers cannot drift.

    Callers: _gc_dead_sessions (deletes) and `tasks doctor` (reports). Its bash
    twin is the sweep in `scripts/session-start-hook`; parity is asserted by S18
    in tests/wrapper-multiuser-fixture.sh.

    `pid-*` names are decided by liveness ALONE, never by mtime: `current_state`
    is written only at activation, so a busy session's pointer can be arbitrarily
    old while a dead session's can be seconds fresh. Only legacy non-PID names
    (pre-migration UUIDs, "default") fall back to the 24h mtime rule.
    """
    name = session_dir.name
    if own_session and name == own_session:
        return False                      # never our own session
    if name.startswith("pid-"):
        try:
            os.kill(int(name[4:]), 0)
            return False                  # alive — keep
        except PermissionError:
            # EPERM: the process EXISTS but belongs to another OS user. Alive, so
            # keep it — reclaiming it would be cross-user data loss.
            return False
        except (ValueError, OverflowError, OSError):
            # ValueError: not a number. OverflowError: numeric but too large for
            # C pid_t — NOT an OSError, so leaving it out made one such directory
            # crash every single `tasks` invocation (this runs at CLI entry).
            # OSError/ProcessLookupError: genuinely no such process.
            return True
    state_file = session_dir / "current_state"
    try:
        if state_file.exists() and state_file.stat().st_mtime >= cutoff:
            return False                  # fresh — keep
    except OSError:
        pass
    return True


def _gc_dead_sessions(project_path: Path) -> None:
    """Remove dead session dirs and legacy flat files.

    Called at every tasks invocation. Cheap: O(N sessions × 1 stat).

    THIS IS THE CANONICAL SESSION-GC POLICY, AND IT HAS A BASH TWIN:
    `scripts/session-start-hook` sweeps the same directory at SessionStart and
    must implement the same rules. Until v1.4.6 it did not — it keyed purely on
    `current_state` mtime, so it deleted the live session's own pointer for any
    task active >24h (task 027). Change both or neither; the parity assertion
    lives in `tests/wrapper-multiuser-fixture.sh` S18.

    Policy: never our own session → `pid-*` kept iff the pid is alive → any
    other name (legacy UUID, "default") kept iff `current_state` is <24h old.
    A `pid-*` name that isn't a live pid is removed regardless of mtime: a busy
    session's pointer can be arbitrarily old (it is written only at activation),
    and a dead session's can be seconds fresh.

    Legacy flat files (.hook_counters.*, current_state*) in .agent/ root
    are always removed — they're pre-migration artifacts.
    """
    agent_dir = resolve_agent_dir(project_path)
    sessions_dir = agent_dir / "sessions"

    # Clean legacy flat files from pre-migration layout
    for pattern in (".hook_counters.*", "current_state", "current_state.*"):
        for f in agent_dir.glob(pattern):
            if f.is_file():
                try:
                    f.unlink()
                except OSError:
                    pass

    # Clean dead session dirs (see _session_is_dead)
    if not sessions_dir.exists():
        return
    cutoff = time.time() - 86400
    own_session = _own_session_id()
    for session_dir in sessions_dir.iterdir():
        # Skip symlinks explicitly. rmtree already refuses to follow one, but the
        # bash twin must skip them too (there, `rm -rf "link/"` destroys the
        # TARGET), so stating it keeps the two policies visibly identical.
        if session_dir.is_symlink() or not session_dir.is_dir():
            continue
        if _session_is_dead(session_dir, own_session, cutoff):
            shutil.rmtree(session_dir, ignore_errors=True)


def _merge_verify_module():
    """Load the merge skill's merge-verify.py as a module, or None.

    The skill ships standalone (it must run from its own directory with no
    package on the path), so it can't be imported normally — and its filename is
    hyphenated. Loading it by path is still worth it: doctor then validates
    `merge_verify` with the exact rules a merge will enforce, instead of a second
    copy of them that can drift.
    """
    import importlib.util
    path = Path(__file__).resolve().parent.parent / "skills" / "merge" / "merge-verify.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location("_playbook_merge_verify", path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _merge_verify_issues(cfg: dict) -> list[str]:
    """Advisory warnings for config.json's `merge_verify` declaration.

    Empty when nothing is declared (a legitimate choice) or when the declaration
    is usable. A present-but-broken declaration BLOCKS the merge skill's verify
    step rather than being silently ignored, so surfacing it in doctor is how a
    typo gets caught before it costs someone a merge.
    """
    if not isinstance(cfg, dict) or "merge_verify" not in cfg:
        return []
    # Loading compiles the shipped runner, so a corrupt or partially-written
    # copy raises here. Doctor is advisory: it must report that it couldn't
    # check, never take the whole run down with it.
    try:
        mod = _merge_verify_module()
    except Exception as e:
        return [f"could not be validated ({e})"]
    if mod is None:  # skill dir absent (partial install) — nothing to say
        return []
    try:
        command = mod.command_from_config(cfg)
    except mod.Unusable as exc:
        return [f"{exc} — the merge skill will BLOCK on this, not skip it"]
    except Exception as e:
        return [f"could not be validated ({e})"]
    if command is None:
        return ["declared but empty — merges will report SKIPPED "
                "(no post-merge soundness check will run)"]
    return []


def _merge_verify_untracked(project_path: Path, cfg: dict) -> list[str]:
    """Warn when a declared `merge_verify` lives in a file git isn't tracking.

    A verify command only does its job if every clone sees it; an untracked
    config means the gate exists on one machine and every other clone reports
    SKIPPED. Advisory — plenty of legitimate setups (a fresh repo, a non-git
    checkout) hit this transiently.
    """
    if not isinstance(cfg, dict) or "merge_verify" not in cfg:
        return []
    import subprocess

    def _git(*args):
        return subprocess.run(["git", *args], cwd=project_path,
                              capture_output=True, text=True)

    try:
        # Not a git work tree at all (e.g. a dogfooding workspace whose repos are
        # nested inside it) — "other clones" would be a meaningless thing to say.
        inside = _git("rev-parse", "--is-inside-work-tree")
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return []
        proc = _git("ls-files", "--error-unmatch", ".agent/config.json")
    except (OSError, subprocess.SubprocessError):
        return []  # no git available — nothing useful to say
    if proc.returncode == 0:
        return []
    return ["declared in an untracked .agent/config.json — other clones will "
            "report SKIPPED; `git add .agent/config.json` to make it repo policy"]
