#!/usr/bin/env python3
"""Transactional snapshot + rollback for `scripts/init` (PB-INSTALL-ROLLBACK).

`scripts/init` mutates project files (CLAUDE.md, .gitignore, .claude/settings.json,
the .claude/bin wrappers, the monitor scaffold, .agent/config.json, MIND_MAP.md)
and user files (~/.claude/bash-log.*, the shell rc file, ~/.claude/settings.json).
Before this helper existed, a failure partway through left a half-provisioned,
possibly mixed-version install and could have clobbered a hand-written CLAUDE.md,
.gitignore, or shell rc file with no way back.

This helper makes init all-or-nothing:

  * `begin` — called before init's first mutation. Snapshots every file init may
    modify (byte-for-byte, with its permission bits) into a timestamped backup
    dir, and records which candidate dirs already existed. Project files back up
    under `<project>/.agent/backups/init-<stamp>/`, user files under
    `<home>/.playbook-init-backups/init-<stamp>/` — the two realms the task
    splits. Prints the manifest path for init to hand back to `restore`.

  * `restore` — called from init's EXIT trap on any non-zero exit (a `set -e`
    abort, the soft FAILED summary, or a trapped interrupt). Restores every
    pre-existing file byte-identically, removes files init created that were
    absent before, and removes now-empty dirs init created. The backup itself is
    kept — it is the forensic/undo record — so a freshly-created `.agent/` may
    survive a rollback holding only `backups/`.

Inputs to `begin` arrive via environment variables, never argv: a project path
containing a quote or a space must not be able to break anything (the same I4
discipline the rest of init already follows).

Stdlib only. The atomic-write primitive lives in the tasks package
(`tasks/atomic.py`, sibling of this `scripts/` dir); this standalone script is
not run with the tasks package importable, so it path-loads that one stdlib-only
file — the exact idiom `claude-md-merge.py` uses, keeping ONE primitive.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

_ATOMIC_PATH = Path(__file__).resolve().parent.parent / "tasks" / "atomic.py"
_atomic_spec = importlib.util.spec_from_file_location("_pb_atomic_write", _ATOMIC_PATH)
_atomic_mod = importlib.util.module_from_spec(_atomic_spec)
_atomic_spec.loader.exec_module(_atomic_mod)
atomic_write = _atomic_mod.atomic_write


def _split_env_list(name: str) -> "list[str]":
    """Newline-separated, non-blank entries from an env var (empty if unset)."""
    return [line for line in os.environ.get(name, "").split("\n") if line.strip()]


def _realm(path: str, project_root: str) -> str:
    """'project' if *path* is inside project_root, else 'user'."""
    try:
        if os.path.commonpath([path, project_root]) == project_root:
            return "project"
    except ValueError:  # different drives on Windows
        pass
    return "user"


def _rel_within(path: str, base: str) -> str:
    """Path of *path* relative to *base*, or its basename if it escapes base."""
    try:
        rel = os.path.relpath(path, base)
    except ValueError:
        return os.path.basename(path)
    if rel.startswith(".."):
        return os.path.basename(path)
    return rel


def begin() -> int:
    project_root = os.path.abspath(os.environ["PB_TXN_PROJECT_ROOT"])
    home = os.path.abspath(os.environ["PB_TXN_HOME"])
    stamp = os.environ["PB_TXN_STAMP"]
    files = [os.path.abspath(f) for f in _split_env_list("PB_TXN_FILES")]
    dirs = [os.path.abspath(d) for d in _split_env_list("PB_TXN_DIRS")]

    project_backup = os.path.join(project_root, ".agent", "backups", "init-" + stamp)
    user_backup = os.path.join(home, ".playbook-init-backups", "init-" + stamp)

    # Record dir pre-existence BEFORE creating any backup dir: `begin` itself
    # creates .agent/ and ~/.playbook-init-backups/, and rollback must not then
    # mistake a dir it created for one that pre-existed.
    dir_entries = [
        {"path": d, "realm": _realm(d, project_root), "existed": os.path.isdir(d)}
        for d in dirs
    ]
    file_entries = [
        {
            "path": f,
            "realm": _realm(f, project_root),
            "existed": os.path.isfile(f),
            "backup": None,
            "mode": None,
        }
        for f in files
    ]

    created_roots: "list[str]" = []
    try:
        for root in (project_backup, user_backup):
            if not os.path.isdir(root):
                os.makedirs(root, exist_ok=True)
                created_roots.append(root)

        for e in file_entries:
            if not e["existed"]:
                continue
            base = project_root if e["realm"] == "project" else home
            realm_root = project_backup if e["realm"] == "project" else user_backup
            dest = os.path.join(realm_root, "tree", _rel_within(e["path"], base))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(e["path"], dest)  # verbatim bytes + permission bits
            e["backup"] = dest
            e["mode"] = os.stat(e["path"]).st_mode & 0o777

        manifest = {
            "stamp": stamp,
            "project_root": project_root,
            "home": home,
            "project_backup": project_backup,
            "user_backup": user_backup,
            "files": file_entries,
            "dirs": dir_entries,
        }
        manifest_path = os.path.join(project_backup, "manifest.json")
        atomic_write(manifest_path, json.dumps(manifest, indent=2) + "\n")
    except OSError as exc:
        # Snapshot failed → init must abort before mutating anything. Clean up
        # any backup roots we just made so a failed snapshot leaves no litter.
        for root in reversed(created_roots):
            shutil.rmtree(root, ignore_errors=True)
        print(f"ERROR:snapshot failed: {exc}", file=sys.stderr)
        return 1

    print("MANIFEST:" + manifest_path)
    print("PROJECT_BACKUP:" + project_backup)
    print("USER_BACKUP:" + user_backup)
    return 0


def _manifest_path_from_argv() -> "str | None":
    argv = sys.argv[2:]
    if "--manifest" in argv:
        i = argv.index("--manifest")
        if i + 1 < len(argv):
            return argv[i + 1]
    return os.environ.get("PB_TXN_MANIFEST")


def restore() -> int:
    manifest_path = _manifest_path_from_argv()
    if not manifest_path or not os.path.isfile(manifest_path):
        print(f"ERROR:manifest not found: {manifest_path!r}", file=sys.stderr)
        return 2
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)

    restored: "list[str]" = []
    removed: "list[str]" = []
    errors: "list[str]" = []

    for e in manifest.get("files", []):
        path = e["path"]
        if e["existed"]:
            backup = e.get("backup")
            if not backup or not os.path.isfile(backup):
                errors.append(f"backup missing for {path}")
                continue
            try:
                data = Path(backup).read_bytes()
                # Already original (init never got to modify it, e.g. a read-only
                # dir blocked the write) → leave it untouched. This keeps restore
                # idempotent and avoids a needless write into a dir that may now
                # be read-only, which would report a false rollback error.
                if os.path.isfile(path) and Path(path).read_bytes() == data:
                    restored.append(path)
                    continue
                os.makedirs(os.path.dirname(path), exist_ok=True)
                atomic_write(path, data)  # byte-identical, all-or-nothing
                if e.get("mode") is not None:
                    os.chmod(path, e["mode"])
                restored.append(path)
            except OSError as exc:
                errors.append(f"could not restore {path}: {exc}")
        else:
            # Init created this file where none existed — remove it. A directory
            # sitting at the path was not init's file (see the CLAUDE.md-is-a-dir
            # vector); leave it alone.
            try:
                if os.path.islink(path) or os.path.isfile(path):
                    os.unlink(path)
                    removed.append(path)
            except OSError as exc:
                errors.append(f"could not remove {path}: {exc}")

    # Remove now-empty dirs init created, deepest first. os.rmdir only succeeds
    # on an empty dir, so a dir still holding the backup (e.g. .agent/) is kept.
    for d in sorted(manifest.get("dirs", []), key=lambda d: len(d["path"]), reverse=True):
        if d["existed"]:
            continue
        p = d["path"]
        if os.path.isdir(p):
            try:
                os.rmdir(p)
                removed.append(p)
            except OSError:
                pass  # non-empty (holds the backup or user content) → keep

    print(f"ROLLBACK: restored {len(restored)} file(s), removed {len(removed)} created path(s)")
    for p in restored:
        print("  restored: " + p)
    for p in removed:
        print("  removed:  " + p)
    for msg in errors:
        print("  ERROR:    " + msg, file=sys.stderr)
    return 1 if errors else 0


def main(argv: "list[str]") -> int:
    if len(argv) < 2 or argv[1] not in ("begin", "restore"):
        print("ERROR:usage: init_txn.py begin | restore --manifest <path>", file=sys.stderr)
        return 2
    return begin() if argv[1] == "begin" else restore()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
