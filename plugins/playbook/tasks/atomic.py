"""The one atomic-write primitive for the tasks package.

Every consequence-relevant file write (task.md, models.json, hook config,
counters) should route through `atomic_write` rather than a plain
`write_text`/`open(..., "w")`. A plain writer truncates then writes, so a
concurrent reader — a second user, a hook, `tasks status` — can observe an
EMPTY or half-written file in the window between truncate and write, and a
crash mid-write leaves the file permanently truncated.

`atomic_write` closes both: it writes a temp file IN THE SAME DIRECTORY as the
target (so the final `os.replace` is a rename within one filesystem, which is
atomic on POSIX and on Windows), fsyncs the temp before the rename when durable
consequence warrants it, preserves the target's permission bits across the
rename, and unlinks the temp on any failure so an interrupt never litters
`.tmp` files.

Why `os.replace` and not `Path.rename`: `rename` onto an existing target raises
FileExistsError (WinError 183) on Windows; `os.replace` overwrites atomically
on every platform. Windows adds one real caveat — it cannot replace a file
another handle holds open without FILE_SHARE_DELETE, which Python does not set,
so concurrent read+write of the SAME path is not torn-read-safe there the way
it is on POSIX. That is a platform bound, not a defect in this primitive; the
CI windows lane is the proof of the `os.replace` semantics we do guarantee.

Stdlib only; no dependency on any other tasks module, so every module
(including tasks.core) can import it without a cycle.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Union


def atomic_write(
    path: Union[str, "os.PathLike[str]"],
    data: Union[str, bytes, bytearray],
    *,
    encoding: str = "utf-8",
    newline: "str | None" = None,
    fsync: bool = True,
) -> None:
    """Write `data` to `path` all-or-nothing via a same-directory temp + os.replace.

    `data` may be text (written with `encoding`/`newline`) or bytes (written
    verbatim; `encoding`/`newline` are then ignored). `newline=""` disables
    newline translation, preserving a CRLF file byte-for-byte — the rest of the
    tasks package uses the default (`None`, i.e. `\\n` → `os.linesep` on write).

    `fsync=True` (default) flushes and fsyncs the temp file before the rename so
    the bytes are durable across a power loss, not merely a process crash; the
    parent directory is fsynced best-effort on POSIX so the rename itself
    survives too. Pass `fsync=False` only for a hot path where the atomicity
    guarantee matters but power-loss durability does not.

    Permission bits: if `path` already exists, its mode is preserved across the
    replace (a plain mkstemp temp is 0600, which would otherwise silently strip
    group/other read from a shared config on every rewrite); a brand-new file
    gets the normal umask-masked mode, never mkstemp's private 0600.

    On any failure the temp file is removed before the exception propagates, so
    an interrupt between write and replace leaves neither a torn target nor a
    stray temp.
    """
    p = Path(path)
    parent = p.parent
    is_bytes = isinstance(data, (bytes, bytearray))

    # Capture the target's current mode BEFORE we create the temp, so a rewrite
    # preserves it (see docstring). None means "new file".
    try:
        prev_mode: "int | None" = os.stat(p).st_mode & 0o777
    except OSError:
        prev_mode = None

    fd, tmp = tempfile.mkstemp(dir=str(parent), prefix=p.name + ".", suffix=".tmp")
    try:
        if is_bytes:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                if fsync:
                    os.fsync(fh.fileno())
        else:
            with os.fdopen(fd, "w", encoding=encoding, newline=newline) as fh:
                fh.write(data)
                fh.flush()
                if fsync:
                    os.fsync(fh.fileno())

        if prev_mode is not None:
            os.chmod(tmp, prev_mode)
        else:
            _umask = os.umask(0)
            os.umask(_umask)
            os.chmod(tmp, 0o666 & ~_umask)

        os.replace(tmp, str(p))

        if fsync:
            # Best-effort: fsync the directory so the rename entry itself is
            # durable. Not portable (Windows has no directory fd to fsync, and
            # some POSIX filesystems reject O_DIRECTORY fsync) — a failure here
            # never invalidates the already-completed atomic replace.
            try:
                dir_fd = os.open(str(parent), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
    except BaseException:
        # BaseException (not just OSError/Exception): a KeyboardInterrupt landing
        # between the write and the replace is exactly the interruption this
        # primitive exists to survive — clean up the temp before re-raising.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
