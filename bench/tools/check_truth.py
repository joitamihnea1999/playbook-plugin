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
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from bench.tools._common import (DEFAULT_CORPUS_DIR, PROMPT_BUDGET_CHARS, ToolError,  # noqa: E402
                                 git, git_ok, parse_source_repos, short, utf8_stdio)
from bench.lib import cases as cases_mod, package  # noqa: E402
from bench.tools.case_from_task import derive_diff  # noqa: E402


def _blob_exists(repo: Path, sha: str, path: str) -> bool:
    return git_ok(repo, "cat-file", "-e", f"{sha}:{path}")


def _touched(repo: Path, sha: str) -> set:
    out = git(repo, "show", "--pretty=", "--name-only", "-m", "--first-parent", sha)
    return {ln.strip() for ln in out.splitlines() if ln.strip()}


def check_case(case, repo: Path, max_chars: int) -> list:
    """Failure strings for one case ([] = OK)."""
    fails = []
    sha = case.repo_base_sha
    if not git_ok(repo, "cat-file", "-e", f"{sha}^{{commit}}"):
        return [f"repo_base_sha {short(sha)} does not resolve in {repo}"]
    parent, sep, reviewed = case.meta["diff_of"].partition("..")
    if not sep:
        fails.append(f"diff_of {case.meta['diff_of']!r} is not '<parent>..<reviewed>' — cannot re-derive")
    else:
        try:
            expected = derive_diff(repo, parent, reviewed, list(case.meta.get("diff_excludes") or []))
            actual = case.diff_path.read_text(encoding="utf-8", errors="replace")
            if expected != actual:
                fails.append("diff.patch does not re-derive from diff_of + diff_excludes (hand-edited or stale)")
        except ToolError as exc:
            fails.append(f"diff.patch re-derivation failed: {exc}")
    for f in case.truth.get("findings", []):
        fid, path, symbol = f["id"], f["file"], f.get("symbol")
        if not _blob_exists(repo, sha, path):
            fails.append(f"finding {fid}: file {path} does not exist at {short(sha)}")
            continue
        if symbol:
            body = git(repo, "show", f"{sha}:{path}")
            if symbol not in body:
                fails.append(f"finding {fid}: symbol {symbol!r} not found in {path} at {short(sha)}")
        fix = f.get("fix_commit")
        if f["historical_outcome"] == "accepted+fixed":
            if not fix:
                fails.append(f"finding {fid}: accepted+fixed but no fix_commit named")
                continue
            if not git_ok(repo, "cat-file", "-e", f"{fix}^{{commit}}"):
                fails.append(f"finding {fid}: fix_commit {fix} does not resolve")
                continue
            if git_ok(repo, "merge-base", "--is-ancestor", fix, sha):
                fails.append(f"finding {fid}: fix_commit {short(fix)} is an ancestor of (or equal to) the "
                             f"reviewed commit {short(sha)} — the fix would already be in the reviewed tree")
                continue
            if path not in _touched(repo, fix):
                fails.append(f"finding {fid}: fix_commit {short(fix)} does not touch {path}")
        elif fix and not git_ok(repo, "cat-file", "-e", f"{fix}^{{commit}}"):
            fails.append(f"finding {fid}: fix_commit {fix} does not resolve")
    for r in case.truth.get("known_rejects", []):
        if r.get("file") and not _blob_exists(repo, sha, r["file"]):
            fails.append(f"reject {r['id']}: file {r['file']} does not exist at {short(sha)}")
    try:
        pkg = package.build_package(case)
        if pkg.prompt_chars > max_chars:
            fails.append(f"prompt {pkg.prompt_chars:,} chars exceeds the {max_chars:,} budget")
        fails_size = pkg.prompt_chars
    except package.LeakageError as exc:
        fails.append(f"package: {exc}")
        fails_size = None
    return fails if fails else [f"__size__:{fails_size}"]


def main(argv=None) -> int:
    utf8_stdio()
    ap = argparse.ArgumentParser(prog="check_truth", description=__doc__.split("\n\n")[0])
    ap.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_DIR)
    ap.add_argument("--source-repo", action="append", default=[], metavar="NAME=PATH")
    ap.add_argument("--cases", default="all")
    ap.add_argument("--max-prompt-chars", type=int, default=PROMPT_BUDGET_CHARS)
    a = ap.parse_args(argv)
    try:
        repos = parse_source_repos(a.source_repo)
        corpus = cases_mod.load_corpus(a.corpus)
        selected = cases_mod.select_cases(corpus, a.cases)
    except (ToolError, cases_mod.CorpusError) as exc:
        print(f"error: {exc}")
        return 2
    bad = 0
    for case in selected:
        repo = repos.get(case.source["repo"])
        if repo is None:
            print(f"FAIL {case.id}: no --source-repo mapping for {case.source['repo']!r}")
            bad += 1
            continue
        if not (Path(repo) / ".git").exists() and not git_ok(Path(repo), "rev-parse", "--git-dir"):
            print(f"FAIL {case.id}: {repo} is not a git checkout")
            bad += 1
            continue
        result = check_case(case, Path(repo), a.max_prompt_chars)
        if len(result) == 1 and result[0].startswith("__size__:"):
            size = int(result[0].split(":", 1)[1])
            nf, nr = len(case.truth.get("findings", [])), len(case.truth.get("known_rejects", []))
            print(f"OK   {case.id:<28} prompt {size:>7,} chars  findings {nf:>2}  rejects {nr:>2}  "
                  f"{case.kind}/{case.area}/{case.difficulty}")
        else:
            bad += 1
            print(f"FAIL {case.id}:")
            for msg in result:
                print(f"     - {msg}")
    print(f"\n{len(selected) - bad}/{len(selected)} cases OK" + ("" if not bad else f", {bad} FAILED"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
