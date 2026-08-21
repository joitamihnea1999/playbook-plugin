"""THE single "which bash, and is it usable" resolver. Stdlib-only leaf module.

One policy, four consumers: the audit sweeper (`tasks/audit.py`), the merge
skill's verify runner (`skills/merge/merge-verify.py`), the verification
entrypoint (`scripts/verify`), and the test suite (`tests/_bashcheck.py`). Two
implementations of this one policy drifting apart is this repo's recurring
defect, and the guarantee ledger forbids a competing semantic copy without a
parity test — so it lives here, ONCE, and every caller imports or path-loads it.

It sits in *product* code, not the dev verifier. audit and merge-verify ship to
users; product code must not depend on `scripts/verify` (a dev script), which was
the old, backwards direction. `scripts/verify` and the tests now depend on THIS.

Why a leaf module and not `tasks/shared.py` (the obvious home): `merge-verify.py`
must run standalone — it is invoked as `python3 <skill-dir>/merge-verify.py` from
an arbitrary repo with no `tasks` package on `sys.path`, and is also path-loaded
by the close gate — so it can only reach this policy by loading a file relative
to itself. `tasks/shared.py` does `from tasks.core import ...`, so it cannot be
executed in isolation; this module imports the stdlib only, so it can be
path-loaded from anywhere.

Override precedence, highest first:

    $PLAYBOOK_BASH          the product-level knob — the variable a real user (or
                            a Windows shell) sets to name the bash to use.
    $PLAYBOOK_VERIFY_BASH   the historical dev-harness variable, honoured ONLY as
                            a documented fallback so existing CI — which exports
                            it from its Git Bash step — keeps working unchanged.
    bash on PATH            otherwise.

A presence check is NOT enough. On Windows a bare `bash` on PATH is usually the
System32 WSL launcher: with no distro it prints an install hint and exits
non-zero (and a stub could even exit zero). So the resolver PROBES the chosen
bash with a sentinel — it must run `printf ok` and print exactly `ok`; any other
outcome is "unusable" and the resolver fails closed.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

BASH_ENV_VAR = "PLAYBOOK_BASH"                   # product-level override
BASH_FALLBACK_ENV_VAR = "PLAYBOOK_VERIFY_BASH"   # documented dev/CI fallback

# Dogfooding vars that would corrupt the sentinel probe: a BASH_ENV logger sourced
# at startup can print to stdout, so `printf ok` no longer prints exactly `ok`.
# Stripped only by the split-form probe (bash_usable), which the verifier and the
# test suite use; the combined form leaves the environment untouched, preserving
# the historical behaviour of audit.py / merge-verify.py to the byte.
_DOGFOOD_VARS = ("BASH_ENV", "PLAYBOOK_SESSION_ID", "PLAYBOOK_ROLE", "PLAYBOOK_EVAL_CONFIG")

# Resolved once per process for the combined form (audit / merge-verify), which
# probe bash repeatedly. The split form is cheap and re-resolves on demand.
_RESOLVED_BASH: "tuple[str | None, str] | None" = None


def _candidate() -> "tuple[str | None, str | None]":
    """The bash to try and the env var it came from (or None when from PATH).

    Honour $PLAYBOOK_BASH, then the documented $PLAYBOOK_VERIFY_BASH fallback,
    else `bash` on PATH. A `cygpath -w` conversion can drop the `.exe`; recover
    it so CreateProcess execs the real binary rather than falling through to a
    spurious "not usable".
    """
    for name in (BASH_ENV_VAR, BASH_FALLBACK_ENV_VAR):
        val = os.environ.get(name)
        if val:
            if not Path(val).exists() and Path(val + ".exe").exists():
                val += ".exe"
            return val, name
    return shutil.which("bash"), None


def _clean_env() -> dict:
    """os.environ minus the dogfooding vars — a clean environment to probe in."""
    e = dict(os.environ)
    for k in _DOGFOOD_VARS:
        e.pop(k, None)
    return e


def _probe(path: str, env: "dict | None" = None) -> "tuple[int, bytes]":
    """Run the sentinel `printf ok` under `path`. Returns (rc, raw_stdout).

    Bytes, not text: a Windows stub can emit odd encodings, and callers compare
    the stripped bytes to `b"ok"`. `env=None` inherits the parent environment.
    Raises OSError / subprocess.SubprocessError to the caller.
    """
    p = subprocess.run([path, "-c", "printf ok"], env=env, timeout=30,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.returncode, (p.stdout or b"")


def usable_bash() -> "tuple[str | None, str]":
    """(path_or_None, reason) — combined resolve + probe, cached per process.

    THE form audit.py and merge-verify.py use. On success `(path, "")`; on
    failure `(None, reason)`, and the caller must fail closed — an unusable bash
    is never silently treated as usable. The reason strings are unchanged from
    the two `_usable_bash()` copies this replaces. The probe runs in the inherited
    environment, matching those copies exactly.
    """
    global _RESOLVED_BASH
    if _RESOLVED_BASH is not None:
        return _RESOLVED_BASH
    candidate, _ = _candidate()
    if not candidate:
        _RESOLVED_BASH = (None, "no bash found on PATH")
        return _RESOLVED_BASH
    try:
        rc, raw = _probe(candidate)
    except (OSError, subprocess.SubprocessError) as exc:
        _RESOLVED_BASH = (None, f"{type(exc).__name__}: {exc}")
        return _RESOLVED_BASH
    if rc == 0 and raw.strip() == b"ok":
        _RESOLVED_BASH = (candidate, "")
    else:
        _RESOLVED_BASH = (None, f"bash at {candidate} is not usable (rc={rc}) "
                                "— likely the Windows WSL stub")
    return _RESOLVED_BASH


def resolve_bash() -> "tuple[str | None, str]":
    """(path_or_None, how) — candidate selection only, no probe.

    The split-form step-1 used by `scripts/verify` and the test suite, which then
    call `bash_usable()` and format `how` into a human note. `how` names the
    override variable actually used, so the existing $PLAYBOOK_VERIFY_BASH path
    prints exactly as before.
    """
    candidate, src = _candidate()
    if src is not None:
        return candidate, f"${src}"
    return candidate, ("PATH" if candidate else "PATH (not found)")


def bash_usable(path: str, env: "dict | None" = None) -> "tuple[bool, str]":
    """(usable, why) — probe `path` for the sentinel. Split-form step-2.

    Used by `scripts/verify` and the test suite. The probe runs in a
    dogfood-stripped environment by default (env=None), matching the verifier's
    historical `env()` hygiene so a sourced BASH_ENV logger cannot forge the
    result; pass an explicit `env` to override. `why` is empty on success and a
    short `rc=N: <last output line>` on failure.
    """
    e = _clean_env() if env is None else env
    try:
        rc, raw = _probe(path, e)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    out = raw.decode("utf-8", "replace").replace("\x00", "")
    if rc == 0 and out.strip() == "ok":
        return True, ""
    lines = [ln.strip() for ln in out.strip().splitlines() if ln.strip()]
    detail = lines[-1][:220] if lines else "(no output)"
    return False, f"rc={rc}: {detail}"
