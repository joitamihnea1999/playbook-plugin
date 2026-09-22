"""One portable advisory lock for a task's records (task 058).

Every task.md / judge.md writer is atomic per WRITE (`tasks.atomic`), but a
read-transform-write is not atomic as a TRANSACTION. Measured on this repo: the
real close shape — read task.md, run the verify contract for minutes, then write
the receipt — lost a concurrent `tasks blocked` in **5 of 5** trials; the task
ended `in_progress` with no `## Blocked` section at all, so the honest-pause
record simply vanished. The same window sits under the panel's F0 stamp (a judge
call long), the judge/receipt/audit stackers, and handoff/resume.

Design decisions, each load-bearing and each the answer to a plan-panel finding:

* **Capability probe, not `sys.platform`.** `fcntl` where it imports, else
  `msvcrt`, else no backend. Git-Bash on Windows runs the Windows CPython, which
  has `msvcrt` and no `fcntl`; `selected_backend()` is public so a test can
  ASSERT which branch a CI lane actually exercised rather than assume it.
* **Never wedge the CLI.** The wait is bounded and raises `LockTimeout` naming
  the holder; a platform with neither backend proceeds after ONE loud stderr
  advisory. A tool that cannot run is worse than a race.
* **The lock file is persistent.** Created once, never unlinked on release: an
  unlinked inode a waiter still holds, plus a fresh one a third process locks,
  is two simultaneous "owners".
* **No stale-lock stealing.** Both backends are released by the OS when the
  holder dies (`kill -9` included), so there is nothing to reclaim; a pid-based
  steal would race pid reuse and let two writers proceed — reintroducing the
  very lost update this module exists to stop. The pid in the lock file is for
  the timeout MESSAGE only.
* **One fd per resolved path per process, refcounted.** A second `flock` on a
  fresh fd deadlocks on BSD/macOS and a second `msvcrt.locking` on the same byte
  range fails, so nesting locks only on the 0→1 transition — which is what lets
  a caller compose several `rewrite` calls into ONE transaction.
* **Readers never lock.** `tasks status`, the gate hook and the state-echo hook
  read task.md on every tool call; making them wait on a minutes-long close
  would stall the session. `atomic_write` already guarantees they never see a
  torn file.

Stdlib only. Imports nothing from the tasks package, so any module can use it.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

LOCK_SUFFIX = ".lock"
DEFAULT_TIMEOUT = 30.0
_POLL_SECS = 0.05


class LockTimeout(RuntimeError):
    """Raised when the bounded wait for a task lock expires."""


def _probe_backend() -> str:
    try:
        import fcntl  # noqa: F401
        return "fcntl"
    except Exception:       # noqa: BLE001 — absence is the signal, whatever the reason
        pass
    try:
        import msvcrt  # noqa: F401
        return "msvcrt"
    except Exception:       # noqa: BLE001
        pass
    return "none"


_BACKEND = _probe_backend()
_ADVISED = False                      # the no-backend advisory is printed once
_STATE_LOCK = threading.RLock()       # guards _HOLDERS across threads
_HOLDERS: "dict[str, list]" = {}      # resolved lock path -> [fd, depth]


def selected_backend() -> str:
    """`"fcntl"`, `"msvcrt"` or `"none"` — which branch this interpreter uses."""
    return _BACKEND


def lock_path_for(path) -> Path:
    """The lock file that guards a task's records: ONE per task DIRECTORY.

    Not per filename (058 impl panel r1, codex-high #5): the close reads
    judge.md for its evidence while writing task.md, so a per-file lock would let
    a panel replace the newest verdict between the two. One directory, one lock;
    two different tasks never contend.
    """
    p = Path(path)
    base = p if p.is_dir() else p.parent
    return base / ("task.md" + LOCK_SUFFIX)


def _key_for(path) -> str:
    """The holder-table key. Derived IDENTICALLY here and in `task_lock` (058
    impl panel r1, opus F1 / sonnet #1: one side resolved the path and the other
    did not, so on macOS — where a temp dir resolves `/var` → `/private/var` —
    the lookup always missed and every "the lock was released" assertion in the
    tests was vacuous)."""
    lp = lock_path_for(path)
    try:
        return str(lp.resolve())
    except (OSError, RuntimeError, ValueError):
        return str(lp)


def _depth_for(path) -> int:
    """Current re-entrancy depth for `path` in THIS process (tests read it to
    prove the lock was released on every exit path)."""
    with _STATE_LOCK:
        held = _HOLDERS.get(_key_for(path))
        return held[1] if held else 0


def _try_lock(fd: int) -> bool:
    """One NON-BLOCKING attempt. True when this process now holds the lock."""
    if _BACKEND == "fcntl":
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    if _BACKEND == "msvcrt":
        import msvcrt
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    return True                        # no backend: the caller was already advised


def _unlock(fd: int) -> None:
    if _BACKEND == "fcntl":
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
    elif _BACKEND == "msvcrt":
        import msvcrt
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass


def _holder_pid(lock_file: Path) -> str:
    """The pid recorded by the current holder — for the TIMEOUT MESSAGE only.
    Never used to decide whether a lock may be taken (see the module docstring)."""
    try:
        txt = lock_file.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return "unknown"
    return txt.splitlines()[0].strip() if txt else "unknown"


@contextmanager
def named_lock(lock_file, *, timeout: float = DEFAULT_TIMEOUT):
    """`task_lock` on an EXPLICIT lock file — for a resource that already has a
    lock protocol of its own. The only caller today is `tasks tag`, which must
    rendezvous on the lock file the shell `chat-log-hook` uses for its counter
    (`<agent>/chat_log_counter.lock`) rather than on a task directory's lock."""
    with _lock_on(Path(lock_file), timeout=timeout):
        yield


@contextmanager
def task_lock(path, *, timeout: float = DEFAULT_TIMEOUT):
    """Serialize the read-transform-write transactions on `path`'s records.

    Re-entrant within one process, so a caller may hold this across several
    `atomic.rewrite` calls and have them land as ONE transaction. Raises
    `LockTimeout` (naming the holder pid) when the bounded wait expires. On a
    platform with no backend it yields after one loud advisory — the write still
    happens, unserialized, and the operator is told.
    """
    with _lock_on(lock_path_for(path), timeout=timeout):
        yield


@contextmanager
def _lock_on(lock_file: Path, *, timeout: float = DEFAULT_TIMEOUT):
    """The lock itself, on an exact lock-file path."""
    global _ADVISED
    try:
        lock_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    try:
        key = str(lock_file.resolve())
    except (OSError, RuntimeError, ValueError):
        key = str(lock_file)

    if _BACKEND == "none":
        if not _ADVISED:
            _ADVISED = True
            print("[playbook] this platform offers no file-locking backend "
                  "(no fcntl, no msvcrt) — task records are written WITHOUT LOCKING; "
                  "avoid running two sessions against one task at the same time.",
                  file=sys.stderr, flush=True)
        yield
        return

    me = threading.get_ident()
    with _STATE_LOCK:
        held = _HOLDERS.get(key)
        if held is not None and held[2] == me:     # ours, same THREAD: nest
            held[1] += 1
            nested = True
        elif held is not None:
            # Another thread in this process holds it. Re-entrancy belongs to the
            # owner (058 impl panel r1, codex-high #4): a second thread used to
            # walk straight into the protected region. It must wait like any
            # other contender — and the OS lock is already held by this process,
            # so waiting means waiting on the in-process holder.
            nested = False
        else:
            nested = False
    if nested:
        try:
            yield
        finally:
            with _STATE_LOCK:
                held = _HOLDERS.get(key)
                if held is not None:
                    held[1] -= 1
        return

    # The lock file is opened O_RDWR|O_CREAT and NEVER truncated: truncation
    # would race a concurrent holder's pid line, and unlinking would split the
    # inode. It is only ever rewritten in place by the holder.
    fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + max(0.0, float(timeout))
    try:
        while True:
            with _STATE_LOCK:
                other_thread_holds = key in _HOLDERS      # a sibling thread owns it
            if not other_thread_holds and _try_lock(fd):
                break
            if time.monotonic() >= deadline:
                raise LockTimeout(
                    f"another playbook holder (pid {_holder_pid(lock_file)}) has held "
                    f"{lock_file.name} for more than {timeout:g}s — it is probably mid-close "
                    f"or mid-panel. Wait for it, or check with `tasks status`.")
            time.sleep(_POLL_SECS)
    except BaseException:
        os.close(fd)
        raise

    try:
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"{os.getpid()}\n".encode("ascii", "replace"))
    except OSError:
        pass                                        # the pid line is a courtesy, not the lock
    with _STATE_LOCK:
        _HOLDERS[key] = [fd, 1, me]
    try:
        yield
    finally:
        with _STATE_LOCK:
            held = _HOLDERS.get(key)
            if held is not None:
                held[1] -= 1
                if held[1] <= 0:
                    _HOLDERS.pop(key, None)
                    _unlock(fd)
                    try:
                        os.close(fd)
                    except OSError:
                        pass
