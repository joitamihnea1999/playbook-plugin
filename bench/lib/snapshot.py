"""Frozen tree snapshots for judge runs (plan §9.5, revised by the plan-review panel).

The plan said "temporary detached worktree". A worktree at the base sha still
shares the source repo's object database and refs, so a curious judge can run
`git log --all` and read the FUTURE — the fix commits and everything that came
after the review. That is exactly the leakage §20 promises is "structurally
absent". So the bench snapshots with `git archive <sha>` extracted into a temp
dir instead (the `arena/` precedent): no `.git`, no history, no worktree
registry to race on, and `provider.sandbox._git_dir_of` tolerates a non-git
root. Judges lose `git blame/log` inside the snapshot — acceptable for
reviewing a diff, disclosed in bench/README.md.

ONE snapshot per case, shared by every candidate (they review the identical
pre-review tree), removed after the last candidate finishes.
"""
from __future__ import annotations

import io
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path


class SnapshotError(RuntimeError):
    """`git archive` failed (unknown sha, not a repo, git missing)."""


def _safe_members(tf: tarfile.TarFile, dest: Path):
    """Reject members that would land outside `dest` (absolute paths, `..`, links
    pointing out). Python 3.10 has no `filter="data"`, so do it by hand."""
    dest_r = dest.resolve()
    for m in tf.getmembers():
        name = m.name
        if name.startswith("/") or name.startswith("\\") or ".." in Path(name).parts:
            raise SnapshotError(f"refusing unsafe archive member {name!r}")
        if m.issym() or m.islnk():
            target = (dest / Path(name).parent / m.linkname)
            try:
                target.resolve().relative_to(dest_r)
            except ValueError:
                raise SnapshotError(f"refusing link escaping the snapshot: {name!r}") from None
        yield m


def _rmtree(path: Path) -> None:
    def _onerror(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
            func(p)
        except OSError:
            pass
    shutil.rmtree(str(path), onerror=_onerror)


def export_tree(repo: Path, sha: str, dest: Path) -> None:
    """`git archive <sha>` from `repo` extracted into `dest` (created if needed)."""
    repo = Path(repo)
    try:
        proc = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", sha],
                              capture_output=True, timeout=600)
    except FileNotFoundError as exc:
        raise SnapshotError("git not found on PATH") from exc
    except subprocess.SubprocessError as exc:
        raise SnapshotError(f"git archive failed: {exc}") from exc
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise SnapshotError(f"git archive {sha!r} in {repo} failed (exit {proc.returncode}): {err}")
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:") as tf:
        tf.extractall(str(dest), members=_safe_members(tf, dest))


@contextmanager
def snapshot_tree(repo: Path, sha: str, parent_dir=None):
    """Context manager yielding the snapshot root; always removed on exit."""
    tmp = Path(tempfile.mkdtemp(prefix="judgebench-", dir=str(parent_dir) if parent_dir else None))
    try:
        export_tree(repo, sha, tmp)
        yield tmp
    finally:
        _rmtree(tmp)
