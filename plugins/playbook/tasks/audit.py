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

Three severities: `error` sweeps FAIL the audit when they find something (unambiguous
breakage — conflict markers, .orig/.rej files); `advisory` sweeps surface findings
without failing (TODO/FIXME are legitimate, just worth not making a judge find);
`info` is below advisory — a visibility-only line that is never actionable and never
fails the audit (e.g. an acknowledged verify-contract removal, surfaced because its
ack lives on a self-servable path). An ERROR from ANY sweep fails the audit
regardless of severity; advisory and info never fail it.

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
import subprocess
import tempfile
from pathlib import Path

from tasks.bash_resolver import usable_bash
from tasks.core import load_config

# --exclude-dir keeps the sweeps off build output and the workspace's own state,
# so a `- [ ]` in a task.md or a marker in node_modules is never a finding.
# `__pycache__` is excluded because a compiled `.pyc` can carry a conflict/marker
# byte-run (compiled string constants, marshal data) that grep would report as a
# binary match — false-failing the error-severity conflict sweep and forcing a
# manual cache clear before every audit.
_EXCLUDES = ("--exclude-dir=.git --exclude-dir=.agent --exclude-dir=node_modules "
             "--exclude-dir=.venv --exclude-dir=__pycache__")
# `-I` treats a binary file (one containing NUL) as a non-match, so a marker byte
# baked into ANY binary — a stray `.pyc`, a compiled artifact outside __pycache__,
# an image — can never surface as a finding. Belt-and-suspenders with the
# __pycache__ exclude above; the exclude also spares grep the descent+scan work.
_GREP = f"grep -rIEn {_EXCLUDES}"

DEFAULT_SWEEPS = [
    {
        "name": "conflict-markers",
        "severity": "error",
        "why": "unresolved git conflict markers are half-merged, broken code",
        # `<<<<<<<` / `>>>>>>>` at line start are unambiguous — no legitimate use.
        # `=======` alone is skipped: a 7-char markdown underline collides with it.
        "command": rf"{_GREP} '^(<<<<<<<|>>>>>>>)' .",
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
        "command": rf"{_GREP} '(TODO|FIXME|XXX|HACK)' .",
    },
]

_VALID_SEVERITY = ("error", "advisory", "info")


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
    bash, why = usable_bash()
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


# The verify command is declared in the gate-exempt `.agent/config.json`, so it
# can be weakened/deleted unwatched and tasks then closed against a hollow bar.
# Every close receipt NAMES the commands it ran; this sweep flags any command
# that was run at SOME past close but is no longer in the current contract — a
# removed/weakened bar, made visible (T5).
_VC_CMD_RE = re.compile(r"^\s*- \[[^\]]+\] `([^`]*)`")
_VC_ENTRY_RE = re.compile(r"^### (\S+) · risk (\S+) · commit")


def _recorded_verify_commands(project_path) -> "tuple[dict, str | None]":
    """(mapping risk → union of every cmd1 recorded at a close of THAT risk,
    across every lane's tasks; latest-receipt-timestamp). Empty mapping + None
    when no receipt exists anywhere (first close — no baseline).

    Keyed BY RISK, not a flat all-risk union: a close of risk R records whatever
    the current config resolves for R, and the sweep must check each recorded
    command against the contract for THAT SAME risk. A flat union hid a cross-key
    move — a command shifted from `_always` to a single risk key still appears
    in *some* risk's current surface, so a flat "is it anywhere?" read stays
    clean while every other risk's bar silently lost it (panel round-3
    grok/opus). Per-risk comparison catches it.

    Still a union WITHIN each risk, not most-recent-only: a close records
    whatever the CURRENT config resolves, so a weaken-then-close would launder a
    most-recent baseline clean forever (panel opus). Every command ever run at a
    given risk stays flagged regardless of laundering.

    Each receipt section can hold several `### <ts> · risk <r>` entries
    (newest-first); each entry owns the command lines beneath it until the next
    entry, so an entry's commands are attributed to ITS risk. Sections are found
    by an EXACT heading line, scanned fence-aware so a prose mention (panel grok)
    or a ``` fenced/duplicate heading example (panel codex) is not mistaken for a
    real receipt, and EVERY such section in a file is read (not just the first).
    Both `task.md` and `task-archive.md` are scanned so a compacted receipt still
    baselines (panel sonnet/grok). Timestamps parse with fromisoformat, naive
    stamps normalized to UTC so aware/naive never crash the compare (panel)."""
    import datetime as _dt
    recorded: "dict[str, set]" = {}
    latest_ts = None
    latest_dt = None
    # Scan EVERY lane's task history, not just the caller's: `verify` in
    # `.agent/config.json` is a repo-global contract, so a weakening another
    # lane recorded must still baseline a fresh lane (panel: per-lane baseline
    # defeats a shared contract). Root lane + per-user lanes.
    agent = Path(project_path) / ".agent"
    receipts: "list[Path]" = []
    if agent.is_dir():
        # task.md AND task-archive.md: `tasks compact` now refuses to move a
        # receipt, but a receipt archived before that guard (or by hand) must
        # still baseline, or compaction would launder a weakening clean (panel
        # round-6 sonnet/grok). Reader-side defence in depth for the compact fix.
        for pat in ("tasks/*/task.md", "*/tasks/*/task.md",
                    "tasks/*/task-archive.md", "*/tasks/*/task-archive.md"):
            receipts += sorted(agent.glob(pat))
    for tf in receipts:
        try:
            lines = tf.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        # Skip only lines inside a properly CLOSED fence (a decoy example);
        # everything else — including an unclosed or 4-space-indented
        # pseudo-fence — is read, so a malformed fence cannot hide a real
        # receipt (fail closed, panel round-8). Treat EVERY
        # `## Verification Receipt` section in the file (not just the first),
        # attributing each `### … · risk …` entry's command lines to its risk.
        from tasks.core import _closed_fence_line_indices
        skip = _closed_fence_line_indices(lines)   # ONE shared CommonMark scanner
        in_receipt = False
        cur_risk = None
        for i, ln in enumerate(lines):
            if i in skip:
                continue
            # Match on the STRIPPED line everywhere (heading, entry, and command
            # bullet), so a legally 1-3-space-indented `### \u2026 \u00b7 risk \u2026` entry is
            # not orphaned from its command bullets (panel round-10 grok).
            s = ln.strip().lstrip("\ufeff")
            if s.startswith("## "):
                in_receipt = (s == "## Verification Receipt")
                cur_risk = None
                continue
            if not in_receipt:
                continue
            em = _VC_ENTRY_RE.match(s)
            if em:
                cur_risk = em.group(2)
                ts = em.group(1)
                try:
                    d = _dt.datetime.fromisoformat(ts)
                    # The entry regex accepts both offset-aware (`...+00:00`) and
                    # naive receipt timestamps; normalize a naive stamp to UTC so
                    # the `>` below never compares aware-vs-naive (which raises
                    # TypeError, NOT ValueError — and run_audit does not wrap this
                    # check, so an unhandled raise would abort the whole audit and
                    # silently disable detection). latest_ts is display-only, so a
                    # best-effort ordering here must never take down the sweep.
                    if d.tzinfo is None:
                        d = d.replace(tzinfo=_dt.timezone.utc)
                except (ValueError, TypeError):
                    d = None
                if d is not None and (latest_dt is None or d > latest_dt):
                    latest_dt, latest_ts = d, ts
                continue
            cm = _VC_CMD_RE.match(s)
            if cm and cur_risk is not None:
                recorded.setdefault(cur_risk, set()).add(cm.group(1))
    return recorded, latest_ts


def check_verify_contract_change(project_path) -> "dict | None":
    """Advisory sweep: was the `verify` contract weakened since a past close?

    Flags any command recorded at a past close that is no longer in the CURRENT
    contract FOR THE SAME RISK the close ran under. Comparing per-risk (not
    against a flat all-risk union) catches a cross-key move: a command shifted
    from `_always` into one risk key stays in *that* risk's surface but is gone
    from every other risk's bar, which a flat "is it anywhere?" read missed
    (panel round-3). Advisory, not error: the sweep cannot prove intent, so it
    surfaces the removal for acknowledgement rather than hard-blocking — a legit
    replacement is acknowledged, a real weakening is caught. Union-within-risk so
    a weaken-then-close cannot launder it clean.

    `verify_contract_ack` lives on the SAME gate-exempt `.agent/config.json` path
    this guards, so the actor who drops `verify` can acknowledge the drop in the
    same write — prevention is structurally impossible, so an acknowledged
    removal is never FULLY silenced: it degrades to an INFORMATIONAL (info,
    below advisory) line naming the command and the ack, still visible, never
    failing the audit (owner decision round-3). Visibility, not prevention, is
    the guarantee; the OS sandbox + human review are the containment.

    Returns None when there is nothing to surface — no prior receipt (the FIRST
    close has no baseline, exempt by design) or no command was dropped — matching
    the other built-in checks that stay silent unless they have a finding.

    Bound: comparison is per-command cmd1 (first line), because that is what the
    receipt records; a change confined to lines 2+ of a MULTI-LINE command is not
    distinguished. Rather than silently certify such a command clean, the sweep
    surfaces any multi-line current verify command as an ADVISORY
    (unsupported-for-drift-detection) — visibility, never silence (owner decision,
    B5). Full multi-line drift support is deferred. A clean sweep therefore means
    'no first-line command was dropped and no multi-line command is present', not
    'verify was never touched' — the enforcement journal retains the full
    per-close trail."""
    from tasks.core import load_config, resolve_verify_commands
    recorded, latest_ts = _recorded_verify_commands(project_path)
    if not recorded:
        return None                              # no baseline (first close)
    # `verify_contract_ack`: commands whose removal the owner has explicitly
    # accepted. An ack does not ERASE the removal (the ack is on the gate-exempt
    # path — see docstring); it downgrades it from a finding to informational. It
    # matches by command string across all risks (not risk-qualified). Malformed
    # entries are ignored.
    # ONE immutable config snapshot for the ack AND every risk resolution below:
    # a concurrent cross-risk edit between separate loads could otherwise
    # synthesize a state that never existed on disk (panel round-11 codex).
    cfg = load_config(project_path)
    _ack = cfg.get("verify_contract_ack") if isinstance(cfg, dict) else None
    ack = {a for a in _ack if isinstance(a, str)} if isinstance(_ack, list) else set()
    # Compare PER RISK and keep the risk attribution all the way through the
    # report: a command dropped from one risk's bar can still be current in
    # another's, so a flattened "dropped: [X] / current: [X]" line reads as a
    # self-contradiction an agent dismisses (panel round-7/8). Each output line
    # names its risk instead.
    removed_by_risk: "dict[str, list]" = {}      # risk -> sorted UNacknowledged drops
    acked_by_risk: "dict[str, list]" = {}        # risk -> sorted acknowledged drops
    current_by_risk: "dict[str, list]" = {}      # risk -> sorted current bar
    multiline_cmds: "set[str]" = set()           # cmd1 of any MULTI-LINE current command
    for risk, cmds in sorted(recorded.items()):
        current_r: "set[str]" = set()
        for _lbl, c in resolve_verify_commands(project_path, risk, cfg=cfg):
            cs = c.strip()
            if not cs:
                continue
            lines = cs.splitlines()
            current_r.add(lines[0])
            if len(lines) > 1:
                # cmd1 IS what the receipt records and what this sweep compares,
                # so a weakening confined to lines 2+ of a multi-line command is
                # invisible to drift detection. Surface it rather than pass silent.
                multiline_cmds.add(lines[0])
        current_by_risk[risk] = sorted(current_r)
        dropped = cmds - current_r
        if not dropped:
            continue
        un = sorted(dropped - ack)
        ac = sorted(dropped & ack)
        if un:
            removed_by_risk[risk] = un
        if ac:
            acked_by_risk[risk] = ac
    if not removed_by_risk and not acked_by_risk and not multiline_cmds:
        return None                              # nothing dropped, nothing opaque — clean

    # Fail-loud, never silence (owner decision, B5): a multi-line current verify
    # command cannot be drift-checked past its first line, so it is reported as an
    # advisory even when the cmd1 comparison found no drop. Full multi-line drift
    # support is deferred — this only makes the LIMITATION visible.
    def _multiline_lines():
        return ["verify command(s) are MULTI-LINE and unsupported for drift "
                "detection — only the first line (cmd1) is compared, so a change "
                "on any later line is NOT detected by this sweep (check the full "
                "command / enforcement journal by hand):"] + \
               [f"  [multi-line] {c1} …" for c1 in sorted(multiline_cmds)]

    def _lines(mapping):
        return [f"  [{r}] dropped: {mapping[r]}; {r} now runs: "
                f"{current_by_risk.get(r) or ['(none declared)']}"
                for r in sorted(mapping)]

    if removed_by_risk:
        out = ["verify command(s) recorded at a past close are no longer in the "
               "contract for the risk that close ran under (weakened?) — latest "
               f"close {latest_ts or '(unknown)'}:"]
        out += _lines(removed_by_risk)
        if acked_by_risk:
            out += ["  acknowledged (informational, still recorded):"]
            out += _lines(acked_by_risk)
        if multiline_cmds:
            out += _multiline_lines()
        out.append(
            "  If a removal is intentional, add the command to "
            "`verify_contract_ack` in .agent/config.json to downgrade it to an "
            "informational line (still visible — an ack never fully silences a "
            "removal); otherwise restore the `verify` command.")
        return {
            "name": "verify-contract-change",
            "severity": "advisory",
            "why": ("a verify command run at a past close is gone from the "
                    "gate-exempt .agent/config.json — a silently weakened verify "
                    "closes tasks against a hollow bar; acknowledge if intentional"),
            "status": "findings",
            "output": "\n".join(out),
        }
    if multiline_cmds:
        # No cmd1 drop, but a multi-line current command means the sweep cannot
        # certify the LATER lines unchanged — an ADVISORY (above the ack-info
        # tier), never a silent pass. Any acknowledged cmd1 drop is folded in.
        out = _multiline_lines()
        if acked_by_risk:
            out += ["  acknowledged cmd1 removals (informational, still recorded):"]
            out += _lines(acked_by_risk)
        out.append(
            "  Full multi-line drift detection is deferred; until then, review a "
            "multi-line verify command's later lines by hand (the enforcement "
            "journal retains the full per-close command trail).")
        return {
            "name": "verify-contract-change",
            "severity": "advisory",
            "why": ("a multi-line verify command is only drift-checked on its "
                    "first line — a weakening on a later line would not be "
                    "detected; surfaced so it is never silently certified clean"),
            "status": "findings",
            "output": "\n".join(out),
        }
    # Only acknowledged removals remain: informational, never a finding, never
    # failing the audit — but never fully silenced (the ack list is self-servable).
    out = ["verify command(s) recorded at a past close were removed and "
           "ACKNOWLEDGED in .agent/config.json — latest close "
           f"{latest_ts or '(unknown)'}:"]
    out += _lines(acked_by_risk)
    out.append(
        "  Informational only (the ack list is on the same gate-exempt path it "
        "guards, so this is visibility, not prevention). Confirm the removal was "
        "intended.")
    return {
        "name": "verify-contract-change",
        "severity": "info",
        "why": ("a verify command run at a past close was removed but "
                "acknowledged in .agent/config.json — surfaced for visibility, "
                "below finding severity, never fails the audit"),
        "status": "findings",
        "output": "\n".join(out),
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
    # Built-in checks run through an ISOLATING wrapper: unlike resolved sweeps
    # (which go through run_sweep and never raise), these are plain functions, and
    # run_audit used to call them unwrapped — so a single raise (e.g. the round-5
    # aware/naive-timestamp TypeError, or a future edit, or a malformed config)
    # aborted the ENTIRE audit, silently disabling every other sweep including
    # this integrity guard (panel round-8 opus). A raise now degrades to one
    # error-status result: one broken instrument can't take down the panel.
    for name, check in (
        ("mindmap-stale-refs", check_mindmap_staleness),
        ("task-bloat", check_task_bloat),
        ("mindmap-node-freshness", check_mindmap_node_freshness),
        ("mindmap-dangling-links", check_mindmap_dangling_links),
        ("mindmap-wellformed", check_mindmap_wellformed),
        ("verify-contract-change", check_verify_contract_change),  # T5 drift
    ):
        try:
            r = check(project_path)
        except Exception as exc:                  # noqa: BLE001 — degrade, never abort
            r = {
                "name": name,
                "severity": "error",              # a broken instrument can't certify clean
                "why": "the built-in check raised — audit cannot certify it clean",
                "status": "error",
                "output": f"{type(exc).__name__}: {exc}",
            }
        if r is not None:
            results.append(r)
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
        sev = {"error": "", "info": " ·info"}.get(r["severity"], " ·advisory")
        out.append(f"    - [{tag}{count}] {r['name']}{sev} — {r['why']}")
        if r["status"] in ("findings", "error"):
            for ln in _finding_lines(r["output"], limit=5):
                out.append(f"        {ln[:200]}")
    out.append("")
    return "\n".join(out)
