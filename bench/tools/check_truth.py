#!/usr/bin/env python3
"""check_truth — mechanical proof for every corpus case (plan §4.7, the `assertive` instrument).

    python3 bench/tools/check_truth.py [--corpus bench/corpus] [--source-repo NAME=PATH …]
                                       [--cases all|id,id] [--max-prompt-chars 90000]

Per case, against its `source.repo` checkout (playbook-plugin defaults to this repo):
  • `repo_base_sha` resolves; `diff.patch` RE-DERIVES byte-for-byte from `diff_of` +
    `diff_excludes` (the recipe is deterministic, not hand-edited);
  • every truth finding's `file` exists at `repo_base_sha`, and its `symbol` (when given)
    occurs in that file at that sha;
  • an `accepted+fixed` finding names a `fix_commit` that resolves, is NOT an ancestor of
    (or equal to) the reviewed commit, and touches the finding's file — the fix is real and
    LATER than the reviewed tree; `accepted+parked` needs no fix commit;
  • a `known_rejects` entry with `file` exists at the sha;
  • the package builds (no LeakageError) and its rendered prompt is under the budget.
Exit 0 = every case OK; 1 = at least one failure (all printed); 2 = unusable corpus/args.
Read-only everywhere.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from collections import Counter
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from bench.tools._common import (DEFAULT_CORPUS_DIR, PROMPT_BUDGET_CHARS, ToolError,  # noqa: E402
                                 find_task_dir, git, git_ok, parse_source_repos, short, utf8_stdio)
from bench.lib import cases as cases_mod, package  # noqa: E402
from bench.tools.case_from_task import _lf, apply_spec_edits, derive_diff  # noqa: E402

PROMPT_BUDGET_BYTES = 120_000      # grok's `-p` element rides argv: 131,072-byte POSIX cap per element


def _blob_exists(repo: Path, sha: str, path: str) -> bool:
    return git_ok(repo, "cat-file", "-e", f"{sha}:{path}")


def _touched(repo: Path, sha: str) -> set:
    out = git(repo, "show", "--pretty=", "--name-only", "-m", "--first-parent", sha)
    return {ln.strip() for ln in out.splitlines() if ln.strip()}


def check_case(case, repo: Path, max_chars: int, max_bytes: int) -> dict:
    """{'fails': [...], 'warns': [...], 'chars': n, 'bytes': n} for one case."""
    fails, warns = [], []
    sha = case.repo_base_sha
    if not git_ok(repo, "cat-file", "-e", f"{sha}^{{commit}}"):
        return {"fails": [f"repo_base_sha {short(sha)} does not resolve in {repo}"], "warns": [],
                "chars": None, "bytes": None}
    parent, sep, reviewed = case.meta["diff_of"].partition("..")
    if not sep:
        fails.append(f"diff_of {case.meta['diff_of']!r} is not '<base>..<reviewed>' — cannot re-derive")
    else:
        # The judge's snapshot is repo_base_sha; the diff must END there (impl-panel r1 sol #3 / terra #2).
        r_full = git(repo, "rev-parse", f"{reviewed}^{{commit}}", check=False).strip() if git_ok(repo, "cat-file", "-e", f"{reviewed}^{{commit}}") else ""
        if r_full != sha:
            fails.append(f"diff_of right endpoint {short(reviewed)} != repo_base_sha {short(sha)} — the snapshot "
                         f"and the diff would disagree")
        if not git_ok(repo, "cat-file", "-e", f"{parent}^{{commit}}") or \
                not git_ok(repo, "merge-base", "--is-ancestor", parent, sha):
            fails.append(f"diff_of base {short(parent)} is not an ancestor of the reviewed commit {short(sha)}")
    excluded = set(case.meta.get("diff_excludes") or [])
    mapping = case.meta.get("mapping")
    truth_fixes = {f.get("fix_commit") for f in case.truth.get("findings", []) if f.get("fix_commit")}
    if not isinstance(mapping, dict):
        fails.append("case.json has no `mapping` object (round, rounds_total, fix_commits, evidence) — the "
                     "round↔commit decision must be recorded, not implied")
    else:
        rnd, tot = mapping.get("round"), mapping.get("rounds_total")
        if not isinstance(rnd, int) or rnd < 1:
            fails.append(f"mapping.round must be an int >= 1, got {rnd!r}")
        if not isinstance(tot, int) or (isinstance(rnd, int) and tot < rnd):
            fails.append(f"mapping.rounds_total must be an int >= round, got {tot!r} (round {rnd!r})")
        if not isinstance(mapping.get("evidence"), str) or not mapping["evidence"].strip():
            fails.append("mapping.evidence must be a non-empty sentence")
        mfix = set()
        for c in (mapping.get("fix_commits") or []):
            if not isinstance(c, str) or not git_ok(repo, "cat-file", "-e", f"{c}^{{commit}}"):
                fails.append(f"mapping.fix_commits entry {c!r} does not resolve")
                continue
            full = git(repo, "rev-parse", f"{c}^{{commit}}").strip()
            if full == sha or not git_ok(repo, "merge-base", "--is-ancestor", sha, full):
                fails.append(f"mapping.fix_commits entry {short(c)} does not descend from the reviewed commit {short(sha)}")
            mfix.add(full)
        tfix = {git(repo, "rev-parse", f"{c}^{{commit}}", check=False).strip() for c in truth_fixes
                if git_ok(repo, "cat-file", "-e", f"{c}^{{commit}}")}
        if tfix - mfix:
            fails.append(f"mapping.fix_commits does not list every fix commit the truth cites: "
                         f"{sorted(short(c) for c in tfix - mfix)}")
        try:
            expected = derive_diff(repo, parent, reviewed, list(case.meta.get("diff_excludes") or []))
            actual = case.diff_path.read_text(encoding="utf-8", errors="replace")
            if expected != actual:
                fails.append("diff.patch does not re-derive from diff_of + diff_excludes (hand-edited or stale)")
        except ToolError as exc:
            fails.append(f"diff.patch re-derivation failed: {exc}")
    keys = Counter((f["file"], f.get("symbol")) for f in case.truth.get("findings", []))
    collisions = [k for k, n in keys.items() if n > 1]
    if collisions:
        # Two real accepted defects in one symbol never auto-match (scoring needs exactly one
        # truth entry per key) — they route to the human in `adjudicate`. Warn, never drop.
        warns.append(f"{len(collisions)} (file, symbol) collision(s) → human adjudication: "
                     + ", ".join(f"{f}:{s}" for f, s in collisions))
    for f in case.truth.get("findings", []):
        fid, path, symbol = f["id"], f["file"], f.get("symbol")
        if path in excluded:
            # A judge is scored on the DIFF UNDER REVIEW; a truth file stripped from it is
            # unfair recall (impl-panel r1 grok #3). Keep the path in the diff or drop the entry.
            fails.append(f"finding {fid}: file {path} is in diff_excludes — a truth file must be in diff.patch")
        if not _blob_exists(repo, sha, path):
            fails.append(f"finding {fid}: file {path} does not exist at {short(sha)}")
            continue
        body_at_review = git(repo, "show", f"{sha}:{path}")
        if symbol and symbol not in body_at_review:
            fails.append(f"finding {fid}: symbol {symbol!r} not found in {path} at {short(sha)}")
        fix, evidence = f.get("fix_commit"), f.get("fix_evidence")
        if f["historical_outcome"] == "accepted+fixed":
            if not fix:
                fails.append(f"finding {fid}: accepted+fixed but no fix_commit named")
                continue
            if not evidence or not isinstance(evidence, str):
                fails.append(f"finding {fid}: accepted+fixed but no fix_evidence (an exact substring absent at "
                             f"the reviewed sha and present after the fix)")
                continue
            if not git_ok(repo, "cat-file", "-e", f"{fix}^{{commit}}"):
                fails.append(f"finding {fid}: fix_commit {fix} does not resolve")
                continue
            fix_full = git(repo, "rev-parse", f"{fix}^{{commit}}").strip()
            if fix_full == sha or not git_ok(repo, "merge-base", "--is-ancestor", sha, fix_full):
                fails.append(f"finding {fid}: fix_commit {short(fix)} does not descend from the reviewed commit "
                             f"{short(sha)} — the fix must be strictly LATER on the same history")
                continue
            if path not in _touched(repo, fix_full):
                fails.append(f"finding {fid}: fix_commit {short(fix)} does not touch {path}")
                continue
            if evidence in body_at_review:
                fails.append(f"finding {fid}: fix_evidence {evidence[:50]!r} is ALREADY present in {path} at the "
                             f"reviewed sha {short(sha)} — the fix is in the reviewed diff, not truth for this case")
                continue
            if not _blob_exists(repo, fix_full, path) or evidence not in git(repo, "show", f"{fix_full}:{path}"):
                fails.append(f"finding {fid}: fix_evidence {evidence[:50]!r} not found in {path} at fix_commit "
                             f"{short(fix)}")
                continue
            # The named commit must INTRODUCE the evidence (impl-panel r1 terra #3): absent at its parent.
            if _blob_exists(repo, f"{fix_full}^", path) and evidence in git(repo, "show", f"{fix_full}^:{path}"):
                fails.append(f"finding {fid}: fix_evidence {evidence[:50]!r} is already present at the fix commit's "
                             f"parent {short(fix)}^ — the fix landed earlier than the named commit")
        elif fix and not git_ok(repo, "cat-file", "-e", f"{fix}^{{commit}}"):
            fails.append(f"finding {fid}: fix_commit {fix} does not resolve")
    for r in case.truth.get("known_rejects", []):
        if r.get("file") and not _blob_exists(repo, sha, r["file"]):
            fails.append(f"reject {r['id']}: file {r['file']} does not exist at {short(sha)}")
    chars = nbytes = None
    try:
        pkg = package.build_package(case)
        chars, nbytes = pkg.prompt_chars, len(pkg.prompt.encode("utf-8"))
        if chars > max_chars:
            fails.append(f"prompt {chars:,} chars exceeds the {max_chars:,}-char budget")
        if nbytes > max_bytes:
            fails.append(f"prompt {nbytes:,} bytes exceeds the {max_bytes:,}-byte argv budget")
    except package.LeakageError as exc:
        fails.append(f"package: {exc}")
    return {"fails": fails, "warns": warns, "chars": chars, "bytes": nbytes}


def check_spec_regenerates(case, workspaces: dict) -> tuple:
    """(status, message): 'ok' | 'skip' | 'drift' | 'fail'. Needs the source workspace on disk."""
    ws = workspaces.get(case.source["workspace"])
    if ws is None:
        return "skip", f"spec regeneration check skipped (no --workspace mapping for {case.source['workspace']!r})"
    try:
        tdir = find_task_dir(ws, case.source["task"])
    except ToolError as exc:
        return "skip", f"spec regeneration check skipped: {exc}"
    raw = (tdir / "task.md").read_bytes()
    recorded = case.meta.get("spec_source_sha256")
    if recorded and hashlib.sha256(raw).hexdigest() != recorded:
        return "drift", ("source task.md drifted since the case was frozen (digest differs) — the record no longer "
                         "vouches for spec.md; re-freeze the case from the current record or restore the source")
    edits = [e for e in case.meta.get("spec_edits", []) if isinstance(e, dict)]
    try:
        regen = apply_spec_edits(package.reconstruct_spec(_lf(raw)), edits)
    except ToolError as exc:
        return "fail", f"spec.md does not regenerate: {exc}"
    # Compare bytes decoded WITHOUT newline translation (read_text would fold a CRLF
    # spec into LF and mask a real difference — or invent one on Windows).
    if regen != _lf(case.spec_path.read_bytes()):
        return "fail", "spec.md does not regenerate from reconstruct_spec(task.md) + spec_edits (hand-edited)"
    return "ok", "spec regenerates byte-for-byte from its source"


def main(argv=None) -> int:
    utf8_stdio()
    ap = argparse.ArgumentParser(prog="check_truth", description=__doc__.split("\n\n")[0])
    ap.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_DIR)
    ap.add_argument("--source-repo", action="append", default=[], metavar="NAME=PATH")
    ap.add_argument("--workspace", action="append", default=[], metavar="NAME=PATH",
                    help="workspace dir per case.json source.workspace — enables the spec regeneration check")
    ap.add_argument("--cases", default="all")
    ap.add_argument("--max-prompt-chars", type=int, default=PROMPT_BUDGET_CHARS)
    ap.add_argument("--max-prompt-bytes", type=int, default=PROMPT_BUDGET_BYTES)
    a = ap.parse_args(argv)
    try:
        repos = parse_source_repos(a.source_repo)
        workspaces = parse_source_repos(a.workspace, default_playbook=False)
        corpus = cases_mod.load_corpus(a.corpus)
        selected = cases_mod.select_cases(corpus, a.cases)
    except (ToolError, cases_mod.CorpusError) as exc:
        print(f"error: {exc}")
        return 2
    bad = 0
    # One logical case under two ids would be double-weighted (plan-panel codex:sol #5).
    logical = Counter((c.source["repo"], c.repo_base_sha, c.meta["diff_of"]) for c in selected)
    dupes = {k for k, n in logical.items() if n > 1}
    for case in selected:
        key = (case.source["repo"], case.repo_base_sha, case.meta["diff_of"])
        if key in dupes:
            others = [c.id for c in selected if c is not case and
                      (c.source["repo"], c.repo_base_sha, c.meta["diff_of"]) == key]
            print(f"FAIL {case.id}: duplicate logical case — same repo/reviewed sha/diff_of as {others}")
            bad += 1
            continue
        repo = repos.get(case.source["repo"])
        if repo is None:
            print(f"FAIL {case.id}: no --source-repo mapping for {case.source['repo']!r}")
            bad += 1
            continue
        if not git_ok(Path(repo), "rev-parse", "--git-dir"):
            print(f"FAIL {case.id}: {repo} is not a git checkout")
            bad += 1
            continue
        res = check_case(case, Path(repo), a.max_prompt_chars, a.max_prompt_bytes)
        status, msg = check_spec_regenerates(case, workspaces)
        if status in ("fail", "drift"):            # drift = the source no longer vouches for the spec (r2 sol #3)
            res["fails"].append(msg)
        elif status == "skip":
            res["warns"].append(msg)
        if res["fails"]:
            bad += 1
            print(f"FAIL {case.id}:")
            for m in res["fails"]:
                print(f"     - {m}")
        else:
            nf, nr = len(case.truth.get("findings", [])), len(case.truth.get("known_rejects", []))
            print(f"OK   {case.id:<28} prompt {res['chars']:>7,} chars {res['bytes']:>7,} bytes  "
                  f"findings {nf:>2}  rejects {nr:>2}  {case.kind}/{case.area}/{case.difficulty}")
            if status == "ok":
                print(f"     · {msg}")
        for w in res["warns"]:
            print(f"     ! {w}")
    print(f"\n{len(selected) - bad}/{len(selected)} cases OK" + ("" if not bad else f", {bad} FAILED"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
