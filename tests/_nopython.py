"""Build a PATH that has the shell coreutils a hook needs but NO python3/python.

Several enforcing/advisory hooks must fail OPEN (loudly) when python3 is absent
— "the guard could not run" => allow. Proving that on Linux means running the
real bash hook with a PATH that resolves `bash`, `find`, `grep`, … but not
`python3`. This helper symlinks the real coreutils into a temp dir (resolving
each with shutil.which so a shell alias can't produce a broken relative link)
and deliberately omits python3/python.

The leading underscore keeps unittest's ``test*.py`` discovery from collecting
this module as a test.
"""

from __future__ import annotations

import os
import shutil
import unittest
from pathlib import Path

# The union of coreutils the hooks under test invoke. Generous on purpose: a
# missing coreutil would itself abort a `set -e` hook and confound the "python
# missing" measurement (the exact trap the audit hit with a broken `find`).
_NEEDED = [
    "bash", "sh", "cat", "grep", "sed", "head", "tail", "find", "dirname",
    "basename", "printf", "echo", "uname", "tr", "awk", "mkdir", "rm", "mv",
    "cp", "ls", "env", "date", "wc", "cut", "sort", "ps", "chmod", "expr",
    "ln", "flock", "cygpath",
]


def make_nopython_path(base: Path) -> str:
    """Create ``base`` populated with symlinks to real coreutils (no python3),
    and return it as a single-entry PATH string. Skips the test if any core
    binary the hooks always need is itself unavailable (an environment fact).

    Windows/Git-Bash runs a NATIVE-Windows python where os.symlink needs
    Developer Mode / admin, coreutils resolve to Windows-mount paths, and PATH
    scoping across the MSYS boundary is unreliable — a python3-free PATH cannot
    be built portably there. These are Linux-measured audit items; skip on
    Windows rather than assert a false red. The product fix itself is
    cross-platform; only this PATH-manipulation harness is POSIX-only."""
    if os.name == "nt":
        raise unittest.SkipTest("python3-free PATH not portable on native-Windows python")
    base.mkdir(parents=True, exist_ok=True)
    essential = {"bash", "cat", "grep", "sed", "head", "find", "dirname",
                 "printf", "tr", "awk", "mkdir", "rm"}
    for name in _NEEDED:
        real = shutil.which(name)
        if real is None:
            if name in essential:
                raise unittest.SkipTest(f"coreutil {name!r} not on PATH")
            continue  # optional (flock/cygpath/ps): fine to omit
        link = base / name
        if not link.exists():
            try:
                os.symlink(real, link)
            except OSError as e:
                raise unittest.SkipTest(f"cannot symlink coreutils ({e})")
    if shutil.which("python3", path=str(base)) is not None:
        raise unittest.SkipTest("could not build a python3-free PATH")
    return str(base)
