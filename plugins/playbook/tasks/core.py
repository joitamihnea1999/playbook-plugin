"""Task management operations for .agent/tasks/ directories."""
from __future__ import annotations

import datetime
import functools
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path

from tasks.atomic import atomic_write

VERSION = "1.5.36"

AGENT_PROCESS_NAMES = frozenset({"claude", "codex", "agy", "grok", "pi"})


@functools.lru_cache(maxsize=1)
def find_agent_root_pid() -> int | None:
    """Walk parent process tree, return PID of the highest agent ancestor.

    Identifies claude/codex/agy/grok/pi processes by `comm` (basename, no args).
    Returns None if no agent found within 20 hops or if `ps` is unavailable.
    Used as fallback when PLAYBOOK_SESSION_ID env var isn't propagated —
    Python and bash both walk the same tree and converge on the same PID.
    Result is cached: process tree is stable for the lifetime of this process.
    """
    # Windows/MSYS: this ancestor scan is non-functional and must be skipped.
    # Git-Bash `ps` has no `-o` flag (breaks on the first call), and MSYS vs
    # native-Windows PID namespaces are disjoint — there is no walkable path
    # from a hook/CLI subprocess up to claude.exe. Return None and let
    # resolve_session_id() lean on PLAYBOOK_SESSION_ID. POSIX is untouched.
    if sys.platform == "win32" or os.name == "nt":
        return None
    pid = os.getppid()
    last_agent_pid: int | None = None
    for _ in range(20):
        if pid in (0, 1):
            break
        try:
            r = subprocess.run(
                ["ps", "-p", str(pid), "-o", "ppid=,comm="],
                capture_output=True, text=True, timeout=1,
            )
        except (OSError, subprocess.TimeoutExpired):
            break
        if r.returncode != 0 or not r.stdout.strip():
            break
        parts = r.stdout.strip().split(None, 1)
        if len(parts) < 2:
            break
        try:
            ppid = int(parts[0])
        except ValueError:
            break
        comm = os.path.basename(parts[1].strip())
        if comm in AGENT_PROCESS_NAMES:
            last_agent_pid = pid
        if ppid == pid:
            break
        pid = ppid
    return last_agent_pid


_SESSION_ID_RE = re.compile(r"[A-Za-z0-9._-]+")


def _sanitize_session_id(sid: str) -> str:
    """Accept an externally-supplied session id ONLY if it is a safe single
    directory component; otherwise return "" so the caller falls back to the
    derived pid (C4).

    This value becomes a path component in `rm -rf .agent/sessions/<id>` and in
    EVERY hook's path composition, so an unsanitized `PLAYBOOK_SESSION_ID=../tasks`
    deleted the task DB. Allow the canonical `pid-*` ids AND the sanctioned
    `judge` session id (both match the charset); reject anything with a slash,
    whitespace, control char, or the traversal components `.`/`..`. Neutralize
    (not hard-reject) because this resolver is shared by non-enforcing hooks
    that must keep working (fail-open decree) — a bad value simply loses its
    override, it does not abort the session.
    """
    if sid in (".", ".."):
        return ""
    return sid if _SESSION_ID_RE.fullmatch(sid) else ""


def resolve_session_id() -> str:
    """Resolve session_id used to namespace .agent/sessions/<id>/.

    Order: PLAYBOOK_SESSION_ID env (sanitized) → ancestor scan (root agent PID)
    → immediate-parent PID. The ancestor scan is the robust path: it survives
    env-propagation failures (VSCode CLAUDE_ENV_FILE quirks, missing wrappers,
    subprocess loss). Bash hooks mirror this resolver — including the
    sanitization — in gate-echo-lib.sh.
    """
    sid = _sanitize_session_id(os.environ.get("PLAYBOOK_SESSION_ID", ""))
    if sid:
        return sid
    # On Windows the ancestor scan is skipped (see find_agent_root_pid) and a
    # PID fallback would split-brain: the Python CLI sees native-Windows PIDs
    # while the bash hooks see MSYS PIDs — disjoint namespaces, so the CLI
    # would write .agent/sessions/pid-A/ and the gate hook read pid-B/,
    # silently disabling gate enforcement. Fall back to a constant shared
    # verbatim with gate-echo-lib.sh resolve_session_id so both converge.
    if sys.platform == "win32" or os.name == "nt":
        _warn_windows_session_id_once()
        return "pid-win-fallback"
    agent_pid = find_agent_root_pid()
    if agent_pid is not None:
        return f"pid-{agent_pid}"
    return f"pid-{os.getppid()}"


@functools.lru_cache(maxsize=1)
def _warn_windows_session_id_once() -> None:
    """Emit a one-time stderr warning that Windows session-id namespacing relies
    on PLAYBOOK_SESSION_ID (the ancestor process-walk can't run there)."""
    print(
        "[playbook] PLAYBOOK_SESSION_ID is not set. On Windows the session id "
        "falls back to the constant 'pid-win-fallback' shared by the Python CLI "
        "and the bash hooks, so gate enforcement still works — but sessions are "
        "not uniquely namespaced (fine for one session at a time, collides "
        "across concurrent sessions). Set env.BASH_ENV in ~/.claude/settings.json "
        "so PLAYBOOK_SESSION_ID propagates and each session gets its own id.",
        file=sys.stderr,
    )

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")


def _validate_username(name: str) -> None:
    """Raise SystemExit if name is not a safe directory component."""
    if not name or name in (".", "..") or not _USERNAME_RE.match(name) or "/" in name:
        print(
            f"Error: .agent/current_user contains invalid username {name!r}.\n"
            "Must be non-empty, start with a letter or digit, and contain only "
            "letters, digits, hyphens, underscores, and dots (no spaces or slashes).",
            file=__import__("sys").stderr,
        )
        raise SystemExit(1)


def lanes_without_marker(project_path: Path) -> list[str]:
    """Lane names present when the repo is in the "fresh clone" shape, else [].

    `.agent/current_user` is gitignored install-local, so a clone of a
    multi-user repo has lanes and no marker; `resolve_agent_dir` then answers
    the root and the next write mints a phantom lane beside the real ones.

    Narrow on purpose — a marker, an existing root `.agent/tasks/` (legitimate
    mixed layout), or no lanes at all all return []. Kept behaviorally
    identical to `require_lane_marker` in gate-echo-lib.sh and
    `lanes_without_marker` in provider/paths.py; the shared vector table in
    tests/test_provider_multiuser.py holds all three to the same answers.

    Only state-CREATING paths may refuse on this. Read paths must degrade.
    """
    agent = project_path / ".agent"
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


def require_lane_marker(project_path: Path, context: str = "playbook") -> None:
    """Exit(1) with the fix instructions when in the fresh-clone shape.

    For CLI entry points that create state. A hook must NOT call this — use
    `lanes_without_marker` and skip quietly instead of taking the session down.
    """
    lanes = lanes_without_marker(project_path)
    if not lanes:
        return
    print(
        f"Error: {context} found per-user playbook lanes but no "
        f".agent/current_user marker.\n\n"
        f"  Project: {project_path}\n"
        f"  Lane(s): {', '.join(lanes)}\n\n"
        "The marker is gitignored install-local, so a fresh clone never receives it.\n"
        "Without it every surface would fall back to the shared root .agent/, creating\n"
        "a phantom lane beside the real ones. Pick your lane first:\n\n"
        f"    echo '<your-username>' > \"{project_path}/.agent/current_user\"\n",
        file=sys.stderr,
    )
    raise SystemExit(1)


def resolve_agent_dir(project_path: Path) -> Path:
    """Return the agent state root for this project.

    Multi-user mode: .agent/current_user exists → .agent/<username>/
    Legacy mode:     .agent/current_user absent  → .agent/  (unchanged)
    Invalid content: print error and exit(1).
    """
    # str accepted — this is THE path chokepoint every helper funnels through,
    # and a str caller crashed scan_parked live in the 1.5.3 gauntlet (same
    # str/Path disease fixed in audit at F7a).
    project_path = Path(project_path)
    marker = project_path / ".agent" / "current_user"
    if not marker.exists():
        return project_path / ".agent"
    name = marker.read_text(encoding="utf-8", errors="replace").strip()
    _validate_username(name)
    return project_path / ".agent" / name


def canonical_path(path: "Path | str") -> str:
    """Canonical cross-language string form of a path: forward-slash separators.

    The seam this closes: under MSYS/Git Bash the shell hooks speak POSIX-mount
    paths (forward slashes), while this CLI runs as a native Windows process
    where ``str(Path(...))`` yields backslashes (``C:\\Users\\…``). A shell
    writer and a Python reader then name the same directory with two different
    strings, and any cross-half string comparison disagrees.

    ``as_posix()`` is the Python half of the fix: it renders the native path
    with forward slashes (``C:/Users/…``), meeting the shell's ``cygpath -m``
    output (see ``_canonical_path`` in scripts/gate-echo-lib.sh). On POSIX this
    is identical to ``str()`` for an absolute path, so Linux and macOS behaviour
    is unchanged. Use ONLY at the boundary where a path crosses to/from the
    shell — never to build paths for I/O, which must stay native ``Path``.
    """
    return Path(path).as_posix()


# ── Configuration (.agent/config.json) ──────────────────────────────────────
# Read at the .agent/ ROOT (not the per-user subdir — these are shared across
# users in a multi-user repo). Precedence for every setting: CLI flag > env var >
# config.json > built-in default. A missing file, malformed JSON, or an
# out-of-range value never crashes the CLI — it falls back to the default
# (warning once).
#
# Two tiers of ownership live in this one file:
#   • Review knobs (judge_budget_usd, review_timeout_secs) are naturally
#     per-install — a spend cap is a wallet decision, and a timeout depends on
#     the machine. Committing them just sets a default others can override via
#     the PLAYBOOK_* env tier.
#   • Project policy (merge_verify) only works when it IS committed: the merge
#     skill runs the declared command to decide whether a merge may auto-push,
#     so every clone must see the same declaration. A repo that keeps this file
#     untracked leaves that check permanently skipped.
# Hence the file is committable, and merge-doctor treats a tracked copy as
# correct rather than legacy detritus (SHARED_POLICY_PATHS below).

DEFAULT_JUDGE_BUDGET_USD = "10"
# The HARD kill: hang safety only, and the ceiling on a judge subprocess. Sized
# as the soft deadline plus ~5 minutes of grace, so a judge that is winding down
# on schedule is never cut off mid-sentence. Raised from upstream's 300s, which
# was smaller than the soft default below and so guaranteed a clamp on every
# review — a wind-down deadline with no room to wind down in.
DEFAULT_REVIEW_TIMEOUT_SECS = 1200
# Soft target the judge self-regulates against. The hard kill is separate — see
# resolve_review_soft_timeout for why the two are not one number.
# INVARIANT: keep this BELOW DEFAULT_REVIEW_TIMEOUT_SECS. If it is ever raised
# above, resolve_review_soft_timeout clamps it down and warns on every single
# review. tests/test_config_resolve.py asserts the ordering.
DEFAULT_REVIEW_SOFT_TIMEOUT_SECS = 900
# Sentinel printed in banners / judge.md when a timeout is unlimited.
UNLIMITED_TIMEOUT_LABEL = "unlimited"

# Paths under .agent/ that are legitimately tracked in git — repo-level policy
# rather than per-install state. Everything else at the .agent/ root that is
# tracked is legacy detritus the merge skill wants `git rm --cached`ed.
SHARED_POLICY_PATHS = frozenset({".agent/config.json"})


def load_config(project_path: Path) -> dict:
    """Return the parsed .agent/config.json (install root), or {} if absent or
    unparseable. Never raises — config is advisory, not load-bearing."""
    cfg = project_path / ".agent" / "config.json"
    if not cfg.exists():
        return {}
    try:
        data = json.loads(cfg.read_text(encoding="utf-8", errors="replace"))
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


@functools.lru_cache(maxsize=None)
def _warn_bad_config_value_once(source: str, raw: str) -> None:
    print(
        f"[playbook] review setting from {source}={raw!r} is not valid — "
        "using the built-in default instead.",
        file=sys.stderr,
    )


def _first_valid(tiers, parse, default):
    """Walk precedence tiers (highest first). `tiers` is an iterable of
    (raw_value_or_None, source_label). Return `parse(str(raw))` for the first
    tier whose value is present AND parses; a present-but-malformed value at any
    tier warns once and falls through to the next. Return `default` if none
    parse. Never raises — review config is advisory."""
    for raw, source in tiers:
        if raw is None:
            continue
        raw = str(raw)
        try:
            return parse(raw)
        except (TypeError, ValueError):
            _warn_bad_config_value_once(source, raw)
    return default


def _parse_budget(raw: str) -> str:
    # Reject negative and non-finite (nan/inf) — a bogus --max-budget-usd nan
    # would otherwise reach the claude judge. Keep the original string for argv.
    value = float(raw)
    if not math.isfinite(value) or value < 0:
        raise ValueError(raw)
    return raw


# A verify command with no ceiling can hang `tasks work done` forever — in
# headless use that is a silent deadlock, and a guard that can hang is a guard
# that gets bypassed. Same _parse_timeout grammar as the review knobs, so
# 0/"none"/"unlimited" opts back into no ceiling deliberately.
DEFAULT_VERIFY_TIMEOUT_SECS = 1200


def resolve_verify_timeout(project_path: Path, cli_value: "str | None" = None) -> "int | None":
    """Hard ceiling for ONE declared verify command at close (seconds; None =
    unlimited). Precedence: cli > PLAYBOOK_VERIFY_TIMEOUT_SECS env > config.json
    verify_timeout_secs > DEFAULT_VERIFY_TIMEOUT_SECS."""
    return _first_valid(
        (
            (cli_value, "--verify-timeout"),
            (os.environ.get("PLAYBOOK_VERIFY_TIMEOUT_SECS"), "PLAYBOOK_VERIFY_TIMEOUT_SECS"),
            (load_config(project_path).get("verify_timeout_secs"), "config.json verify_timeout_secs"),
        ),
        _parse_timeout,
        DEFAULT_VERIFY_TIMEOUT_SECS,
    )


# Context budgets are transport-relative (1.5.3): stdin seats (claude/codex)
# have no OS argv limit — their ceiling is model context and attention, so they
# get a HIGH budget; argv seats (grok/agy/pi) stay under the byte-guarded
# default. Raising a ceiling is honest only alongside the receipts that report
# what was actually delivered per seat.
DEFAULT_REVIEW_CONTEXT_CHARS = 100_000
DEFAULT_REVIEW_CONTEXT_CHARS_STDIN = 200_000


def _parse_context_chars(raw) -> int:
    if isinstance(raw, bool):
        raise ValueError(raw)
    value = int(str(raw).strip())
    if value < 10_000:
        # Below ~10k the payload can't even hold orientation — a config typo,
        # not a choice.
        raise ValueError(raw)
    return value


def resolve_review_context_chars(project_path: Path, stdin: bool = False) -> int:
    """Per-transport judge context budget (chars). Precedence: env > config >
    default. Keys: review_context_chars / review_context_chars_stdin."""
    key = "review_context_chars_stdin" if stdin else "review_context_chars"
    env = "PLAYBOOK_REVIEW_CONTEXT_CHARS_STDIN" if stdin else "PLAYBOOK_REVIEW_CONTEXT_CHARS"
    default = DEFAULT_REVIEW_CONTEXT_CHARS_STDIN if stdin else DEFAULT_REVIEW_CONTEXT_CHARS
    return _first_valid(
        (
            (os.environ.get(env), env),
            (load_config(project_path).get(key), f"config.json {key}"),
        ),
        _parse_context_chars,
        default,
    )


# A panel that reports "N/M succeeded" and exits 0 is a report, not a gate: a
# 1/7 panel and a 7/7 panel are the same exit code (C4/P7). resolve_panel_quorum
# turns that count into a verdict. Default is strict majority of the judges that
# actually launched — a defensible floor that flags the real incident (a panel
# shipping at 4/7 because three seats were silently 401) without demanding a
# perfect run. Projects tighten or loosen it with `panel_quorum` in config.json.
DEFAULT_PANEL_QUORUM = "majority"


def _parse_quorum(raw, launched: int) -> int:
    """Parse a panel_quorum value into a required-success count. Accepts:
      * "majority" → floor(launched/2)+1 (strict majority);
      * "all"      → every launched judge;
      * an int ≥ 1 → that many judges (NOT clamped up: requiring more than
        launched is a legitimate way to say "a full panel was expected", and it
        correctly fails a degraded run rather than hiding the shortfall);
      * a float in (0, 1] → that fraction of launched, rounded up.
    Raises ValueError on anything else (bools, ≤0, out-of-range fractions)."""
    if isinstance(raw, bool):
        raise ValueError(raw)
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s == "majority":
            return launched // 2 + 1
        if s == "all":
            return launched
        raw = float(s) if ("." in s or "e" in s) else int(s)
    if isinstance(raw, float):
        if not math.isfinite(raw) or not (0.0 < raw <= 1.0):
            raise ValueError(raw)
        return max(1, min(launched, math.ceil(raw * launched)))
    if isinstance(raw, int):
        if raw < 1:
            raise ValueError(raw)
        return raw
    raise ValueError(raw)


def resolve_panel_quorum(project_path: Path, launched: int) -> int:
    """Minimum succeeding judges for a panel PASS. Precedence:
    PLAYBOOK_PANEL_QUORUM env > config.json panel_quorum > DEFAULT_PANEL_QUORUM.
    Never raises — a bad value warns once and falls back to strict majority."""
    launched = max(1, int(launched))
    for raw, source in (
        (os.environ.get("PLAYBOOK_PANEL_QUORUM"), "PLAYBOOK_PANEL_QUORUM"),
        (load_config(project_path).get("panel_quorum"), "config.json panel_quorum"),
    ):
        if raw is None:
            continue
        try:
            return _parse_quorum(raw, launched)
        except (TypeError, ValueError):
            _warn_bad_config_value_once(source, str(raw))
    return _parse_quorum(DEFAULT_PANEL_QUORUM, launched)


def _parse_timeout(raw) -> "int | None":
    """Parse a timeout value. Returns seconds, or None for unlimited.

    Unlimited forms — a judge must be able to finish a full response rather than
    be killed mid-sentence: 0, "0", "none", "null", "unlimited", "inf",
    "infinite". Positive integers are finite seconds. Everything else raises.

    Accepts non-str input because `tasks doctor` validates the raw JSON value
    while `_first_valid` stringifies before parsing. The two MUST agree on what
    is valid, so floats are rejected outright rather than truncated: the runtime
    sees `str(1.5)` / `str(600.0)` and `int()` raises, so doctor must reject
    `review_timeout_secs: 1.5` and `600.0` too. Truncating instead would have
    doctor report 1.5 as a clean 1s while the runtime silently used the default.
    """
    if raw is None:
        raise ValueError(raw)
    if isinstance(raw, bool):
        # bool subclasses int — True as a timeout is a config mistake, not 1s.
        raise ValueError(raw)
    if isinstance(raw, float):
        # +inf is the numeric spelling of "unlimited" (json.loads parses a bare
        # Infinity), and str(inf) == "inf" already means unlimited below — so
        # accept it here or doctor and runtime disagree. Every other float is
        # rejected; see docstring.
        if math.isinf(raw) and raw > 0:
            return None
        raise ValueError(raw)
    if isinstance(raw, int):
        if raw == 0:
            return None
        if raw < 0:
            raise ValueError(raw)
        return raw
    s = str(raw).strip().lower()
    if s in ("none", "null", "unlimited", "inf", "infinite"):
        return None
    secs = int(s)   # raises on "1.5", "banana", "" — falls through to default
    if secs < 0:
        raise ValueError(raw)
    if secs == 0:
        return None
    return secs


def format_timeout_label(timeout_secs: "int | None") -> str:
    """Human label for banners / judge.md (`1800s` or `unlimited`)."""
    return UNLIMITED_TIMEOUT_LABEL if timeout_secs is None else f"{timeout_secs}s"


def format_soft_hard_timeout_label(
    soft_secs: "int | None", hard_secs: "int | None"
) -> str:
    """Banner / judge.md label: `soft 900s / hard 1200s` (or unlimited)."""
    return (
        f"soft {format_timeout_label(soft_secs)} / "
        f"hard {format_timeout_label(hard_secs)}"
    )


def human_duration(secs: int) -> str:
    """Readable duration for judge prompts (`15 minutes`, `90s`)."""
    if secs >= 60 and secs % 60 == 0:
        minutes = secs // 60
        unit = "minute" if minutes == 1 else "minutes"
        return f"{minutes} {unit}"
    return f"{secs}s"


def resolve_judge_budget(project_path: Path, cli_value: str | None = None) -> str:
    """Resolve the claude judge --max-budget-usd value (USD). Precedence:
    cli_value (`--budget`) > PLAYBOOK_JUDGE_BUDGET_USD env > config.json
    judge_budget_usd > DEFAULT_JUDGE_BUDGET_USD. Returned as a str for direct
    argv use. A negative,
    non-finite, or non-numeric value at ANY tier warns and falls through.
    (claude-only; codex/agy/pi have no budget knob.)"""
    return _first_valid(
        (
            (cli_value, "--budget"),
            (os.environ.get("PLAYBOOK_JUDGE_BUDGET_USD"), "PLAYBOOK_JUDGE_BUDGET_USD"),
            (load_config(project_path).get("judge_budget_usd"), "config.json judge_budget_usd"),
        ),
        _parse_budget,
        DEFAULT_JUDGE_BUDGET_USD,
    )


def resolve_review_timeout(
    project_path: Path, cli_value: "str | int | None" = None
) -> "int | None":
    """Resolve the HARD review subprocess timeout in seconds, or None for
    unlimited (no wall-clock kill). Precedence: cli_value (`--timeout`) >
    PLAYBOOK_REVIEW_TIMEOUT_SECS env > config.json review_timeout_secs >
    DEFAULT_REVIEW_TIMEOUT_SECS.
    A malformed value at ANY tier warns and falls through.

    This is the hang-safety kill only. The soft deadline a judge self-regulates
    against is `resolve_review_soft_timeout`. After resolution the config floor
    applies — see `floor_review_timeout`."""
    resolved = _first_valid(
        (
            (cli_value, "--timeout"),
            (os.environ.get("PLAYBOOK_REVIEW_TIMEOUT_SECS"), "PLAYBOOK_REVIEW_TIMEOUT_SECS"),
            (load_config(project_path).get("review_timeout_secs"), "config.json review_timeout_secs"),
        ),
        _parse_timeout,
        DEFAULT_REVIEW_TIMEOUT_SECS,
    )
    return floor_review_timeout(project_path, resolved)


# Returned by config_review_timeout_floor when config sets no timeout at all —
# distinct from None, which means "config set it to unlimited". Without this the
# built-in 300s default would act as a floor on installs that never opted into
# one, making `--timeout 60` impossible and silently contradicting the documented
# "--flag > env > config" precedence.
_NO_FLOOR = object()


def config_review_timeout_floor(project_path: Path):
    """Config-only hard-timeout floor, ignoring the CLI and env tiers.

    Returns seconds (finite floor), None (config says unlimited), or `_NO_FLOOR`
    when config does not set `review_timeout_secs` — a floor is something an
    install opts into, so an absent setting must not impose one."""
    raw = load_config(project_path).get("review_timeout_secs")
    if raw is None:
        return _NO_FLOOR
    try:
        return _parse_timeout(str(raw))
    except (TypeError, ValueError):
        # Malformed config can't define a floor; the warning already came from
        # the resolve path's _first_valid walk.
        return _NO_FLOOR


def floor_review_timeout(
    project_path: Path, resolved: "int | None"
) -> "int | None":
    """Apply the config reliability floor to an already-resolved HARD timeout.

    The point: an agent passing `--timeout 600` must not be able to re-introduce
    a kill window that the install's config deliberately removed. So config is a
    floor, not just another tier — CLI and env may raise the ceiling, never lower
    it.

    - Config sets no timeout: no floor at all, ordinary precedence applies.
    - Config unlimited (None): always unlimited.
    - Resolved unlimited (None): stays unlimited — always "above" any floor.
    - Finite resolved below a finite floor: raised to the floor, with a warning.
    """
    floor = config_review_timeout_floor(project_path)
    if floor is _NO_FLOOR:
        return resolved
    if floor is None:
        if resolved is not None:
            print(
                f"[playbook] hard review timeout is unlimited by config — "
                f"ignoring the finite {resolved}s from --timeout/env.",
                file=sys.stderr,
                flush=True,
            )
        return None
    if resolved is None:
        return None
    if resolved < floor:
        print(
            f"[playbook] hard review timeout {resolved}s is below config floor "
            f"{floor}s — using {floor}s (pass --timeout only to go ABOVE the "
            f"floor; the soft deadline is separate: review_soft_timeout_secs).",
            file=sys.stderr,
            flush=True,
        )
        return floor
    return resolved


# Distinguishes "caller did not pass a hard timeout" from "hard timeout is
# unlimited" — both of which would otherwise be None. Without it, resolving soft
# against an unlimited hard would silently re-resolve hard from config and clamp
# the soft deadline to a kill window that is not in force.
_HARD_NOT_GIVEN = object()


def resolve_review_soft_timeout(
    project_path: Path,
    hard_timeout_secs: "int | None" = _HARD_NOT_GIVEN,
    cli_value: "str | int | None" = None,
) -> "int | None":
    """Resolve the SOFT review deadline in seconds — the number the judge is
    told to self-regulate against. Precedence: cli_value (`--soft-timeout`) >
    PLAYBOOK_REVIEW_SOFT_TIMEOUT_SECS env > config.json review_soft_timeout_secs
    > DEFAULT_REVIEW_SOFT_TIMEOUT_SECS.

    None (0/unlimited) means "no soft instruction" — judges get no wind-down
    paragraph at all. When both soft and hard are finite and soft > hard, soft is
    clamped to hard with a warning, so the prompt never promises a judge more
    time than its process will live.

    Soft = when to finish the current thought and write findings. Hard = process
    kill, hang safety only.
    """
    soft = _first_valid(
        (
            (cli_value, "--soft-timeout"),
            (os.environ.get("PLAYBOOK_REVIEW_SOFT_TIMEOUT_SECS"),
             "PLAYBOOK_REVIEW_SOFT_TIMEOUT_SECS"),
            (load_config(project_path).get("review_soft_timeout_secs"),
             "config.json review_soft_timeout_secs"),
        ),
        _parse_timeout,
        DEFAULT_REVIEW_SOFT_TIMEOUT_SECS,
    )
    if soft is None:
        return None
    if hard_timeout_secs is _HARD_NOT_GIVEN:
        # Caller didn't resolve hard yet. Resolve it without CLI so soft clamps
        # against the install floor rather than a transient undercut.
        hard_timeout_secs = resolve_review_timeout(project_path, None)
    if hard_timeout_secs is not None and soft > hard_timeout_secs:
        print(
            f"[playbook] soft timeout {soft}s exceeds hard {hard_timeout_secs}s "
            f"— clamping soft to hard so the prompt matches process life.",
            file=sys.stderr,
            flush=True,
        )
        return hard_timeout_secs
    return soft


# ── Multi-user lane discovery ─────────────────────────────────────────────────
# Lived in global_retro_collect.py until that command was removed from this fork
# (projects are islands here — nothing collects across them). doctor still needs
# lane enumeration, so the helper moved to core rather than dying with its old
# host.

# Reserved `.agent/` children that are never user lanes.
_RESERVED_AGENT_DIRS = {"tasks", "sessions", "monitor", "playbooks"}


def _agent_lanes(project: Path) -> "list[tuple[str | None, Path]]":
    """Return the task-bearing lanes of a project, each as (user, agent_reldir).

    A lane is a directory holding a `tasks/` subdir:
      - the root lane          → (None, Path('.agent'))
      - a per-user lane [30]   → ('<user>', Path('.agent/<user>'))

    Reserved `.agent/` children (tasks, sessions, monitor, playbooks) are never
    treated as user lanes. Root single-user repos yield exactly [(None, .agent)].
    """
    agent = project / ".agent"
    lanes: "list[tuple[str | None, Path]]" = []
    if (agent / "tasks").is_dir():
        lanes.append((None, Path(".agent")))
    if agent.is_dir():
        for child in sorted(agent.iterdir(), key=lambda p: p.name):
            if (child.is_dir() and child.name not in _RESERVED_AGENT_DIRS
                    and (child / "tasks").is_dir()):
                lanes.append((child.name, Path(".agent") / child.name))
    return lanes


# ── Context selection for reviews (structure-aware, receipted) ───────────────
# A task.md is append-ordered: Intent and Design sit at the top, the CURRENT
# round's fixes at the very bottom. A naive head-slice (content[:budget]) keeps
# the orientation and throws away exactly the evidence a review needs — a judge
# then reviews the design four times and never sees a single fix (C3). Two rules
# fix this: keep BOTH ends (orientation sections always, then the MOST RECENT
# sections that fit), and NEVER truncate silently — return a receipt naming what
# was dropped so the operator can compensate. Pure + unit-tested.

# Sections that orient a reviewer regardless of age. Matched against the heading
# text (already stripped of leading '#'). Handoff is here so a resume note is
# never the thing that gets dropped (P12 depends on this).
_ORIENTATION_HEADING = re.compile(r'^(intent|design|handoff)\b', re.IGNORECASE)


def split_md_sections(text: str) -> "list[tuple[str | None, str]]":
    """Split markdown into (heading, chunk) at level-1/2 headings ('# ', '## ').

    The chunk includes its own heading line. Content before the first heading is
    a leading chunk with heading None. Level-3+ headings stay inside their parent
    section — task.md's structure is level-2 sections with level-3 subsections."""
    sections: "list[tuple[str | None, str]]" = []
    cur_heading: "str | None" = None
    cur: list[str] = []
    started = False
    for ln in text.splitlines(keepends=True):
        if re.match(r'^#{1,2}\s', ln):
            if started:
                sections.append((cur_heading, "".join(cur)))
            cur_heading = ln.lstrip('#').strip()
            cur = [ln]
            started = True
        else:
            cur.append(ln)
            started = True
    if started:
        sections.append((cur_heading, "".join(cur)))
    return sections


def select_task_context(text: str, budget: int) -> "tuple[str, str]":
    """Fit a task.md into `budget` chars, keeping both ends, and describe the cut.

    Returns (selected_text, receipt). The receipt is '' when nothing was dropped;
    otherwise it is a single human line naming the kept and dropped sections, e.g.
    'task.md 169,036 → 49,210 chars · kept: Intent, Design, Debrief · dropped:
    Work Plan, Plan Review'. Elisions are also marked inline in selected_text so a
    reader of the payload itself sees the gap.

    Selection: (1) always keep any leading preamble and the orientation sections
    (Intent / Design / Handoff); (2) then fill from the MOST RECENT section
    backwards until the budget is spent. Order is preserved on output."""
    if len(text) <= budget:
        return text, ""

    sections = split_md_sections(text)
    n = len(sections)
    keep = [False] * n
    used = 0

    def _pinned(chunk: str) -> bool:
        # `<!-- pin -->` on its own line within the first 3 lines UNDER the
        # heading marks an author-pinned section: the selection heuristic can't
        # know which old section is load-bearing this time, so the author says
        # so. The marker deliberately lives BELOW the heading, never inside it —
        # heading text is parsed exactly by receipts/evidence/selection, and a
        # decorated heading would break that family (the 1.5.1 accretion bug's
        # shape).
        for ln in chunk.splitlines()[1:4]:
            if ln.strip() == "<!-- pin -->":
                return True
        return False

    for i, (heading, chunk) in enumerate(sections):
        if heading is None or _ORIENTATION_HEADING.match(heading) or _pinned(chunk):
            keep[i] = True
            used += len(chunk)

    for i in range(n - 1, -1, -1):
        if keep[i]:
            continue
        if used + len(sections[i][1]) <= budget:
            keep[i] = True
            used += len(sections[i][1])

    out: list[str] = []
    dropped: list[str] = []
    run = 0
    for i, (heading, chunk) in enumerate(sections):
        name = heading if heading else "(preamble)"
        if keep[i]:
            if run:
                out.append(
                    f"\n\n[... {run} section(s) elided to fit context budget ...]\n\n"
                )
                run = 0
            out.append(chunk)
        else:
            dropped.append(name)
            run += 1
    if run:
        out.append(f"\n\n[... {run} section(s) elided to fit context budget ...]\n\n")

    selected = "".join(out)
    # Safety floor: orientation alone can exceed the budget. Keep it (orientation
    # wins) but bound the payload so it can never overflow an argv limit, and say
    # so in the receipt — silence is the one thing forbidden here.
    overflowed = False
    if len(selected) > budget:
        selected = selected[:budget] + "\n\n[... hard-truncated: orientation exceeded budget ...]"
        overflowed = True

    kept_names = [
        (sections[i][0] or "(preamble)") for i in range(n) if keep[i]
    ]
    receipt = (
        f"task.md {len(text):,} → {len(selected):,} chars"
        f" · kept: {', '.join(kept_names) or '(none)'}"
        f" · dropped: {', '.join(dropped) or '(none)'}"
    )
    if overflowed:
        receipt += (" · WARNING: orientation+pinned sections exceed the budget, "
                    "hard-truncated — unpin something or raise the budget")
    return selected, receipt


# ── Consequence classification + evidence contract at close (P1 / P2) ────────
# The playbook had no concept of EVIDENCE (close wrote the string "done" and
# checked nothing) and no concept of CONSEQUENCE (review depth followed diff
# shape, so a docs-only diff that upgraded "uncalibrated" to "audited accurate"
# got a light close). These helpers add both, as pure/testable policy.

def append_standing_gates(content: str, cfg: dict, task_num: int) -> "tuple[str, list[str]]":
    """Append project-declared standing gates as the FINAL gates of a task.

    `standing_gates` in .agent/config.json: a list of {title, text} objects.
    Field evidence (F8, StrataDB batches 2/2b): the project's journal gate was
    hand-relocated below Pre-review VERBATIM on consecutive tasks — a gate a
    project wants on EVERY task should come from generation, not from the
    agent remembering to re-add it. Opt-in: key absent/empty → content
    returned byte-identical.

    Each valid entry becomes `## <title>` + one `- [ ] <text>` gate, appended
    at the very END of the assembled content (after any custom playbook
    append) in declared order — the last gates of the document. `{{NNN}}` in
    title/text substitutes the zero-padded task number (the journal use case:
    `journal/{{NNN}}.md`).

    Returns (content, issues). Malformed entries and titles colliding with an
    existing section heading are SKIPPED and named in `issues` (callers print
    them) — never silently written, never silently dropped. Title and text are
    whitespace-collapsed to one line, so a config value cannot open a new
    heading line or gate line of its own (the #09 multi-heading disease, via
    config).
    """
    raw = cfg.get("standing_gates")
    if raw is None:
        return content, []
    if not isinstance(raw, list):
        return content, [
            f"standing_gates must be a list of {{title, text}} objects, "
            f"got {type(raw).__name__} — ignored"]
    issues: "list[str]" = []
    nnn = f"{int(task_num):03d}"
    existing = {ln.strip()[3:].strip().lower()
                for ln in content.splitlines() if ln.strip().startswith("## ")}
    blocks: "list[str]" = []
    seen: "set[str]" = set()
    for i, entry in enumerate(raw):
        label = f"standing_gates[{i}]"
        if not isinstance(entry, dict):
            issues.append(f"{label}: not a {{title, text}} object — skipped")
            continue
        title = " ".join(str(entry.get("title", "")).replace("{{NNN}}", nnn).split())
        title = title.lstrip("#").strip()
        text = " ".join(str(entry.get("text", "")).replace("{{NNN}}", nnn).split())
        if not title or not text:
            issues.append(f"{label}: needs non-empty 'title' and 'text' — skipped")
            continue
        if title.lower() in existing or title.lower() in seen:
            issues.append(
                f"{label}: title {title!r} collides with an existing section "
                "— skipped (a duplicate heading breaks the receipt/evidence parsers)")
            continue
        seen.add(title.lower())
        blocks.append(f"## {title}\n- [ ] {text}")
    if not blocks:
        return content, issues
    return content.rstrip("\n") + "\n\n" + "\n\n".join(blocks) + "\n", issues


RISK_CLASSES = ("reversible", "irreversible", "assertive")
# assertive = changes a CLAIM about the world (docs, a calibration, a measurement,
# a "verified accurate"). irreversible = deletes/migrates data, rotates a secret,
# rewrites history, or publishes. Both are high-consequence: they cannot be
# light-closed for being small.
HIGH_CONSEQUENCE = frozenset({"irreversible", "assertive"})
DEFAULT_RISK = "unclassified"


def _as_command_list(value) -> "list[str]":
    """Coerce a verify-contract value (str | list) into a clean command list."""
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [s for s in value if isinstance(s, str) and s.strip()]
    return []


_RISK_HEADING_RE = re.compile(
    r"^##[ \t]+Risk[ \t]*(?::.*)?$", re.IGNORECASE
)
_RISK_FIELD_RE = re.compile(r"^##[ \t]+Risk[ \t]*$", re.IGNORECASE)


def _risk_heading_lines(lines: "list[str]") -> "list[tuple[int, str]]":
    """Risk headings outside Markdown fences as ``(index, stripped_line)``.

    A task may quote the template in a fenced review/example.  Such text is not
    metadata.  BOMs are ignored at line starts (not just at file start), and
    heading case/tabs follow Markdown's ordinary permissiveness.  Duplicates are
    returned deliberately: callers treat more than one field as malformed
    rather than letting an attacker choose which duplicate wins.
    """
    found: "list[tuple[int, str]]" = []
    fence_char = ""
    fence_len = 0
    for i, line in enumerate(lines):
        stripped = line.strip().lstrip("\ufeff")
        fm = re.match(r"^(`{3,}|~{3,})", stripped)
        if fence_char:
            if (fm and fm.group(1)[0] == fence_char
                    and len(fm.group(1)) >= fence_len):
                fence_char = ""
                fence_len = 0
            continue
        if fm:
            fence_char = fm.group(1)[0]
            fence_len = len(fm.group(1))
            continue
        if _RISK_HEADING_RE.match(stripped):
            found.append((i, stripped))
    return found


def extract_risk(task_file) -> str:
    """Read the `## Risk` classification from a task.md — the token on the line
    after the heading. Returns one of RISK_CLASSES, or 'unclassified' if the
    section is absent, blank, or holds an unrecognized value."""
    try:
        lines = Path(task_file).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return DEFAULT_RISK
    headings = _risk_heading_lines(lines)
    if len(headings) != 1:
        return DEFAULT_RISK
    i, heading = headings[0]
    # A one-line heading (`## Risk: assertive`) proves the gate was offered but
    # is not the field shape, so it stays unclassified and strict at close.
    if not _RISK_FIELD_RE.match(heading) or i + 1 >= len(lines):
        return DEFAULT_RISK
    raw = lines[i + 1].strip().strip("`*_").lower()
    token = raw.split()[0] if raw else ""
    if token in RISK_CLASSES:
        return token
    return DEFAULT_RISK


def has_risk_section(task_file) -> bool:
    """True when the task.md carries a `## Risk` HEADING at all.

    This is the discriminator that lets `unclassified` fail closed without
    breaking history, and it needs no new metadata: pre-1.5.0 templates have no
    Risk section, so a missing heading means the gate was never offered to
    whoever wrote the task, while a present-but-unset heading means it was
    offered and skipped. Those are different facts and the close gate treats
    them differently (see close_decision).

    Deliberately LOOSER than extract_risk's exact `## Risk` match: any `## Risk…`
    heading counts as offered, so the malformed one-liner `## Risk: assertive`
    — which extract_risk correctly degrades to `unclassified` — lands on the
    strict side rather than being mistaken for a pre-1.5.0 task. A skipped gate
    and a botched gate are the same fact for this purpose. Erring toward strict
    costs one word or a `--force --reason`; erring the other way is the fail-open
    this function exists to remove.

    Heading-only by design, and only THIS heading: prose that mentions risk must
    not silently promote a task to the strict bar, `### Risk` is a different
    level, and `## Risk Routing` is the light template's gate checklist rather
    than the classification field. Unreadable file → False: fail toward the
    documented legacy behavior, never toward inventing a block from an I/O error.
    """
    try:
        lines = Path(task_file).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    return bool(_risk_heading_lines(lines))


def tree_state_fingerprint(project_path: Path) -> str:
    """Content fingerprint of the CODE STATE: sha256 over HEAD + porcelain status
    + working diff, 12 hex chars. Names *what state* a panel reviewed or a close
    certified — deterministic, unlike mtimes, and sensitive to uncommitted work
    (which is the normal state at review time). Empty string when git is absent:
    no fingerprint beats a fabricated one."""
    import hashlib
    # `.agent/` is EXCLUDED: triaging findings edits task.md between the panel
    # and the close by design — the fingerprint must answer "did the CODE
    # change?", not "did the workflow record change?" (else the advisory fires
    # on every single close and gets ignored). Projects can extend the
    # exclusion for owner-declared bookkeeping (`fingerprint_exclude` in
    # .agent/config.json, git pathspec strings — e.g. "journal/"): a standing
    # journal gate writes project-side files after the last panel by
    # construction, and F18's irreversible gate must not tax that.
    exclude = [":(exclude).agent"]
    try:
        _cfg = load_config(Path(project_path))
    except Exception:
        _cfg = {}
    _raw_ex = _cfg.get("fingerprint_exclude")
    if _raw_ex is not None:
        if not isinstance(_raw_ex, list):
            print("[playbook] fingerprint_exclude: must be a list of git "
                  "pathspec strings — ignored", file=sys.stderr)
        else:
            for _i, _p in enumerate(_raw_ex):
                if isinstance(_p, str) and _p.strip() and "\x00" not in _p:
                    exclude.append(f":(exclude){_p.strip()}")
                else:
                    print(f"[playbook] fingerprint_exclude[{_i}]: needs a "
                          "non-empty pathspec string — skipped", file=sys.stderr)
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project_path,
                              capture_output=True, text=True).stdout.strip()
        if not head:
            return ""
        # -uall enumerates untracked files INDIVIDUALLY (a bare `?? dir/`
        # hides everything inside the directory from the hash below).
        porcelain = subprocess.run(
            ["git", "status", "--porcelain", "-uall", "--", ".", *exclude],
            cwd=project_path, capture_output=True, text=True).stdout
        diff = subprocess.run(
            ["git", "diff", "HEAD", "--", ".", *exclude],
            cwd=project_path, capture_output=True, text=True).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    # Untracked CONTENT is hashed explicitly (F18 judge C1, verified
    # empirically): porcelain names an untracked file but never its bytes,
    # and `diff HEAD` covers tracked paths only — so the pre-1.5.6
    # fingerprint was blind to edits inside new files, which is exactly
    # where post-panel fixes land (batches 4 and 5 both did). NOTE: this
    # changes fingerprint values across the 1.5.5→1.5.6 upgrade; a stamp
    # from an older round reads STALE once and self-heals at the next panel.
    untracked_digest = hashlib.sha256()
    for line in sorted(porcelain.splitlines()):
        if not line.startswith("?? "):
            continue
        rel = line[3:].strip().strip('"')
        try:
            content = (Path(project_path) / rel).read_bytes()
            fhash = hashlib.sha256(content).hexdigest()
        except OSError:
            fhash = "unreadable"
        untracked_digest.update(f"{rel}\0{fhash}\n".encode("utf-8", "replace"))
    return hashlib.sha256(
        (head + porcelain + diff + untracked_digest.hexdigest())
        .encode("utf-8", "replace")).hexdigest()[:12]


# One round per `# Panel {Plan|Impl} Review` H1. judge.md stacks rounds NEWEST
# FIRST (stack_judge_round), so rounds[0] is the round that decides anything.
_ROUND_HEAD_RE = re.compile(r"^# Panel (Plan|Impl) Review\b", re.MULTILINE)
_ROUND_VERDICT_RE = re.compile(r"\*\*PANEL VERDICT: (PASS|FAIL)\*\*")
_ROUND_TREE_RE = re.compile(r"\*\*Tree-state:\*\* ([0-9a-f]{6,64})")
JUDGE_MD_MAX_ROUNDS = 5


def parse_judge_rounds(text: str) -> "list[dict]":
    """Structural parse of judge.md into rounds, file order (newest first under
    the stacking convention). Each round: {mode: 'plan'|'impl', verdict:
    'PASS'|'FAIL'|None, tree_state: str, body: str}. Substring checks over the
    whole file are how a stale or wrong-mode PASS satisfies a gate — the #09
    disease; this parser exists so verdicts are read per-round, never per-file."""
    matches = list(_ROUND_HEAD_RE.finditer(text))
    rounds: "list[dict]" = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.start():end]
        vm = _ROUND_VERDICT_RE.search(body)
        tm = _ROUND_TREE_RE.search(body)
        rounds.append({
            "mode": m.group(1).lower(),
            "verdict": vm.group(1) if vm else None,
            "tree_state": tm.group(1) if tm else "",
            "body": body,
        })
    return rounds


def stack_judge_round(judge_md: Path, round_text: str,
                      max_rounds: int = JUDGE_MD_MAX_ROUNDS) -> None:
    """Prepend a panel round to judge.md, newest first — a re-run must never
    clobber the previous round's verdicts (they are paid work and the record).
    Retention keeps the newest `max_rounds`; older rounds live on in git history,
    and the trim is announced in the file rather than silent."""
    old_bodies: "list[str]" = []
    if judge_md.exists():
        try:
            old_text = judge_md.read_text(encoding="utf-8", errors="replace")
            old_rounds = parse_judge_rounds(old_text)
            if old_rounds:
                old_bodies = [r["body"].rstrip() for r in old_rounds]
            elif old_text.strip():
                # Legacy / taskless content that predates round headings: keep it
                # as one opaque block — stacking must never silently destroy a
                # prior record it merely cannot parse.
                old_bodies = [old_text.strip()]
        except OSError:
            old_bodies = []
    kept = [round_text.rstrip()] + old_bodies
    trimmed = len(kept) - max_rounds
    kept = kept[:max_rounds]
    out = "\n\n".join(kept) + "\n"
    if trimmed > 0:
        out += (f"\n[... {trimmed} older round(s) trimmed — the full history is "
                "in git ...]\n")
    _atomic_write(judge_md, out)


def resolve_panel_required(project_path: Path, risk: str) -> bool:
    """Owner policy: does a close of a task with this risk class demand
    PANEL-grade review evidence (all available judges), not just a single judge?

    config.json `panel_required_for`: "all", or a list of risk classes
    (["irreversible", "assertive"]). Absent/malformed → False (single-judge
    evidence suffices, the pre-1.5.2 behavior). Rationale: another pair of eyes
    is nearly free insurance when tokens are not the constraint — and a policy
    that lives in config is enforced, where one that lives in memory decays."""
    raw = load_config(project_path).get("panel_required_for")
    if isinstance(raw, str):
        # F5 (panel finding): the seeded default is "all". A near-miss like
        # "ALL"/"All" used to fall through to False and SILENTLY disable the
        # close gate — a case typo quietly downgrading the safety posture. Match
        # case-insensitively, and WARN (never silently) on any other unrecognized
        # scalar so a real typo is loud rather than a hidden fail-open.
        rv = raw.strip().lower()
        if rv == "all":
            return True
        if rv in ("", "none"):
            return False
        import sys as _sys
        print(f"[playbook] config.json panel_required_for={raw!r} is not "
              "recognized (use \"all\", a risk-class list like "
              "[\"assertive\",\"irreversible\"], or omit) — treating as NO panel "
              "requirement; fix it to restore the close gate.", file=_sys.stderr)
        return False
    if isinstance(raw, list):
        return risk in raw or "all" in raw
    return False


def has_panel_impl_evidence(task_file) -> bool:
    """PANEL-grade implementation evidence: judge.md's NEWEST round must be an
    IMPL panel that reached quorum (PANEL VERDICT: PASS). Parsed structurally —
    a stale impl-PASS buried under a newer FAIL (or under a newer PLAN round,
    which implies replanning and new work) must never satisfy the close gate.
    A FAIL-verdict panel is a degraded panel; a plan panel cannot vouch for what
    was BUILT."""
    p = Path(task_file)
    jm = p.parent / "judge.md"
    try:
        if not jm.exists():
            return False
        rounds = parse_judge_rounds(jm.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return False
    if not rounds:
        return False
    newest = rounds[0]
    return newest["mode"] == "impl" and newest["verdict"] == "PASS"


def has_review_evidence(task_file, impl_only: bool = False) -> bool:
    """True when a task carries evidence that a review actually ran: a judge.md
    in its directory, or a checked plan/impl/panel-review gate in task.md. Used
    by the close policy — an assertive/irreversible task with no such evidence
    cannot light-close (the 056 fix).

    impl_only=True demands IMPLEMENTATION-grade evidence: a plan review examines
    intent before the work exists, so it cannot vouch for what was actually
    built or claimed — a plan-phase judge.md must not satisfy the
    high-consequence close gate forever after."""
    p = Path(task_file)
    try:
        jm = p.parent / "judge.md"
        if jm.exists():
            if not impl_only:
                return True
            if "impl review" in jm.read_text(encoding="utf-8", errors="replace").lower():
                return True
        for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
            s = ln.strip().lower()
            if not s.startswith("- [x]"):
                continue
            if "impl-review" in s:
                return True
            if impl_only:
                if "panel-review" in s and "impl" in s:
                    return True
            elif "panel-review" in s or "plan-review" in s:
                return True
    except OSError:
        pass
    return False


def resolve_verify_commands(project_path: Path, risk: str = DEFAULT_RISK) -> "list[tuple[str, str]]":
    """Ordered (source_label, command) list to run at close for a task of `risk`.

    Config `verify` in .agent/config.json:
        "verify": "npm run check"                 → one always-run command
        "verify": {"_always": ["check", "test"],  → base bar for every close
                   "assertive": ["check:claims"]}  → extra for that risk class
    Values are a string or a list of strings. With no `verify` key, fall back to
    the legacy `merge_verify.command` as the base bar (the seed this generalizes).
    Returns [] when nothing is declared — the caller warns and allows the close."""
    cfg = load_config(project_path)
    v = cfg.get("verify")
    out: "list[tuple[str, str]]" = []
    if isinstance(v, str):
        out += [("verify", c) for c in _as_command_list(v)]
    elif isinstance(v, dict):
        out += [("verify._always", c) for c in _as_command_list(v.get("_always"))]
        if risk in RISK_CLASSES:
            out += [(f"verify.{risk}", c) for c in _as_command_list(v.get(risk))]
    elif v is None:
        mv = cfg.get("merge_verify")
        if isinstance(mv, dict):
            out += [("merge_verify.command", c) for c in _as_command_list(mv.get("command"))]
    return out


def close_decision(*, risk: str, verify_declared: bool, verify_failed: bool,
                   has_review_evidence: bool, force: bool, reason: "str | None",
                   panel_required: bool = False,
                   risk_section_present: bool = False) -> "tuple[bool, str]":
    """Pure close policy → (allowed, block_reason). block_reason is '' when allowed.

    1. --force ALWAYS requires a non-empty reason: a forced close must be
       self-documenting (task 046 was force-closed with 25 open gates and left no
       trace). With a reason, force allows the close.
    2. otherwise a FAILING declared verify blocks — the evidence bar.
    3. panel_required (owner policy `panel_required_for`): EVERY close in scope
       needs the evidence the caller passed — which the caller has resolved as
       PANEL-grade (all available judges, quorum PASS). Another pair of eyes is
       cheap insurance; the policy is enforced here so it cannot decay into a
       habit someone forgets.
    4. otherwise an assertive/irreversible task with NO review evidence blocks —
       high-consequence work cannot be light-closed for being small (056).
    5. an UNSET risk gate is held to that same bar. `unclassified` is in no risk
       class, so the whole risk-keyed requirement used to evaluate to nothing and
       the close proceeded on a warning — making "leave the field blank" the
       cheapest path through the strictest gate in the system, chosen by the very
       agent the gate constrains. `risk_section_present` separates the two facts
       the old code conflated: no `## Risk` heading = a pre-1.5.0 task that was
       never offered the gate (warn and pass, unchanged), heading present but
       unset = offered and skipped (block). Default False so a caller that cannot
       tell gets the lenient legacy path rather than an invented block."""
    if force:
        if not (reason and reason.strip()):
            return False, '--force requires --reason "why" — a forced close must record why.'
        return True, ""
    if verify_declared and verify_failed:
        return False, "declared verification failed — fix it, or override with --force --reason."
    if panel_required and not has_review_evidence:
        return False, (
            "panel review required by policy (`panel_required_for`): close needs a "
            "quorum-PASS panel IMPL review in judge.md — run `tasks panel-review <N> "
            "--mode impl`, or override with --force --reason."
        )
    if risk in HIGH_CONSEQUENCE and not has_review_evidence:
        return False, (
            f"{risk} task cannot light-close: it changes "
            + ("a claim about the world" if risk == "assertive" else "state that is hard to undo")
            + " — run impl-review/panel-review first, or override with --force --reason."
        )
    if (risk == DEFAULT_RISK and risk_section_present
            and not has_review_evidence):
        return False, (
            "## Risk was left unclassified, so the risk-keyed review requirement "
            "cannot be evaluated — an unset gate is held to the high-consequence "
            "bar rather than skipped. Set ## Risk to exactly one word "
            "(reversible / irreversible / assertive), or supply impl-review "
            "evidence, or override with --force --reason."
        )
    return True, ""


def freshness_gate_decision(*, risk: str, panel_required: bool,
                            evidence_carries: bool, round_fp: str, now_fp: str,
                            force: bool, stale_ok: bool,
                            stale_reason: "str | None") -> "tuple[bool, str]":
    """F18 (design-1.5.6.md, blind-judge conditional-PASS, conditions built):
    an IRREVERSIBLE close resting on panel evidence must not silently rest on
    a verdict that predates the closed code. Pure policy → (allowed, block_reason).

    The gate is deliberately narrow (judge C3): it applies only when the panel
    evidence would actually CARRY this close — rounds[0] is an impl round with
    verdict PASS (`evidence_carries`) — so a FAIL round or a replan on top
    falls through to the panel-evidence block instead of double-blocking, and
    only when both fingerprints exist and disagree. --force bypasses close
    policy wholesale as always (A8 — one blunt hatch, unchanged semantics);
    `--stale-panel-ok --reason "..."` is the narrow exit, and the reason is
    recorded in the receipt's freshness clause. Advisory (console note +
    receipt clause, no block) remains the behavior for every other risk:
    batch 5 showed re-panels happen voluntarily when the delta is material —
    the block is reserved for the one place a wrong close cannot be undone."""
    if force:
        return True, ""
    if risk != "irreversible" or not panel_required or not evidence_carries:
        return True, ""
    if not round_fp or not now_fp or round_fp == now_fp:
        return True, ""
    if stale_ok:
        if stale_reason and stale_reason.strip():
            return True, ""
        return False, ('--stale-panel-ok requires --reason "why the post-panel '
                       "delta doesn't need a re-panel\" — the acceptance must "
                       "be on the record.")
    return False, (
        "risk is irreversible and the code state changed after the newest impl "
        f"panel (tree-state {round_fp} → {now_fp}) — the panel's verdict "
        "predates the code being closed.\n"
        "  Either re-run:  tasks panel-review <N> --mode impl\n"
        "  or record the delta judgment:  tasks work done --stale-panel-ok "
        '--reason "..."\n'
        "  (run `git status` / `git diff` to see what changed since the panel)"
    )


def format_verify_receipt(entries, head_sha, risk, *, reason=None, timestamp=None,
                          dirty_files=0, freshness=None) -> str:
    """Render ONE receipt ENTRY for the `## Verification Receipt` section (the
    heading itself belongs to upsert_task_section, which keeps entries
    newest-first). `entries` is a list of (source_label, command, rc, output);
    an empty list means nothing was declared. Never raises.

    `dirty_files`: modified/untracked count at close. The normal flow closes THEN
    commits, so the stamped commit predates the verified code — observed live
    (StrataDB task 005 closed with 100% of its work uncommitted). The receipt
    must say so, or 'commit X' claims a state X does not contain."""
    ts = timestamp or datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    commit_label = head_sha or "(unknown)"
    if dirty_files:
        commit_label += f" (+{dirty_files} uncommitted file(s) — verified code is NOT in this commit)"
    out = [f"### {ts} · risk {risk} · commit {commit_label}"]
    if reason:
        out.append(f"- **Forced close, reason:** {reason.strip()}")
    # F18 Leg 1: panel freshness is part of the durable record for EVERY close
    # where an impl round exists (the F17 advisory was console-only and its
    # firing at task 010 stayed unwitnessable forever). `freshness` is a dict:
    # {verdict: "FRESH"|"STALE"|"NO-STAMP", round_fp, now_fp, accepted_reason}.
    if freshness:
        v = freshness.get("verdict")
        if v == "NO-STAMP":
            out.append("- **Panel tree-state:** no stamp recorded on the "
                       "newest impl round — freshness unverifiable")
        elif v in ("FRESH", "STALE"):
            line = (f"- **Panel tree-state:** {freshness.get('round_fp', '?')} "
                    f"vs close {freshness.get('now_fp', '?')} — {v}")
            if v == "STALE":
                line += " (code changed after newest impl panel)"
                ar = freshness.get("accepted_reason")
                if ar:
                    line += f', accepted: "{ar.strip()}"'
            out.append(line)
    if not entries:
        out.append("- **Verification:** NONE DECLARED — nothing was verified at close. "
                   "Declare `verify` in `.agent/config.json` to make close self-verifying.")
    else:
        out.append("- **Commands:**")
        for label, command, rc, output in entries:
            first = ""
            for ln in (output or "").splitlines():
                if ln.strip():
                    first = ln.strip()
                    break
            status = "PASS" if rc == 0 else f"FAIL(exit {rc})"
            cmd1 = command.strip().splitlines()[0] if command.strip() else command
            out.append(f"    - [{status}] `{cmd1}` ({label})" + (f" — {first[:160]}" if first else ""))
    out.append("")
    return "\n".join(out)


# ── Parked lifecycle (P9) + learning-loop triggers (P4) ──────────────────────
# 48 of 68 executed tasks parked something and no command ever surfaced a parked
# item again — one collision was parked three rounds running and stayed open. And
# the two learning mechanisms (retro, intent) never fired in 79 tasks because
# nothing triggered them. These helpers give parked items a lifecycle and give
# the retro a trigger, as pure/testable functions.

PARKED_PLACEHOLDER = (
    "(Findings or ideas that emerged during work but are out of scope. "
    "Describe each with enough context for a future task to pick it up.)"
)


def _parked_item_status(item: str) -> str:
    """Classify a parked bullet as open / promoted / dismissed by its resolution
    marker. Convention: `[promoted → NNN]` (or `→ task NNN`) = promoted;
    `[dismissed: reason]` or a `~~struck~~` line = dismissed; otherwise open."""
    stripped = item.strip()
    low = stripped.lower()
    if stripped.startswith("~~") or "[dismissed" in low:
        return "dismissed"
    if "[promoted" in low or "→ task" in low or "-> task" in low:
        return "promoted"
    return "open"


def extract_parked_items(task_md_text: str) -> "list[str]":
    """Return the '- ' bullets under EVERY ## Parked section, skipping the
    template placeholder. Same shape retro.py parses, kept here so `tasks parked`
    and the close-time surface share one definition of 'a parked item'.

    ALL sections, not the first: the template ships a ## Parked section, so an
    agent (or a receipt upsert reordering the file) can easily produce a second
    one — and a first-match read makes every later section invisible. Found live
    by the 1.5.3 gauntlet; the multi-heading hazard, same family as #09."""
    in_section = False
    body: "list[str]" = []
    for line in task_md_text.splitlines():
        if line.strip() == "## Parked":
            in_section = True
            continue
        if in_section:
            if line.startswith("## "):
                in_section = False  # keep scanning — there may be another section
                continue
            body.append(line)
    items: "list[str]" = []
    for line in body:
        s = line.strip()
        if s.startswith("- "):
            text = s[2:].strip()
            if text and text != PARKED_PLACEHOLDER:
                items.append(text)
    return items


def open_parked_items(task_md_text: str) -> "list[str]":
    """Parked bullets still awaiting resolution (open, not promoted/dismissed)."""
    return [i for i in extract_parked_items(task_md_text)
            if _parked_item_status(i) == "open"]


def _iter_task_dirs(project_path: Path):
    """Yield (number, slug, task_md_path) for every `<NNN>-<slug>/task.md`."""
    tasks_dir = resolve_agent_dir(project_path) / "tasks"
    if not tasks_dir.exists():
        return
    for d in sorted(tasks_dir.iterdir()):
        if not d.is_dir():
            continue
        m = re.match(r'^(\d+)-(.*)$', d.name)
        if not m:
            continue
        tf = d / "task.md"
        if tf.exists():
            yield int(m.group(1)), m.group(2), tf


def scan_parked(project_path: Path, open_only: bool = True) -> "list[dict]":
    """Every parked item across all tasks: {task, slug, item, status}. Ordered by
    task number (oldest first) — the debt that has waited longest reads first."""
    out: "list[dict]" = []
    for num, slug, tf in _iter_task_dirs(project_path):
        try:
            text = tf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for item in extract_parked_items(text):
            status = _parked_item_status(item)
            if open_only and status != "open":
                continue
            out.append({"task": num, "slug": slug, "item": item, "status": status})
    return out


def count_tasks_since_retro(project_path: Path) -> "tuple[int, int | None]":
    """Return (closed non-retro tasks since the last retro, last_retro_number).
    last_retro_number is None when no retro has ever run."""
    dirs = list(_iter_task_dirs(project_path))
    last_retro = None
    for num, slug, _tf in dirs:
        if slug.startswith("retro"):
            last_retro = num if last_retro is None else max(last_retro, num)
    closed = 0
    for num, slug, tf in dirs:
        if slug.startswith("retro"):
            continue
        if last_retro is not None and num <= last_retro:
            continue
        try:
            if _extract_status(tf).startswith("done"):
                closed += 1
        except OSError:
            pass
    return closed, last_retro


def retro_proposal(project_path: Path, threshold: int = 10) -> "str | None":
    """A close-time nudge to run `tasks retro`, or None. Fires once the number of
    tasks closed since the last retro reaches `threshold` — the mechanism the
    report says never fired because nothing triggered it (C7)."""
    closed, last_retro = count_tasks_since_retro(project_path)
    if closed < threshold:
        return None
    anchor = f"since retro T{last_retro:03d}" if last_retro is not None else "and no retro has ever run"
    return (
        f"{closed} tasks closed {anchor}. Consider `tasks retro` — the horizontal "
        "learning pass (intent-health, garbage, parked-item triage). A lesson paid "
        "for twice should become a script, not a note you must remember to apply."
    )


# Task type → pattern name in playbook skill
PLAYBOOKS = {
    "feature": "Build",
    "build": "Build",
    "bugfix": "Fix",
    "refactor": "Build",
    "cleanup": "Fix",
    "ops": "Build",
    "audit": "Evaluate",
    "eval": "Evaluate",
    "research": "Investigate",
}



def _slugify(name: str) -> str:
    """Convert name to lowercase hyphen-separated slug."""
    slug = re.sub(r'[\s_]+', '-', name)
    slug = re.sub(r'[^a-zA-Z0-9-]', '', slug)
    slug = re.sub(r'-+', '-', slug)
    return slug.strip('-').lower()


def _display_title(name: str) -> str:
    """Render a task name for markdown headers."""
    return name.replace("-", " ").replace("_", " ").title()


def _next_task_number(tasks_dir: Path) -> int:
    """Find the next available task number."""
    if not tasks_dir.exists():
        return 1

    max_num = 0
    for item in tasks_dir.iterdir():
        if item.is_dir():
            match = re.match(r'^(\d+)-', item.name)
            if match:
                num = int(match.group(1))
                max_num = max(max_num, num)

    return max_num + 1



def _find_playbook_skill(project_path: Path | None = None) -> Path | None:
    """Find the playbook SKILL.md file.

    Resolution order:
    1. project_path/.claude/skills/playbook/SKILL.md  (project-local)
    2. ~/.claude/skills/playbook/SKILL.md              (home install)
    """
    if project_path:
        skill = project_path / ".claude" / "skills" / "playbook" / "SKILL.md"
        if skill.exists():
            return skill

    home_skill = Path.home() / ".claude" / "skills" / "playbook" / "SKILL.md"
    if home_skill.exists():
        return home_skill

    return None


def _load_playbook(task_type: str, project_path: Path | None = None) -> str | None:
    """Load a pattern template from the unified playbook skill.

    Extracts the ```markdown block under the matching ### Pattern heading.
    Returns the template text, or None if not found.
    """
    pattern_name = PLAYBOOKS.get(task_type)
    if not pattern_name:
        return None

    skill_path = _find_playbook_skill(project_path)
    if not skill_path:
        return None

    content = skill_path.read_text(encoding="utf-8", errors="replace")

    # Extract the ```markdown ... ``` block under ### <pattern_name>
    in_section = False
    in_code_block = False
    template_lines = []

    for line in content.splitlines():
        if line.strip() == f"### {pattern_name}":
            in_section = True
            continue
        if in_section:
            # Stop at next ### heading
            if line.startswith("### ") and not in_code_block:
                break
            if line.strip() == "```markdown":
                in_code_block = True
                continue
            if in_code_block:
                if line.strip() == "```":
                    break
                template_lines.append(line)

    return "\n".join(template_lines) if template_lines else None


def _find_custom_playbook(project_path: Path, task_type: str) -> Path | None:
    """Check if a custom playbook template exists in .agent/playbooks/."""
    playbook = resolve_agent_dir(project_path) / "playbooks" / f"{task_type}.md"
    return playbook if playbook.exists() else None


def list_all_types(project_path: Path) -> list[str]:
    """Return sorted list of all available task types (built-in + custom)."""
    types = set(PLAYBOOKS.keys()) | {"quick", "light"}
    playbooks_dir = resolve_agent_dir(project_path) / "playbooks"
    if playbooks_dir.exists():
        for f in playbooks_dir.glob("*.md"):
            if f.name != "README.md":
                types.add(f.stem)
    return sorted(types)


def create_task(project_path: Path, name: str, task_type: str | None = None,
                intent_text: str | None = None, stub: bool = False) -> Path:
    """Create a new task with the given name.

    Args:
        project_path: Path to the project root
        name: Human-readable name for the task
        task_type: Task type (feature, bugfix, etc.) for playbook template.
            If a matching .agent/playbooks/<type>.md exists, uses that
            instead of the base Python template.
        intent_text: Optional intent paragraph to pre-fill ## Intent section.
        stub: If True, generate minimal stub (no gates) instead of full template.

    Returns:
        Path to the created task.md file
    """
    # Creating a task in the fresh-clone shape would mint a root `.agent/tasks/`
    # — and because a root tasks dir is itself a legitimate lane, that single
    # mkdir permanently converts the guarded shape into an "allowed mixed
    # layout", disarming the guard for every other surface too.
    require_lane_marker(project_path, "tasks new")
    tasks_dir = resolve_agent_dir(project_path) / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)

    task_num = _next_task_number(tasks_dir)
    slug = _slugify(name)
    folder_name = f"{task_num:03d}-{slug}"

    task_dir = tasks_dir / folder_name
    task_dir.mkdir()

    # Check for custom playbook template first
    custom = _find_custom_playbook(project_path, task_type) if task_type else None

    if stub:
        # Stub mode: minimal template with no gates
        from tasks.template import render_stub_template
        content = render_stub_template(
            num=task_num, title=_display_title(name),
            intent_text=intent_text or "",
            task_type=task_type,
        )
    elif custom:
        content = custom.read_text(encoding="utf-8", errors="replace")
        content = content.replace("{{NNN}}", f"{task_num:03d}")
        content = content.replace("{{TITLE}}", _display_title(name))
        # F8: let the [intent] arg reach a custom playbook too, via an explicit
        # `{{INTENT}}` token (built-in templates use prose placeholders, matched
        # below; a custom author opts in with this token). No token → unchanged.
        content = content.replace("{{INTENT}}", intent_text or "")
    else:
        # Fall back to base Python template
        from tasks.template import render_template
        content = render_template(num=task_num, title=_display_title(name), task_type=task_type)

        # Append playbook template if task_type specified
        if task_type:
            role_template = _load_playbook(task_type, project_path)
            if role_template:
                content += "\n" + role_template + "\n"

    # Pre-fill Intent section if intent_text provided
    if intent_text and not stub:
        # Replace placeholder in all template variants
        for placeholder in [
            "(what we want to achieve \u2014 the outcome, not the activity)",
            "(one line \u2014 what to do and how to verify)",
            # B1: the `light` template's Intent placeholder \u2014 was missing here, so
            # `tasks new light <name> <intent>` silently discarded the intent.
            "(one line \u2014 what to do and what proves it worked)",
        ]:
            if placeholder in content:
                content = content.replace(placeholder, intent_text)
                break

    # F8: standing gates — project-declared gates appended LAST, whatever
    # branch assembled the content (base template, custom playbook, role
    # append). Stubs are skipped: they carry no gates until activation, and
    # the expansion path applies the same helper then.
    if not stub:
        content, _sg_issues = append_standing_gates(
            content, load_config(project_path), task_num)
        for _msg in _sg_issues:
            print(f"[playbook] standing_gates: {_msg}", file=sys.stderr)

    task_file = task_dir / "task.md"
    _atomic_write(task_file, content)

    return task_file


def _extract_status(task_file: Path) -> str:
    """Extract status from task file (line after last ## Status)."""
    try:
        lines = task_file.read_text(encoding="utf-8", errors="replace").splitlines()
        status_idx = None
        for i, line in enumerate(lines):
            if line.strip() == "## Status":
                status_idx = i
        if status_idx is not None and status_idx + 1 < len(lines):
            return lines[status_idx + 1].strip()
        return "unknown"
    except Exception:
        return "error"


def _extract_problem(task_file: Path) -> str:
    """Extract first line of Problem/Intent section from task file."""
    try:
        lines = task_file.read_text(encoding="utf-8", errors="replace").splitlines()
        in_section = False
        for line in lines:
            if line.strip() in ("## Problem", "## Intent"):
                in_section = True
                continue
            if in_section:
                if not line.strip():
                    continue
                if line.startswith("##"):
                    break
                text = line.strip()
                if text.startswith("(") and text.endswith(")"):
                    text = text[1:-1]
                return text
        return ""
    except Exception:
        return ""


def _extract_head_position(task_file: Path) -> str:
    """Find the first unchecked checkbox or empty required field."""
    try:
        lines = task_file.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines:
            stripped = line.strip()
            # Unchecked checkbox
            if stripped.startswith("- [ ]"):
                return stripped[6:].strip()  # text after "- [ ] "
            # Empty required field (line ending with : and nothing after)
            if stripped.endswith(":") and stripped.startswith("- **"):
                return stripped
        return "(all gates checked)"
    except Exception:
        return "(error reading)"


def _is_done(task_file: Path) -> bool:
    """Check if a task's status starts with 'done'."""
    return _extract_status(task_file).startswith("done")


def _is_blocked(task_file: Path) -> bool:
    """A task paused awaiting the owner's decision (issue #08). Distinct from
    pending/in_progress: it is NOT waiting on the agent, so it must not read as
    work in progress or be auto-adopted as the active task."""
    return _extract_status(task_file).startswith("blocked")


def _atomic_write(path: Path, text: str) -> None:
    """All task.md writers route here: same-directory temp + os.replace, so a
    concurrent reader never sees a sheared file and interleaved writers lose
    whole versions rather than producing half-merged lines — multi-user repos
    are a supported layout, so this is load-bearing, not ceremony.

    Thin wrapper over the package primitive (tasks.atomic.atomic_write), which
    additionally preserves task.md's permission bits across the rewrite (the old
    mkstemp temp was 0600, silently stripping group/other read from a shared
    task.md on the first edit) and fsyncs before the replace."""
    atomic_write(path, text)


def _set_status(task_file: Path, value: str) -> None:
    """Rewrite the line after the LAST ## Status (matching _extract_status).
    The single writer of task status."""
    lines = task_file.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    target = None
    for i, line in enumerate(lines):
        if line.strip() == "## Status" and i + 1 < len(lines):
            target = i
    if target is not None:
        lines[target + 1] = value + "\n"
        _atomic_write(task_file, "".join(lines))


def upsert_task_section(task_file: Path, heading: str, entry: str) -> None:
    """ONE `## {heading}` per task.md, newest entry FIRST beneath it.

    Receipts used to append a whole new `## …` section per close/audit, so a
    reopened task accumulated duplicate headings — and section parsers that take
    the first match then read a STALE receipt as current. One heading with
    newest-first `###` entries keeps the full history AND makes the first thing
    under the heading the truth."""
    p = Path(task_file)
    text = p.read_text(encoding="utf-8", errors="replace")
    marker = f"## {heading}"
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if ln.strip() == marker:
            new = lines[:i + 1] + ["", *entry.rstrip("\n").splitlines()] + lines[i + 1:]
            _atomic_write(p, "\n".join(new) + "\n")
            return
    _atomic_write(p, text.rstrip("\n") + f"\n\n{marker}\n\n{entry.rstrip()}\n")


def set_task_blocked(task_file: Path, reason: str) -> None:
    """Mark a task BLOCKED with a self-documenting reason (#08). Sets status to
    `blocked` and writes a `## Blocked` section. Does NOT touch a single gate.

    The reason is collapsed to one line and rendered as a blockquote, so a reason
    containing `- [ ]`, backticks, or a `## ` heading can never become a phantom
    gate or section for the line-anchored parsers (the #09 hazard)."""
    clean = " ".join(reason.split()) or "(no reason given)"
    ts = datetime.datetime.now().astimezone().isoformat(timespec="minutes")
    _set_status(task_file, "blocked")
    lines = task_file.read_text(encoding="utf-8", errors="replace").splitlines()
    # Drop any prior ## Blocked section (idempotent re-block), then append fresh.
    out, skip = [], False
    for line in lines:
        if line.strip() == "## Blocked":
            skip = True
            continue
        if skip:
            if line.startswith("## "):
                skip = False
                out.append(line)
            continue
        out.append(line)
    while out and out[-1].strip() == "":
        out.pop()
    out += ["", "## Blocked", f"> {clean}  (since {ts})", ""]
    _atomic_write(task_file, "\n".join(out) + "\n")


def resume_blocked_task(task_file: Path) -> None:
    """Clear a block: status → in_progress, and stamp the ## Blocked section with a
    resume line so the history stays true and current rather than stale (#08)."""
    _set_status(task_file, "in_progress")
    ts = datetime.datetime.now().astimezone().isoformat(timespec="minutes")
    lines = task_file.read_text(encoding="utf-8", errors="replace").splitlines()
    out, i, n, stamped = [], 0, len(lines), False
    while i < n:
        out.append(lines[i])
        if lines[i].strip() == "## Blocked":
            i += 1
            while i < n and not lines[i].startswith("## "):
                out.append(lines[i])
                i += 1
            out.append(f"> Resumed {ts}")
            stamped = True
            continue
        i += 1
    if stamped:
        _atomic_write(task_file, "\n".join(out) + "\n")


def _folder_matches_filter(folder_name: str, name_filter: str) -> bool:
    """Does a task folder name match the activation filter?

    A numeric filter is a task NUMBER — it must match ONLY the exact `NNN-`
    prefix, never a substring (C1b: `tasks work 100` must not resolve to
    `1000-bar`; the old `name_filter not in folder` substring test let it,
    then wrote the raw pointer `100`, which fed the C1 non-resolving-pointer
    crash on close). A non-numeric filter keeps the substring behaviour for
    slug-style lookups.
    """
    if not name_filter:
        return True
    if name_filter.isdigit():
        return (folder_name == name_filter
                or folder_name.startswith(name_filter + "-"))
    return name_filter in folder_name


def _find_active_task(project_path: Path, name_filter: str = "") -> Path | None:
    """Find the active task: earliest non-done task with unchecked gates.

    If name_filter is given, only match tasks whose folder name matches it
    (exact `NNN-` prefix for a numeric filter, substring otherwise).
    """
    tasks_dir = resolve_agent_dir(project_path) / "tasks"
    if not tasks_dir.exists():
        return None
    for task_file in sorted(tasks_dir.glob("*/task.md")):
        if name_filter and not _folder_matches_filter(task_file.parent.name, name_filter):
            continue
        if _is_done(task_file):
            continue
        if _is_blocked(task_file):
            continue  # #08: a blocked task waits on the owner, not the agent
        head = _extract_head_position(task_file)
        if not head.startswith("("):
            return task_file
    return None


def task_done(project_path: Path, name_filter: str = "") -> dict:
    """Check off the current gate and return checked + next gate info.

    Returns dict with keys: task_name, checked, next, task_file.
    On error, returns dict with 'error' key.
    """
    task_file = None

    agent_dir = resolve_agent_dir(project_path)
    session_id = resolve_session_id()
    state_files = [agent_dir / "sessions" / session_id / "current_state"]

    for state_file in state_files:
        if not state_file.exists():
            continue
        task_num = state_file.read_text(encoding="utf-8", errors="replace").strip()
        if not task_num:
            continue
        matches = sorted((agent_dir / "tasks").glob(f"{task_num}-*/task.md"))
        if not matches:
            continue
        candidate = matches[0]
        if name_filter and name_filter not in candidate.parent.name:
            continue
        if _is_done(candidate):
            continue
        head = _extract_head_position(candidate)
        if not head.startswith("("):
            task_file = candidate
            break

    if task_file is None:
        task_file = _find_active_task(project_path, name_filter)
    if not task_file:
        return {"error": "No active task with open gates"}

    task_name = task_file.parent.name
    lines = task_file.read_text(encoding="utf-8", errors="replace").splitlines()

    # Find and check off the first unchecked gate
    checked_text = None
    checked_idx = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("- [ ]"):
            checked_text = stripped[6:].strip()
            # Preserve original indentation, just flip the checkbox
            lines[i] = line.replace("- [ ]", "- [x]", 1)
            checked_idx = i
            break

    if checked_text is None:
        return {"error": f"No unchecked gate in {task_name}"}

    # Write back (atomic: this rewrites an existing task.md a reader may be
    # holding open — a plain write_text would expose a truncate→write torn read).
    _atomic_write(task_file, "\n".join(lines) + "\n")

    # Collect next gates (up to 3) after the one we just checked
    upcoming = []
    for line in lines[checked_idx + 1:]:
        stripped = line.strip()
        if stripped.startswith("- [ ]"):
            upcoming.append(stripped[6:].strip())
        elif stripped.endswith(":") and stripped.startswith("- **"):
            upcoming.append(stripped)
        else:
            continue
        if len(upcoming) >= 3:
            break

    return {
        "task_name": task_name,
        "checked": checked_text,
        "upcoming": upcoming,
        "task_file": task_file,
    }


# One line-anchored definition of "a gate marker", shared by the count and the
# head-position parser so they cannot disagree about what a gate is (issue #09).
# A `- [ ]` in mid-line PROSE ("the convention is `- [ ]` until…") is NOT a gate
# — the old substring count treated it as one, so a task could close at 71/74
# while `status` said "(all gates checked)": the count that does not gate, and the
# gate that does not count. Matches the Stop hook's `^[[:space:]]*- \[ \]` and
# retro.py's gate scan. NOT fence-aware — a fenced ` - [ ]` template example is
# still counted, consistently, by all three consumers; making the whole family
# fence-aware (as `_node_starts` is on the mind-map side) needs the bash Stop hook
# to agree too and is a separate design decision.
_GATE_LINE_RE = re.compile(r"^[ \t]*- \[([ xX])\]", re.MULTILINE)


def _gate_counts(content: str) -> "tuple[int, int]":
    """Return (checked, total) line-anchored gate markers in `content`."""
    marks = _GATE_LINE_RE.findall(content)
    checked = sum(1 for m in marks if m in ("x", "X"))
    return checked, len(marks)


def _extract_progress(task_file: Path) -> str:
    """Count checked/total gates in a task file (line-anchored, prose-safe)."""
    try:
        checked, total = _gate_counts(task_file.read_text(encoding="utf-8", errors="replace"))
        return f"{checked}/{total}" if total > 0 else "-"
    except Exception:
        return "-"


def list_tasks(project_path: Path, pending_only: bool = False) -> None:
    """List all tasks with their status and intent."""
    tasks_dir = resolve_agent_dir(project_path) / "tasks"

    if not tasks_dir.exists():
        # Lane-aware (genesis finding): name the path this command RESOLVED —
        # single-user repos reproduce the old ".agent/tasks/" literal exactly.
        print(f"No {tasks_dir.relative_to(project_path).as_posix()}/ directory found")
        return

    task_files = sorted(tasks_dir.glob("*/task.md"))

    if not task_files:
        print("No tasks found")
        return

    status_w = 7
    progress_w = 8
    intent_w = 500

    # Collect rows first to compute dynamic name column width
    rows = []
    counts = {"done": 0, "pending": 0, "blocked": 0, "other": 0}

    for task_file in task_files:
        name = task_file.parent.name
        status = _extract_status(task_file)
        status_key = status.split()[0] if status else "unknown"

        if status_key in ("done", "pending", "blocked"):
            counts[status_key] += 1
        else:
            counts["other"] += 1

        if pending_only and status_key == "done":
            continue

        intent = _extract_problem(task_file)
        progress = _extract_progress(task_file)

        if len(intent) > intent_w:
            intent = intent[:intent_w-1] + "…"
        if len(status) > status_w:
            status = status[:status_w]

        rows.append((name, status, progress, intent))

    name_w = max((len(r[0]) for r in rows), default=4)
    name_w = max(name_w, 4)  # at least wide enough for "Name"

    print(f"{'Name':<{name_w}} | {'Status':<{status_w}} | {'Progress':<{progress_w}} | Intent")
    print(f"{'-'*name_w}-+-{'-'*status_w}-+-{'-'*progress_w}-+-{'-'*intent_w}")

    for name, status, progress, intent in rows:
        print(f"{name:<{name_w}} | {status:<{status_w}} | {progress:<{progress_w}} | {intent}")

    print("")
    parts = []
    if counts["done"]:
        parts.append(f"{counts['done']} done")
    if counts["pending"]:
        parts.append(f"{counts['pending']} pending")
    if counts["blocked"]:
        parts.append(f"{counts['blocked']} blocked")
    if counts["other"]:
        parts.append(f"{counts['other']} other")
    summary = f"Summary: {', '.join(parts)}"
    if pending_only:
        summary += f" (showing {len(rows)} open)"
    print(summary)
    # Lane-aware: <lane>/tasks/<name>/task.md, where <lane> is what this very
    # listing read — ".agent" single-user (byte-identical to the old literal),
    # ".agent/<user>" on a multi-user repo (the genesis-gauntlet cosmetic).
    print(f"Task files: {tasks_dir.relative_to(project_path).as_posix()}/<name>/task.md — activate with: tasks work <number>")


def task_status(project_path: Path) -> None:
    """Show head position (first unchecked gate) for each active task."""
    tasks_dir = resolve_agent_dir(project_path) / "tasks"

    if not tasks_dir.exists():
        # Lane-aware (genesis finding): name the path this command RESOLVED —
        # single-user repos reproduce the old ".agent/tasks/" literal exactly.
        print(f"No {tasks_dir.relative_to(project_path).as_posix()}/ directory found")
        return

    task_files = sorted(tasks_dir.glob("*/task.md"))

    if not task_files:
        print("No tasks found")
        return

    for task_file in task_files:
        name = task_file.parent.name
        status = _extract_status(task_file)

        if status == "done":
            continue

        progress = _extract_progress(task_file)
        # #08: a blocked task is not waiting on the agent — show that, not a gate
        # it cannot complete. It should not read like ordinary work in progress.
        if _is_blocked(task_file):
            print(f"{name:<40} | {progress:<8} | BLOCKED (awaiting decision — `tasks work {name.split('-')[0]}` to resume)")
            continue

        head = _extract_head_position(task_file)
        print(f"{name:<40} | {progress:<8} | {head}")


# merge-doctor — mechanical contamination check for cross-namespace merges
# --------------------------------------------------------------------------

# Lines under this length are too noisy (empty, "ok", single punctuation) to
# treat as evidence of contamination by themselves.
_MERGE_DOCTOR_LINE_FLOOR = 4
# Flag a per-user file when the *cumulative* non-whitespace bytes of foreign
# lines clear this threshold — catches one long foreign line OR many short
# ones (chat-log timestamps, M-tags, "tasks done" markers).
_MERGE_DOCTOR_FOREIGN_BYTES_MIN = 20
# (conflict-marker detection lives in _md_has_conflict_markers / _CONFLICT_MARKER_RE —
# line-start angle markers only; a substring tuple would re-introduce the
# `=======`/prose false positives.)


def _md_git(cmd: list[str], cwd: Path) -> tuple[int, str, str]:
    # errors="replace": git blobs may be non-UTF-8 (e.g. Windows cp1252 task.md);
    # strict decoding would raise UnicodeDecodeError and abort the whole audit.
    proc = subprocess.run(
        ["git", *cmd],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _md_git_show(ref: str, path: str, cwd: Path) -> str | None:
    rc, out, _ = _md_git(["show", f"{ref}:{path}"], cwd)
    return out if rc == 0 else None


def _md_user_dirs(ref: str, cwd: Path) -> set[str]:
    """Names of .agent/<user>/ tree entries on <ref>."""
    rc, out, _ = _md_git(
        ["ls-tree", "-d", "--name-only", ref, ".agent/"], cwd
    )
    if rc != 0:
        return set()
    users: set[str] = set()
    for line in out.splitlines():
        line = line.strip().rstrip("/")
        if not line:
            continue
        # entries look like ".agent/userA"
        parts = line.split("/")
        if len(parts) == 2 and parts[0] == ".agent" and parts[1]:
            users.add(parts[1])
    return users


def _md_unmerged_paths(cwd: Path) -> set[str]:
    """Paths git considers currently unmerged (active merge conflicts).

    Parses `git ls-files --unmerged` output where each row is
    `<mode> <hash> <stage>\\t<path>`. Splits on `\\t` to handle paths
    containing spaces. Returns empty set if no merge is in progress.
    """
    rc, out, _ = _md_git(["ls-files", "--unmerged"], cwd)
    if rc != 0:
        return set()
    paths: set[str] = set()
    for line in out.splitlines():
        if "\t" not in line:
            continue
        path = line.split("\t", 1)[1].strip()
        if path:
            paths.add(path)
    return paths


def _md_tracked(path: str, cwd: Path) -> bool:
    """True iff <path> is currently tracked in the git index."""
    rc, out, _ = _md_git(["ls-files", "--", path], cwd)
    return rc == 0 and bool(out.strip())


def _md_ignored(path: str, cwd: Path) -> bool:
    """True iff <path> is covered by .gitignore.

    `git check-ignore <path>` exits 0 when the path matches an ignore rule,
    1 when it does not, and 128 on errors (bad path, not a git repo). Collapse
    only exit 0 to True so transient errors don't mask findings.
    """
    rc, _, _ = _md_git(["check-ignore", "-q", path], cwd)
    return rc == 0


def _md_nontrivial(text: str) -> set[str]:
    """Return the set of stripped lines >= LINE_FLOOR chars long.

    The line floor screens out pure-noise lines (empty, single chars). The
    real contamination threshold is checked per-comparison against
    FOREIGN_BYTES_MIN on the *sum* of foreign-line lengths, so a single short
    "tasks done" appearing on the wrong branch can still be detected if other
    short foreign lines accompany it.
    """
    lines: set[str] = set()
    for raw in text.splitlines():
        stripped = raw.strip()
        if len(stripped) >= _MERGE_DOCTOR_LINE_FLOOR:
            lines.add(stripped)
    return lines


_CONFLICT_MARKER_RE = re.compile(r'(?m)^(<{7}|>{7})')


def _md_has_conflict_markers(text: str) -> bool:
    """True iff text has a git conflict marker at LINE-START (``<<<<<<<`` or
    ``>>>>>>>``, 7 chars). A bare ``=======`` line is NOT treated as a marker —
    it is valid markdown (setext H1 underline / horizontal rule). Matching at
    line-start (not substring) avoids flagging prose like ``grep '<<<<<<'``."""
    return bool(_CONFLICT_MARKER_RE.search(text))


def _md_marker_lines(text: str) -> set[str]:
    """Conflict-marker lines (``<<<<<<<…`` / ``>>>>>>>…``) at line-start."""
    return {ln for ln in text.splitlines() if _CONFLICT_MARKER_RE.match(ln)}


def _md_new_marker_lines(rel: str, cur_text: str, parents: list[str],
                         cwd: Path) -> bool:
    """True iff `rel` has a conflict-marker line NOT present in any parent's
    version of the file. Git's synthesized conflict markers are in neither
    parent, so a NEW marker line is a real stranded marker; a marker line that
    already exists in a parent is pre-existing documentation (e.g. a task.md
    showing an example conflict, which a merge may merely append to) — not a
    false positive to gate on."""
    cur = _md_marker_lines(cur_text)
    if not cur:
        return False
    parent_markers: set[str] = set()
    for p in parents:
        pt = _md_git_show(p, rel, cwd)
        if pt is not None:
            parent_markers |= _md_marker_lines(pt)
    return bool(cur - parent_markers)


def run_merge_doctor(project_path: Path, source: str, target: str) -> int:
    """Audit a merge for per-user cross-contamination and stranded markers.

    Inspection contract:
      - Working tree if a merge is in progress (.git/MERGE_HEAD present).
      - Else the most recent merge commit reachable from HEAD.
      - Neither → print "no merge state detected" and return 0.

    Findings are classified into three buckets:
      - actionable: real problems (contamination, tracked legacy paths,
        stranded conflict markers). Counted toward exit code.
      - expected: mid-merge surface that Step 5 of the skill will resolve
        (conflict markers in files git lists as --unmerged).
      - informational: pre-existing untracked files outside any user
        namespace that aren't gitignored. Printed but not counted.

    A fourth tier — untracked + gitignored — is suppressed entirely so the
    user doesn't see the disk noise (.DS_Store, bash_history) they
    explicitly named as annoying.

    Returns len(actionable). Callers map >0 → exit code 1.
    """
    merge_head = project_path / ".git" / "MERGE_HEAD"
    merge_commit = None
    if merge_head.exists():
        mid_merge = True
        state = "mid-merge (working tree)"
    else:
        rc, out, _ = _md_git(
            ["log", "--merges", "-n", "1", "--pretty=%H"], project_path
        )
        if rc == 0 and out.strip():
            mid_merge = False
            merge_commit = out.strip()
            state = f"post-merge (commit {merge_commit[:8]})"
        else:
            print("no merge state detected")
            return 0

    print(f"merge-doctor: inspecting {state}")
    print(f"  source ref: {source}")
    print(f"  target ref: {target}")
    print()

    actionable: list[str] = []
    expected: list[str] = []
    informational: list[str] = []

    # Paths git considers actively unmerged — only populated mid-merge.
    # In post-merge mode this is empty by construction, which collapses
    # the [EXPECTED] bucket so any surviving marker reports as actionable.
    unmerged = _md_unmerged_paths(project_path) if mid_merge else set()

    # The two merge parents. A conflict marker git writes is in NEITHER parent,
    # so a marker line that already exists in a parent is pre-existing
    # documentation (e.g. a task.md showing an example conflict), not a stranded
    # merge marker — see `_md_markers_from_this_merge`. This classifies markers
    # by content novelty, not just whether the path was touched, so a
    # merge-modified doc with pre-existing example markers isn't a false positive.
    if mid_merge:
        merge_parents = ["HEAD", "MERGE_HEAD"]
    else:
        merge_parents = [f"{merge_commit}^1", f"{merge_commit}^2"]

    # 1. User detection (union of both sides)
    src_users = _md_user_dirs(source, project_path)
    tgt_users = _md_user_dirs(target, project_path)
    all_users = src_users | tgt_users
    print(f"detected user namespaces: {sorted(all_users) or '(none)'}")
    if src_users != tgt_users:
        if src_users - tgt_users:
            print(f"  source-only: {sorted(src_users - tgt_users)}")
        if tgt_users - src_users:
            print(f"  target-only: {sorted(tgt_users - src_users)}")

    # current_user marker cross-check — configuration error, always actionable
    for ref, label in [(source, "source"), (target, "target")]:
        marker = _md_git_show(ref, ".agent/current_user", project_path)
        if marker is not None:
            name = marker.strip()
            if name and name not in all_users:
                actionable.append(
                    f"current_user marker on {label} '{ref}' is '{name}' "
                    f"but no .agent/{name}/ directory exists on either side"
                )
    print()

    # 2. Per-user cross-contamination + per-user marker scan
    user_to_refs: dict[str, list[str]] = {}
    for u in src_users:
        user_to_refs.setdefault(u, []).append(source)
    for u in tgt_users:
        user_to_refs.setdefault(u, []).append(target)

    reported_markers: set[str] = set()  # files whose conflict-marker finding
                                        # is already classified; the global
                                        # stranded scan skips these.
    for user in sorted(all_users):
        user_dir = project_path / ".agent" / user
        if not user_dir.exists():
            continue
        for f in sorted(user_dir.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(project_path).as_posix()
            # Skip untracked files: git can't write a merge result to them and
            # they can't enter the commit, so they can't carry contamination or
            # stranded markers INTO the merge (the field-test FP: an untracked
            # chat_log.md flagged as contamination).
            if not _md_tracked(rel, project_path):
                continue
            try:
                # errors="replace" so a non-UTF-8 (e.g. cp1252) working-tree file
                # is still scanned for contamination/markers, not silently skipped.
                wt_text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            # First: per-user contamination check (always actionable when it
            # fires — runs BEFORE marker classification so contamination on
            # an unmerged file still wins over the "expected marker" bucket).
            wt_lines = _md_nontrivial(wt_text)
            contam_found = False
            if wt_lines:
                self_lines: set[str] = set()
                for ref in user_to_refs.get(user, []):
                    content = _md_git_show(ref, rel, project_path)
                    if content is not None:
                        self_lines |= _md_nontrivial(content)

                parts = rel.split("/", 2)
                rest = parts[2] if len(parts) >= 3 else None
                if rest:
                    for other in all_users - {user}:
                        if contam_found:
                            break
                        for other_ref in user_to_refs.get(other, []):
                            other_rel = f".agent/{other}/{rest}"
                            other_content = _md_git_show(other_ref, other_rel, project_path)
                            if other_content is None:
                                continue
                            other_lines = _md_nontrivial(other_content)
                            foreign = (other_lines & wt_lines) - self_lines
                            foreign_bytes = sum(len(line) for line in foreign)
                            if foreign and foreign_bytes >= _MERGE_DOCTOR_FOREIGN_BYTES_MIN:
                                sample = next(iter(foreign))
                                snippet = sample if len(sample) <= 80 else sample[:77] + "..."
                                actionable.append(
                                    f"contamination: {rel} contains {len(foreign)} line(s) "
                                    f"({foreign_bytes} bytes) from {other_ref}:{other_rel} "
                                    f"— sample: {snippet}"
                                )
                                contam_found = True
                                reported_markers.add(rel)
                                break

            # Then: marker classification. Skip if contamination already
            # claimed the file (it's been added to reported_markers).
            if not contam_found and _md_has_conflict_markers(wt_text):
                if mid_merge and rel in unmerged:
                    expected.append(f"conflict markers in {rel} (active merge surface)")
                elif _md_new_marker_lines(rel, wt_text, merge_parents, project_path):
                    actionable.append(f"stranded conflict markers in {rel}")
                else:
                    informational.append(
                        f"conflict-marker line(s) in {rel} (pre-existing in a parent — likely documentation)")
                reported_markers.add(rel)

    # 3. Global stranded marker scan, deduped against per-user findings.
    # Match git conflict markers at LINE-START only (angle markers, 7 chars) —
    # NOT a bare ======= (markdown) nor a substring in prose. A marker line that
    # already exists in a parent is pre-existing documentation → informational.
    rc, out, _ = _md_git(
        ["grep", "-l", "-E", r"^(<<<<<<<|>>>>>>>)"], project_path
    )
    if rc == 0:
        for line in out.splitlines():
            line = line.strip()
            if not line or line in reported_markers:
                continue
            try:
                cur_text = (project_path / line).read_text(encoding="utf-8", errors="replace")
            except OSError:
                cur_text = ""
            if mid_merge and line in unmerged:
                expected.append(f"conflict markers in {line} (active merge surface)")
            elif _md_new_marker_lines(line, cur_text, merge_parents, project_path):
                actionable.append(f"stranded conflict markers in {line}")
            else:
                informational.append(
                    f"conflict-marker line(s) in {line} (pre-existing in a parent — likely documentation)")

    # 4. Legacy paths under .agent/ — three-way classify (suppress quiet noise)
    # Recursive scan: real installs accumulate detritus deeper than the top
    # level (.agent/cache/x, .agent/legacy/y). The `all_users` guard skips
    # anything inside a per-user namespace — those files were already
    # classified by the contamination scan above.
    # `.agent/current_user` flows through standard classification: tracked →
    # actionable (the install-day bug Step 6 of the skill fixes), ignored →
    # suppressed, else → informational.
    agent_dir = project_path / ".agent"
    if agent_dir.exists():
        for f in sorted(agent_dir.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(project_path).as_posix()
            # Skip per-user-namespace files (handled by contamination scan).
            # rel looks like ".agent/<first>/..." for nested paths.
            parts = rel.split("/", 2)
            if len(parts) >= 2 and parts[1] in all_users:
                continue
            if rel in SHARED_POLICY_PATHS:
                # Deliberately committable: config.json can carry repo-level
                # policy (e.g. merge_verify, which the merge skill runs and
                # which only works if every clone sees it), so a tracked copy
                # is correct rather than legacy detritus. Without this the
                # merge skill's own Step 7(b) gate would fail on any repo that
                # follows its instruction to commit the file.
                continue
            if _md_tracked(rel, project_path):
                actionable.append(
                    f"legacy shared path tracked in git: {rel} "
                    f"(needs `git rm --cached {rel}`)"
                )
            elif _md_ignored(rel, project_path):
                # untracked AND gitignored: guaranteed never to enter a
                # commit. Suppress entirely — this is the .DS_Store /
                # bash_history noise the user explicitly named.
                continue
            else:
                informational.append(
                    f"untracked path outside any user namespace: {rel} "
                    f"(could end up in a commit if `git add` is blind)"
                )

    # Print buckets in priority order
    if actionable:
        print("[ACTIONABLE] — fix before continuing:")
        for item in actionable:
            print(f"  {item}")
        print()
    if expected:
        print("[EXPECTED] — mid-merge surface, Step 5 will resolve:")
        for item in expected:
            print(f"  {item}")
        print()
    if informational:
        print("[INFORMATIONAL] — note, do not block:")
        for item in informational:
            print(f"  {item}")
        print()
    if not (actionable or expected or informational):
        print("(no findings)")
        print()

    # Summary
    verdict = "NEEDS ATTENTION" if actionable else "SAFE TO CONTINUE"
    print(
        f"merge-doctor: {len(actionable)} actionable, "
        f"{len(expected)} expected, "
        f"{len(informational)} informational — {verdict}"
    )
    return len(actionable)
