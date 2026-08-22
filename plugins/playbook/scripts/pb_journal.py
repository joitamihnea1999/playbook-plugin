#!/usr/bin/env python3
"""Best-effort, append-only enforcement-event journal (log-only).

Branch feat/enforcement-journal — an OWNER-APPROVED feature-freeze exception
(decision 2026-08-21). One JSON object per enforcement DECISION, appended to the
lane-resolved `.agent/<lane>/journal/enforcement.jsonl` (root lane:
`.agent/journal/enforcement.jsonl`). No reader tooling ships here — that is
deferred to 1.6; this module only writes.

HARD CONTRACT — a journal failure must NEVER change an enforcement decision:
  * every public function swallows ALL errors (a one-line stderr warning at
    most). Callers additionally wrap the call, so this is defence in depth.
  * one O_APPEND write per record: no fsync, no locking, no read-modify-write.
    A record is emitted with a single os.write() of one line. POSIX guarantees
    that a write of at most PIPE_BUF bytes to a regular file opened O_APPEND is
    atomic against concurrent appenders (PIPE_BUF is >= 512 everywhere and 4096
    on Linux), so lines from separate hook processes do not interleave. This
    holds only while a record stays under PIPE_BUF; the command/path fields are
    capped (see _head) which keeps real records well under 512 bytes, but a
    pathological path could exceed it — an honest, accepted bound for a
    best-effort log, never a correctness risk (the decision was already made).
  * writes ONLY when the resolved lane dir already exists (the project is
    playbook-managed). The `journal/` subdir is created inside that existing
    lane; `.agent` itself is NEVER created as a side effect.

Stdlib only. Python 3.10+ floor (same as the rest of the plugin).
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
from pathlib import Path

# Same username grammar as gate-echo-lib.sh::resolve_agent_dir and
# provider/paths.validate_username — kept deliberately in lockstep.
_USER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

_HEAD_LIMIT = 200


def resolve_lane_dir(project_root) -> "Path | None":
    """Resolve the `.agent` lane dir WITHOUT creating anything.

    Multi-user: a valid `.agent/current_user` → `.agent/<name>`.
    Legacy / absent / unvalidatable marker → the root `.agent`.

    Fresh-clone shape (per-user lanes exist, no marker, no root `.agent/tasks`)
    → None: the true lane is unresolvable and answering the root would mint
    phantom root-lane state, exactly the leak the enforcing surfaces refuse
    (see gate-echo-lib.sh::lanes_without_marker and the S15 fixture). A
    best-effort log is never worth that, so skip.

    This is a best-effort logger, never an authority: where the enforcing
    resolvers would *raise* on an unreadable/invalid marker, here we fall back
    to the root lane so a decision still gets logged somewhere sane. Returns
    None on the fresh-clone shape or an unexpected internal error.
    """
    try:
        agent = Path(project_root) / ".agent"
        marker = agent / "current_user"
        if not marker.exists() and not (agent / "tasks").is_dir() and agent.is_dir():
            # No marker and no root lane: if any child lane has tasks/, this is a
            # fresh clone — refuse rather than write the shared root.
            for child in agent.iterdir():
                if (child / "tasks").is_dir():
                    return None
        try:
            raw = marker.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError, OSError):
            return agent
        lines = raw.splitlines()
        content = [ln for ln in lines if ln.strip()]
        name = lines[0].strip() if lines else ""
        if (len(content) == 1 and name not in (".", "..")
                and "/" not in name and _USER_RE.match(name)):
            return agent / name
        return agent
    except Exception:
        return None


def append(agent_dir, hook, decision, reason, session_id="",
           tool="", path="", command="") -> None:
    """Append one enforcement-journal record. Never raises; returns None.

    `agent_dir` is the ALREADY-RESOLVED lane dir. The write is skipped silently
    unless it exists (playbook-managed project). The `journal/` subdir is made
    inside it; `.agent` is never minted here.
    """
    try:
        if not agent_dir:
            return
        agent_dir = Path(agent_dir)
        if not agent_dir.is_dir():          # playbook-managed lane only
            return
        jdir = agent_dir / "journal"
        try:
            jdir.mkdir(exist_ok=True)       # inside an existing lane; not `.agent`
        except OSError:
            pass                            # e.g. path is a file — the open below fails, swallowed
        rec = {
            "ts": _utcnow(),
            "session_id": session_id or "",
            "hook": hook,
            "decision": decision,
            "reason": reason,
        }
        if tool:
            rec["tool"] = tool
        if path:
            rec["path"] = path
        if command:
            rec["command"] = _head(command)
        data = (json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        fd = os.open(str(jdir / "enforcement.jsonl"),
                     os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, data)              # single append; atomic under PIPE_BUF
        finally:
            os.close(fd)
    except Exception as exc:                # defence in depth: never propagate
        _warn(exc)


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _head(command, limit: int = _HEAD_LIMIT) -> str:
    """Command head only — first line, capped. Never the full payload."""
    s = str(command).strip().split("\n", 1)[0]
    return s[:limit]


def _warn(exc: Exception) -> None:
    try:
        sys.stderr.write(f"[pb_journal] warn: {type(exc).__name__}: {exc}\n")
    except Exception:
        pass
