"""Executable pre-panel audit (report P6 / C6).

13 of 28 findings on one real task were mechanically detectable before a judge
spent a token — stale markers, half-merged conflict leftovers, zombie patch
files. The playbook had no equivalent; the local answer lived in prose and
depended on someone remembering to run it. This module makes it a command.

A sweep is a shell search for a BAD pattern, classified by exit code — the grep
convention, stated so no sweep can be a false green:

    exit 0  → FINDINGS   the pattern was found (candidates to fix before review)
    exit 1  → CLEAN      nothing found
    exit ≥2 → ERROR      the sweep itself did not run (bad regex, missing tool) —
                         NOT a pass. A measuring tool that can't run cannot report
                         "clean"; treating exit 2 as clean is the false green this
                         exists to prevent.

Two severities: `error` sweeps FAIL the audit when they find something (unambiguous
breakage — conflict markers, .orig/.rej files); `advisory` sweeps surface findings
without failing (TODO/FIXME are legitimate, just worth not making a judge find).
An ERROR from ANY sweep fails the audit regardless of severity.

Every default sweep has a negative control in tests/test_audit.py proving it fires
on a crafted dirty fixture — a sweep that cannot detect its own target is worse
than none.

The command is written to a temp script and run as `bash <script>`, never
interpolated into `bash -c`: a sweep may contain any quoting. Unlike the verify
runner it is NOT under `set -e` — a grep exiting 1 (clean) is the expected case,
not a failure. Sweeps needing backreferences must use `grep -P` (`grep -E`
silently matches nothing for a backreference — a known trap, carried here).
"""
from __future__ import annotations

import datetime
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from tasks.core import load_config

# Resolved once per process: a bash that actually runs, or (None, why-not).
_RESOLVED_BASH: "tuple[str | None, str] | None" = None


def _usable_bash() -> "tuple[str | None, str]":
    """A bash that runs a sentinel, or (None, reason). See run_sweep for why.

    On Windows a bare `bash` on PATH is usually the System32 WSL launcher: with
    no distro installed it prints an install hint and exits NON-ZERO. Fed to a
    sweep that would read as exit 1 = "clean" (classify), turning every audit
    into a false-green that never actually scanned. A presence check is not
    enough — probe for a sentinel. Honour $PLAYBOOK_VERIFY_BASH first (CI points
    it at Git Bash for exactly this reason). Cached per process.
    """
    global _RESOLVED_BASH
    if _RESOLVED_BASH is not None:
        return _RESOLVED_BASH
    candidate = os.environ.get("PLAYBOOK_VERIFY_BASH") or shutil.which("bash")
    # A cygpath -w conversion can drop the .exe; recover it.
    if candidate and not os.path.exists(candidate) and os.path.exists(candidate + ".exe"):
        candidate += ".exe"
    if not candidate:
        _RESOLVED_BASH = (None, "no bash found on PATH")
        return _RESOLVED_BASH
    try:
        p = subprocess.run([candidate, "-c", "printf ok"],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        _RESOLVED_BASH = (None, f"{type(exc).__name__}: {exc}")
        return _RESOLVED_BASH
    if p.returncode == 0 and (p.stdout or b"").strip() == b"ok":
        _RESOLVED_BASH = (candidate, "")
    else:
        _RESOLVED_BASH = (None, f"bash at {candidate} is not usable (rc={p.returncode}) "
                                "— likely the Windows WSL stub")
    return _RESOLVED_BASH

# --exclude-dir keeps the sweeps off build output and the workspace's own state,
# so a `- [ ]` in a task.md or a marker in node_modules is never a finding.
_EXCLUDES = "--exclude-dir=.git --exclude-dir=.agent --exclude-dir=node_modules --exclude-dir=.venv"

DEFAULT_SWEEPS = [
    {
        "name": "conflict-markers",
        "severity": "error",
        "why": "unresolved git conflict markers are half-merged, broken code",
        # `<<<<<<<` / `>>>>>>>` at line start are unambiguous — no legitimate use.
        # `=======` alone is skipped: a 7-char markdown underline collides with it.
        "command": rf"grep -rEn {_EXCLUDES} '^(<<<<<<<|>>>>>>>)' .",
    },
    {
        "name": "merge-artifacts",
        "severity": "error",
        "why": ".orig/.rej files left by a merge or failed patch are zombie files",
        # I6: the old `find … | grep .` took the pipeline status from grep, so a
        # find that errored mid-scan (permission-denied dir) → grep sees no
        # input → exit 1 → false CLEAN. `pipefail` does NOT fix it (find's error
        # exit is 1, colliding with grep's clean exit 1). Capture find's output
        # and check ITS status: a find error → exit 2 (ERROR, scan incomplete),
        # matches printed → exit 0 (FINDINGS), nothing → exit 1 (CLEAN).
        "command": (
            r"out=$(find . -path ./.git -prune -o \( -name '*.orig' -o -name '*.rej' \) -print) || exit 2"
            "\n"
            r'[ -n "$out" ] && { printf "%s\n" "$out"; exit 0; }'
            "\n"
            r"exit 1"
        ),
    },
    {
        "name": "stale-markers",
        "severity": "advisory",
        "why": "TODO/FIXME/XXX/HACK — candidates a review should not have to find",
        "command": rf"grep -rEn {_EXCLUDES} '(TODO|FIXME|XXX|HACK)' .",
    },
]

_VALID_SEVERITY = ("error", "advisory")


def resolve_sweeps(project_path) -> "list[dict]":
    """Sweeps to run: the safety defaults plus any declared in
    `.agent/audit.json` ("sweeps": [{name, command, why, severity}]). Set
    "disable_defaults": true to drop the defaults. Malformed project sweeps
    (missing name/command) are skipped — never crash an audit over config."""
    cfg = load_config(project_path)
    audit_cfg = cfg.get("audit") if isinstance(cfg.get("audit"), dict) else {}
    sweeps: "list[dict]" = [] if audit_cfg.get("disable_defaults") else [dict(s) for s in DEFAULT_SWEEPS]
    for s in audit_cfg.get("sweeps", []) if isinstance(audit_cfg.get("sweeps"), list) else []:
        if isinstance(s, dict) and s.get("name") and s.get("command"):
            sev = s.get("severity")
            sweeps.append({
                "name": str(s["name"]),
                "command": str(s["command"]),
                "why": str(s.get("why", "")),
                "severity": sev if sev in _VALID_SEVERITY else "advisory",
            })
    return sweeps


def classify(rc: int) -> str:
    """Map a sweep's exit code to a status. exit ≥2 is ERROR (did not run), never
    a pass — the whole point."""
    if rc == 0:
        return "findings"
    if rc == 1:
        return "clean"
    return "error"


def run_sweep(sweep: dict, project_path, timeout_secs=300) -> dict:
    """Run one sweep and classify it. Returns the sweep dict augmented with rc,
    status, and captured output. Never raises.

    `timeout_secs` (None = unlimited) bounds one sweep: greps are fast, but a
    project-declared sweep can hang, and a hung audit blocks the review it was
    meant to precede. A timeout is rc 124 → classified ERROR — the sweep did NOT
    complete its scan, which is never a pass."""
    command = sweep["command"]
    bash, why = _usable_bash()
    if bash is None:
        # Fail CLOSED: with no bash that can run the scan, the sweep did NOT run,
        # and an unrun scan can certify nothing. rc 126 → ERROR, never "clean" —
        # the alternative (bare `bash` hitting the Windows WSL stub, which exits
        # non-zero → classify() = "clean") is a false-green that defeats the
        # whole audit.
        return {
            "name": sweep.get("name", "(unnamed)"),
            "severity": sweep.get("severity", "advisory"),
            "why": sweep.get("why", ""),
            "command": command,
            "rc": 126,
            "status": classify(126),
            "output": f"(no usable bash to run the sweep: {why} — scan NOT run, "
                      "cannot certify clean)",
        }
    fd, script = tempfile.mkstemp(prefix="audit-sweep-", suffix=".sh")
    try:
        # newline="\n": Windows text-mode would emit a CRLF script and git-bash
        # would run a CR-corrupted sweep. No-op on POSIX.
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(command)
            if not command.endswith("\n"):
                fh.write("\n")
        proc = subprocess.run(
            [bash, script], cwd=str(project_path),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", timeout=timeout_secs,
        )
        rc, output = proc.returncode, (proc.stdout or "")
    except subprocess.TimeoutExpired as e:
        raw = e.stdout or ""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        rc = 124
        output = (f"(sweep timed out after {timeout_secs}s — scan incomplete, "
                  "NOT a pass)" + ("\n" + raw if raw else ""))
    except OSError as e:
        rc, output = 127, f"(audit runner failed: {e})"
    finally:
        try:
            os.unlink(script)
        except OSError:
            pass
    return {
        "name": sweep.get("name", "(unnamed)"),
        "severity": sweep.get("severity", "advisory"),
        "why": sweep.get("why", ""),
        "command": command,
        "rc": rc,
        "status": classify(rc),
        "output": output,
    }


# Code-file extensions a mind-map path reference is anchored on. Extension-anchored
# matching is what keeps function names, commit hashes, and prose out of the check.
_CODE_EXT = (
    "py js jsx ts tsx go rs java rb php c cpp cc h hpp cs kt swift scala sh bash zsh "
    "css scss html htm sql md json toml yaml yml tf proto gradle lua r pl ex exs vue svelte"
).split()
# Longest extension first + a boundary guard: with `js` tried before `json` and
# no guard, `.agent/config.json` matched as `agent/config.js` and was reported
# stale — a false positive found live in the StrataDB field test. A noisy
# detector gets ignored, which is worse than none.
_MM_PATH_RE = re.compile(
    r"([A-Za-z0-9_./\-]+\.(?:"
    + "|".join(sorted(_CODE_EXT, key=len, reverse=True))
    + r"))(?![A-Za-z0-9])(?::\d+)?"
)
_MM_EXCLUDE_DIRS = {".git", ".agent", "node_modules", ".venv", "__pycache__", "dist", "build"}


def _extract_mindmap_paths(text: str) -> "list[str]":
    """Slash-containing, extension-anchored file paths cited in the mind map.

    High precision on purpose (a noisy staleness check gets ignored, which is
    worse than none): only tokens that look like a real repo path are considered,
    URLs (`//host/…`) and domain-shaped first segments (`foo.com/…`) are dropped,
    and bare filenames without a directory are skipped as too ambiguous."""
    out = []
    for m in _MM_PATH_RE.finditer(text):
        start = m.start(1)
        # Preceded by ':' or '/' → part of a URL (`https:` → `//host/…`) or a
        # longer path token; not a standalone repo path.
        if start >= 1 and text[start - 1] in ":/":
            continue
        cand = m.group(1)
        # `./`-prefix removal, NOT lstrip("./") — lstrip strips a char SET, so it
        # mangled `.agent/config.json` into `agent/config.json` (field FP #3).
        while cand.startswith("./"):
            cand = cand[2:]
        if "/" not in cand:
            continue  # bare filename — too fuzzy to verify precisely
        segs = cand.split("/")
        if any(s in _MM_EXCLUDE_DIRS for s in segs[:-1]):
            continue  # lives in a dir the walker skips — we cannot judge it
        first_seg = segs[0]
        if "." in first_seg and not first_seg.startswith("."):
            continue  # domain-shaped (github.com/…); dot-DIRS (.claude/) are real paths
        out.append(cand)
    return out


def check_mindmap_staleness(project_path) -> "dict | None":
    """Built-in Python check (report P6, 'clean mindmap'): a MIND_MAP.md that
    cites a file path which no longer exists ANYWHERE in the repo (by exact path
    or basename) has drifted from the code — the agent reads its own map and is
    misled. Returns a sweep-shaped result, or None when there is no map to check.

    Deletion/full-rename precision: a path is stale only if neither it nor its
    basename survives, so a file merely moved to a new directory is NOT flagged
    (its basename still exists) — chosen to keep false positives near zero."""
    project_path = Path(project_path)  # str accepted — crashed live when a caller passed one
    mm = project_path / "MIND_MAP.md"
    if not mm.exists():
        return None
    try:
        text = mm.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    candidates = sorted(set(_extract_mindmap_paths(text)))

    relpaths, basenames = set(), set()
    for root, dirs, files in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in _MM_EXCLUDE_DIRS]
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), project_path).replace(os.sep, "/")
            relpaths.add(rel)
            basenames.add(f)

    def _placeholder(cand: str) -> bool:
        # `journal/NNN.md`-style template placeholders are documentation of a
        # NAMING SCHEME, not a citation of a file — an all-caps/digit stem on a
        # path that doesn't exist is a placeholder, not staleness (field FP).
        stem = os.path.splitext(os.path.basename(cand))[0]
        return bool(re.fullmatch(r"[A-Z0-9_]{2,}", stem))

    stale = [c for c in candidates
             if c not in relpaths and os.path.basename(c) not in basenames
             and not _placeholder(c)]
    # Advisory by default (a stale ref can be a legit historical note), but a
    # project that wants zero-tolerance sets audit.mindmap_severity: "error".
    cfg = load_config(project_path)
    audit_cfg = cfg.get("audit") if isinstance(cfg.get("audit"), dict) else {}
    severity = audit_cfg.get("mindmap_severity")
    if severity not in _VALID_SEVERITY:
        severity = "advisory"
    return {
        "name": "mindmap-stale-refs",
        "severity": severity,
        "why": "mind-map cites a file path that no longer exists — the map drifted from the code",
        "command": "(built-in)",
        "rc": 0 if stale else 1,
        "status": "findings" if stale else "clean",
        "output": "\n".join(f"MIND_MAP.md cites missing path: {c}" for c in stale),
    }


def check_task_bloat(project_path) -> "dict | None":
    """Advisory: an OPEN task.md that has outgrown the review context budget will
    be judged through a trimmed keyhole — nudge the sanctioned compaction ritual
    (move old review-round narrative verbatim to task-archive.md) BEFORE a judge
    reviews a partial view. Threshold: half the argv context budget (the task.md
    share of a review payload), overridable via audit.task_bloat_chars. Returns a
    sweep-shaped result, or None when there are no open tasks."""
    from tasks.core import (_extract_status, _iter_task_dirs, load_config,
                            resolve_review_context_chars)
    project_path = Path(project_path)
    cfg = load_config(project_path)
    audit_cfg = cfg.get("audit") if isinstance(cfg.get("audit"), dict) else {}
    try:
        threshold = int(audit_cfg["task_bloat_chars"]) if "task_bloat_chars" in audit_cfg else 0
    except (TypeError, ValueError):
        threshold = 0
    if threshold <= 0:
        threshold = resolve_review_context_chars(project_path) // 2

    findings = []
    seen_any = False
    for _num, _slug, tf in _iter_task_dirs(project_path):
        try:
            if _extract_status(tf).startswith("done"):
                continue
            seen_any = True
            size = tf.stat().st_size
        except OSError:
            continue
        if size > threshold:
            findings.append(
                f"{tf.parent.name}/task.md is {size:,} bytes (> {threshold:,}) — "
                "judges will see a trimmed view; compact old review narrative to "
                "task-archive.md (see the task sticker) or pin what must survive")
    if not seen_any:
        return None
    return {
        "name": "task-bloat",
        "severity": "advisory",
        "why": "an open task.md larger than the review budget gets judged through a keyhole",
        "command": "(built-in)",
        "rc": 0 if findings else 1,
        "status": "findings" if findings else "clean",
        "output": "\n".join(findings),
    }


def _git_lines(args, cwd) -> "list[str] | None":
    """`git <args>` stdout as a line list, or None when git is absent/failed.

    None vs [] matters: None = the instrument did not run (not a git repo, git
    missing), so callers stay SILENT rather than certifying anything; [] = it ran
    and found nothing."""
    try:
        r = subprocess.run(["git", *args], cwd=str(cwd),
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.splitlines()


def check_mindmap_node_freshness(project_path) -> "dict | None":
    """Advisory: a mind-map NODE whose cited code changed AFTER the node was last
    written is describing a subsystem that has moved on — stale institutional
    memory the agent will trust. Complements `check_mindmap_staleness` (that one
    catches DELETED paths; this catches paths that still exist but EVOLVED).

    Mechanism, git-only (returns None outside a git repo — nothing sound to
    compare against): each node is a line span in MIND_MAP.md, so `git blame`
    gives when the node itself was last edited (max committer-time over its
    lines; an UNCOMMITTED edit blames as ~now, so touching a node clears it). For
    each EXISTING path the node cites (reusing the node's own filename citations
    as its anchor — the map format already mandates real paths, so no new syntax),
    count commits to that path newer than the node. A node is flagged only when
    some cited path has >= `audit.node_freshness_commits` (default 2) such
    commits — one incidental edit is not drift, sustained change is. Advisory by
    default; `audit.node_freshness: false` disables it, `audit.node_freshness_severity`
    raises it. High-precision on purpose: a noisy staleness detector gets ignored,
    which is worse than none."""
    from tasks.mindmap import _node_starts, _node_title
    project_path = Path(project_path)
    cfg = load_config(project_path)
    audit_cfg = cfg.get("audit") if isinstance(cfg.get("audit"), dict) else {}
    if audit_cfg.get("node_freshness") is False:
        return None
    try:
        threshold = int(audit_cfg["node_freshness_commits"])
    except (KeyError, TypeError, ValueError):
        threshold = 2
    if threshold < 1:
        threshold = 1

    mm = project_path / "MIND_MAP.md"
    if not mm.exists():
        return None
    # Git gate: no HEAD → not a git repo (or empty) → stay silent.
    if _git_lines(["rev-parse", "HEAD"], project_path) is None:
        return None
    try:
        text = mm.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines(keepends=True)
    starts, in_fence = _node_starts(lines)
    if in_fence or not starts:
        return None

    # Cache: path -> sorted list of that path's commit times (newest first), or
    # None when the path is untracked/absent from history.
    path_times: "dict[str, list[int] | None]" = {}

    def _commit_times(path: str) -> "list[int] | None":
        if path not in path_times:
            out = _git_lines(["log", "--format=%ct", "--", path], project_path)
            path_times[path] = ([int(x) for x in out if x.strip().isdigit()]
                                if out else None)
        return path_times[path]

    findings = []
    for k, (idx, nid) in enumerate(starts):
        end = starts[k + 1][0] if k + 1 < len(starts) else len(lines)
        node_text = "".join(lines[idx:end])
        cited = sorted(set(_extract_mindmap_paths(node_text)))
        if not cited:
            continue
        # When was THIS node last edited? Max committer-time over its blame span.
        blame = _git_lines(
            ["blame", "-L", f"{idx + 1},{end}", "--line-porcelain", "--", "MIND_MAP.md"],
            project_path)
        if blame is None:
            continue
        node_time = max((int(ln.split(" ", 1)[1]) for ln in blame
                         if ln.startswith("committer-time ")), default=0)
        if node_time == 0:
            continue
        worst = None
        for cand in cited:
            # A path that no longer exists on disk is the STALENESS check's job
            # (`check_mindmap_staleness`), not freshness — skip it so a git-rm'd
            # file (whose `git log` history survives) can't be double-reported
            # here as "changed since the node".
            if not (project_path / cand).exists():
                continue
            times = _commit_times(cand)
            if not times:
                continue  # untracked (no history to compare against)
            newer = sum(1 for t in times if t > node_time)
            if newer >= threshold and (worst is None or newer > worst[1]):
                worst = (cand, newer)
        if worst is not None:
            title = _node_title(lines[idx])[1]
            findings.append(
                f"node [{nid}] ({title}): {worst[0]} changed in {worst[1]} commits "
                f"since the node was last edited — re-read and refresh, or confirm still accurate")

    severity = audit_cfg.get("node_freshness_severity")
    if severity not in _VALID_SEVERITY:
        severity = "advisory"
    return {
        "name": "mindmap-node-freshness",
        "severity": severity,
        "why": "a mind-map node whose cited code evolved after the node is stale memory",
        "command": "(built-in)",
        "rc": 0 if findings else 1,
        "status": "findings" if findings else "clean",
        "output": "\n".join(findings),
    }


def check_mindmap_dangling_links(project_path) -> "dict | None":
    """Advisory: a mind-map `[N]` cross-link that points at a node id which is
    not DEFINED anywhere in the map is a dead link — the agent follows it (or
    tries to `recall` it) and finds nothing. This is the internal-consistency
    complement to the staleness checks (those compare the map to the code; this
    compares the map to itself).

    Fence-aware (a `[9]` inside a ``` example is neither a definition nor a link),
    and precise by construction: only `[<digits>]` tokens are treated as node
    links, so markdown checkboxes (`- [ ]`), `[text](url)` links, version tags
    (`[1.5.0]`), and range tokens (`[1-5]`) never register. Each finding names the
    SOURCE node so the drift is fixable, not just flagged. Advisory by default;
    `audit.dangling_links_severity` raises it."""
    from tasks.mindmap import _node_starts, _FENCE_RE
    project_path = Path(project_path)
    mm = project_path / "MIND_MAP.md"
    if not mm.exists():
        return None
    try:
        text = mm.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines(keepends=True)
    starts, _in_fence = _node_starts(lines)
    if not starts:
        return None
    defined = {nid for _idx, nid in starts}
    start_at = {idx: nid for idx, nid in starts}
    link_re = re.compile(r"\[(\d+)\]")

    dangling = set()   # (source_node_id, missing_target)
    cur = None
    infence = False
    for i, ln in enumerate(lines):
        if _FENCE_RE.match(ln):
            infence = not infence
            continue
        if i in start_at:
            cur = start_at[i]
        if infence:
            continue
        for m in link_re.finditer(ln):
            tgt = int(m.group(1))
            if tgt not in defined:
                dangling.add((cur, tgt))

    findings = [
        (f"MIND_MAP.md node [{src}] links to [{tgt}], which is not a defined node"
         if src is not None else
         f"MIND_MAP.md preamble (before the first node) links to [{tgt}], "
         "which is not a defined node")
        for src, tgt in sorted(dangling, key=lambda p: (p[0] if p[0] is not None else -1, p[1]))
    ]
    cfg = load_config(project_path)
    audit_cfg = cfg.get("audit") if isinstance(cfg.get("audit"), dict) else {}
    severity = audit_cfg.get("dangling_links_severity")
    if severity not in _VALID_SEVERITY:
        severity = "advisory"
    return {
        "name": "mindmap-dangling-links",
        "severity": severity,
        "why": "a mind-map [N] link to an undefined node is a dead end the agent follows",
        "command": "(built-in)",
        "rc": 0 if findings else 1,
        "status": "findings" if findings else "clean",
        "output": "\n".join(findings),
    }


def check_mindmap_wellformed(project_path) -> "dict | None":
    """Advisory: STRUCTURAL defects in how the map is written, caught mechanically
    instead of hoping the author followed the /mindmap checklist. Three kinds, all
    unambiguous against the documented format:

      * duplicate node id — two `[5]` definitions; retrieval (`recall`, the index)
        can only keep one, so the other is silently lost;
      * missing title — a node with no `**Bold Title**`, so the index/TOC shows a
        degenerate label and the node is hard to scan for;
      * unreachable node — a non-routing node that NOTHING links to (the format's
        own rule is "every node 2+ links"), so it is dead memory: surfaced by the
        index but never arrived at by following links.

    Fence-aware (shares `_node_starts`); routing nodes (the first five in file
    order) are exempt from the unreachable check — they are entry points, not
    link targets. Advisory; `audit.wellformed_severity` raises it."""
    from tasks.mindmap import _node_starts, _FENCE_RE
    project_path = Path(project_path)
    mm = project_path / "MIND_MAP.md"
    if not mm.exists():
        return None
    try:
        text = mm.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines(keepends=True)
    starts, _in_fence = _node_starts(lines)
    if not starts:
        return None

    bold_re = re.compile(r"\*\*.+?\*\*")
    link_re = re.compile(r"\[(\d+)\]")

    # Duplicate ids (in file order of first offense).
    seen, dups = set(), []
    for _idx, nid in starts:
        if nid in seen and nid not in dups:
            dups.append(nid)
        seen.add(nid)

    # Missing titles: node line carries no **bold** span.
    missing_title = [nid for idx, nid in starts
                     if not bold_re.search(lines[idx].rstrip("\n"))]

    # Link targets (fence-aware, excluding each node's own definition token).
    defined = [nid for _idx, nid in starts]
    routing = set(defined[:5])
    linked_to = set()
    infence = False
    for k, (idx, nid) in enumerate(starts):
        end = starts[k + 1][0] if k + 1 < len(starts) else len(lines)
        for j in range(idx, end):
            ln = lines[j]
            if _FENCE_RE.match(ln):
                infence = not infence
                continue
            if infence:
                continue
            for m in link_re.finditer(ln):
                tgt = int(m.group(1))
                # A node referencing its OWN id (the definition token, or a
                # self-mention in its body) does not make it reachable from
                # elsewhere — else a self-citing island hides from this check.
                if tgt != nid:
                    linked_to.add(tgt)
    unreachable = [nid for nid in dict.fromkeys(defined)
                   if nid not in routing and nid not in linked_to]

    findings = []
    findings += [f"duplicate node id [{n}] — defined more than once; retrieval keeps only one" for n in dups]
    findings += [f"node [{n}] has no **bold title** — index/TOC shows a degenerate label" for n in missing_title]
    findings += [f"node [{n}] is unreachable — no other node links to it (dead memory; add a link or fold it in)" for n in unreachable]

    cfg = load_config(project_path)
    audit_cfg = cfg.get("audit") if isinstance(cfg.get("audit"), dict) else {}
    severity = audit_cfg.get("wellformed_severity")
    if severity not in _VALID_SEVERITY:
        severity = "advisory"
    return {
        "name": "mindmap-wellformed",
        "severity": severity,
        "why": "structural defects in the map make nodes unfindable or unreachable",
        "command": "(built-in)",
        "rc": 0 if findings else 1,
        "status": "findings" if findings else "clean",
        "output": "\n".join(findings),
    }


def run_audit(project_path) -> dict:
    """Run every resolved sweep plus the built-in mind-map staleness check.
    Returns {results, passed}. The audit FAILS when any sweep ERRORED (a broken
    instrument can't certify clean) or any error-severity sweep found FINDINGS
    (unambiguous breakage). Per-sweep ceiling from `audit.timeout_secs`
    (default 300; the review-knob grammar, so 0/none = unlimited)."""
    from tasks.core import _parse_timeout
    cfg = load_config(project_path)
    audit_cfg = cfg.get("audit") if isinstance(cfg.get("audit"), dict) else {}
    try:
        timeout = _parse_timeout(audit_cfg["timeout_secs"]) if "timeout_secs" in audit_cfg else 300
    except (TypeError, ValueError):
        timeout = 300
    results = [run_sweep(s, project_path, timeout_secs=timeout)
               for s in resolve_sweeps(project_path)]
    mm = check_mindmap_staleness(project_path)
    if mm is not None:
        results.append(mm)
    tb = check_task_bloat(project_path)
    if tb is not None:
        results.append(tb)
    nf = check_mindmap_node_freshness(project_path)
    if nf is not None:
        results.append(nf)
    dl = check_mindmap_dangling_links(project_path)
    if dl is not None:
        results.append(dl)
    wf = check_mindmap_wellformed(project_path)
    if wf is not None:
        results.append(wf)
    passed = not any(
        r["status"] == "error" or (r["status"] == "findings" and r["severity"] == "error")
        for r in results
    )
    return {"results": results, "passed": passed}


def _finding_lines(output: str, limit: int = 5) -> "list[str]":
    lines = [ln for ln in output.splitlines() if ln.strip()]
    return lines[:limit]


# First `###` entry after the heading is the NEWEST (upsert inserts newest-first).
_AUDIT_ENTRY_SHA_RE = re.compile(r"^### .*·\s*commit\s+([0-9a-fA-F]{7,40})", re.MULTILINE)


def audit_freshness_note(task_text: str, head_sha: str) -> "str | None":
    """None when a current audit receipt exists; else a one-line nudge.

    'Once ever' is not freshness: an audit that ran ten commits ago silently
    vouches for code it never scanned. The newest entry's recorded commit must
    match HEAD. With no git HEAD available, stay quiet — there is nothing sound
    to compare against, and a guess would be noise."""
    idx = task_text.find("## Pre-Panel Audit")
    if idx == -1:
        return ("no pre-panel audit on this task — consider `tasks audit <N>` "
                "first to clear mechanically-detectable issues before the panel")
    if not head_sha:
        return None
    m = _AUDIT_ENTRY_SHA_RE.search(task_text, idx)
    if m and (m.group(1) == head_sha or head_sha.startswith(m.group(1))):
        return None
    ran_at = m.group(1)[:7] if m else "(unknown commit)"
    return (f"pre-panel audit is STALE (ran at {ran_at}; HEAD is {head_sha[:7]}) — "
            "re-run `tasks audit <N>` so the panel reviews audited code")


def format_audit_receipt(audit: dict, *, timestamp: str | None = None, head_sha: str = "") -> str:
    """Render ONE receipt ENTRY for the `## Pre-Panel Audit` section (the heading
    belongs to core.upsert_task_section, newest entry first). The `commit` field
    is load-bearing: audit_freshness_note compares it to HEAD to tell a current
    audit from a stale one."""
    ts = timestamp or datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    out = [f"### {ts} · {'PASS' if audit['passed'] else 'FAIL'} · commit {head_sha or '(unknown)'}"]
    for r in audit["results"]:
        tag = {"clean": "CLEAN", "findings": f"FINDINGS", "error": "ERROR"}[r["status"]]
        n = len(_finding_lines(r["output"], limit=10_000)) if r["status"] != "clean" else 0
        count = f"({n})" if r["status"] == "findings" else ""
        sev = "" if r["severity"] == "error" else " ·advisory"
        out.append(f"    - [{tag}{count}] {r['name']}{sev} — {r['why']}")
        if r["status"] in ("findings", "error"):
            for ln in _finding_lines(r["output"], limit=5):
                out.append(f"        {ln[:200]}")
    out.append("")
    return "\n".join(out)
