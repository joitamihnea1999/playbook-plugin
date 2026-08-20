"""Shared bash resolution for the test suite.

This imports the *canonical* policy from ``scripts/verify`` rather than
re-implementing it, so the tests and the verifier can never drift — this repo's
recurring defect is two implementations of one policy diverging. There is only
one implementation of "which bash, and is it usable"; both callers share it.

On Linux and macOS ``bash`` on PATH is usable, so nothing here ever skips. On
Windows the System32 ``bash.exe`` is the WSL launcher: with no distro installed
it prints an install hint and exits non-zero, so a presence check is not enough
and the shell-dependent tests SKIP with that reason — a missing usable bash is
an environment fact, not a product defect. CI exports ``$PLAYBOOK_VERIFY_BASH``
(from its Git Bash step) to point Python at the real bash; ``resolve_bash()``
honours it, exactly as ``scripts/verify`` does.

The leading underscore keeps unittest's ``test*.py`` discovery from collecting
this module as a test.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_VERIFY = _ROOT / "scripts" / "verify"

# scripts/verify has no .py suffix, so force the source loader. Importing it only
# registers @check-decorated functions (they append to a list — no I/O, no
# subprocess); main() is guarded by `if __name__ == "__main__"`, so nothing runs.
_loader = importlib.machinery.SourceFileLoader("_pb_verify", str(_VERIFY))
_spec = importlib.util.spec_from_loader("_pb_verify", _loader)
assert _spec is not None
_verify = importlib.util.module_from_spec(_spec)
_loader.exec_module(_verify)

resolve_bash = _verify.resolve_bash
bash_usable = _verify.bash_usable

_probe: "tuple[str | None, bool, str] | None" = None


def bash_probe() -> "tuple[str | None, bool, str]":
    """``(path_or_None, usable, note)`` — resolved once per process, using the
    exact same steps as ``scripts/verify.main()``."""
    global _probe
    if _probe is None:
        path, how = resolve_bash()
        if path is None:
            _probe = (None, False, how)
        else:
            usable, why = bash_usable(path)
            _probe = (path, usable, how if usable else f"{how}: {why}")
    return _probe


def bash_or_skip() -> str:
    """Return a usable bash path, or raise ``SkipTest`` with the environment
    reason. Never skips on Linux or macOS, where ``bash`` on PATH is usable."""
    path, usable, note = bash_probe()
    if not usable or path is None:
        raise unittest.SkipTest(f"no usable bash: {note}")
    return path
