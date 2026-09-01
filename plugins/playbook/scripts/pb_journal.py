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
import stat
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
        _write_record(agent_dir, rec)
    except Exception as exc:                # defence in depth: never propagate
        _warn(exc)


def append_review(agent_dir, *, session_id="", seat="", task="", round_no=0,
                  kind="", duration_ms=None, status="", usage=None) -> None:
    """Append one REVIEW-SPEND record and return None. Never raises.

    Same HARD CONTRACT as `append`: a write failure must NEVER change or break a
    review or any decision, one O_APPEND write of a single line kept under
    PIPE_BUF, and the write is skipped unless the resolved lane dir already
    exists. This records what a judge invocation COST — nothing it decides.

    The record shares the enforcement envelope (`hook="review"`,
    `decision="record"`, `reason="review spend"`) so a journal reader sees it as
    one more `record` line, plus review-specific fields:
      * `kind`        — "panel" | "single" | "tail-cert"
      * `seat`        — the judge spec, e.g. "claude:opus" / "codex:gpt-5.6:medium"
      * `task`        — task number ("042") or "-" for a taskless/--prompt review
      * `round`       — the review iteration this spend belongs to (int; 0 = unknown)
      * `duration_ms` — wall time of the judge subprocess, milliseconds (omitted if unknown)
      * `status`      — "ok" | "fail" | "timeout" | "dnf" (did-not-finish/spawn error)
      * `usage`       — token usage WHERE the CLI reports it, else the explicit
                        marker `{"status":"unknown"}`. Numbers are NEVER fabricated:
                        the claude judge runs in plain-text mode and codex/grok do
                        not surface per-call tokens here, so `unknown` is the norm.

    EVERY field is bounded so the record stays well under the 512-byte
    atomic-write floor — the same bound `append` documents. `session_id` is
    byte-capped and `usage` is normalized to the fixed `{status[,in,out]}` schema
    (arbitrary caller dicts are NOT copied verbatim), because an unbounded
    session id or a large usage dict would blow the PIPE_BUF concurrent-append
    bound (impl-panel codex, task 042).
    """
    try:
        rec = {
            "ts": _utcnow(),
            "session_id": _head(session_id or "", 80),
            "hook": "review",
            "decision": "record",
            "reason": "review spend",
            "kind": _head(kind, 24),
            "seat": _head(seat, 80),
            "task": _head(str(task), 16),
            "round": _cap_int(int(round_no)) if _is_int(round_no) else 0,
        }
        if isinstance(duration_ms, (int, float)) and duration_ms >= 0:
            rec["duration_ms"] = _cap_int(int(duration_ms))
        if status:
            rec["status"] = _head(status, 16)
        rec["usage"] = _normalize_usage(usage)
        _write_record(agent_dir, rec)
    except Exception as exc:                # defence in depth: never propagate
        _warn(exc)


# Numeric magnitude cap (impl-panel round 2): the string fields are byte-capped,
# but a pathological caller could pass an arbitrarily large `round`/`duration_ms`/
# token int whose DECIMAL LENGTH blows the PIPE_BUF atomic-write bound. Clamp
# every numeric to this ceiling so "every field is bounded" is literally true.
# 10**15 - 1 (15 digits) sits far above every real value — round is < 10^4, a
# multi-hour review is ~10^7 ms, token counts are < ~10^8 — so the clamp is only
# ever reached by a pathological/synthetic call; even then the line stays small.
_INT_CAP = 10 ** 15 - 1


def _cap_int(v: int) -> int:
    if v < 0:
        return 0
    return v if v <= _INT_CAP else _INT_CAP


def _normalize_usage(usage) -> dict:
    """Coerce any caller `usage` into the fixed, bounded shape the record shape
    doc pins — never a verbatim copy of an arbitrary dict (which could blow the
    PIPE_BUF bound). Returns `{"status":"known","in":<int>,"out":<int>}` only
    when both token counts are real ints; otherwise `{"status":"unknown"}`.
    Numbers are never fabricated — a non-int in/out degrades to unknown."""
    if isinstance(usage, dict) and usage.get("status") == "known":
        _in, _out = usage.get("in"), usage.get("out")
        if isinstance(_in, int) and not isinstance(_in, bool) \
                and isinstance(_out, int) and not isinstance(_out, bool):
            # Magnitude-capped like every other numeric so a pathological token
            # count cannot blow the PIPE_BUF line bound (impl-panel round 2).
            return {"status": "known", "in": _cap_int(_in), "out": _cap_int(_out)}
    return {"status": "unknown"}


def _is_int(v) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return True
    try:
        return str(v).strip().lstrip("-").isdigit()
    except Exception:
        return False


def _write_record(agent_dir, rec) -> None:
    """Write ONE record dict as a single O_APPEND line into the lane's journal.

    Shared by `append` and `append_review`. Skips silently unless the resolved
    lane dir already exists (playbook-managed project); the `journal/` subdir is
    created inside that existing lane, never `.agent` itself. One `os.write` of
    one line: POSIX makes an O_APPEND write of at most PIPE_BUF bytes atomic
    against concurrent appenders, so lines from separate processes (multi-lane,
    parallel panel seats) do not interleave. The caller is responsible for
    keeping the record under that bound (the field caps above do).

    Hostile-tree safe (impl-panel round 3): the journal file is opened
    O_NONBLOCK|O_NOFOLLOW and its fd fstat'd to confirm a regular file before any
    write. Without this, a rogue judge that swapped `enforcement.jsonl` for a
    FIFO would turn the next review's O_WRONLY open into an indefinite HANG
    (worse than the "a write never affects the review" contract allows), and a
    symlinked `journal/` could write outside the lane. A non-regular / symlinked
    sink is silently skipped instead."""
    if not agent_dir:
        return
    agent_dir = Path(agent_dir)
    if not agent_dir.is_dir():              # playbook-managed lane only
        return
    jdir = agent_dir / "journal"
    try:
        jdir.mkdir(exist_ok=True)           # inside an existing lane; not `.agent`
    except OSError:
        pass                                # e.g. path is a file — the open below fails, swallowed
    data = (json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    # O_NONBLOCK: a FIFO with no reader fails ENXIO here rather than blocking.
    # O_NOFOLLOW: a symlinked final component fails ELOOP (no lane escape).
    fd = os.open(str(jdir / "enforcement.jsonl"),
                 os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK
                 | getattr(os, "O_NOFOLLOW", 0), 0o644)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):    # FIFO/device/dir/socket → not our sink
            return
        os.write(fd, data)                  # single append; atomic under PIPE_BUF
    finally:
        os.close(fd)


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _head(command, limit: int = _HEAD_LIMIT) -> str:
    """Command head only — first line, capped to `limit` BYTES (UTF-8), never the
    full payload. Byte-capping (not character-capping) is what actually keeps the
    record under PIPE_BUF: 200 multi-byte characters (e.g. emoji) are up to 800
    bytes and would blow the 512-byte atomic-write bound (panel). A truncated
    trailing multi-byte sequence is dropped (errors='ignore'), never emitted
    half-encoded.

    Control characters are stripped FIRST (impl-panel round 3): a byte cap alone
    does not bound the SERIALIZED line, because json.dumps escapes each control
    char to a 6-byte `\\uXXXX`, so an all-NUL field within the byte cap could
    still expand the record past PIPE_BUF. These fields (seat/kind/task/status/
    session_id/command-head) never legitimately carry control chars, so dropping
    them keeps the encoded line bounded (the only remaining JSON expansion is `"`
    and `\\` → 2 bytes, which the small caps already absorb)."""
    s = str(command).strip().split("\n", 1)[0]
    s = "".join(ch for ch in s if ch >= " " and ch != "\x7f")
    return s.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def _warn(exc: Exception) -> None:
    try:
        sys.stderr.write(f"[pb_journal] warn: {type(exc).__name__}: {exc}\n")
    except Exception:
        pass
