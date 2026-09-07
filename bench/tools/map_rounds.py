#!/usr/bin/env python3
"""map_rounds — print the round↔commit EVIDENCE for a historical task so the
corpus builder confirms the mapping by hand instead of guessing (plan §4.1).

    python3 bench/tools/map_rounds.py --workspace ~/ws --task 36 --repo ~/ws/app [--pad-minutes 90]

Prints three tables and a suggested pairing:
  ROUNDS    — every `# Panel … Review` block in judge.md (+ judge-archive.md) in FILE
              order, with verdict, judges, the outer `**Commit:**` and the nested-scope
              commits from `**Panel-snapshot:**` when present. Playbook PREPENDS new
              rounds, so the file is NEWEST-FIRST: never read position as chronology.
  RECEIPTS  — `### <ts> · … · commit <sha>` lines from task.md (Pre-Panel Audit +
              Verification Receipt), chronological. An audit receipt is written just
              before a panel launches, so it dates a round.
  COMMITS   — the repo's commits between the first receipt − pad and the last + pad.
Snapshot scope commits are the only DETERMINISTIC link (plugin ≥ task 039, HowFar ≥
015); everything else is dated evidence for a human decision. Read-only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from bench.tools._common import (ToolError, find_task_dir, git, read_text, short,  # noqa: E402
                                 utf8_stdio)

_ROUND_HEADER_RE = re.compile(r"^# (Panel (?:Impl|Plan) Review|Plan Review|Impl(?:ementation)? Review)\b.*$",
                              re.MULTILINE)
_VERDICT_RE = re.compile(r"\*\*PANEL VERDICT:\s*(PASS|FAIL)\*\*")
_JUDGES_RE = re.compile(r"\*\*Judges:\*\*\s*(\d+/\d+)")
_COMMIT_RE = re.compile(r"^\*\*Commit:\*\*\s*([0-9a-f]{7,40})", re.MULTILINE)
_SNAPSHOT_RE = re.compile(r"^\*\*Panel-snapshot:\*\*\s*(\{.*\})\s*$", re.MULTILINE)
_RECEIPT_RE = re.compile(r"^### (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[+-]\d{2}:\d{2}|Z)?)\s*·\s*(.*?)\s*·\s*commit\s+([0-9a-f]{7,40})",
                         re.MULTILINE)


def parse_rounds(judge_text: str) -> list:
    """Round blocks in FILE order: {kind, verdict, judges, commit, snapshot{scope: sha}, dirty{scope: n}}."""
    heads = list(_ROUND_HEADER_RE.finditer(judge_text or ""))
    rounds = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(judge_text)
        # The orchestrator-authored header region ends at the first box-drawing separator;
        # judge bodies live after it and must never supply a commit/snapshot (task 036 r12 F1).
        block = judge_text[m.start():end]
        header = block.split("═", 1)[0]
        v = _VERDICT_RE.search(header)
        j = _JUDGES_RE.search(header)
        c = _COMMIT_RE.search(header)
        snap, dirty = {}, {}
        s = _SNAPSHOT_RE.search(header)
        if s:
            try:
                obj = json.loads(s.group(1))
                for scope, info in (obj.get("scopes") or {}).items():
                    if isinstance(info, dict) and isinstance(info.get("commit"), str):
                        snap[scope] = info["commit"]
                        dirty[scope] = len(info.get("dirty") or {})
            except ValueError:
                snap = {"<unparseable>": ""}
        rounds.append({"kind": m.group(1), "verdict": v.group(1) if v else "?",
                       "judges": j.group(1) if j else "?", "commit": c.group(1) if c else "",
                       "snapshot": snap, "dirty": dirty, "line": judge_text.count("\n", 0, m.start()) + 1})
    return rounds


def parse_receipts(task_text: str) -> list:
    """Timestamped receipts, chronological: {ts, kind (audit|verification), commit, text}."""
    out = []
    for m in _RECEIPT_RE.finditer(task_text or ""):
        middle = m.group(2)
        kind = "verification" if middle.lower().startswith("risk") else "audit"
        out.append({"ts": m.group(1), "kind": kind, "commit": m.group(3), "text": middle,
                    "_dt": _parse_ts(m.group(1))})
    out.sort(key=lambda r: r["_dt"])
    for r in out:
        del r["_dt"]
    return out


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def window_commits(repo: Path, receipts: list, pad_minutes: int) -> list:
    if not receipts:
        return []
    first = _parse_ts(receipts[0]["ts"]) - timedelta(minutes=pad_minutes)
    last = _parse_ts(receipts[-1]["ts"]) + timedelta(minutes=pad_minutes)
    out = git(repo, "log", "--all", "--date=iso-strict", "--format=%H%x09%ad%x09%s",
              f"--since={first.isoformat()}", f"--until={last.isoformat()}", "--reverse")
    rows = []
    for line in out.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            rows.append({"sha": parts[0], "date": parts[1], "subject": parts[2]})
    return rows


def main(argv=None) -> int:
    utf8_stdio()
    ap = argparse.ArgumentParser(prog="map_rounds", description=__doc__.split("\n\n")[0])
    ap.add_argument("--workspace", required=True, type=Path, help="workspace holding .agent/tasks/")
    ap.add_argument("--task", required=True, help="task number (e.g. 36 or 036)")
    ap.add_argument("--repo", required=True, type=Path, help="the code repo the task changed")
    ap.add_argument("--pad-minutes", type=int, default=90)
    a = ap.parse_args(argv)
    try:
        tdir = find_task_dir(a.workspace, a.task)
        judge_text = ""
        for name in ("judge-archive.md", "judge.md"):
            p = tdir / name
            if p.is_file():
                judge_text += read_text(p) + "\n"
        task_text = read_text(tdir / "task.md") if (tdir / "task.md").is_file() else ""
        rounds = parse_rounds(judge_text)
        receipts = parse_receipts(task_text)
        commits = window_commits(a.repo, receipts, a.pad_minutes)
    except ToolError as exc:
        print(f"error: {exc}")
        return 2
    print(f"TASK {tdir}")
    print(f"REPO {a.repo}")
    print()
    print("ROUNDS (judge-archive.md then judge.md, FILE order — playbook prepends, so each file is "
          "NEWEST-FIRST; do not read position as chronology)")
    print(f"{'#':>2}  {'kind':<18} {'verdict':<7} {'judges':<6} {'commit(outer)':<14} snapshot scopes")
    for i, r in enumerate(rounds):
        scopes = " ".join(f"{k or '<root>'}={short(v)}{'*' if r['dirty'].get(k) else ''}"
                          for k, v in r["snapshot"].items()) or "-"
        print(f"{i:>2}  {r['kind']:<18} {r['verdict']:<7} {r['judges']:<6} {short(r['commit']):<14} {scopes}")
    if not rounds:
        print("   (no rounds found)")
    print("   (* = that scope had dirty files at panel time)")
    print()
    print("RECEIPTS (task.md, chronological)")
    for r in receipts:
        print(f"   {r['ts']}  {r['kind']:<12} {short(r['commit'])}  {r['text'][:60]}")
    if not receipts:
        print("   (none)")
    print()
    print(f"COMMITS in repo between first receipt -{a.pad_minutes}m and last receipt +{a.pad_minutes}m (chronological)")
    for c in commits:
        print(f"   {short(c['sha'])}  {c['date']}  {c['subject'][:80]}")
    if not commits:
        print("   (none in window — widen --pad-minutes or the receipts are missing)")
    print()
    print("SUGGESTED PAIRING (deterministic only where a snapshot names a commit that exists in --repo)")
    any_pair = False
    known = {c["sha"] for c in commits}
    for i, r in enumerate(rounds):
        for scope, sha in r["snapshot"].items():
            if sha in known or (sha and _exists(a.repo, sha)):
                print(f"   round #{i} ({r['verdict']}) reviewed {scope or '<root>'}={short(sha)}"
                      f"{' (DIRTY tree — reviewed content may differ from the commit)' if r['dirty'].get(scope) else ''}")
                any_pair = True
    if not any_pair:
        print("   (no snapshot pairing available — pair audit receipts to the latest earlier commit BY HAND)")
    return 0


def _exists(repo: Path, sha: str) -> bool:
    from bench.tools._common import git_ok
    return git_ok(repo, "cat-file", "-e", f"{sha}^{{commit}}")


if __name__ == "__main__":
    sys.exit(main())
