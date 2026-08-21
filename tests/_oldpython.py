"""Build a PATH whose `python3` reports an interpreter BELOW the 3.10 floor.

Every shell entrypoint must refuse a < 3.10 interpreter with a clear message
instead of letting a 3.10-only construct explode later. Proving that on Linux
means running the real entrypoint with a `python3` on PATH whose version probe
answers "old": the shipped guard is
`python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'`, so
the fake exits 1 for that probe and prints `Python 3.9.18` for `--version`.

This mirrors ``tests/_nopython.py`` (coreutils symlinked in, real python
omitted); here python3 is present but lies about its version. The leading
underscore keeps unittest's ``test*.py`` discovery from collecting it.
"""

from __future__ import annotations

import os
import shutil
import stat
import unittest
from pathlib import Path

# Coreutils the entrypoints touch before/around the floor guard. Generous on
# purpose — a missing coreutil would abort a `set -e` script and confound the
# "python too old" measurement (same rationale as _nopython._NEEDED).
_NEEDED = [
    "bash", "sh", "cat", "grep", "sed", "head", "tail", "find", "dirname",
    "basename", "printf", "echo", "uname", "tr", "awk", "mkdir", "rm", "mv",
    "cp", "ls", "env", "date", "wc", "cut", "sort", "ps", "chmod", "expr",
    "flock", "cygpath", "id", "whoami", "touch", "readlink", "pwd", "test",
]

# A fake `python3`: exit 1 for the >= 3.10 version probe (→ guard fails),
# report an old version for --version, and otherwise no-op cleanly so an
# entrypoint that reaches it after the guard does not crash the harness.
_FAKE_PYTHON3 = """#!/bin/bash
for a in "$@"; do
  case "$a" in
    --version|-V) echo "Python 3.9.18"; exit 0 ;;
  esac
done
case "$*" in
  *version_info*) exit 1 ;;   # the floor probe → simulate < 3.10
esac
exit 0
"""


def make_oldpython_path(base: Path) -> str:
    """Create ``base`` with coreutil symlinks plus a fake python3/python that
    reports 3.9, and return it as a single-entry PATH string. Skips (not fails)
    when the POSIX shim cannot be built — same platform reasons as _nopython."""
    if os.name == "nt":
        raise unittest.SkipTest("old-python3 PATH shim is not portable on native-Windows python")
    base.mkdir(parents=True, exist_ok=True)
    essential = {"bash", "cat", "grep", "sed", "head", "find", "dirname",
                 "printf", "tr", "awk", "mkdir", "rm", "env"}
    for name in _NEEDED:
        real = shutil.which(name)
        if real is None:
            if name in essential:
                raise unittest.SkipTest(f"coreutil {name!r} not on PATH")
            continue
        link = base / name
        if not link.exists():
            try:
                os.symlink(real, link)
            except OSError as e:
                raise unittest.SkipTest(f"cannot symlink coreutils ({e})")
    for pyname in ("python3", "python"):
        fake = base / pyname
        fake.write_text(_FAKE_PYTHON3, encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    real_py = shutil.which("python3", path=str(base))
    if real_py is None or Path(real_py).parent != base:
        raise unittest.SkipTest("could not place a fake python3 first on PATH")
    return str(base)
