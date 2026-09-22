"""Judge orchestration: the `panel-review` arm, the single-judge
`plan-review` / `impl-review` / `judge` arm, and the review machinery both
share.

Boundary: everything about SPAWNING judges, guarding them, and delivering
their output — per-transport context assembly (via mindmap's `_load_mind_map`
and core's `select_task_context`), the tamper trio
(`_snapshot_repo_state` / `_detect_tamper` / `_tamper_banner` — the #1 guard;
judges are read-only evaluators), the triage frame the reading agent meets
before findings, sentinel-delimited findings write-back
(`_write_review_findings` — the trusted parent writes, never the judge), the
per-backend log-name contract, quorum verdicts, timeout salvage, and the
model-unavailable hard stops. The CLOSE path is not here — it consumes
judge.md via tasks.core parsers. Imports stdlib + tasks.core + tasks.shared +
tasks.mindmap + tasks.template/models_check/audit + provider.*; never a
command module (design-1.5.9.md §4).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from tasks.atomic import atomic_write
from tasks.core import _safe_hash_regular, resolve_agent_dir
from tasks.mindmap import _load_mind_map
from tasks.shared import find_project_root

# The host caps a single foreground tool call at 600 s. A review whose hard
# timeout can outlast that will be killed mid-run if launched in the foreground,
# so the CLI advises a background launch up front (task 038).
_FOREGROUND_TOOL_CAP_SECS = 600


# ── Review-spend journal (task 042) ──────────────────────────────────────────
# Every judge invocation appends ONE best-effort spend record to the enforcement
# journal (hook="review", decision="record") via pb_journal.append_review — seat,
# task, round, kind, wall duration, exit status, token usage. IDENTICAL hard
# contract to the enforcement journal: a write failure NEVER changes or breaks a
# review or any decision. Emitted from the review runner itself where the judge
# subprocess has ALREADY completed — no new interpreter startup on any hot path.

_PB_JOURNAL_MOD = None
_PB_JOURNAL_LOADED = False


def _load_pb_journal():
    """Load scripts/pb_journal.py once, in-process, and cache it. Returns the
    module or None. Loaded like command_guard's helper (by file path) so its
    absence can never break a review. This is an in-process import, NOT a
    subprocess — the 'no new interpreter startups on hot paths' constraint."""
    global _PB_JOURNAL_MOD, _PB_JOURNAL_LOADED
    if _PB_JOURNAL_LOADED:
        return _PB_JOURNAL_MOD
    _PB_JOURNAL_LOADED = True
    try:
        import importlib.util as _ilu
        _p = Path(__file__).resolve().parent.parent / "scripts" / "pb_journal.py"
        _spec = _ilu.spec_from_file_location("_pb_journal_review", _p)
        if _spec is not None and _spec.loader is not None:
            _mod = _ilu.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
            _PB_JOURNAL_MOD = _mod
    except Exception:
        _PB_JOURNAL_MOD = None
    return _PB_JOURNAL_MOD


def _journal_review_spend(project_path, *, kind, seat, task, round_no,
                          duration_ms, status, usage=None, session_id=""):
    """Best-effort review-spend record. NEVER raises; a failure is swallowed so a
    review is unaffected. `usage=None` → the explicit `{"status":"unknown"}`
    marker (numbers are never fabricated)."""
    try:
        pbj = _load_pb_journal()
        if pbj is None:
            return
        agent_dir = pbj.resolve_lane_dir(project_path)
        sid = session_id or os.environ.get("PLAYBOOK_SESSION_ID", "") or ""
        pbj.append_review(
            agent_dir, session_id=sid, seat=seat or "",
            task=(str(task) if task else "-"), round_no=round_no,
            kind=kind, duration_ms=duration_ms, status=status, usage=usage)
    except Exception:
        pass


# The fixed reasoning effort claude judges run at — kept in step with
# provider/adapters/claude.py::run_headless_judge and the single-judge claude
# branch below (both hardcode `--effort high`). If that constant ever changes,
# change it in all three so the recorded seat effort stays truthful.
_CLAUDE_JUDGE_EFFORT = "high"


def _seat_with_effort(provider_name, variant):
    """The spend-record seat spec as `model:effort` (owner ask, task 042).

    codex and grok already encode their reasoning effort INSIDE the variant
    (e.g. `gpt-5.6-terra:medium`, `grok-4.6:high`), so their seat already carries
    it. Claude runs at a FIXED `--effort` not present in the model variant, so it
    is appended here — the one provider that would otherwise lose the effort
    attribution the owner wants for spend analysis."""
    base = f"{provider_name}:{variant}" if variant else provider_name
    if provider_name == "claude":
        return f"{base}:{_CLAUDE_JUDGE_EFFORT}"
    return base


def _judge_status(output, timed_out=False):
    """Map a judge result to the spend status enum: ok | fail | timeout | dnf.

    dnf = did-not-finish: a spawn/resolution error ("(error: …)" — CLI missing,
    adapter raised) that never produced a review. A budget/failure-marked output
    is `fail`; a clean review is `ok`. The tail-cert path returns its timeout as
    an "(error: … timed out)" string rather than raising, so a leading error
    that says so is classified `timeout`, not `dnf`."""
    if timed_out:
        return "timeout"
    _o = (output or "").lstrip()
    if _o.startswith("(error:"):
        return "timeout" if "timed out" in _o.lower() else "dnf"
    try:
        from tasks.models_check import judge_failed as _jf
        if _jf(output):
            return "fail"
    except Exception:
        pass
    return "ok"


def _parse_judge_usage(output_text):
    """Token usage for the spend record — else None (the caller then records
    `{"status":"unknown"}`). NEVER fabricates numbers.

    ONE source (task 056, impl round 1): the usage CARRIED by the value itself —
    adapters that requested structured CLI output (codex `exec --json`, grok
    `--output-format json`) return `provider.usage.JudgeOutput`, a str subclass
    whose `.usage` was parsed from that structured stdout before the review text
    was extracted. A plain str carries nothing: every legacy caller/test double,
    every timeout/error string, and — deliberately — a plain-text judge whose
    entire output happens to be a JSON usage envelope (a claude seat, or any seat
    quoting one) stays `unknown`. Text is never parsed for usage: the review is
    prose, prose can never poison the field (task 042's rule, now absolute).
    Enabling structured output for claude is a one-line adapter change that would
    attach the carrier — a deliberate decision, not an accident."""
    carried = getattr(output_text, "usage", None)
    if isinstance(carried, dict):
        from provider.usage import _valid_known
        if _valid_known(carried):
            return {"status": "known", "in": carried["in"], "out": carried["out"]}
    return None


def _next_review_round(project_path, task_file):
    """The review iteration this spend belongs to = existing rounds + 1.
    Best-effort: any failure → 0 (unknown). This is a log field, never a gate
    input. Uniform across kinds — for single/tail-cert it reflects the panel
    rounds recorded so far (an approximate spend-correlation hint), documented
    as such in the record shape doc.

    Counts BOTH judge.md and its overflow sibling judge-archive.md (impl-panel
    codex:sol, task 042): judge.md retains only the newest JUDGE_MD_MAX_ROUNDS
    (5) rounds and archives the rest, so counting judge.md alone would cap the
    round at 6 once archiving begins. The true count = rounds in both files (see
    MIND_MAP [8])."""
    try:
        if not task_file:
            return 1
        from tasks.core import parse_judge_rounds
        total = 0
        seen = False
        for name in ("judge.md", "judge-archive.md"):
            p = Path(task_file).parent / name
            if p.exists():
                seen = True
                total += len(parse_judge_rounds(
                    p.read_text(encoding="utf-8", errors="replace")))
        if not seen:
            return 1
        return total + 1
    except Exception:
        return 0


def _print_background_advisory(hard_timeout_secs: "int | None") -> None:
    """Print a one-line advisory when a review's hard timeout can exceed the
    600 s foreground tool-call cap. `None` = unlimited, which also can exceed it.
    No-op for a bounded run at/under the cap."""
    if hard_timeout_secs is None or hard_timeout_secs > _FOREGROUND_TOOL_CAP_SECS:
        print(
            "  ⚠ this run may exceed the 600 s foreground tool-call cap — launch "
            "it in the background (run detached / '&') and poll, or the host may "
            "kill it mid-run.",
            flush=True,
        )


def _panel_triage_frame() -> list[str]:
    """Return the lines to append to a panel-review judge.md so the reading
    agent meets the triage discipline alongside the findings.

    Same wording for plan and impl modes (the panel-review assembly is shared);
    mirrors the per-task pushback gate from `template.judge_section()` /
    `template.judge_impl_section()` but lives in the file the agent actually
    reads after the panel runs.
    """
    bar = "═" * 60
    return [
        bar,
        "## Triage",  # No indent — must match `^## ` line-start parsers (impl-review F4).
        bar + "\n",
        (
            "These findings are opinion, not gospel. Before applying any of "
            "them, decide per-finding: real correctness issue, speculative "
            "concern, or wrong call. Document accept (with rationale) / park "
            "(with rationale) / reject (with rationale). Verify file:line "
            "claims before applying — panel judges sometimes cite wrong "
            "locations. The panel doesn't live with the outcomes — you do. "
            "Push back where you have concrete evidence the panel doesn't."
        ),
        "",
        # P11: name what a panel structurally CANNOT catch, at the moment the
        # agent is tempted to read a clean panel as "all clear". Three classes
        # went through 46 real panels untouched because everyone assumed the
        # panel covered them. A panel does CONFORMANCE (does the code match the
        # intent); it cannot do these — so a green panel is NOT evidence on them.
        "**A clean panel does NOT clear these — they are outside what any judge can verify:**",
        (
            "- **Correspondence** — does the result match the WORLD, not just the "
            "intent? A judge reads code and text, not reality. If the work is "
            "user-facing or asserts a measured fact, YOU check the real artifact "
            "(screenshot/recording/actual output) or the measuring instrument — "
            "the panel cannot."
        ),
        (
            "- **Disclosure** — provenance, secrets, attribution, AI-authorship in "
            "public files. This is a mechanical grep (`tasks audit` / a pre-commit "
            "scan), not a judgement call — run it; do not expect a judge to."
        ),
        (
            "- **Irreversibility / blast radius** — a panel weighs correctness, not "
            "consequence. If `## Risk` is `irreversible` or `assertive`, the "
            "rollback plan or the claim-and-its-instrument needs YOUR explicit "
            "sign-off regardless of how clean the findings are."
        ),
        "",
    ]


# Per-file content-hash cap for the dirty-tree tamper signal. A file already
# dirty at snapshot time keeps an identical porcelain LINE when only its content
# changes, so porcelain alone is content-blind; we additionally hash each dirty
# file's bytes. Reading is bounded so a huge dirty artifact can't stall a review
# — an oversize file records an honest `unhashed:too-large` marker (never a
# silent skip), an accepted, visible limitation.
_TAMPER_HASH_CAP = 5 * 1024 * 1024  # 5 MiB per file
# Cumulative ceiling across ALL dirty files in one snapshot: `-uall` enumerates
# untracked files individually, so a huge non-ignored tree could otherwise make
# every review read gigabytes twice. Files past the ceiling get an honest
# `unhashed:budget-exceeded` marker.
_TAMPER_TOTAL_BUDGET = 200 * 1024 * 1024  # 200 MiB


# `_safe_hash_regular` — the single safe-read primitive — was moved to
# `tasks.core` (R1/1.5.39) so the untracked-content digest in
# `_repo_fingerprint_material` can share it without a core→review import cycle.
# Re-exported at the top of this module; the tamper trio below is unchanged.


def _safe_task_fingerprint(task_file: "Path") -> "str | None":
    """Fingerprint task.md for the tamper guard via the safe-read primitive
    (`_safe_hash_regular`). Returns None only when task.md is ABSENT (the
    deleted-task signal); a symlink is fingerprinted by LINK TEXT (never
    dereferenced); an oversize file returns a SIZE-BEARING `too-large:<size>`
    marker (so a size change is caught by the before/after compare while a
    legitimately-oversize-but-unchanged task.md does NOT false-positive — panel
    round-6 opus/codex:sol); a directory/FIFO/device or read error returns the
    `unreadable` sentinel, which a readable→unreadable transition flags as tamper.
    """
    import hashlib
    import stat as _stat
    try:
        st = os.lstat(task_file)
    except OSError:
        return None                              # absent → deleted-task signal
    if _stat.S_ISLNK(st.st_mode):
        try:
            return "symlink:" + hashlib.sha256(
                os.fsencode(os.readlink(task_file))).hexdigest()
        except OSError:
            return "unreadable"
    kind, detail, _ = _safe_hash_regular(task_file, _TAMPER_HASH_CAP)
    if kind == "hash":
        return detail                            # type: ignore[return-value]
    if kind == "toolarge":
        return f"too-large:{detail}"             # size-bearing: unchanged → no flag
    return "unreadable"                          # dir / FIFO / device / error


def _porcelain_z_paths(z: bytes) -> "list[str]":
    """Working-tree paths from `git status --porcelain -z -uall` output (BYTES).

    `-z` is NUL-terminated and — unlike the human-readable form — emits paths
    RAW (no C-quoting of non-ASCII/special chars) and splits a rename/copy into
    two separate NUL fields (`XY <new>\\0<old>\\0`) instead of the ambiguous
    ` <orig> -> <dest> ` on one line. Line-splitting the readable form misparsed
    both a git-quoted name (e.g. `über.py`) and any literal filename containing
    ` -> ` (T3 panel round 2 — opus/sonnet/grok), silently dropping them from
    the content-hash set.

    Consumes BYTES and `os.fsdecode`s each path: decoding the stream as UTF-8
    with `errors="replace"` (or universal-newlines `text=True`) corrupted names
    containing `\\r` or invalid UTF-8, resolving them to non-existent paths that
    were then silently skipped (T3 panel round 3 — grok). `fsdecode` round-trips
    any real on-disk name back to something `Path` can open.

    Each record is `XY <path>`; bytes 0-1 are the status, index 2 a space, 3+ the
    path. For an `R`/`C` status the NEXT field is the source path — consume and
    skip it (we want the destination, the file that now exists)."""
    return [p for _st, p in _porcelain_z_entries(z)]


def _porcelain_z_entries(z: bytes) -> "list[tuple[str, str]]":
    """`(status, path)` pairs from the same stream — the status letters let the
    caller tell an UNTRACKED (`??`) path from a tracked-but-dirty one (task 059:
    only an untracked record file is the panel's own churn)."""
    import os as _os
    out: "list[tuple[str, str]]" = []
    fields = z.split(b"\0")
    i = 0
    while i < len(fields):
        rec = fields[i]
        if len(rec) < 4:
            i += 1
            continue
        status, path = rec[:2], _os.fsdecode(rec[3:])
        if path:
            out.append((status.decode("ascii", "replace"), path))
        i += 2 if status[:1] in (b"R", b"C") else 1
    return out


def _git_head_state(repo_path: Path) -> dict:
    """`{"state": "ok"|"unborn"|"error", "value": sha|None}` for `repo_path`'s
    HEAD (task 059, P8): a read FAILURE is recorded as such so two failed reads
    can never compare equal and silently disable the moved-HEAD caution."""
    import subprocess
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_path),
                           capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return {"state": "error", "value": None}
    if r.returncode == 0:
        v = r.stdout.strip().decode("ascii", "replace") if isinstance(r.stdout, bytes) else str(r.stdout).strip()
        return {"state": "ok", "value": v}
    err = r.stderr if isinstance(r.stderr, bytes) else str(r.stderr).encode()
    if b"unknown revision" in err or b"ambiguous argument" in err or b"Needed a single revision" in err:
        return {"state": "unborn", "value": None}
    return {"state": "error", "value": None}


def _taskdir_identity(task_file: Path) -> dict:
    """Git-INDEPENDENT identity of the task directory and task.md (task 059,
    plan panel codex-high #1): realpath + (dev, ino) + is-symlink for both. A
    judge that swaps the task dir for a symlink to a copy holding an identical
    task.md while `git status` is degraded would otherwise redirect the trusted
    parent's later `judge.md`/`task.md` writes outside the repo — `atomic_write`
    follows the parent path. Never raises; unreadable → stable markers."""
    out: dict = {}
    for label, p in (("td", task_file.parent), ("tf", task_file)):
        try:
            out[label + "_link"] = os.path.islink(p)
        except OSError:
            out[label + "_link"] = None
        try:
            out[label + "_real"] = os.path.realpath(p)
        except (OSError, RuntimeError, ValueError):
            out[label + "_real"] = "<unresolvable>"
        try:
            st = os.stat(p)
            out[label + "_ino"] = (st.st_dev, st.st_ino)
        except OSError:
            out[label + "_ino"] = None
    return out


def _snapshot_repo_state(project_path: Path, task_file: Path | None, _depth: int = 0) -> dict:
    """Capture the repo's mutable state before spawning judges, so a rogue judge
    that writes the working tree can be detected afterward (#1 tamper guard).

    Judges are read-only evaluators; nothing they run should change the repo. On
    platforms with OS containment `project_writable=False` blocks writes, but the
    sandbox falls back to UNCONTAINED direct exec when no seatbelt/bwrap exists
    (Windows) or when already nested — there this snapshot/compare is the ONLY
    tamper defense, so it is mandatory, not belt-and-braces.

    Signals (best-effort, each degradation recorded as a field the detector can
    NAME rather than silently pass):
      - `porcelain`: readable `git status --porcelain -uall`, kept byte-OPAQUE as
        the before==after line signal (never parsed — C-quoting and ` -> ` make
        the readable form unsafe to parse; see `_porcelain_z_paths`). None when
        not a git repo (or git failed — `is_git` tells the two apart).
      - `dirty_hashes`: sha256 of the CONTENT of each dirty / untracked file,
        enumerated from `--porcelain -z` (raw paths). Task 059 (074 panel, T023):
        porcelain paths are TOPLEVEL-relative even when the project is a
        SUBDIRECTORY of its repo, so files are opened at `toplevel / rel` and
        keyed PROJECT-relative (`_project_relative`). When the toplevel cannot
        be resolved while porcelain exists, `hash_scope_ok` is False and NO
        hashes are taken — never a `project_path / rel` join (the 074 bug read
        every such file as absent).
      - `prefix`: the toplevel→project prefix, so the detector's exemption
        regexes (monitor / catalog / task dir) can match toplevel-relative lines.
      - `head`: `_git_head_state` (moved HEAD is a CAUTION — a read-only judge
        cannot commit, so a move is a concurrent actor, not tamper; P1).
      - `roots`: one nested snapshot per `code_roots` entry (depth 1), with the
        root's resolved identity + containment (T019 F3, T040 P-1, P7/P10).
      - `taskdir_identity`: git-independent identity of the task dir (P6).
      - `task_hash`: sha256 of task.md — the primary tamper target; the only
        signal when the project isn't a git repo.
    """
    import hashlib
    import subprocess
    from tasks.core import (_git_toplevel, _toplevel_prefix, _project_relative,
                            _code_roots, load_config, _scope_identity)
    porcelain = None
    try:
        r = subprocess.run(
            # -uall: enumerate untracked files INDIVIDUALLY. A collapsed
            # `?? newdir/` line hides what is inside — the F22 monitor
            # exclusion needs full paths to match, and naming each rogue
            # file is strictly better evidence than naming its directory.
            ["git", "-C", str(project_path), "status", "--porcelain", "-uall"],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
        )
        if r.returncode == 0:
            porcelain = r.stdout
    except (OSError, subprocess.SubprocessError):
        porcelain = None
    # A SEPARATE `-z` read for content-hash path enumeration: the human-readable
    # form above stays the opaque line-diff signal (quoting can't break a
    # before==after equality check), while `-z` gives raw, unambiguous paths to
    # actually open and hash.
    z_out = None
    try:
        rz = subprocess.run(
            # BYTES (no text=/encoding): the NUL stream is decoded per-path with
            # os.fsdecode so `\r`/non-UTF-8 names survive (round-3 grok).
            ["git", "-C", str(project_path), "status", "--porcelain", "-z", "-uall"],
            capture_output=True, timeout=30,
        )
        if rz.returncode == 0:
            z_out = rz.stdout            # bytes
    except (OSError, subprocess.SubprocessError):
        z_out = None
    # Toplevel + prefix (task 059): only meaningful for a git repo. A resolvable
    # toplevel is REQUIRED to open porcelain-named files correctly.
    # Resolve the toplevel whenever content hashing could run — NOT only when the
    # readable porcelain succeeded. The readable call and the `-z` call are
    # separate 30 s subprocesses, so on a large dirty tree the first can time out
    # while the second succeeds; the earlier shape then left `hash_scope_ok` True
    # with `toplevel` None and `Path(None) / rel` raised TypeError, which the
    # detector turned into a MUTATION and a discarded paid panel (059 impl-panel
    # r2, grok #3).
    toplevel = prefix = None
    if porcelain is not None or z_out is not None:
        toplevel = _git_toplevel(Path(project_path))
        prefix = _toplevel_prefix(Path(project_path), toplevel) if toplevel is not None else None
    hash_scope_ok = toplevel is not None and prefix is not None
    dirty_hashes: dict[str, str] = {}
    untracked: "set[str]" = set()
    if z_out and hash_scope_ok:
        budget = _TAMPER_TOTAL_BUDGET       # cumulative-bytes ceiling across the run
        for _status, rel in _porcelain_z_entries(z_out):
            fp = Path(toplevel) / rel
            key = _project_relative(rel, prefix)
            if _status.strip() == "??":
                untracked.add(key)
            try:
                if fp.is_symlink():
                    # Hash the LINK TEXT, not the referent: `read_bytes` follows a
                    # symlink, so a retarget to a same-content file would be
                    # invisible (round-4 grok). os.readlink captures the target.
                    dirty_hashes[key] = "symlink:" + hashlib.sha256(
                        os.fsencode(os.readlink(fp))).hexdigest()
                    continue
                import stat as _stat_mod
                try:
                    _lst = os.lstat(fp)
                except OSError:
                    continue                     # gone → the porcelain diff covers it
                if not _stat_mod.S_ISREG(_lst.st_mode):
                    # NOT a regular file (dir / gitlink / FIFO / device). Record a
                    # stable type marker instead of skipping, so a tracked file
                    # already listed as dirty (` M victim`) that a rogue swaps to a
                    # FIFO — whose porcelain line can stay unchanged on some git
                    # versions — compares regular-hash → `nonregular:<type>` and is
                    # flagged (panel round-8 codex:terra). A path that is
                    # non-regular in BOTH snapshots (a real submodule) keeps the
                    # same marker → no false flag.
                    dirty_hashes[key] = f"nonregular:{_stat_mod.S_IFMT(_lst.st_mode)}"
                    continue
                if budget <= 0:
                    # Total-bytes budget exhausted: a huge dirty/untracked tree
                    # (e.g. a fresh repo with no .gitignore for node_modules)
                    # must not make every review hash gigabytes twice. Honest
                    # marker, never a silent skip (round-4 opus).
                    dirty_hashes[key] = "unhashed:budget-exceeded"
                    continue
                # Route the read through the SAME safe primitive as task.md: a
                # detached writer swapping a dirty file to a FIFO between the
                # lstat above and this read would otherwise block `read_bytes`
                # FOREVER, hanging _detect_tamper (and every exit that depends on
                # it) — a DoS worse than a missed banner (panel round-6, sonnet
                # Critical / opus). The primitive re-validates with fstat.
                kind, detail, nbytes = _safe_hash_regular(fp, _TAMPER_HASH_CAP)
                if kind == "hash":
                    dirty_hashes[key] = detail   # type: ignore[assignment]
                    budget -= nbytes
                elif kind == "toolarge":
                    dirty_hashes[key] = f"unhashed:too-large:{detail}"
                else:  # a TOCTOU swap after the lstat, or a read failure
                    dirty_hashes[key] = "unhashed:error"
            except OSError:
                dirty_hashes[key] = "unhashed:error"
    # task.md fingerprint — a hostile swap must not crash OR HANG the guard
    # before the exit banner (panel rounds 2-4, codex×2). See _safe_task_fingerprint.
    task_hash = _safe_task_fingerprint(task_file) if task_file else None
    # `is_git` (A2): a cheap `.git` probe that lets _detect_tamper distinguish
    # "git repo whose readable `git status` FAILED" (fail CLOSED) from "genuinely
    # not a git repo" (documented uncontained fallback), symmetric with the
    # `z_read_ok` guard. It WALKS ANCESTORS the way git does — a Playbook root
    # (located by `.agent/tasks/`) can sit nested inside a parent Git worktree
    # whose `.git` is above `project_path` (panel round-1 codex:sol), and
    # `git -C project_path status` reports on that ancestor repo. Matches a `.git`
    # dir OR file (worktrees/submodules). Pure filesystem stats — never a
    # subprocess that could itself hang.
    snap = {"porcelain": porcelain, "task_hash": task_hash,
            "dirty_hashes": dirty_hashes, "z_read_ok": z_out is not None,
            "is_git": _in_git_repo(Path(project_path)),
            "hash_scope_ok": hash_scope_ok, "prefix": prefix,
            "untracked": untracked,
            "head": _git_head_state(Path(project_path)) if porcelain is not None else {"state": "n/a", "value": None},
            # Strict own-repo check (like the fingerprint's strict mode): a root
            # whose toplevel is NOT itself is not its own repo (its `.git` is gone
            # or never existed — git would report the PARENT's status for it).
            "own_repo": (toplevel is not None and prefix in ("", ".")) if porcelain is not None else False}
    if task_file is not None:
        snap["taskdir_identity"] = _taskdir_identity(Path(task_file))
        # Task 059 (T045, plan panel codex-high #3): is task.md itself TRACKED?
        # `tracked | untracked | unknown | n/a` — only a SUCCESSFUL "not tracked"
        # widens the task-dir exemption; a failed probe is a caution and narrows.
        tracked = "n/a"
        if porcelain is not None:
            try:
                rel_tf = Path(task_file).relative_to(project_path).as_posix()
                rl = subprocess.run(
                    ["git", "-C", str(project_path), "ls-files", "--error-unmatch", "--", rel_tf],
                    capture_output=True, timeout=30)
                if rl.returncode == 0:
                    tracked = "tracked"
                elif rl.returncode == 1 and b"did not match" in (rl.stderr or b""):
                    tracked = "untracked"
                else:
                    tracked = "unknown"
            except (OSError, subprocess.SubprocessError, ValueError):
                tracked = "unknown"
        snap["taskmd_tracked"] = tracked
    # code_roots (depth 1 only — a root's own code_roots are not followed).
    roots: dict = {}
    if _depth == 0:
        try:
            cfg = load_config(Path(project_path))
            rels = _code_roots(cfg)
        except Exception:      # noqa: BLE001 — config is advisory, never a crash here
            rels = []
        proj_resolved = None
        try:
            proj_resolved = Path(project_path).resolve()
        except (OSError, RuntimeError, ValueError):
            proj_resolved = None
        for rel in rels:
            cand = Path(project_path) / rel
            identity = _scope_identity(Path(project_path), cand)
            try:
                cand_resolved = cand.resolve()
                contained = proj_resolved is not None and (
                    cand_resolved == proj_resolved or proj_resolved in cand_resolved.parents)
            except (OSError, RuntimeError, ValueError):
                contained = False
            entry: dict = {"identity": identity, "contained": contained, "snap": None}
            if contained:
                try:
                    entry["snap"] = _snapshot_repo_state(cand, None, _depth=1)
                except Exception as _e:   # noqa: BLE001
                    entry["snap"] = None
                    entry["error"] = str(_e)
            roots[rel] = entry
    snap["roots"] = roots
    return snap


# One probe, shared with the close path (task 059, impl-panel r2 sonnet #2).
from tasks.core import in_git_repo as _in_git_repo  # noqa: E402


class _TempPath:
    """A temp file owned from allocation to exit (task 059 / T008 #5): codex's
    `-o` transcript lives in the system temp dir (the read-only judge sandbox
    forbids project writes) and used to be unlinked only on the success save,
    so every timeout / budget / dead-pin / tamper `sys.exit(1)` — and any
    exception between `mkstemp` and the spawn — leaked one file per review.
    Usable as a context manager (unlink on normal exit, exception AND
    SystemExit) or via `keep_until_exit()` (atexit-registered unlink for the
    long single-review body whose exits are `sys.exit` calls). `cleanup()` is
    idempotent — the success path may unlink first."""

    def __init__(self, suffix: str = ""):
        import tempfile as _tempfile
        fd, p = _tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        self.path = Path(p)

    def __enter__(self) -> Path:
        return self.path

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.cleanup()
        return False

    def cleanup(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass

    def keep_until_exit(self) -> Path:
        """Own the file for the rest of the command: the long single-review body
        exits through ~14 `sys.exit` sites, so the unlink rides on BOTH an
        `atexit` hook (the real CLI, which exits the process right after) and a
        module-level registry drained by `cmd_single_review`'s own `finally`
        (an in-process drive — tests, or an embedded caller — where `atexit`
        would not fire until much later)."""
        import atexit
        atexit.register(self.cleanup)
        _TEMP_REGISTRY.append(self)
        return self.path


# Drained in `cmd_single_review`'s finally (see `_TempPath.keep_until_exit`).
_TEMP_REGISTRY: "list[_TempPath]" = []


def _drain_temp_registry() -> None:
    while _TEMP_REGISTRY:
        _TEMP_REGISTRY.pop().cleanup()


# Each mode accepts BOTH placeholder generations: pre-1.5.2 templates say
# "findings appear here", the panel-first templates say "triage appears here".
# The single-judge fallback write-back must anchor in either — caught live by
# the 1.5.3 gauntlet, where a new-template task refused the fallback's write.
_REVIEW_SECTIONS = {
    "plan": ("## Plan Review",
             ("(plan review findings appear here)",
              "(plan review triage appears here)")),
    "impl": ("## Implementation Review",
             ("(implementation review findings appear here)",
              "(implementation review triage appears here)")),
}


def _findings_markers(review_mode: str) -> tuple[str, str]:
    """Open/close sentinels delimiting parent-written findings in task.md."""
    return (f"<!-- playbook:{review_mode}-review-findings -->",
            f"<!-- /playbook:{review_mode}-review-findings -->")


def _neutralise_markers(findings: str, review_mode: str) -> str:
    """Defang sentinel tokens inside judge output.

    Findings are UNTRUSTED text. If they contained our own markers, the next
    rerun's replace would bind to the wrong span and could eat the surrounding
    gates — the same class of damage the tamper guard exists to prevent. Break
    the tokens so they can never be mistaken for delimiters.
    """
    out = findings
    for marker in _findings_markers(review_mode):
        out = out.replace(marker, marker.replace("<!--", "<!_-").replace("-->", "-_>"))
    return out


def _write_review_findings(task_file: Path, review_mode: str, findings: str) -> str | None:
    """Write a single judge's findings into task.md. Returns None on success,
    else a human-readable reason it refused.

    The judge is sandboxed read-only (`project_writable=False`) and must stay
    that way, so the trusted parent performs this write — the same division the
    panel path already uses. Idempotent across reruns: findings live between
    explicit sentinels, so a re-review replaces a delimited region instead of
    guessing where the previous findings ended.

    Refuses rather than guesses. If the section has neither its placeholder nor
    exactly one well-ordered sentinel pair, nothing is written and the caller
    reports it — the findings still exist in the judge log, so refusing costs
    the operator nothing, while a wrong insertion could destroy work-plan gates.
    """
    section = _REVIEW_SECTIONS.get(review_mode)
    if section is None:
        return f"unknown review mode {review_mode!r}"
    heading, placeholders = section
    open_m, close_m = _findings_markers(review_mode)
    body = _neutralise_markers(findings.strip(), review_mode)
    block = f"{open_m}\n{body}\n{close_m}"

    # Task 058: the read, the splice and the write are ONE locked transaction —
    # a panel round landing between them used to overwrite the other's work.
    from tasks.filelock import task_lock
    try:
        _lock_cm = task_lock(task_file)
        _lock_cm.__enter__()
    except Exception as _lock_err:      # noqa: BLE001 — never block a delivered review
        return f"could not lock {task_file.name}: {_lock_err}"
    try:
        return _write_review_findings_locked(task_file, review_mode, findings)
    finally:
        _lock_cm.__exit__(None, None, None)


def _write_review_findings_locked(task_file: Path, review_mode: str, findings: str) -> "str | None":
    """The body of `_write_review_findings`, run with the task lock held."""
    section = _REVIEW_SECTIONS.get(review_mode)
    if section is None:
        return f"unknown review mode {review_mode!r}"
    heading, placeholders = section
    open_m, close_m = _findings_markers(review_mode)
    body = _neutralise_markers(findings.strip(), review_mode)
    block = f"{open_m}\n{body}\n{close_m}"

    try:
        text = task_file.read_text(encoding="utf-8")
    except OSError as e:
        return f"could not read {task_file.name}: {e}"

    # Operate ONLY inside the named section. Searching the whole file would let a
    # placeholder or marker quoted anywhere else — prose, a nested example, the
    # other review section's text — capture the write and land findings in the
    # wrong place, or silently target text that is not a section at all.
    sec_start = text.find(f"\n{heading}\n")
    if sec_start == -1:
        sec_start = 0 if text.startswith(f"{heading}\n") else -1
    if sec_start == -1:
        return f"{heading} section not found"
    body_start = text.index("\n", sec_start + 1) + 1 if sec_start else len(heading) + 1
    next_heading = text.find("\n## ", body_start)
    sec_end = len(text) if next_heading == -1 else next_heading + 1
    section, before, after = text[body_start:sec_end], text[:body_start], text[sec_end:]

    n_open, n_close = section.count(open_m), section.count(close_m)
    if n_open or n_close:
        if n_open != 1 or n_close != 1:
            return (f"{heading} has {n_open} opening and {n_close} closing "
                    f"findings markers — expected exactly one of each")
        start, end = section.index(open_m), section.index(close_m)
        if end < start:
            return f"{heading} findings markers are out of order"
        section = section[:start] + block + section[end + len(close_m):]
    else:
        _once = [p for p in placeholders if section.count(p) == 1]
        _multi = [p for p in placeholders if section.count(p) > 1]
        if _once:
            section = section.replace(_once[0], block, 1)
        elif _multi:
            return f"{heading} placeholder appears more than once"
        else:
            return (f"{heading} has neither its placeholder nor findings markers "
                    f"(hand-edited?)")
    new_text = before + section + after

    # Atomic: task.md IS the execution trace, so an interrupt must not truncate
    # it. Package primitive: same-dir temp + fsync + os.replace, cleaning up the
    # temp itself on failure. Keep this writer's soft contract of returning an
    # error string (not raising) on an I/O failure.
    try:
        atomic_write(task_file, new_text)
    except OSError as e:
        return f"could not write {task_file.name}: {e}"
    return None


def _judge_log_name(backend: str) -> str:
    """Review-log filename for a backend. Shared by the save path and the hard
    timeout path, which writes `<stem>.partial.log` beside it — they must agree
    on the name or a salvaged partial would not be findable next to its review."""
    return {
        "claude": "judge.log",
        "codex": "judge-codex.log",
        "antigravity": "judge-agy.log",
        "grok": "judge-grok.log",
        "pi": "judge-pi.log",
    }.get(backend, "judge.log")


def _prefixed(prefix: "str | None", body: str, status: str = "..") -> "re.Pattern":
    """A porcelain-LINE exemption regex that also matches the toplevel-relative
    form git emits for a SUBDIRECTORY project (task 059, P3): the two status
    characters (`status`, default any) then `\s+"?`, an optional `<prefix>/`,
    then `body`. The readable line itself is never parsed or rewritten — only
    matched, so C-quoting and ` -> ` renames cannot be mangled."""
    pre = "" if prefix in (None, "", ".") else "(?:" + re.escape(prefix) + "/)?"
    return re.compile(r"^" + status + r'\s+"?' + pre + body)


def _containment_available() -> bool:
    """Is an OS sandbox actually denying judge writes here? False on Windows and
    inside a nested sandbox — where the tamper guard is the ONLY defense and the
    concurrent-commit demotions below must not apply (059 impl-panel r2, grok
    #1). Never raises: an unanswerable probe is treated as UNCONTAINED."""
    try:
        from provider import sandbox as _sb
        return bool(_sb.containment_available())
    except Exception:      # noqa: BLE001 — fail toward caution
        return False


def _commit_paths(repo_path: Path, before_sha: "str | None", after_sha: "str | None") -> "set[str]":
    """Paths the `before..after` commits touched, toplevel-relative, or an empty
    set when git cannot say (which keeps every removal a MUTATION — the safe
    direction)."""
    if not before_sha or not after_sha:
        return set()
    import subprocess
    try:
        r = subprocess.run(["git", "-C", str(repo_path), "diff", "--name-only",
                            f"{before_sha}..{after_sha}"],
                           capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return set()
    if r.returncode != 0:
        return set()
    return {os.fsdecode(b) for b in r.stdout.split(b"\n") if b}


def _line_path_in(line: str, paths: "set[str]") -> bool:
    """Does a porcelain LINE name one of `paths`? The readable line is never
    parsed (C-quoting, ` -> ` renames), so this is a containment test on the
    line's text — deliberately conservative: an unmatched line stays a
    mutation."""
    if not paths:
        return False
    body = line[3:].strip().strip('"')
    return any(body == p or body.endswith("/" + p) or p.endswith("/" + body) or p == body
               for p in paths)


def _standard_exempts(prefix: "str | None") -> "tuple[list[re.Pattern], list[re.Pattern]]":
    """The (line, path) exemptions every git scope carries — the OUTER tree and
    each `code_roots` root alike (059 impl-panel r1, sonnet #2: built for the
    outer scope only, a sanctioned concurrent write inside a root read as a
    MUTATION and would discard a paid panel):
      - F22: the conversation monitor writes trace.md/session.md under
        `.agent/monitor/` (or `.agent/<user>/monitor/`) WHILE panels run — a
        sanctioned concurrent writer whose own sandbox confines it to exactly
        that directory. Everything else under .agent still flags.
      - task 054: `tasks dashboard` records the provider catalog baseline at
        `.agent/model-catalog.json` and may legitimately run while a panel does;
        that ONE path only (a sibling file still flags).
    """
    return ([_prefixed(prefix, r"\.agent(/[^/]+)?/monitor/"),
             _prefixed(prefix, r"\.agent/model-catalog\.json\"?$")],
            [re.compile(r"^\.agent(/[^/]+)?/monitor/"),
             re.compile(r"^\.agent/model-catalog\.json$")])


def _scope_changes(before: dict, after: dict, *, label: str = "",
                   line_exempt: "list[re.Pattern]" = (), path_exempt: "list[re.Pattern]" = (),
                   untracked_exempt: "list[re.Pattern]" = (),
                   task_rel: "str | None" = None, contained: bool = True,
                   committed_paths: "set[str] | None" = None
                   ) -> "tuple[list[str], list[str], bool]":
    """Porcelain-line and content-hash comparison for ONE git scope (the outer
    tree or a `code_roots` root). Returns (mutations, cautions). `label` is a
    `code root <rel>: ` prefix for nested scopes so an inner `x.py` never reads
    as the outer `x.py` (the readable porcelain line is never parsed, so the
    path is named by scope + line rather than rewritten)."""
    mutations: list[str] = []
    cautions: list[str] = []
    # `degraded` marks the GUARD as having been unable to run — the state the
    # round header records and the close gate keys on. A concurrent commit is a
    # caution but NOT a degraded guard (059 impl-panel r2, opus F2: marking it
    # degraded made tail certification unreachable after any background commit).
    degraded = False
    committed_paths = committed_paths or set()
    b_porc, a_porc = before.get("porcelain"), after.get("porcelain")
    # Named degradations (CAUTIONS since task 059 — the guard could not run, which
    # is not evidence that a judge wrote anything; printed + recorded, and
    # non-certifying for a high-consequence close). Round-5's rationale still
    # holds: never a SILENT pass.
    if a_porc is not None and not after.get("z_read_ok", False):
        cautions.append(f"{label}content-hash guard degraded: could not enumerate dirty "
                        "files at close (`git status -z` failed)")
        degraded = True
    if b_porc is not None and not before.get("z_read_ok", False):
        cautions.append(f"{label}content-hash guard degraded: dirty files were not "
                        "enumerable at review start (`git status -z` failed)")
        degraded = True
    if a_porc is not None and not after.get("hash_scope_ok", True):
        cautions.append(f"{label}content-hash guard degraded: hash scope unresolvable at close "
                        "(`git rev-parse --show-toplevel` failed) — no content hashes taken")
        degraded = True
    if b_porc is not None and not before.get("hash_scope_ok", True):
        cautions.append(f"{label}content-hash guard degraded: hash scope unresolvable at review "
                        "start (`git rev-parse --show-toplevel` failed) — no content hashes taken")
        degraded = True
    if a_porc is None and after.get("is_git"):
        cautions.append(f"{label}content-hash guard degraded: `git status` did not run at "
                        "close on a git repo (hostile/huge tree, corrupt index, or "
                        "repo broken?)")
        degraded = True
    if b_porc is None and before.get("is_git"):
        cautions.append(f"{label}content-hash guard degraded: `git status` did not run at "
                        "review start on a git repo (hostile/huge tree, corrupt "
                        "index, or repo broken?)")
        degraded = True     # symmetric with the close-side twin above (059 r3)
    # HEAD (P1/P8): a moved HEAD is a concurrent COMMIT — a read-only judge cannot
    # commit — so it is a caution about what the verdict describes, never
    # 'a judge modified the repo'; a read ERROR on either side is a caution too
    # (two errors must not compare equal and hide a move).
    bh, ah = before.get("head") or {}, after.get("head") or {}   # also read by the line diff below
    head_moved = (bh.get("state") == "ok" and ah.get("state") == "ok"
                  and bh.get("value") != ah.get("value"))
    if bh.get("state") == "error" or ah.get("state") == "error":
        cautions.append(f"{label}HEAD could not be read on one side of the review — a "
                        "concurrent commit would go unnoticed")
        degraded = True
    elif head_moved:
        if contained:
            cautions.append(f"{label}head moved during review ({str(bh.get('value'))[:12]} → "
                            f"{str(ah.get('value'))[:12]}) — a concurrent commit; the verdict "
                            "may not describe the current HEAD")
        else:
            # No OS containment (Windows, or an already-nested sandbox): the guard
            # is the ONLY defense, and a judge that can write can also `git
            # commit` its writes — which would hide them from the porcelain diff
            # and (below) launder its own removals. Fail closed there (059
            # impl-panel r2, grok #1).
            mutations.append(f"{label}HEAD moved during an UNCONTAINED review "
                             f"({str(bh.get('value'))[:12]} → {str(ah.get('value'))[:12]}) — "
                             "with no OS sandbox a judge can commit its own writes")
    if (b_porc is None) != (a_porc is None):
        # git↔non-git transition WHILE a review runs. A rogue DELETING `.git/` is
        # catastrophic, history-destroying tamper and must hard-stop (round-4
        # sonnet, Critical) — but a readable `git status` that merely FAILED on
        # one side (hostile/huge tree, corrupt index, a timeout) is the degraded
        # guard, already named as a caution above, and turning it into a banner
        # re-opened the exact T009 harm this task removes (059 impl-panel r1,
        # grok #2). Distinguish them with the `.git` probe: a MUTATION needs the
        # repo to have actually disappeared.
        if before.get("is_git") and not after.get("is_git"):
            mutations.append(f"{label}git repository state became unreadable during review "
                             "(.git removed or repo broken?)")
        else:
            cautions.append(f"{label}`git status` succeeded on only one side of the review "
                            "— the working-tree comparison could not run")
            degraded = True
    # A concurrent COMMIT (HEAD moved) removes the porcelain lines of everything
    # it committed. A read-only judge cannot commit, so on a moved HEAD those
    # REMOVALS are the concurrent actor's and must not void a paid review (059
    # impl-panel r1, opus F1 — the T009 harm, still open for the commit's
    # working-tree side effects after P1 demoted only the HEAD sha). NEW lines
    # and content changes stay MUTATIONS either way. Bound, disclosed: while a
    # commit is landing, a judge deleting an untracked file in the same window
    # is reported as a caution rather than a banner.
    if b_porc is not None and a_porc is not None and b_porc != a_porc:
        b_lines, a_lines = set(b_porc.splitlines()), set(a_porc.splitlines())
        # NEW porcelain lines: a rogue created/modified a tracked or untracked
        # file. REMOVED lines: a rogue reverted an already-dirty file to clean or
        # deleted an untracked one — also a working-tree mutation, and one the
        # content-hash diff below cannot see (the path is gone from `after`).
        for line in sorted(a_lines - b_lines):
            if any(rx.match(line) for rx in line_exempt):
                continue
            mutations.append(f"{label}working tree: {line.strip()}")
        for line in sorted(b_lines - a_lines):
            if any(rx.match(line) for rx in line_exempt):
                continue
            # Demote ONLY when the concurrent commit actually explains this path
            # (opus F1: an unconditional demotion let a rogue move HEAD and
            # launder unrelated deletions). `committed_paths` is the commit's own
            # name list; anything outside it stays a mutation.
            if head_moved and contained and _line_path_in(line, committed_paths):
                cautions.append(f"{label}no longer listed after the concurrent commit: "
                                f"{line.strip()}")
            else:
                mutations.append(f"{label}working tree reverted/removed: {line.strip()}")
    # Content edits to files that were ALREADY dirty at snapshot time — an
    # identical porcelain line, so only the content hash reveals them. Compare
    # paths present in BOTH snapshots (new / removed paths are named by the
    # porcelain-line diff above). Keys are PROJECT-relative (task 059).
    b_dirty = before.get("dirty_hashes") or {}
    a_dirty = after.get("dirty_hashes") or {}
    b_untracked = before.get("untracked") or set()
    a_untracked = after.get("untracked") or set()
    for rel in sorted(set(b_dirty) & set(a_dirty)):
        if rel == task_rel or any(rx.match(rel) for rx in path_exempt):
            continue
        # An UNTRACKED record file the panel itself appends to between rounds
        # (round 2 rewrites judge.md) is sanctioned churn; a COMMITTED one that
        # changes is a rogue editing evidence (059 impl-panel r1, grok #1).
        if (rel in b_untracked and rel in a_untracked
                and any(rx.match(rel) for rx in untracked_exempt)):
            continue
        if b_dirty[rel] != a_dirty[rel]:
            mutations.append(f"{label}working tree content changed: {rel}")
    return mutations, cautions, degraded


def _classify_tamper(project_path: Path, task_file: Path | None, before: dict, after: dict) -> dict:
    """The comparison behind `_detect_tamper_full` — split into MUTATIONS (a
    judge, or something, wrote the tree: hard stop) and CAUTIONS (the guard
    could not fully run, or a concurrent commit moved HEAD: printed, recorded,
    review kept but non-certifying for a high-consequence close). Task 059.

    Residual limitations (accepted, not silent): (1) a file larger than
    `_TAMPER_HASH_CAP`, or files past the `_TAMPER_TOTAL_BUDGET` cumulative
    ceiling, record an honest `unhashed:*` marker, so an edit that keeps such a
    file in the same marker state both times is not distinguished; (2) the
    regular-file signature is content bytes only, so a mode-bit-only change (e.g.
    `chmod +x`, no byte edit) is not caught; (3) only regular files and symlinks
    are hashed (symlinks by link text), so an edit inside an already-dirty
    submodule/gitlink (its porcelain line stays ` M <sub>`) is not caught. The
    readable-line diff may also OVER-report a benign index-only change (e.g. a
    `git add` that flips ` M f`→`M  f`) as a working-tree change — a safe
    direction (never a missed edit), and one that does not arise while judges
    run read-only."""
    prefix = before.get("prefix") if before.get("prefix") is not None else after.get("prefix")
    line_exempt, path_exempt = _standard_exempts(prefix)
    untracked_exempt: "list[re.Pattern]" = []
    # F22: the conversation monitor writes trace.md/session.md under
    # `.agent/monitor/` (or `.agent/<user>/monitor/`) WHILE panels run — a
    # sanctioned concurrent writer whose own sandbox confines it to exactly that
    # directory, so churn there is expected, not a judge writing the repo.
    # Everything else under .agent still flags. Two matchers: one for porcelain
    # LINES (prefix-aware for a subdir project), one for the PROJECT-relative
    # paths keyed in `dirty_hashes`.
    # E1: the task's OWN record directory is sanctioned churn — a panel
    # legitimately writes judge.md / review artifacts / task.md updates there
    # WHILE it runs. When that directory is UNTRACKED at snapshot time, `-uall`
    # enumerates each record file as a `??` line, and the panel's later record
    # writes add more `??` lines that would otherwise surface as working-tree
    # tamper and void a paid panel — the same class as the monitor exclusion
    # above. Scope the exemption to that actual condition (the task dir was
    # untracked at snapshot, so at least one `??` line under it is present in
    # `before`): when the task dir is TRACKED the guard already worked and stays
    # fully armed — a rogue editing a tracked file there is still caught. Either
    # way task.md's own CONTENT stays guarded by `task_hash` (below), so a rogue
    # rewriting the work plan is caught regardless.
    b_porc = before.get("porcelain")
    _task_rel = None
    if task_file is not None:
        try:
            _task_rel = task_file.relative_to(project_path).as_posix()
        except (ValueError, AttributeError):
            _task_rel = task_file.name
        try:
            _td = task_file.parent.relative_to(project_path).as_posix()
        except (ValueError, AttributeError):
            _td = None
        # `b_porc is not None` (not truthiness): a CLEAN tree's porcelain is the
        # empty string, and the trackedness-keyed exemption below must still be
        # computed for it — the old `??`-presence rule needed a non-empty
        # listing, the new rule does not (task 059).
        if _td and _td != "." and b_porc is not None:
            # Task 059 (T045 / P5 / codex-high #3): the exemption is keyed on
            # task.md's OWN tracked-ness, probed at snapshot time.
            #   untracked → the whole task dir is the panel's fresh record dir:
            #               broad exemption (as before).
            #   tracked   → the dir is a committed record dir; only the panel's
            #               own record FILES (by name) are exempt — a rogue
            #               sibling (`evil.py`) flags.
            #   unknown   → the probe failed: CAUTION + the narrow exemption.
            #   None/n-a  → pre-059 snapshot / non-git: the old `??`-presence rule.
            tracked = before.get("taskmd_tracked")
            # EXACT names the orchestrator itself writes — never a `judge-*.log`
            # glob, which exempted any novel sibling a judge could plant (059
            # impl-panel r2, opus F3 / sonnet #1) and still missed the supported
            # claude backend's own `judge.log` (grok #4).
            from tasks.core import judge_log_names as _jln
            # `task.md.lock` (task 058): the transaction lock the writers take is
            # created in the task dir the first time anything writes there — which
            # can be DURING a panel window, and a `??` line for it would void a
            # paid panel (058 plan panel P3, verified against this very exemption).
            record_names = "(?:" + "|".join(
                re.escape(n) for n in sorted(
                    {"judge.md", "judge-archive.md", "task-archive.md",
                     # ONE lock per task directory, always named `task.md.lock`
                     # (058 impl panel r2, sonnet #3: a `judge.md.lock` literal
                     # was exempted but nothing ever creates it).
                     "vetting-ledger.json", "task.md.lock"}
                    | _jln())) + ")"
            if tracked == "untracked":
                line_exempt.append(_prefixed(prefix, re.escape(_td) + r"/"))
                path_exempt.append(re.compile(r"^" + re.escape(_td) + r"/"))
            elif tracked in ("tracked", "unknown"):
                # …and the same record files are exempt from the CONTENT compare
                # while they stay UNTRACKED on both sides (a second panel round
                # rewrites judge.md in place — 059 impl-panel r1, grok #1).
                untracked_exempt.append(
                    re.compile(r"^" + re.escape(_td) + r"/" + record_names + r"$"))
                # Only a NEWLY CREATED (`??`) record file is the panel's own
                # churn. A TRACKED record file that was MODIFIED during the
                # window is a rogue editing committed evidence and must still
                # flag (regression `test_tracked_task_dir_edit_still_flags`), so
                # the exemption is anchored on the untracked status and NO
                # content-hash (path) exemption is added.
                line_exempt.append(_prefixed(prefix, re.escape(_td) + r"/" + record_names + r'"?$',
                                             status=r"\?\?"))
            else:
                _untracked_td_re = _prefixed(prefix, re.escape(_td) + r"/").pattern.replace(r"^..\s+", r"^\?\?\s+", 1)
                if b_porc and any(re.match(_untracked_td_re, ln) for ln in b_porc.splitlines()):
                    line_exempt.append(_prefixed(prefix, re.escape(_td) + r"/"))
                    path_exempt.append(re.compile(r"^" + re.escape(_td) + r"/"))
    contained = _containment_available()
    _bh, _ah = before.get("head") or {}, after.get("head") or {}
    committed = _commit_paths(Path(project_path), _bh.get("value"), _ah.get("value"))
    mutations, cautions, degraded = _scope_changes(
        before, after, line_exempt=line_exempt, path_exempt=path_exempt,
        untracked_exempt=untracked_exempt, task_rel=_task_rel,
        contained=contained, committed_paths=committed)
    if before.get("taskmd_tracked") == "unknown" or after.get("taskmd_tracked") == "unknown":
        cautions.append("task.md tracked-ness unknown (`git ls-files` failed) — the task-dir "
                        "exemption was narrowed to the panel's own record files")
        degraded = True
    # task.md — its own hash (the primary tamper target).
    b_hash, a_hash = before.get("task_hash"), after.get("task_hash")
    if b_hash:
        rel: Path | str = task_file
        try:
            rel = task_file.relative_to(project_path)
        except (ValueError, AttributeError):
            pass
        if not a_hash:
            # task.md existed at snapshot but is gone now — a rogue deleted it.
            # In a git repo the porcelain diff also flags this, but in the
            # non-git fallback the task hash is the ONLY signal, and a bare
            # `b_hash and a_hash` compare would short-circuit on the deletion
            # (round-3 sonnet, Critical).
            mutations.append(f"task.md deleted ({rel})")
        elif a_hash == "unreadable" and b_hash != "unreadable":
            # A readable regular file BECAME non-regular mid-review (dir / FIFO /
            # device, or perms stripped) — a tamper. Both-unreadable is a stable
            # non-regular task.md, not a mid-review change, so it is NOT flagged
            # here (that false-positived a legitimately-oversize task.md in the
            # round-5 fail-closed form — panel round-6 opus/codex:sol). Oversize
            # is caught instead by the size-bearing `too-large:<size>` marker
            # flowing through the change compare below.
            mutations.append(f"task.md is no longer a readable regular file — "
                             f"swapped for a directory/FIFO/device or perms stripped ({rel})")
        elif b_hash != a_hash:
            # Content changed, OR the size-bearing oversize marker changed (a
            # grow/shrink across or within the cap), OR a readable↔oversize flip.
            mutations.append(f"task.md content changed ({rel})")
    # Task-dir identity (P6): git-independent.
    bti, ati = before.get("taskdir_identity"), after.get("taskdir_identity")
    if bti is not None and ati is not None and bti != ati:
        mutations.append("task directory identity changed — swapped/relinked during review "
                         f"({bti.get('td_real')} → {ati.get('td_real')})")
    # code_roots (T019 F3 / P7 / P10): identity + containment are MUTATIONS when
    # they change; a root that stops being its own readable repo mid-review is a
    # mutation (a judge deleted its .git); a root unavailable on BOTH sides is a
    # caution (unavailable ≠ unwritten); inner changes are labelled `<root>/`.
    b_roots, a_roots = before.get("roots") or {}, after.get("roots") or {}
    for rel in sorted(set(b_roots) | set(a_roots)):
        br, ar = b_roots.get(rel), a_roots.get(rel)
        if br is None or ar is None:
            cautions.append(f"code root {rel}: configured on only one side of the review "
                            "(config changed?)")
            degraded = True
            continue
        if br is not None and ar is not None and (
                br.get("identity") != ar.get("identity")
                or br.get("contained") != ar.get("contained")):
            mutations.append(f"code root {rel}: identity changed ({br.get('identity')} → "
                             f"{ar.get('identity')}) — repointed/relinked during review")
            continue
        bs, as_ = br.get("snap"), ar.get("snap")
        b_ok = bool(bs and bs.get("porcelain") is not None and bs.get("own_repo"))
        a_ok = bool(as_ and as_.get("porcelain") is not None and as_.get("own_repo"))
        if b_ok and not a_ok:
            mutations.append(f"code root {rel}: became unreadable / not its own git repo during "
                             "review (.git removed or repo broken?)")
            continue
        if not b_ok and not a_ok:
            cautions.append(f"code root {rel}: unavailable (not a readable git repo of its own) — "
                            "edits inside it are not guarded")
            degraded = True
            continue
        if not b_ok and a_ok:
            cautions.append(f"code root {rel}: became readable during review (initialised?)")
            continue
        _rl, _rp = _standard_exempts(bs.get("prefix") if bs else None)
        _rcommitted = _commit_paths(Path(project_path) / rel,
                                    (bs.get("head") or {}).get("value"),
                                    (as_.get("head") or {}).get("value"))
        m, c, dg = _scope_changes(bs, as_, label=f"code root {rel}: ",
                                  line_exempt=_rl, path_exempt=_rp,
                                  contained=contained, committed_paths=_rcommitted)
        mutations += m
        cautions += c
        degraded = degraded or dg
    return {"mutations": mutations, "cautions": cautions, "degraded": degraded}


def _detect_tamper_full(project_path: Path, task_file: Path | None, before: dict) -> dict:
    """Compare current repo state against a `_snapshot_repo_state` result.
    Returns `{"mutations": [...], "cautions": [...]}` (task 059). Wrapped so an
    unexpected raise NEVER skips a guaranteed banner: an exception is a
    MUTATION-class `review UNVERIFIED` entry (P4/grok — an errored detector is
    not a named degradation, so it keeps the hard-stop posture)."""
    try:
        after = _snapshot_repo_state(project_path, task_file)
        return _classify_tamper(project_path, task_file, before, after)
    except Exception as _e:   # noqa: BLE001 — last-resort guard, must not raise
        return {"mutations": [f"tamper check itself errored ({_e}) — review UNVERIFIED"],
                "cautions": [], "degraded": True}


def _detect_tamper(project_path: Path, task_file: Path | None, before: dict) -> list[str]:
    """Legacy union view: mutations + cautions as one list (empty = clean AND
    fully verified). Kept for the tail-cert path (fail-closed on ANY entry) and
    the existing tests; the panel/single paths use `_detect_tamper_full`."""
    full = _detect_tamper_full(project_path, task_file, before)
    return list(full["mutations"]) + list(full["cautions"])


def _tamper_mark_text(full: dict) -> str:
    """The durable one-line tamper receipt for a judge log / findings block.

    `degraded` means the GUARD could not run; a caution can also be something it
    saw and attributed elsewhere (a concurrent commit). The stamp is emitted
    precisely BECAUSE there were cautions, so it must never read `clean` while
    any exist — the console and the durable record would contradict each other
    (059 impl-panel r3, opus #2)."""
    cautions = full.get("cautions") or []
    if full.get("degraded"):
        return "degraded — " + "; ".join(c.replace("\n", " ") for c in cautions)
    if cautions:
        return "clean, with notes — " + "; ".join(c.replace("\n", " ") for c in cautions)
    return "clean"


def _degraded_notice(cautions: list[str]) -> str:
    """Advisory for a review whose tamper guard could not fully run (task 059,
    T009 prioritized): the verdict is KEPT, its tamper-freedom is UNVERIFIED on
    the named points, and — recorded in the round header — a high-consequence
    close will not certify on it."""
    lines = ["⚠ tamper guard degraded — review KEPT, tamper-freedom UNVERIFIED on:"]
    lines += [f"    - {c}" for c in cautions]
    lines.append("  (recorded in the round header; an assertive/irreversible close treats "
                 "this round like a stale one)")
    return "\n".join(lines)


def _detect_tamper_safe(project_path: Path, task_file: Path | None, before: dict) -> list[str]:
    """`_detect_tamper` wrapped so an unexpected raise NEVER skips a guaranteed
    tamper banner/exit. Every I/O inside `_detect_tamper`/`_snapshot_repo_state`
    is already guarded, but this last-resort guard means a post-snapshot exit —
    on EITHER the panel or single-judge path — can always reach its `sys.exit`.
    On any error it surfaces a loud UNVERIFIED note (which itself trips the
    banner), the safe direction for a best-effort guard (panel rounds 5, 7)."""
    try:
        return _detect_tamper(project_path, task_file, before)
    except Exception as _e:   # noqa: BLE001 — last-resort guard, must not raise
        return [f"tamper check itself errored ({_e}) — review UNVERIFIED"]


def _tamper_banner(changes: list[str]) -> str:
    """Loud banner naming what a judge mutated during a review run."""
    bar = "!" * 60
    lines = [
        bar,
        "!! TAMPER DETECTED — a judge modified the repo during review !!",
        bar,
        "Judges are read-only evaluators; these changes are NOT trustworthy work:",
    ]
    lines += [f"  - {c}" for c in changes]
    lines += [
        "Do NOT ingest this review into task.md. Inspect and restore:",
        "  git status && git diff    # then: git checkout -- <path> / rm <new file>",
        bar,
    ]
    return "\n".join(lines)


def cmd_panel_review(cmd_args):
    import subprocess
    from concurrent.futures import ProcessPoolExecutor, TimeoutError as FuturesTimeout

    # Parse flags
    review_mode = "plan"
    web_search = False
    timeout_flag = None  # --timeout HARD override (raw str); resolved from config below
    soft_timeout_flag = None  # --soft-timeout SOFT override (prompt wind-down)
    budget_flag = None   # --budget override (claude judges only)
    extra_prompt = ""
    no_mind_map = False
    bare = False
    models_flag = None  # --models CSV → explicit judge set for this run
    remaining_args = []
    i = 0
    while i < len(cmd_args):
        if cmd_args[i] == "--mode" and i + 1 < len(cmd_args):
            review_mode = cmd_args[i + 1]
            i += 2
        elif cmd_args[i] == "--models" and i + 1 < len(cmd_args):
            models_flag = [s.strip() for s in cmd_args[i + 1].split(",") if s.strip()]
            i += 2
        elif cmd_args[i] == "--web-search":
            web_search = True
            i += 1
        elif cmd_args[i] == "--timeout" and i + 1 < len(cmd_args):
            timeout_flag = cmd_args[i + 1]
            i += 2
        elif cmd_args[i] == "--soft-timeout" and i + 1 < len(cmd_args):
            soft_timeout_flag = cmd_args[i + 1]
            i += 2
        elif cmd_args[i] == "--budget" and i + 1 < len(cmd_args):
            budget_flag = cmd_args[i + 1]
            i += 2
        elif cmd_args[i] == "--prompt" and i + 1 < len(cmd_args):
            extra_prompt = cmd_args[i + 1]
            i += 2
        elif cmd_args[i] == "--no-mind-map":
            no_mind_map = True
            i += 1
        elif cmd_args[i] == "--bare":
            bare = True
            i += 1
        else:
            remaining_args.append(cmd_args[i])
            i += 1

    if review_mode not in ("plan", "impl"):
        print(f"Error: unknown mode '{review_mode}'", file=sys.stderr)
        sys.exit(1)

    task_num = remaining_args[0] if remaining_args else ""
    if task_num.isdigit():
        task_num = task_num.zfill(3)

    # Task number is optional; --prompt required when omitted
    if not task_num and not extra_prompt:
        print("Error: 'panel-review' requires a task number or --prompt", file=sys.stderr)
        print("Usage: tasks panel-review [<number>] [--mode plan|impl] [--models codex:gpt-5.5,agy,...] [--prompt \"...\"] [--no-mind-map] [--bare] [--web-search] [--timeout SECONDS] [--soft-timeout SECONDS] [--budget USD]", file=sys.stderr)
        sys.exit(1)

    project_path = find_project_root()
    # Review knobs — precedence: --flag > env var > .agent/config.json > default,
    # with config acting as a FLOOR on the hard timeout. Hard = hang-safety
    # kill; soft = the deadline the judge is told to self-regulate against.
    from tasks.core import (
        format_soft_hard_timeout_label,
        format_timeout_label,
        resolve_judge_budget,
        resolve_review_soft_timeout,
        resolve_review_timeout,
    )
    timeout_secs = resolve_review_timeout(project_path, timeout_flag)
    soft_timeout_secs = resolve_review_soft_timeout(
        project_path, hard_timeout_secs=timeout_secs, cli_value=soft_timeout_flag,
    )
    panel_budget = resolve_judge_budget(project_path, budget_flag)

    # Resolve task file if task number given
    task_file = None
    task_path = None
    if task_num:
        tasks_dir = resolve_agent_dir(project_path) / "tasks"
        matches = list(tasks_dir.glob(f"{task_num}-*/task.md"))
        if not matches:
            print(f"Task {task_num} not found", file=sys.stderr)
            sys.exit(1)
        task_file = matches[0]
        task_path = str(task_file.relative_to(project_path))
        # P6 nudge: mechanically-detectable issues shouldn't cost judge
        # tokens — and 'an audit ran once' is not freshness: the note also
        # fires when the newest receipt's commit is not HEAD.
        from tasks.audit import audit_freshness_note
        try:
            _panel_head = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=project_path,
                capture_output=True, text=True).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            _panel_head = ""
        _audit_note = audit_freshness_note(
            task_file.read_text(encoding="utf-8"), _panel_head)
        if _audit_note:
            print(f"  note: {_audit_note}", file=sys.stderr, flush=True)

    from tasks.template import panel_plan_review_prompt, panel_impl_review_prompt

    # Build context PER TRANSPORT (1.5.3). stdin seats (claude/codex) have no
    # OS argv limit, so they get the high budget — an argv seat's ceiling
    # must not dictate what a stdin seat is allowed to read. Every truncation
    # is receipted (C3/P3) per transport, and a trimmed seat's PROMPT names
    # what it did not receive and where the full file lives.
    from tasks.core import (load_config, resolve_review_context_chars,
                            select_task_context)

    def _build_payload(budget: int) -> dict:
        parts, receipts = [], []
        trim_notice = ""
        if not bare:
            if not no_mind_map:
                mm_content = _load_mind_map(project_path)
                if mm_content:
                    parts.append(f"=== MIND_MAP.md ===\n{mm_content}")
            if task_file:
                task_content = task_file.read_text(encoding="utf-8")
                task_content, task_receipt = select_task_context(
                    task_content, budget // 2)
                if task_receipt:
                    receipts.append(task_receipt)
                    # Aliased on purpose: other arms' local `import re`
                    # statements make bare `re` a local of main() for the
                    # WHOLE function, so this path (only reached when the
                    # task was actually trimmed) crashed UnboundLocal from
                    # 1.5.3 until the 1.5.9 judge caught it.
                    import re as _re
                    _dm = _re.search(r"· dropped: (.+?)(?: · WARNING|$)", task_receipt)
                    _dropped = (_dm.group(1)[:200] if _dm else "some sections")
                    trim_notice = (
                        f"your inline copy of {task_path} was TRIMMED to fit "
                        f"your transport (dropped sections: {_dropped}) — read "
                        f"{task_path} in the repo for the full trace.")
                parts.append(f"=== {task_path} ===\n{task_content}")
            else:
                # Taskless: include recent chat log as project context
                chat_log = resolve_agent_dir(project_path) / "chat_log.md"
                if chat_log.exists():
                    chat_content = chat_log.read_text(encoding="utf-8", errors="replace")
                    max_chat = budget // 2
                    if len(chat_content) > max_chat:
                        receipts.append(
                            f"chat_log.md {len(chat_content):,} → {max_chat:,} chars "
                            "(kept most recent tail)")
                        chat_content = "[... older chat elided ...]\n\n" + chat_content[-max_chat:]
                    parts.append(f"=== .agent/chat_log.md (recent) ===\n{chat_content}")
        sc = "\n\n".join(parts)
        if len(sc) > budget:
            receipts.append(
                f"combined system context {len(sc):,} → {budget:,} chars "
                "(head-clamped — mind map is large; raise headroom or use "
                "--no-mind-map)")
            sc = sc[:budget] + "\n\n[... truncated ...]"
        return {"context": sc, "receipts": receipts, "trim_notice": trim_notice}

    _argv_budget = resolve_review_context_chars(project_path, stdin=False)
    _stdin_budget = resolve_review_context_chars(project_path, stdin=True)
    payloads = {"argv": _build_payload(_argv_budget)}
    payloads["stdin"] = (payloads["argv"] if _stdin_budget == _argv_budget
                         else _build_payload(_stdin_budget))
    for _tname in ("stdin", "argv"):
        if payloads[_tname]["receipts"]:
            print(f"  context[{_tname}]: " + " | ".join(payloads[_tname]["receipts"]),
                  file=sys.stderr, flush=True)

    # Judge execution L1: the project may declare commands safe to run in the
    # judge's read-only sandbox. Undeclared → clause absent, pre-1.5.3 prompt.
    _jv_raw = load_config(project_path).get("judge_verify")
    judge_verify_cmds = ([c for c in _jv_raw if isinstance(c, str) and c.strip()]
                         if isinstance(_jv_raw, list) else [])

    # Prompt strategy: bare/taskless → extra_prompt is full mission; with task → review prompt + optional steering
    if task_file:
        prompt_fn = panel_plan_review_prompt if review_mode == "plan" else panel_impl_review_prompt
        review_label = "plan review" if review_mode == "plan" else "impl review"
    else:
        prompt_fn = None
        review_label = "panel"

    # Output path: task dir when task given, agent_dir/ otherwise
    if task_file:
        judge_md = task_file.parent / "judge.md"
    else:
        agent_dir = resolve_agent_dir(project_path)
        agent_dir.mkdir(exist_ok=True)
        judge_md = agent_dir / "judge.md"

    # Discover available judges via adapter classes — each adapter declares
    # its own binary_name() and panel_variants(). Adding a new provider is
    # a one-line append to PANEL_ADAPTERS; no dispatch changes needed.
    from provider.adapters.claude import ClaudeAdapter
    from provider.adapters.codex import CodexAdapter
    from provider.adapters.antigravity import AntigravityAdapter
    from provider.adapters.pi import PiAdapter
    from provider.adapters.grok import GrokAdapter
    from provider.sandbox import load_judge_config, resolve_judge_spec
    PANEL_ADAPTERS = (ClaudeAdapter, CodexAdapter, AntigravityAdapter, GrokAdapter, PiAdapter)
    _JUDGE_ADAPTERS = {
        "claude": ClaudeAdapter, "codex": CodexAdapter,
        "agy": AntigravityAdapter, "pi": PiAdapter,
        "grok": GrokAdapter,
    }

    # Judge-set precedence: --models flag → models.json `panel` (shipped ⊕
    # project .agent/models.json) → legacy full fan-out (only if no config).
    if models_flag is not None:
        spec_names = models_flag
    else:
        spec_names = load_judge_config().get("panel") or None

    judges = []  # list of (adapter_cls, variant)
    if spec_names:
        skipped = []
        for nm in spec_names:
            try:
                provider, variant = resolve_judge_spec(nm)
            except ValueError as e:
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(1)
            cls = _JUDGE_ADAPTERS.get(provider)
            if cls is None:
                print(f"Error: no adapter for provider '{provider}' (spec '{nm}')", file=sys.stderr)
                sys.exit(1)
            if cls.is_available():
                judges.append((cls, variant))
            else:
                skipped.append(f"{nm} ({cls.binary_name()} not on PATH)")
        if skipped:
            print(f"  Skipped unavailable: {', '.join(skipped)}", flush=True)
    else:
        # No configured panel — legacy discovery (all providers × variants).
        for cls in PANEL_ADAPTERS:
            if cls.is_available():
                for variant in cls.panel_variants():
                    judges.append((cls, variant))

    if not judges:
        print("Error: no available judges. Install a provider CLI, or name "
              "reachable ones with --models (e.g. --models codex:gpt-5.5,agy).",
              file=sys.stderr)
        sys.exit(1)

    display_target = task_path or "the --prompt consultation"   # task 073 (C14)
    timeout_label = format_soft_hard_timeout_label(soft_timeout_secs, timeout_secs)
    hard_timeout_label = format_timeout_label(timeout_secs)
    print(
        f"Running panel {review_label} on {display_target} "
        f"({len(judges)} judges, timeout {timeout_label})...",
        flush=True,
    )
    _print_background_advisory(timeout_secs)

    def run_judge(judge_spec):
        adapter_cls, variant = judge_spec
        provider_name = adapter_cls.binary_name()
        label = f"{provider_name}:{variant}" if variant else provider_name
        # Per-transport payload (1.5.3): each seat gets the biggest context
        # its transport can carry; a trimmed seat's prompt says what was cut.
        _payload = payloads.get(adapter_cls.context_transport(), payloads["argv"])
        if prompt_fn:
            prompt = prompt_fn(
                task_path,
                inline_context=(provider_name != "claude"),
                soft_timeout_secs=soft_timeout_secs,
                hard_timeout_secs=timeout_secs,
                trim_notice=_payload["trim_notice"],
                judge_verify=judge_verify_cmds,
            )
            if extra_prompt:
                prompt += f"\n\nAdditional steering from the user:\n{extra_prompt}"
        else:
            # Taskless / --bare: still prepend the soft-deadline steering when
            # there is a soft budget, so free-form panel prompts self-regulate too.
            from tasks.template import time_budget_instruction
            steer = time_budget_instruction(soft_timeout_secs, timeout_secs)
            prompt = (steer + "\n\n" + extra_prompt) if steer else extra_prompt

        # Single attempt only — never restart a long-running judge. The
        # hard timeout is hang safety alone (None = no kill at all).
        # `_t0` wall clock brackets the subprocess for the spend record (task
        # 042); every return path reports its own elapsed + timed-out flag.
        _t0 = time.monotonic()
        try:
            adapter = adapter_cls(session_id="judge", project_root=project_path)
            output = adapter.run_headless_judge(
                prompt=prompt,
                model=variant,
                system_context=_payload["context"],
                web_search=web_search,
                timeout_secs=timeout_secs,
                budget_usd=panel_budget,
            )
            return label, output, int((time.monotonic() - _t0) * 1000), False
        except subprocess.TimeoutExpired as expired:
            # Keep whatever this seat had written. The marker stays FIRST so
            # the seat is still classified as failed and no reader mistakes a
            # truncated block for a finished review — but a seat that spent
            # the whole budget should still contribute what it found.
            _dur = int((time.monotonic() - _t0) * 1000)
            raw = getattr(expired, "stdout", None) or getattr(expired, "output", None) or ""
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            # Structured judge stdout (codex `--json`) is protocol frames: salvage
            # the completed message text, not the frames (task 056).
            from provider.usage import JudgeOutput as _JO, parse_usage as _pu, salvage_text as _salvage
            # A usage frame the CLI already wrote before the kill is real spend
            # (round 3): parse it STRICTLY from the partial stdout and carry it.
            _to_usage = _pu(raw)
            raw = _salvage(provider_name, raw.strip())
            marker = f"(timed out after hard {hard_timeout_label})"
            if raw:
                return label, _JO(
                    f"{marker}\n\n**INCOMPLETE** — killed mid-response; the "
                    f"findings below may be cut off and reached no conclusion:"
                    f"\n\n{raw}", usage=_to_usage), _dur, True
            return label, _JO(marker, usage=_to_usage), _dur, True
        except Exception as e:
            return label, f"(error: {e})", int((time.monotonic() - _t0) * 1000), False

    # Judge tamper guard (#1): judges are read-only evaluators, so snapshot
    # the repo before spawning and refuse to trust the run if the working
    # tree changed under them. On uncontained platforms (no seatbelt/bwrap,
    # or nested) project_writable=False was a no-op — this snapshot is then
    # the ONLY defense, so warn.
    from provider import sandbox as _sandbox_mod
    if not _sandbox_mod.containment_available():
        print("  ⚠ judges running UNCONTAINED (no usable OS sandbox here) — "
              "the tamper guard is the only defense against repo mutation.",
              file=sys.stderr, flush=True)
    _tamper_before = _snapshot_repo_state(project_path, task_file)

    # Run all judges in parallel
    import concurrent.futures
    results = {}
    # Spend record (task 042): one line PER SEAT, all sharing this panel's round.
    # Computed once here (not per seat) so every seat reports the same iteration.
    # The per-seat spend metadata is COLLECTED in the loop but EMITTED only after
    # the tamper hard-stop below — the journal write must never precede the tamper
    # banner, or a hostile-tree hang inside it (e.g. a FIFO-swapped lane path)
    # would suppress the banner, exactly the operation-before-banner class the
    # panels hunted down (rounds 1,5,7,8,9,10). A tamper hard-stop therefore
    # records no spend — consistent with the single/tail-cert paths.
    _spend_round = _next_review_round(project_path, task_file)
    _spend_pending = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(judges)) as executor:
        futures = {executor.submit(run_judge, j): j for j in judges}
        for future in concurrent.futures.as_completed(futures):
            label, output, _dur_ms, _timed_out = future.result()
            results[label] = output
            print(f"  [{label}] done", flush=True)
            # Seat carries model:effort (not the display `label`, which omits
            # claude's fixed effort) — derived from the judge spec for this future.
            _cls, _var = futures[future]
            _spend_pending.append((_seat_with_effort(_cls.binary_name(), _var),
                                   _dur_ms,
                                   _judge_status(output, timed_out=_timed_out),
                                   _parse_judge_usage(output)))

    _tamper_full = _detect_tamper_full(project_path, task_file, _tamper_before)
    # On a MUTATION the panel, like the single-judge path, does NOTHING but emit
    # the banner and exit — before the tree fingerprint and judge.md assembly/
    # write below, any of which could hang (an unbounded/FIFO read) or write
    # through an attacker-redirected task dir on a hostile tree (panel rounds
    # 9-10 codex:sol). The panel verdicts are untrustworthy on a mutated tree, so
    # not persisting them costs nothing; the operator inspects per the banner.
    # A CAUTION (task 059, T009): the guard could not fully run — that is not
    # evidence a judge wrote anything, so the paid verdict is KEPT; the notice is
    # printed and the round header records `**Tamper guard:** degraded`, which a
    # high-consequence close treats like a stale round (lifecycle, W3b).
    if _tamper_full["mutations"]:
        print("\n" + _tamper_banner(_tamper_full["mutations"]), file=sys.stderr, flush=True)
        sys.exit(1)
    _tamper_cautions = list(_tamper_full["cautions"])
    if _tamper_cautions:
        print("\n" + _degraded_notice(_tamper_cautions), file=sys.stderr, flush=True)

    # Clean tree: NOW emit the per-seat spend records (best-effort — fully
    # swallowed inside the helper, so they never affect the review; and past the
    # tamper banner, so they can never suppress it). One line per seat.
    for _lbl, _dms, _st, _usg in _spend_pending:
        _journal_review_spend(
            project_path, kind="panel", seat=_lbl,
            task=(task_num if task_file else None), round_no=_spend_round,
            duration_ms=_dms, status=_st, usage=_usg)

    # Classify each judge as succeeded vs failed — a failed judge must NOT
    # read as a clean empty review (T139) or a successful one. Shared
    # predicate (task 012) also catches claude's budget-exhaustion message,
    # which arrives as exit-0 stdout and previously counted as success.
    from tasks.models_check import budget_exceeded, judge_failed as _judge_failed

    failed = {lbl for lbl, out in results.items() if _judge_failed(out)}
    over_budget = {lbl for lbl in failed if budget_exceeded(results[lbl])}
    succeeded = len(results) - len(failed)

    # Verdict, not just a count (C4/P7). A panel is a gate: resolve the
    # quorum against the judges that actually launched, decide PASS/FAIL, and
    # exit non-zero below it (at the end, after all diagnostics print). The
    # tamper hard-stop below still wins — a mutated tree fails regardless of
    # how many judges succeeded.
    from tasks.core import resolve_panel_quorum
    panel_quorum = resolve_panel_quorum(project_path, len(results))
    panel_passed = succeeded >= panel_quorum
    verdict_reason = (
        f"{succeeded}/{len(results)} judges succeeded, quorum {panel_quorum}"
        + ("" if panel_passed else " — below quorum")
    )
    verdict_banner = (
        f"**PANEL VERDICT: {'PASS' if panel_passed else 'FAIL'}** — {verdict_reason}\n"
    )

    # Write judge.md (path already set above based on task_file presence).
    # (No tamper branch here: a mutated tree already exited above with the banner,
    # so this assembly/write is only reached on a clean tree.)
    display_label = task_path or extra_prompt[:60]
    _title = f"Panel {review_label.title()}" if task_path else "Panel Consultation"   # task 073 (C14)
    lines = [f"# {_title} — {display_label}\n", verdict_banner]
    lines.append(f"**Judges:** {succeeded}/{len(results)} succeeded | **Quorum:** {panel_quorum} | **Web search:** {'yes' if web_search else 'no'} | **Timeout:** {timeout_label}\n")
    # Context receipt (C3/P3), per transport (1.5.3): each seat saw what its
    # line says it saw — nothing was dropped without being named.
    _seats_by_transport = {"stdin": [], "argv": []}
    for _cls, _var in judges:
        _lbl = f"{_cls.binary_name()}:{_var}" if _var else _cls.binary_name()
        _seats_by_transport.setdefault(_cls.context_transport(), []).append(_lbl)
    for _tname in ("stdin", "argv"):
        _seats = _seats_by_transport.get(_tname) or []
        if not _seats:
            continue
        _r = payloads[_tname]["receipts"]
        _desc = " | ".join(_r) if _r else "full task.md + mind map delivered (no truncation)"
        lines.append(f"**Context[{_tname}: {', '.join(sorted(_seats))}]:** {_desc}\n")
    # Commit + tree-state stamps: name WHICH code state this panel reviewed.
    # The fingerprint (content-based, .agent-excluded) is what the close
    # gate's freshness advisory compares against — mtimes lie, content
    # doesn't.
    from tasks.core import tree_state_fingerprint
    try:
        _judged_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=project_path,
            capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        _judged_head = ""
    if _judged_head:
        lines.append(f"**Commit:** {_judged_head}\n")
    # Tamper-guard receipt (task 059): what the guard could and could not verify
    # for THIS round, on one line the close-gate reader can key on.
    # `degraded` marks the GUARD, not merely "something was noticed": a
    # concurrent commit is a caution but leaves the guard fully working, and
    # marking such a round degraded made tail certification unreachable after
    # any background commit (059 impl-panel r2, opus F2).
    lines.append("**Tamper guard:** " + _tamper_mark_text(_tamper_full) + "\n")
    _fp = tree_state_fingerprint(project_path)
    if _fp:
        lines.append(f"**Tree-state:** {_fp}\n")
        # Tail-cert F0 descriptor (task 036, finding F): ride the snapshot on the
        # round next to the stamp it is authenticated by — the close reads it to
        # enumerate the exact F0→final delta and, when that delta is
        # non-behavioral-only, certify freshness via a single judge instead of
        # burning a fresh panel. Best-effort: a descriptor that cannot be built
        # is simply absent, and the close-time enumerator fails CLOSED (require a
        # fresh panel) rather than certify on a missing snapshot.
        try:
            from tasks.core import (
                build_panel_snapshot, format_panel_snapshot_line,
                tree_state_fingerprint as _tsf,
            )
            # Task 060: an owner `fingerprint_exclude` that hides SOURCE makes
            # this stamp blind to that code. Say so loudly and emit NO
            # descriptor (the close blocks EXCLUDE-COVERS-CODE regardless; a
            # missing descriptor is the belt to that brace).
            from tasks.core import load_config as _lc060, owner_exclude_covers_behavioral
            try:
                _cov060, _hits060 = owner_exclude_covers_behavioral(
                    project_path, _lc060(Path(project_path)))
            except Exception:
                _cov060, _hits060 = True, ["<config unreadable>"]
            if _cov060:
                print("  ⚠ fingerprint_exclude hides BEHAVIORAL paths from the "
                      "freshness fingerprint (" + ", ".join(_hits060[:5])
                      + ") — this stamp cannot vouch for that code; the close "
                      "will block EXCLUDE-COVERS-CODE until the exclude is fixed",
                      file=sys.stderr, flush=True)
                _snap = None
            else:
                _snap = build_panel_snapshot(project_path, _fp)
            # Panel-time TOCTOU compare-and-swap (impl-panel sonnet#1 / codex#2 /
            # grok#2): build_panel_snapshot makes its OWN git calls after the
            # stamp above, so a code edit in the gap could be baked into the F0
            # baseline and never seen as a delta. Emit the descriptor ONLY if the
            # tree still hashes to the SAME `_fp` the panel reviewed; otherwise
            # omit it (a missing descriptor makes the close fail closed to a fresh
            # panel — never a silent stale baseline). None (a scope that could not
            # be captured cleanly, impl-panel opus#2/grok#3) is likewise omitted.
            if _snap is not None and _tsf(project_path) == _fp:
                lines.append(format_panel_snapshot_line(_snap) + "\n")
        except Exception:
            pass
    if failed:
        lines.append(f"**⚠ Failed judges:** {', '.join(sorted(failed))} — see their blocks below for the exit code / stderr. NOT a clean empty review.\n")
    if over_budget:
        lines.append(f"**⚠ Budget-capped judges:** {', '.join(sorted(over_budget))} — hit the ${panel_budget} cap and produced no review. Raise `judge_budget_usd` in `.agent/config.json` or pass `--budget`.\n")
    lines.append("\n")
    # Triage frame (T124): prepend the pushback discipline AT THE TOP so
    # the reading agent meets the instruction BEFORE the per-judge
    # findings — primes the triage lens before the data is read.
    # The judges themselves never see this; it's bundled with their
    # outputs purely for the reading agent. Mirrors the in-task pushback
    # gate from template.judge_section / judge_impl_section, but for
    # panel reviews (where findings live in judge.md, not task.md) the
    # discipline rides with the data. Helper is unit-tested in tests/test_cli.py.
    lines.extend(_panel_triage_frame())
    for label in sorted(results.keys()):
        tag = "  [FAILED]" if label in failed else ""
        lines.append("═" * 60)
        lines.append(f"  JUDGE: {label}{tag}")
        lines.append("═" * 60 + "\n")
        lines.append(results[label].strip())
        lines.append("\n\n")
    # Stack, never clobber (1.5.3): a re-run panel must not destroy the
    # previous round's verdicts. Newest round first; the close gate reads
    # only the newest round's mode+verdict. Best-effort so a hostile tree can't
    # crash the run before the exit code.
    from tasks.core import stack_judge_round
    _panel_save_failed = False
    try:
        stack_judge_round(judge_md, "\n".join(lines))
        _saved_note = f"\nSaved: {judge_md.relative_to(project_path)}"
    except OSError as _e:
        _panel_save_failed = True
        _saved_note = f"\n(could not write {judge_md.name}: {_e})"
    summary = (f"\nPANEL {'PASS' if panel_passed else 'FAIL'}: {verdict_reason}"
               + _saved_note)
    if failed:
        summary += f"; FAILED: {', '.join(sorted(failed))}"
    if over_budget:
        summary += (f"\nBudget notice: {', '.join(sorted(over_budget))} hit the "
                    f"${panel_budget} cap — raise judge_budget_usd in "
                    f".agent/config.json or pass --budget to re-run them.")
    print(summary, flush=True)

    # (Tamper already exited above with the banner, before this assembly/write.)
    # Chain of custody (codex:sol round-9, panel twin of the single-judge rule):
    # a clean panel whose verdicts could NOT be persisted must not report success
    # — the reading agent would ingest a PASS that left no judge.md to read.
    if _panel_save_failed:
        print(f"\nPanel verdicts could not be written to {judge_md.name}; "
              f"exiting nonzero so the run is not mistaken for a delivered review.",
              file=sys.stderr, flush=True)
        sys.exit(1)

    # Hard stop on probe-confirmed dead pins (task 012). Pattern
    # classification alone is only a hint (failure tails can echo prompt
    # fragments containing the very same signatures); a live probe of the
    # exact failed spec is what triggers exit 1. judge.md is already
    # written above, so the review is never lost. Timeout/budget/other
    # failures keep the soft behavior (exit 0 fall-through).
    if failed:
        from tasks.models_check import (
            NEEDS_CLI_UPGRADE, apply_confirmed, check_pins,
            confirm_dead_specs, render_report,
        )
        label_provider = {}
        for adapter_cls, variant in judges:
            provider_name = adapter_cls.binary_name()
            lbl = f"{provider_name}:{variant}" if variant else provider_name
            label_provider[lbl] = (provider_name, variant)
        confirmed = confirm_dead_specs(
            {lbl: results[lbl] for lbl in failed}, label_provider)
        if confirmed:
            print("\nHARD STOP: judge pin(s) unavailable (probe-confirmed):", file=sys.stderr)
            for lbl in sorted(confirmed):
                pv, detail = confirmed[lbl]
                fix = ("upgrade the codex CLI (`codex update`)"
                       if pv == NEEDS_CLI_UPGRADE
                       else "re-select the panel (`tasks models select`)")
                print(f"  {lbl}: {pv} — {detail} → {fix}", file=sys.stderr)
            print("\nCurrent availability:", file=sys.stderr)
            report = apply_confirmed(
                check_pins(project_path, probe=False, extra_specs=sorted(confirmed)),
                confirmed)
            print(render_report(report), file=sys.stderr)
            print("\nReview saved to judge.md but the panel is degraded — "
                  "decide how to proceed before re-running.", file=sys.stderr)
            sys.exit(1)

    # Quorum gate (C4/P7): below the required number of succeeding judges the
    # panel is a FAIL, not a report. judge.md leads with the same verdict and
    # is already written, so nothing is lost — but the run exits non-zero so a
    # caller (or the reviewing agent) cannot mistake a 4/7 panel for a pass.
    if not panel_passed:
        print(f"\nPANEL FAIL: {verdict_reason}. Raise the panel (fix/rerun the "
              "failed seats) or set `panel_quorum` in .agent/config.json if this "
              "bar is wrong for the project.", file=sys.stderr, flush=True)
        sys.exit(1)


_TAIL_CERT_TOTAL_CAP = 96 * 1024    # total judge payload — under the argv limit
_TAIL_CERT_DIFF_CAP = _TAIL_CERT_TOTAL_CAP   # a single content file's ceiling
                                             # (impl-panel r6 opus F3: one ceiling)


def _git_diff_text(repo, args, rel, exclude):
    """One `git diff` restricted to `rel`, encoding-safe + rc-checked. `--text`
    forces a textual diff so a doc with a NUL byte is shown, not summarized as
    "Binary files … differ" (impl-panel r6 grok#1/codex:sol#3). `--no-ext-diff`
    ignores any user difftool. Returns the diff text, or None on a git error OR a
    binary summary — either → the caller falls through to raw content."""
    import subprocess
    try:
        r = subprocess.run(
            ["git", "diff", "--text", "--no-ext-diff", *args, "--", rel, *exclude],
            cwd=repo, capture_output=True, text=True,
            encoding="utf-8", errors="replace")
        if r.returncode != 0:
            return None
        if "\nBinary files " in r.stdout or r.stdout.startswith("Binary files "):
            return None                    # still binary → fall to content
        return r.stdout
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _tail_cert_review_diff(project_path, snapshot) -> "str | None":
    """The F0→final delta the certifying judge reviews, enumerated PER SCOPE from
    the snapshot (impl-panel r5 sonnet#2: no string-prefix scope re-derivation, so
    a `code_roots` entry named like a top-level dir can't misattribute a path).

    DIFF-FIRST for transport friendliness (r5 grok#2/opus F2): for a tracked path
    the small unified diff — worktree-vs-HEAD, index-vs-HEAD (staged, what
    `git commit` ships), and F0..HEAD (committed since panel) — is shown, so the
    flagship large `docs/**/*.json` ledger tail (edited, not rewritten) stays well
    under the judge's argv limit. Only a path with NO diff (a new untracked doc, or
    one reverted to HEAD) falls back to its bounded, no-follow CONTENT (never
    following a symlink or blocking on a FIFO — r2 codex:sol#1/grok#1). Every
    certifiable path is therefore represented (diff or content or an explicit
    REMOVED note); a path whose content cannot be captured, or a total payload
    that would exceed the transport cap, returns None → the caller fails CLOSED."""
    import subprocess
    from tasks.core import (
        _enumerate_scope_delta, _fingerprint_exclude_pathspecs,
        _safe_read_regular, _tail_cert_scopes, classify_delta_paths, load_config,
    )
    try:
        cfg = load_config(Path(project_path))
    except Exception:
        cfg = {}
    exclude = _fingerprint_exclude_pathspecs(cfg)
    scopes = _tail_cert_scopes(Path(project_path), cfg)
    snap_scopes = (snapshot or {}).get("scopes", {}) if isinstance(snapshot, dict) else {}
    parts = []
    total = 0

    def _emit(text):
        nonlocal total
        # Count BYTES, not characters (impl-panel r11 opus F1): the transport
        # limit this protects (an argv judge's ~30 KiB cap) is bytes, so a
        # multi-byte-UTF-8 doc could otherwise exceed it while `total` stayed under.
        total += len(text.encode("utf-8", "replace"))
        if total > _TAIL_CERT_TOTAL_CAP:
            return False                   # payload too large for the transport
        parts.append(text)
        return True

    # sonnet#1 (r5): explicit scope-set / commit-presence check, so fail-closed
    # never rests on git's handling of a degenerate `""..HEAD` ref-range.
    if set(scopes.keys()) != set(snap_scopes.keys()):
        return None
    for name, repo in scopes.items():
        rec = snap_scopes.get(name) or {}
        if not isinstance(rec, dict) or not (rec.get("commit") or ""):
            return None
        f0 = rec.get("commit") or ""
        f0_dirty = rec.get("dirty") if isinstance(rec.get("dirty"), dict) else {}
        scope_paths = _enumerate_scope_delta(repo, f0, f0_dirty, exclude)
        if scope_paths is None:
            return None                    # git error → fail closed
        beh, non = classify_delta_paths(sorted(scope_paths),
                                        is_outer_scope=(name == ""))
        if beh:
            return None                    # a behavioral path here → fail closed
        for rel in non:
            prefixed = f"{name}/{rel}" if name else rel
            # DIFF-FIRST: worktree, staged, committed-since-F0 (all restricted to
            # rel). If ANY source ERRORS or is BINARY (`_git_diff_text` → None),
            # do NOT trust a partial diff — fall through to the explicit content +
            # index path (impl-panel r6 grok#1: a binary `--cached` was silently
            # skipped while the worktree diff still showed, hiding the staged blob).
            diffs = []
            diff_ok = True
            for label, args in (("worktree vs HEAD", ["HEAD"]),
                                ("STAGED (index) vs HEAD", ["--cached", "HEAD"]),
                                ("committed since panel",
                                 [f"{f0}..HEAD"] if f0 else None)):
                if args is None:
                    continue
                dt = _git_diff_text(repo, args, rel, exclude)
                if dt is None:
                    diff_ok = False
                    break
                if dt.strip():
                    diffs.append(f"# {prefixed} — {label}\n{dt}")
            if diff_ok and diffs:
                if not _emit("\n".join(diffs)):
                    return None
                continue
            # NO diff → show bounded content (worktree + index) or REMOVED.
            fpath = Path(repo) / rel
            shown = False
            if fpath.exists() or fpath.is_symlink():
                data = _safe_read_regular(fpath, _TAIL_CERT_DIFF_CAP)
                if data is None:
                    return None            # non-regular / oversize / unreadable
                if not _emit(f"--- current content of {prefixed} ---\n"
                             + data.decode("utf-8", "replace")):
                    return None
                shown = True
            try:
                in_index = subprocess.run(["git", "cat-file", "-e", f":{rel}"],
                                          cwd=repo, capture_output=True).returncode == 0
            except (OSError, subprocess.SubprocessError):
                return None
            if in_index:
                try:
                    gi = subprocess.run(["git", "show", f":{rel}"], cwd=repo,
                                        capture_output=True)
                except (OSError, subprocess.SubprocessError):
                    return None
                if gi.returncode != 0 or len(gi.stdout) > _TAIL_CERT_DIFF_CAP:
                    return None
                if not _emit(f"--- STAGED (index) content of {prefixed} ---\n"
                             + gi.stdout.decode("utf-8", "replace")):
                    return None
                shown = True
            if not shown:
                if not _emit(f"--- {prefixed}: REMOVED/DELETED from worktree AND "
                             f"index since F0 (a claim was withdrawn) ---"):
                    return None
    return "\n\n".join(parts) if parts else "(no certifiable delta content)"


def _tail_cert_prompt(non_behavioral, panel_summary, diff_text, nonce) -> str:
    """The dedicated tail-cert judge prompt (finding E — never cmd_single_review's
    plan/impl prompt). Frames the exact contract: a full panel ALREADY PASSED the
    code at tree F0; the only changes since are these NON-behavioral (docs/tests/
    claim) files; confirm the delta does not invalidate that verdict or introduce
    a false claim, and emit a single structured verdict line. The verdict token
    carries a per-invocation `nonce` so nothing in the reviewed CONTENT can forge
    it (impl-panel r2 grok#2)."""
    files = "\n".join(f"  - {p}" for p in non_behavioral) or "  (none listed)"
    return (
        "You are a TAIL-CERTIFICATION judge. A full multi-model panel already "
        "reviewed and PASSED this task's IMPLEMENTATION at an earlier tree state "
        "(call it F0). Since F0, the ONLY changes to the reviewed code state are "
        "in NON-BEHAVIORAL file classes — documentation, the guarantee ledger, "
        "tests, and claim-bearing docs. NO source/behavioral code changed (that "
        "was verified mechanically before you were called; if it had, this would "
        "be a fresh full panel instead).\n\n"
        f"The panel's verdict summary:\n{panel_summary}\n\n"
        f"The non-behavioral files changed since F0:\n{files}\n\n"
        "The exact delta diff (F0 → the tree being closed):\n"
        "```diff\n" + (diff_text or "(empty)") + "\n```\n\n"
        "YOUR TASK: decide whether this non-behavioral delta is consistent with "
        "the panel's PASS — i.e. it does not introduce a FALSE or UNSUPPORTED "
        "claim, does not contradict the code the panel approved, and does not "
        "smuggle behavioral change through a doc/test file. Read the repository "
        "as needed. You are NOT re-reviewing the code the panel already passed — "
        "only whether this delta invalidates that verdict.\n\n"
        "Answer with your reasoning FIRST. Then, as the VERY LAST line of your "
        "response, emit your verdict token — the token carries a one-time id you "
        "must copy exactly, and the last line is the ONLY line read:\n"
        f"    TAIL-CERT {nonce}: VERDICT\n"
        "replacing VERDICT with the single word PASS (the delta is consistent and "
        "the panel verdict stands) or FAIL (it needs a fresh full panel). The last "
        "line must contain ONLY that token and nothing else — do not quote this "
        "template earlier, and put no text after the verdict word. Any other final "
        "line blocks the close (fail-closed)."
    )


def _tail_cert_seat(project_path) -> str:
    """Resolve the tail-cert judge's seat spec (backend[:variant]) — the same
    `default_judge` the raw runner spawns — for the spend record. Best-effort:
    any failure → "default_judge" as an opaque marker (never raises)."""
    try:
        from provider.sandbox import load_judge_config, resolve_judge_spec
        dj = load_judge_config(project_path).get("default_judge") or "claude"
        try:
            backend, variant = resolve_judge_spec(dj)
        except ValueError:
            backend, variant = dj, None
        return _seat_with_effort(backend, variant)
    except Exception:
        return "default_judge"


def _tail_cert_task_num(task_file):
    """Best-effort task number from a tail-cert task_file path (its parent dir
    is `<NNN>-<slug>`). Returns the leading digits or None."""
    try:
        name = Path(task_file).parent.name
        num = name.split("-", 1)[0]
        return num if num.isdigit() else None
    except Exception:
        return None


def _run_tail_cert_judge_raw(project_path, prompt, timeout_secs) -> str:
    """Spawn the tail-cert judge (the configured `default_judge`, a real provider
    adapter) READ-ONLY under the same sandbox as every other judge, and return its
    raw output. A resolution error or a missing CLI returns an `(error: …)` string
    that the fail-closed parser maps to None → block.

    There is NO ambient production seam (impl-panel r3 grok#2/codex:sol#4): the
    round-2 `__test_stub__`/env override was a bypass because BOTH `.agent/
    models.json` and the env var are machine-local (models.json is gitignored), so
    it is removed. Deterministic tests of the PASS/FAIL path inject a fake verdict
    IN-PROCESS by monkeypatching `run_tail_cert_judge`."""
    import subprocess
    from provider.sandbox import load_judge_config, resolve_judge_spec
    dj = load_judge_config(project_path).get("default_judge") or "claude"
    try:
        backend, variant = resolve_judge_spec(dj)
    except ValueError:
        backend, variant = dj, None
    try:
        from provider.subagent import _adapter_class
        from tasks.core import resolve_judge_budget
        adapter = _adapter_class(backend)(session_id="tail-cert",
                                          project_root=Path(project_path))
        return adapter.run_headless_judge(
            prompt=prompt, model=variant, system_context="",
            web_search=False, timeout_secs=timeout_secs,
            budget_usd=resolve_judge_budget(project_path))
    except subprocess.TimeoutExpired as _expired:
        from provider.usage import JudgeOutput as _JO, parse_usage as _pu
        _raw = getattr(_expired, "stdout", None) or getattr(_expired, "output", None) or ""
        if isinstance(_raw, bytes):
            _raw = _raw.decode("utf-8", errors="replace")
        return _JO("(error: tail-cert judge timed out)", usage=_pu(_raw))   # spend survives (round 3)
    except Exception as e:
        return f"(error: tail-cert judge spawn failed: {e})"


def run_tail_cert_judge(project_path, snapshot, non_behavioral, panel_summary,
                        *, timeout_secs=None, task_file=None) -> "str | None":
    """Dedicated tail-cert judge (finding E): materialize the certifiable delta,
    build the tail-cert prompt, spawn the default single judge READ-ONLY under the
    same tamper backstop the other judge paths use, and parse its NONCED
    `TAIL-CERT <nonce>: PASS|FAIL` verdict FAIL-CLOSED. Returns "PASS"/"FAIL"/None
    (None = block). NEVER stacks judge.md and NEVER calls cmd_single_review.

    `task_file` is passed to the tamper guard so it fingerprints `task.md`
    specifically (impl-panel r7 opus#2/grok#1: `.agent/` is gitignored, so without
    naming the task file the guard's porcelain/dirty-hash sweep never covers the
    task record a rogue judge could rewrite during certification)."""
    import secrets
    from tasks.core import parse_tail_cert_verdict
    diff_text = _tail_cert_review_diff(project_path, snapshot)
    if diff_text is None:
        return None                    # could not capture the delta → fail closed
    nonce = secrets.token_hex(8)
    prompt = _tail_cert_prompt(non_behavioral, panel_summary, diff_text, nonce)
    # Same uncontained-judge warning the panel/single-judge paths print (impl-panel
    # r5 opus F4): on a host with no usable OS sandbox the read-only judge runs
    # with write access and only the tamper snapshot below defends the tree.
    try:
        from provider import sandbox as _sandbox_mod
        if not _sandbox_mod.containment_available():
            print("  ⚠ tail-cert judge running UNCONTAINED (no usable OS sandbox "
                  "here) — the tamper guard is the only defense against repo "
                  "mutation.", file=sys.stderr, flush=True)
    except Exception:
        pass
    # Tamper backstop (impl-panel r2 opus#2): the panel + single-judge paths
    # snapshot the repo before spawning and refuse the verdict if the read-only
    # judge mutated the tree. The close-time fingerprint CAS excludes `.agent/`,
    # so without this a rogue write to task.md/judge.md during certification is
    # invisible. Task 059 (T036): the snapshot is REQUIRED — if it cannot be
    # taken there is no verdict (a single certifying judge stands in for a whole
    # panel, so this path stays fail-closed where the panel/single paths advise).
    _tf = Path(task_file) if task_file else None
    try:
        _tb = _snapshot_repo_state(project_path, _tf)
    except Exception:
        return None                    # no snapshot → no certification
    # Spend record (task 042): bracket the raw tail-cert call here in the caller
    # (it has task_file + timeout in scope, and leaving the raw runner's 3-arg
    # signature untouched keeps its many test doubles valid). The elapsed spans
    # the judge subprocess. This caller is monkeypatched wholesale in the verdict
    # tests, so the emit runs in production, not in those doubles (by design).
    _tc_t0 = time.monotonic()
    raw = _run_tail_cert_judge_raw(project_path, prompt, timeout_secs)
    try:
        _full = _detect_tamper_full(project_path, _tf, _tb)
        if _full["mutations"] or _full["cautions"]:
            return None                # repo mutated OR guard degraded → no verdict
    except Exception:
        return None                    # tamper check itself failed → fail closed
                                       # (r4 grok#3: never certify on an errored guard)
    # Emit the spend record only PAST the tamper check (clean tree) — consistent
    # with the panel/single paths, and so the journal write can never precede /
    # hang and suppress a tamper stop. A FAILED/errored judge is still recorded
    # here (it ran and spent) before the fail-closed return below; only a tamper
    # hard-stop records nothing.
    _journal_review_spend(
        project_path, kind="tail-cert", seat=_tail_cert_seat(project_path),
        task=(_tail_cert_task_num(task_file) if task_file else None),
        round_no=_next_review_round(project_path, task_file),
        duration_ms=int((time.monotonic() - _tc_t0) * 1000),
        status=_judge_status(raw), usage=_parse_judge_usage(raw))
    # A FAILED/crashed/errored judge must NEVER certify (impl-panel grok#5):
    # format_judge_output PREFIXES a nonzero exit with "(FAILED \u2026" and a spawn/
    # resolution error with "(error: \u2026" \u2014 both at the START of the output. Match
    # only the LEADING marker (impl-panel r9 grok#1: a plain `in raw` also rejected
    # a legitimate PASS whose prose quotes the injected panel body's failed-seat
    # `(FAILED \u2014 exit N)` blocks, so the feature never fired). The last-line nonce
    # parse handles the rest.
    _rl = (raw or "").lstrip()
    if not raw or _rl.startswith("(error:") or _rl.startswith("(FAILED"):
        return None
    return parse_tail_cert_verdict(raw, nonce)


def cmd_single_review(cmd, cmd_args):
    """Thin owner of the temp-file registry around the real body (task 059 /
    T008 #5): whatever exit the body takes — return, `sys.exit`, or an
    exception — the codex `-o` transcript is unlinked."""
    try:
        return _cmd_single_review(cmd, cmd_args)
    finally:
        _drain_temp_registry()


def _cmd_single_review(cmd, cmd_args):
    # "judge" is a legacy alias — auto-detects mode from task status
    review_cmd = cmd
    if not cmd_args:
        print(f"Error: '{review_cmd}' requires a task number", file=sys.stderr)
        print(f"Usage: tasks {review_cmd} <number> [--backend claude|codex|agy|grok|pi] [--model <variant>] [--prompt \"...\"] [--timeout SECONDS] [--budget USD]  (default backend: models.json default_judge, ships \"opus\" (claude); --budget is claude-only)", file=sys.stderr)
        sys.exit(1)

    import subprocess

    # Parse flags
    backend = None   # explicit --backend; else from models.json default_judge
    model = None     # explicit --model (variant within the backend)
    extra_prompt = ""
    timeout_flag = None   # --timeout N  HARD (overrides env / config / default)
    soft_timeout_flag = None  # --soft-timeout N  SOFT (prompt wind-down)
    budget_flag = None    # --budget N   (claude only; overrides env / config / default)
    remaining_args = []
    i = 0
    while i < len(cmd_args):
        if cmd_args[i] == "--backend" and i + 1 < len(cmd_args):
            backend = cmd_args[i + 1]
            i += 2
        elif cmd_args[i] == "--model" and i + 1 < len(cmd_args):
            model = cmd_args[i + 1]
            i += 2
        elif cmd_args[i] == "--prompt" and i + 1 < len(cmd_args):
            extra_prompt = cmd_args[i + 1]
            i += 2
        elif cmd_args[i] == "--timeout" and i + 1 < len(cmd_args):
            timeout_flag = cmd_args[i + 1]
            i += 2
        elif cmd_args[i] == "--soft-timeout" and i + 1 < len(cmd_args):
            soft_timeout_flag = cmd_args[i + 1]
            i += 2
        elif cmd_args[i] == "--budget" and i + 1 < len(cmd_args):
            budget_flag = cmd_args[i + 1]
            i += 2
        else:
            remaining_args.append(cmd_args[i])
            i += 1

    # No --backend → models.json default_judge (provider or provider:variant,
    # project-overridable; since 1.5.12 ships as "opus" — the all-Claude default,
    # so a Claude-first user's review doesn't exercise the codex/grok adapter
    # drift). --model overrides the variant.
    if backend is None:
        from provider.sandbox import load_judge_config, resolve_judge_spec
        dj = load_judge_config().get("default_judge") or "claude"
        try:
            backend, dj_variant = resolve_judge_spec(dj)
        except ValueError:
            backend, dj_variant = dj, None
        if model is None:
            model = dj_variant

    # Accept friendlier aliases: "agy"/"gemini" → "antigravity", "qwen" → "pi"
    if backend in ("agy", "gemini"):
        backend = "antigravity"
    elif backend == "qwen":
        backend = "pi"
    if backend not in ("claude", "codex", "antigravity", "grok", "pi"):
        print(f"Error: unknown backend '{backend}'", file=sys.stderr)
        print("Supported: codex (default), claude, antigravity (alias: agy), grok, pi (alias: qwen)", file=sys.stderr)
        sys.exit(1)

    if not remaining_args:
        print(f"Error: '{review_cmd}' requires a task number", file=sys.stderr)
        sys.exit(1)

    task_num = remaining_args[0]
    if task_num.isdigit():
        task_num = task_num.zfill(3)
    project_path = find_project_root()
    # Review knobs — precedence: --flag > env var > .agent/config.json >
    # built-in default (resolvers live in tasks.core), with config as a
    # FLOOR on the hard timeout. Hard = hang-safety kill; soft = the
    # deadline the judge is told to wind down against.
    from tasks.core import (
        format_soft_hard_timeout_label,
        format_timeout_label,
        resolve_judge_budget,
        resolve_review_soft_timeout,
        resolve_review_timeout,
    )
    review_timeout = resolve_review_timeout(project_path, timeout_flag)
    review_soft_timeout = resolve_review_soft_timeout(
        project_path, hard_timeout_secs=review_timeout, cli_value=soft_timeout_flag,
    )
    review_budget = resolve_judge_budget(project_path, budget_flag)
    review_timeout_label = format_timeout_label(review_timeout)
    review_soft_hard_label = format_soft_hard_timeout_label(
        review_soft_timeout, review_timeout,
    )
    tasks_dir = resolve_agent_dir(project_path) / "tasks"
    matches = list(tasks_dir.glob(f"{task_num}-*/task.md"))
    if not matches:
        print(f"Task {task_num} not found", file=sys.stderr)
        sys.exit(1)

    task_file = matches[0]
    task_path = str(task_file.relative_to(project_path))

    from tasks.template import plan_review_prompt, impl_review_prompt

    # Build context: mind map + task content, transport-aware (1.5.3) —
    # stdin backends (claude/codex) get the high budget, argv backends the
    # byte-guarded one. Structure-aware and receipted (C3/P3).
    from tasks.core import (load_config, resolve_review_context_chars,
                            select_task_context)
    _sj_stdin = backend in ("claude", "codex")
    MAX_CONTEXT_CHARS = resolve_review_context_chars(project_path, stdin=_sj_stdin)
    context_parts = []
    context_receipts = []
    sj_trim_notice = ""
    mm_content = _load_mind_map(project_path)
    if mm_content:
        context_parts.append(f"=== MIND_MAP.md ===\n{mm_content}")
    task_content = task_file.read_text(encoding="utf-8")
    task_content, task_receipt = select_task_context(task_content, MAX_CONTEXT_CHARS // 2)
    if task_receipt:
        context_receipts.append(task_receipt)
        # Aliased for the same reason as the panel arm's trim-notice site:
        # bare `re` is an unbound main() local on this path (judge F1).
        import re as _re
        _dm = _re.search(r"· dropped: (.+?)(?: · WARNING|$)", task_receipt)
        sj_trim_notice = (
            f"your inline copy of {task_path} was TRIMMED "
            f"(dropped sections: {(_dm.group(1)[:200] if _dm else 'some sections')}) — "
            f"read {task_path} in the repo for the full trace.")
    context_parts.append(f"=== {task_path} ===\n{task_content}")
    system_context = "\n\n".join(context_parts)
    if len(system_context) > MAX_CONTEXT_CHARS:
        context_receipts.append(
            f"combined system context {len(system_context):,} → {MAX_CONTEXT_CHARS:,} "
            "chars (head-clamped)")
        system_context = system_context[:MAX_CONTEXT_CHARS] + "\n\n[... truncated for context budget ...]"
    if context_receipts:
        print("  context: " + " | ".join(context_receipts), file=sys.stderr, flush=True)
    _jv_raw = load_config(project_path).get("judge_verify")
    sj_judge_verify = ([c for c in _jv_raw if isinstance(c, str) and c.strip()]
                       if isinstance(_jv_raw, list) else [])

    # Determine mode: explicit from command, or auto-detect for legacy "judge"
    if review_cmd == "plan-review":
        review_mode = "plan"
    elif review_cmd == "impl-review":
        review_mode = "impl"
    else:  # legacy "judge" — auto-detect from status
        from tasks.core import _extract_status
        review_mode = "impl" if _extract_status(task_file).startswith("done") else "plan"

    _base_prompt_fn = plan_review_prompt if review_mode == "plan" else impl_review_prompt

    def prompt_fn(task_path_arg, inline_context=False):
        return _base_prompt_fn(
            task_path_arg,
            inline_context=inline_context,
            soft_timeout_secs=review_soft_timeout,
            hard_timeout_secs=review_timeout,
            trim_notice=sj_trim_notice,
            judge_verify=sj_judge_verify,
        )

    review_label = "plan review" if review_mode == "plan" else "impl review"

    def _emit_tamper(full):
        # A1: the loud tamper banner, shared by every post-snapshot single-judge
        # exit (timeout / budget / dead-pin / the normal hard-stop). Print-only:
        # each call site owns its own exit code, so this never exits itself.
        # Task 059: `full` is the mutations/cautions split — a MUTATION prints the
        # banner; a CAUTION alone prints the degraded notice (review kept).
        if full["mutations"]:
            print("\n" + _tamper_banner(full["mutations"]), file=sys.stderr, flush=True)
        elif full["cautions"]:
            print("\n" + _degraded_notice(full["cautions"]), file=sys.stderr, flush=True)

    def _safe_detect():
        # The single-judge alias for the module-level `_detect_tamper_full`
        # (which the panel path also uses — parity, opus round-7); never raises.
        return _detect_tamper_full(project_path, task_file, _tamper_before)

    def _tamper_mark(full):
        return _tamper_mark_text(full)

    def _bail_review_timeout(expired=None):
        # Only reachable when a finite HARD timeout is in force.
        #
        # A1 tamper guard (#1): a judge killed at the hard timeout can have
        # mutated the working tree BEFORE the kill — the panel path stops on
        # tamper and so must this exit. Detect and EMIT the banner FIRST, before
        # the partial-log write below, for two reasons: (a) that log lands under
        # the task dir and would self-flag if the snapshot ran after it; and
        # (b) a tampering judge is exactly the one that may have deleted or made
        # the task dir unwritable, so the fragile write must not be able to
        # swallow the banner by raising first (codex:sol Critical). `_tamper_before`
        # was taken before the judge spawned; the closure resolves it at call time
        # (every call site is after its assignment below).
        _to_changes = _safe_detect()
        _emit_tamper(_to_changes)
        if _to_changes["mutations"]:
            # 059 impl-panel r1 (grok #3): on a MUTATION this exit writes NOTHING
            # under the task dir — the partial log below would otherwise be
            # `atomic_write`n through a task directory a rogue may have swapped
            # for a symlink (the P6 write-through the panel/success hard stops
            # already refuse). The banner is already out; the partial output of a
            # review taken on a mutated tree is worthless anyway.
            sys.exit(1)
        #
        # Whatever the judge had already written is salvaged to a SEPARATE
        # `*.partial.log` rather than being dropped or overwriting the main
        # log. Both halves of that matter: a review killed at its ceiling has
        # usually produced most of its findings, and spending the full budget
        # to be handed nothing is the worst outcome; but a partial review must
        # never be mistaken for a complete one, nor replace a previous good
        # review. So: new file, explicit banner, still exit nonzero.
        # Persistence is BEST-EFFORT: a rogue that removed or locked the task
        # dir must not turn this bail into an uncaught crash — the banner is
        # already out, and the exit code must still be reached.
        partial = ""
        _to_usage = None
        if expired is not None:
            raw = getattr(expired, "stdout", None) or getattr(expired, "output", None) or ""
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            from provider.usage import parse_usage as _pu, salvage_text as _salvage
            _to_usage = _pu(raw)                        # a written usage frame is real spend (round 3)
            partial = _salvage(backend, raw.strip())   # codex frames → message text (task 056)
        saved_note = ""
        if partial:
            partial_log = task_file.parent / (
                _judge_log_name(backend).removesuffix(".log") + ".partial.log")
            try:
                atomic_write(
                    partial_log,
                    f"# INCOMPLETE {review_label} — the judge was killed at the hard "
                    f"timeout ({review_timeout_label}) mid-response.\n"
                    f"# This is what it had written by then. It is NOT a finished "
                    f"review: findings may be cut off and it reached no conclusion.\n"
                    f"# The previous complete review, if any, is untouched in "
                    f"{_judge_log_name(backend)}.\n\n{partial}\n",
                )
                saved_note = (f" Partial output ({len(partial)} chars) saved to "
                              f"{partial_log.relative_to(project_path)}.")
            except OSError as _save_err:
                saved_note = (f" Partial output ({len(partial)} chars) could NOT be "
                              f"saved ({_save_err}).")
        else:
            saved_note = " The judge produced no output before the kill."
        print(
            f"\n{review_label} hit hard timeout after {review_timeout_label} "
            f"({review_soft_hard_label}). Raise the hard kill with --timeout, "
            "PLAYBOOK_REVIEW_TIMEOUT_SECS, or .agent/config.json "
            "review_timeout_secs; move the soft deadline with --soft-timeout "
            "or review_soft_timeout_secs. Previous review log left untouched."
            + saved_note,
            file=sys.stderr, flush=True,
        )
        # Spend record (task 042): the timeout path exits here, before the common
        # completion emit below, so record the timed-out seat's spend now — but
        # ONLY on a clean tree. If the timed-out judge also tampered, the banner
        # already fired above and we record nothing, keeping the uniform rule "a
        # tamper hard-stop records no spend" across every review path. The closure
        # resolves `_spend_t0`/`_spend_seat`/`_spend_round` at call time (all call
        # sites are after their assignment). Best-effort — swallowed.
        if not _to_changes["mutations"]:
            _journal_review_spend(
                project_path, kind="single", seat=_spend_seat,
                task=task_num, round_no=_spend_round,
                duration_ms=int((time.monotonic() - _spend_t0) * 1000),
                status="timeout", usage=_to_usage)
        sys.exit(1)

    # Judge tamper guard (#1), same contract as the panel path: snapshot the
    # repo before spawning the single judge; warn if it will run uncontained.
    from provider import sandbox as _sandbox_mod
    if not _sandbox_mod.containment_available():
        print("  ⚠ judge running UNCONTAINED (no usable OS sandbox here) — "
              "the tamper guard is the only defense against repo mutation.",
              file=sys.stderr, flush=True)
    _tamper_before = _snapshot_repo_state(project_path, task_file)
    # One advisory before the backend dispatch — covers plan-review AND
    # impl-review across every backend (task 038).
    _print_background_advisory(review_timeout)

    # Spend record (task 042): bracket the judge subprocess. Only one backend
    # branch runs, so a single start stamp here measures whichever dispatch
    # fires. The seat spec + round are fixed for this single-judge invocation.
    _spend_t0 = time.monotonic()
    _spend_seat = _seat_with_effort(backend, model)
    _spend_round = _next_review_round(project_path, task_file)

    if backend == "claude":
        claude_bin = shutil.which("claude")
        if not claude_bin:
            print("Error: 'claude' not found on PATH", file=sys.stderr)
            sys.exit(1)

        prompt = prompt_fn(task_path)
        if extra_prompt:
            prompt += f"\n\nAdditional steering from the user:\n{extra_prompt}"
        env = os.environ.copy()
        env["CLAUDECODE"] = ""
        env.pop("CLAUDE_CODE_SSE_PORT", None)
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)
        env["PLAYBOOK_SESSION_ID"] = "judge"

        # Bypass flag injected by provider.sandbox.run() — don't pass here.
        # The judge is a read-only evaluator sandboxed via provider.sandbox
        # (write containment via seatbelt/bwrap). PLAYBOOK_SESSION_ID=judge
        # above lets hooks identify judge sessions if needed.
        # --effort high for the same reason as the panel adapter (see
        # ClaudeAdapter.run_headless_judge): a judge is bought for its
        # reasoning. 'high' not 'max' — this bills the owner's own Claude
        # quota. Kept in step with the adapter so `--backend claude` and a
        # claude panel seat review at the same depth.
        claude_args = ["-p", "--effort", "high", "--max-budget-usd", review_budget]
        if model:
            from provider.adapters.claude import ClaudeAdapter
            claude_args += ["--model", ClaudeAdapter._MODEL_MAP.get(model, model)]
        # Windows: passing system_context as an argv element overflows the
        # Win32 command-line cap (32,767 chars → WinError 206). `claude -p`
        # with no positional prompt reads stdin, so pipe context+prompt
        # instead of putting them on argv. encoding="utf-8" keeps the pipe
        # (and stdout decode) off the cp1252 locale default on Windows.
        full_prompt = f"{system_context}\n\n---\n\n{prompt}"

        from provider import sandbox as _sandbox
        print(f"Running {review_label} (claude) on {task_path}...", flush=True)
        try:
            result = _sandbox.run(
                "claude",
                claude_args,
                project_root=project_path,
                project_writable=False,   # judge is read-only — cannot mutate repo/task.md
                env=env,
                input=full_prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=review_timeout,
            )
        except subprocess.TimeoutExpired as _expired:
            _bail_review_timeout(_expired)

    elif backend == "codex":
        if not shutil.which("codex"):
            print("Error: 'codex' not found on PATH", file=sys.stderr)
            print("Install: https://github.com/openai/codex", file=sys.stderr)
            sys.exit(1)

        prompt = prompt_fn(task_path, inline_context=True)
        # Codex has no system prompt — inline context into the user prompt
        full_prompt = f"{system_context}\n\n---\n\n{prompt}"

        # Under the read-only judge sandbox (project_writable=False), codex
        # cannot write its `-o` transcript into the project tree. Point `-o`
        # at a temp file — system temp (/tmp, /var/folders) stays writable
        # under both seatbelt and bwrap — and copy it into the task dir from
        # the parent, after the tamper check (see the save block below).
        # Owned from allocation to process exit (task 059 / T008 #5): every
        # `sys.exit` below (timeout / budget / dead-pin / tamper) and any
        # exception before the spawn used to orphan this file.
        codex_log = _TempPath(suffix="-judge-codex.log").keep_until_exit()
        # Bypass flag (--dangerously-bypass-approvals-and-sandbox) inserted
        # after `exec` by provider.sandbox._compose_agent_argv.
        codex_args = ["exec"]
        if model:
            from provider.adapters.codex import _split_reasoning_effort
            model_id, effort = _split_reasoning_effort(model)
            codex_args += ["-m", model_id]
            if effort:
                codex_args += ["-c", f"model_reasoning_effort={effort}"]
        codex_args += [
            "-s", "workspace-write",
            "--ephemeral",
            "-C", str(project_path),
            "-o", str(codex_log),
            "--json",   # JSONL events incl. turn.completed usage (task 056); `-o` still holds the final message
            "-",  # read prompt from stdin
        ]

        codex_env = os.environ.copy()
        codex_env["PLAYBOOK_SESSION_ID"] = "judge"

        from provider import sandbox as _sandbox
        print(f"Running {review_label} (codex) on {task_path}...", flush=True)
        try:
            result = _sandbox.run(
                "codex", codex_args,
                project_root=project_path,
                project_writable=False,   # judge is read-only — cannot mutate repo/task.md
                env=codex_env,
                input=full_prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=review_timeout,
            )
        except subprocess.TimeoutExpired as _expired:
            _bail_review_timeout(_expired)

    elif backend == "antigravity":  # agy
        if not shutil.which("agy"):
            print("Error: 'agy' not found on PATH", file=sys.stderr)
            sys.exit(1)

        prompt = prompt_fn(task_path, inline_context=True)
        full_prompt = f"{system_context}\n\n---\n\n{prompt}"
        if extra_prompt:
            full_prompt += f"\n\nAdditional steering from the user:\n{extra_prompt}"

        if model:
            print(f"  (note: agy has no model flag — ignoring --model {model}; uses agy's UI-selected model)", flush=True)
        # Prompt goes on STDIN, not argv: `agy --print` with no positional
        # prompt reads stdin (agy >=1.0.15). Windows caps the command line
        # at 32,767 chars (WinError 206), so full_prompt on argv overflows
        # it — same fix as the claude branch above and the adapter's
        # run_headless_judge. --print mode ignores cwd, needs --add-dir;
        # no -m/--model flag yet (uses whatever the agy UI has set).
        # Bypass (--dangerously-skip-permissions) prepended by sandbox.
        agy_args = [
            "--add-dir", str(project_path),
            "--print",
        ]
        # agy's own internal wait — keep it in step with the subprocess
        # timeout when finite; omit it entirely when unlimited so agy does
        # not kill a judge that is still writing.
        if review_timeout is not None:
            agy_args += ["--print-timeout", f"{review_timeout}s"]

        agy_env = os.environ.copy()
        agy_env["PLAYBOOK_SESSION_ID"] = "judge"

        from provider import sandbox as _sandbox
        print(f"Running {review_label} (agy) on {task_path}...", flush=True)
        try:
            result = _sandbox.run(
                "agy", agy_args,
                project_root=project_path,
                project_writable=False,   # judge is read-only — cannot mutate repo/task.md
                env=agy_env,
                input=full_prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=review_timeout,
            )
        except subprocess.TimeoutExpired as _expired:
            _bail_review_timeout(_expired)

    elif backend == "grok":
        if not shutil.which("grok"):
            print("Error: 'grok' not found on PATH", file=sys.stderr)
            sys.exit(1)

        prompt = prompt_fn(task_path, inline_context=True)
        if extra_prompt:
            prompt += f"\n\nAdditional steering from the user:\n{extra_prompt}"

        # Argv construction is delegated to the adapter — it owns the
        # dialect (prompt as `-p` value, model:effort split, context
        # inlined ahead of the prompt). Task-013 lesson: inline argv
        # copies drift from the adapter; don't make a fifth one.
        # Judge-only extra: grok's web tools are default-on — strip them.
        from provider.adapters.grok import GrokAdapter
        try:
            inv = GrokAdapter("judge", project_path).headless_argv(
                prompt, model, context=system_context, structured=True)   # json: text + usage (task 056)
        except ValueError as e:  # bad model:effort spec — fail pre-spawn
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        grok_args = inv.argv + ["--disable-web-search"]

        # Windows caps the whole command line at 32,767 chars (WinError
        # 206); grok reads its prompt from argv (stdin is not a prompt
        # channel) — fail fast like the agy/pi arms.
        if os.name == "nt":
            payload = sum(len(a) + 1 for a in grok_args)
            if payload > 30_000:
                print(f"Error: grok judge prompt+context is ~{payload} chars on argv; "
                      "Windows caps the command line at 32,767 chars and grok reads its "
                      "prompt from argv — shrink the context or use another backend.",
                      file=sys.stderr)
                sys.exit(1)
        # POSIX per-element byte cap (#10) — the char budget can't bound argv bytes.
        from provider.argv_guard import argv_byte_error
        _argv_err = argv_byte_error(grok_args, "grok")
        if _argv_err:
            print(_argv_err, file=sys.stderr)
            sys.exit(1)

        grok_env = os.environ.copy()
        grok_env["PLAYBOOK_SESSION_ID"] = "judge"

        from provider import sandbox as _sandbox
        print(f"Running {review_label} (grok) on {task_path}...", flush=True)
        try:
            result = _sandbox.run(
                "grok", grok_args,
                project_root=project_path,
                project_writable=False,   # judge is read-only — cannot mutate repo/task.md
                env=grok_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=review_timeout,
            )
        except subprocess.TimeoutExpired as _expired:
            _bail_review_timeout(_expired)
        except OSError as _e:
            # #10: E2BIG (argv too long) and kin were an uncaught traceback on
            # this path — a knowable pre-dispatch condition presented as a
            # crash. Turn it into a clean, actionable error.
            print(f"Error: grok judge dispatch failed ({_e}). Most likely the "
                  "prompt+context exceeded this platform's argv byte limit — "
                  "shrink the context or use a stdin-capable backend (claude/codex).",
                  file=sys.stderr)
            sys.exit(1)

    else:  # pi (local Qwen via oMLX)
        if not (shutil.which("pi") or shutil.which("omlx")):
            print("Error: neither 'pi' nor 'omlx' found on PATH", file=sys.stderr)
            print("Install: oMLX app (https://omlx.app/) or pi CLI", file=sys.stderr)
            sys.exit(1)

        prompt = prompt_fn(task_path, inline_context=True)

        # Pi has no system prompt convention — append-system-prompt threads
        # the system context. --no-context-files skips AGENTS.md/CLAUDE.md
        # auto-load so the judge isn't biased by project conventions.
        # --provider oss points at the local oMLX endpoint (127.0.0.1:8000).
        pi_args = [
            "-p", prompt,
            "--provider", "oss",
            "--no-context-files",
            "--append-system-prompt", system_context,
        ]
        if model:
            pi_args += ["--model", model]

        # Windows caps the whole command line at 32,767 chars (WinError 206);
        # pi reads its prompt AND context from argv only (no verified stdin
        # path), so fail fast with a clear message rather than a cryptic
        # spawn failure — mirrors the guard in provider/adapters/pi.py.
        if os.name == "nt":
            payload = sum(len(a) + 1 for a in pi_args)
            if payload > 30_000:
                print(f"Error: pi judge prompt+context is ~{payload} chars on argv; "
                      "Windows caps the command line at 32,767 chars and pi reads its "
                      "prompt from argv only — shrink the context or use another backend.",
                      file=sys.stderr)
                sys.exit(1)
        # POSIX per-element byte cap (#10).
        from provider.argv_guard import argv_byte_error
        _argv_err = argv_byte_error(pi_args, "pi")
        if _argv_err:
            print(_argv_err, file=sys.stderr)
            sys.exit(1)

        pi_env = os.environ.copy()
        pi_env["PLAYBOOK_SESSION_ID"] = "judge"

        from provider import sandbox as _sandbox
        print(f"Running {review_label} (pi) on {task_path}...", flush=True)
        try:
            result = _sandbox.run(
                "pi", pi_args,
                project_root=project_path,
                project_writable=False,   # judge is read-only — cannot mutate repo/task.md
                env=pi_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=review_timeout,
            )
        except subprocess.TimeoutExpired as _expired:
            _bail_review_timeout(_expired)
        except OSError as _e:
            print(f"Error: pi judge dispatch failed ({_e}). Most likely the "
                  "prompt+context exceeded this platform's argv byte limit — "
                  "shrink the context or use a stdin-capable backend (claude/codex).",
                  file=sys.stderr)
            sys.exit(1)

    # Tamper hard-stop (#1) is the VERY FIRST thing after the judge returns, and
    # on tamper it does NOTHING but emit the banner and exit — no output stream,
    # no log save, no budget message, no dead-pin probe/report/write-back. Two
    # reasons this is the whole hard-stop: (a) nothing precedes the banner that
    # can raise/hang and suppress it (panels found that "operation-before-banner"
    # class one site at a time, rounds 1,5,7,8,9); and (b) nothing FOLLOWS it
    # either, so a rogue that redirected the task dir to a symlink or grew a file
    # cannot make a post-banner write escape the repo or a post-banner read hang
    # (round-10 codex:sol). The tampered review is untrustworthy, so losing its
    # log costs nothing; the operator inspects with `git status && git diff` per
    # the banner. `_safe_detect` itself never raises (last-resort guarded).
    _tamper_changes = _safe_detect()
    judge_log = task_file.parent / _judge_log_name(backend)
    if _tamper_changes["mutations"]:
        _emit_tamper(_tamper_changes)
        sys.exit(1)
    if _tamper_changes["cautions"]:
        _emit_tamper(_tamper_changes)          # degraded notice; the review is KEPT (task 059)
    _tamper_receipt = _tamper_mark(_tamper_changes)

    # Structured judge stdout (task 056): codex `--json` / grok `--output-format
    # json` carry the real token usage. Extract ONCE here — before the operator
    # stream, the budget/failure classifiers and the save block all read
    # `result.stdout` — so every consumer sees the review PROSE, and the usage
    # parsed from the ORIGINAL stdout rides into the spend record. Same rule as
    # the adapters (`judge_output_from_result`): rc≠0 keeps its failure text; a
    # recognized envelope with no review text becomes a FAILED result (never a
    # saved review, never a clean seat); unrecognized stdout stays verbatim.
    _spend_usage = None
    if backend in ("codex", "grok"):
        from provider.usage import extract_codex, extract_grok, judge_output_from_result
        _jo = judge_output_from_result(
            result, extract_codex if backend == "codex" else extract_grok,
            _sandbox.format_judge_output)
        _spend_usage = _jo.usage
        # ALWAYS publish the formatted text (round 2): on rc≠0 it is the
        # `(FAILED — exit N)` + tails the adapters/panel show, never raw JSON;
        # on rc 0 a failure-classified text (`judge_failed`: `(FAILED`,
        # `(no output)`, …) synthesizes rc 1 so the failure path — no saved
        # log, journal `fail` — runs exactly as for a real nonzero exit.
        from tasks.models_check import judge_failed as _jf_struct
        _rc = result.returncode if result.returncode != 0 else (1 if _jf_struct(str(_jo)) else 0)
        _text = str(_jo)
        if _text and not _text.endswith("\n"):
            _text += "\n"
        result = subprocess.CompletedProcess(
            getattr(result, "args", None), _rc, stdout=_text, stderr=result.stderr or "")

    # Clean tree: stream the judge's output for the operator (best-effort — a
    # closed sink must not crash a completed review).
    try:
        if result.stdout:
            print(result.stdout, end="", flush=True)
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr, flush=True)
    except OSError:
        pass

    # Save output — backend-specific log files (tamper already exited above)
    output = (result.stdout or "").strip()
    # Spend record (task 042): ONE line for this single-judge invocation, on the
    # clean-tree completion path. Placed here (before the budget/failure exits
    # below) so it covers ok, budget-exhausted (`fail`), and failure-marked runs
    # alike — the tokens were spent regardless of the verdict. The timeout path
    # already recorded in `_bail_review_timeout`; the tamper hard-stop above
    # deliberately records nothing (its banner-exit must have no trailing op).
    # Status is classified on the FORMATTED result (what `judge_failed` expects).
    _journal_review_spend(
        project_path, kind="single", seat=_spend_seat,
        task=task_num, round_no=_spend_round,
        duration_ms=int((time.monotonic() - _spend_t0) * 1000),
        status=_judge_status(_sandbox.format_judge_output(result)),
        usage=_spend_usage if _spend_usage is not None else _parse_judge_usage(output))
    # Budget exhaustion arrives as exit-0 stdout (task 012 L3): detect it
    # BEFORE saving so it never overwrites a prior good review, tell the
    # user how to raise the cap, and exit nonzero — it's not a review.
    from tasks.models_check import budget_exceeded as _budget_exceeded
    from tasks.models_check import judge_failed as _judge_failed_str
    if _budget_exceeded(output):
        kept = (f"; kept previous {judge_log.relative_to(project_path)}"
                if judge_log.exists() else "")
        print(f"\nJudge hit the ${review_budget} budget cap and produced no "
              f"review{kept}. Raise judge_budget_usd in .agent/config.json "
              f"or pass --budget.", flush=True)
        # (No tamper emit here: the early hard-stop above already exited on any
        # tamper, so this budget path is only reached with a clean tree.)
        sys.exit(1)
    # Failure-marked output (e.g. claude's bad-model message: stdout WITH
    # exit 1) is not a review either — never let it overwrite a prior good
    # log (task 012 I1). The formatted string is what classification below
    # sees, so save/keep and hard-stop agree on what counts as a failure.
    _formatted_result = _sandbox.format_judge_output(result)
    # Set only on the success path below; stays None when the review failed,
    # so the write-back at the end cannot ingest a rejected run's output.
    saved_review_text = None
    _log_save_failed = False
    if result.returncode != 0 and (not output or _judge_failed_str(_formatted_result)):
        if judge_log.exists():
            print(f"\nReview failed (exit {result.returncode}); kept previous {judge_log.relative_to(project_path)}", flush=True)
        else:
            print(f"\nReview failed (exit {result.returncode}); no output to save", flush=True)
    else:
        # Only codex writes its own log file (via `-o`); for it, stdout is a
        # fallback used only when that file is missing/empty. Every other
        # backend (claude/antigravity/grok/pi) MUST have stdout written here
        # — and OVERWRITTEN on each successful re-review, else a second run
        # prints "Saved" while silently keeping the stale log (task 014 I4).
        if backend == "codex":
            # codex wrote its clean final message to a temp file outside the
            # RO project; read it here (parent, post-tamper) and copy into the
            # task dir. stdout is the fallback when the temp file is empty.
            # Always overwrite on a successful review so a re-review can't
            # silently keep a stale log (task 014 I4).
            codex_out = ""
            try:
                codex_out = codex_log.read_text(encoding="utf-8")
            except OSError:
                pass
            try:
                codex_log.unlink()
            except OSError:
                pass
            saved_review_text = (
                codex_out if codex_out.strip() else (result.stdout or ""))
        else:
            saved_review_text = result.stdout or ""
        # Durable context receipt (1.5.3): what THIS judge was shown rides
        # with its findings — a log without its delivery record is a verdict
        # with no chain of custody.
        _ctx_header = ("[context] "
                       + (" | ".join(context_receipts) if context_receipts
                          else "full task.md + mind map delivered (no truncation)")
                       + "\n"
                       # Task 059: the tamper-guard receipt rides with the log so a
                       # kept-but-degraded review stays visibly unverified after
                       # the terminal output is gone.
                       + f"[tamper guard] {_tamper_receipt}\n\n")
        # Best-effort: a rogue that returns 0 but deleted/locked the task dir
        # must not crash this write BEFORE the tamper hard-stop below (codex:terra
        # round-5 Critical) — the judge output was already streamed to stdout, so
        # losing the log file is acceptable; losing the banner is not. But a
        # failed save is recorded: on a CLEAN review (no tamper) the findings
        # write-back below is REFUSED when the durable log could not be written,
        # so findings are never ingested with no chain of custody (codex:sol
        # round-6). When there IS tamper, the run exits nonzero at the banner
        # regardless, so the flag only gates the clean path.
        try:
            atomic_write(judge_log, _ctx_header + saved_review_text)
            print(f"\nSaved: {judge_log.relative_to(project_path)}", flush=True)
        except OSError as _save_err:
            _log_save_failed = True
            print(f"\nCould not save {judge_log.name} ({_save_err}); the judge "
                  f"output was printed above.", file=sys.stderr, flush=True)

    # Model-unavailable hard stop (task 012), same contract as the panel:
    # classify the FORMATTED result (both streams survive on nonzero exit
    # — codex 400s land on stderr, which stdout-only `output` misses),
    # then probe-confirm the exact spec before hard-stopping. Timeout
    # (handled above via _bail_review_timeout) and budget paths untouched.
    from tasks.models_check import (
        NEEDS_CLI_UPGRADE, apply_confirmed, check_pins, confirm_dead_specs,
        render_report,
    )
    _sj_provider = "agy" if backend == "antigravity" else backend
    _sj_spec = f"{_sj_provider}:{model}" if model else _sj_provider
    confirmed = confirm_dead_specs(
        {_sj_spec: _formatted_result}, {_sj_spec: (_sj_provider, model)})
    if confirmed:
        pv, detail = confirmed[_sj_spec]
        fix = ("upgrade the codex CLI (`codex update`)"
               if pv == NEEDS_CLI_UPGRADE
               else "re-select the panel (`tasks models select`)")
        print(f"\nHARD STOP: judge pin unavailable (probe-confirmed):\n"
              f"  {_sj_spec}: {pv} — {detail} → {fix}\n\nCurrent availability:",
              file=sys.stderr)
        # No tamper concern here: the early hard-stop above already exited on any
        # tamper, so this dead-pin path is only reached with a clean tree. The
        # availability report is still best-effort (a raising probe/cache should
        # not turn a dead-pin report into a traceback).
        try:
            report = apply_confirmed(
                check_pins(project_path, probe=False, extra_specs=[_sj_spec]),
                confirmed)
            print(render_report(report), file=sys.stderr)
        except Exception as _diag_err:   # noqa: BLE001 — diagnostics are advisory
            print(f"(availability report unavailable: {_diag_err})", file=sys.stderr)
        sys.exit(1)

    # Write the findings into task.md — LAST, deliberately. The judge is
    # sandboxed read-only and cannot do it itself (and must not be able to),
    # so the trusted parent does, exactly as the panel path does. This sits
    # after the budget, failure, model-unavailable and tamper checks because
    # every one of them can reject a run whose output still looks like a
    # review: writing any earlier would ingest findings the very next lines
    # declare untrustworthy. `saved_review_text` is the same content written
    # to the backend log, not raw stdout, so log and task.md never diverge.
    if _log_save_failed and result.returncode == 0 and saved_review_text and saved_review_text.strip():
        # The durable judge log could not be written (task dir deleted/locked,
        # or judge.log pre-existing as a directory). Do NOT ingest findings with
        # no chain of custody on an otherwise-clean review (codex:sol round-6) —
        # refuse and exit nonzero so a status-only caller can't mistake this for
        # a delivered review.
        print(f"\nReview succeeded but its durable log could not be saved "
              f"({judge_log.name}); refusing to write findings into "
              f"{task_file.relative_to(project_path)} without a chain of custody. "
              f"Judge output was printed above.", file=sys.stderr, flush=True)
        sys.exit(1)
    if result.returncode == 0 and saved_review_text and saved_review_text.strip():
        # Task 059: a degraded guard is stamped on the findings themselves so the
        # task record says what was and was not verified (clean adds nothing).
        _findings_text = saved_review_text
        if _tamper_changes["cautions"]:
            _findings_text = f"**Tamper guard: {_tamper_receipt}**\n\n" + saved_review_text
        refusal = _write_review_findings(task_file, review_mode, _findings_text)
        if refusal is None:
            print(f"Findings written to {task_file.relative_to(project_path)} "
                  f"(## {'Plan' if review_mode == 'plan' else 'Implementation'} Review)",
                  flush=True)
        else:
            # Exit non-zero: the review itself succeeded but its findings
            # were NOT delivered, and a caller that only checks the status
            # would otherwise treat an undelivered review as a clean one.
            print(f"\nCould not write findings into "
                  f"{task_file.relative_to(project_path)}: {refusal}\n"
                  f"They are saved in {judge_log.relative_to(project_path)} — "
                  f"paste them in by hand.", file=sys.stderr, flush=True)
            sys.exit(1)

    sys.exit(result.returncode)
