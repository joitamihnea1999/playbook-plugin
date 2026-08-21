"""Shared bash resolution for the test suite.

This imports the *canonical* policy from the shared resolver in product code
(``plugins/playbook/tasks/bash_resolver.py``) rather than re-implementing it, so
the tests, the verifier, and the product can never drift — this repo's recurring
defect is two implementations of one policy diverging. There is only one
implementation of "which bash, and is it usable"; every caller shares it. (The
tests once imported this from ``scripts/verify``; the direction is now inverted —
the dev script and the tests both depend on the product resolver, not vice versa.)

On Linux and macOS ``bash`` on PATH is usable, so nothing here ever skips. On
Windows the System32 ``bash.exe`` is the WSL launcher: with no distro installed
it prints an install hint and exits non-zero, so a presence check is not enough
and the shell-dependent tests SKIP with that reason — a missing usable bash is
an environment fact, not a product defect. CI exports ``$PLAYBOOK_VERIFY_BASH``
(from its Git Bash step) to point Python at the real bash; ``resolve_bash()``
honours it — as the documented fallback to the product-level ``$PLAYBOOK_BASH``.

The leading underscore keeps unittest's ``test*.py`` discovery from collecting
this module as a test.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_RESOLVER = _ROOT / "plugins" / "playbook" / "tasks" / "bash_resolver.py"

# bash_resolver.py is stdlib-only, so path-load it directly (tests need no
# `tasks` package on sys.path for this, and must not depend on the dev script).
_spec = importlib.util.spec_from_file_location("_pb_bash_resolver", _RESOLVER)
assert _spec is not None and _spec.loader is not None
_resolver = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_resolver)

resolve_bash = _resolver.resolve_bash
bash_usable = _resolver.bash_usable

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
