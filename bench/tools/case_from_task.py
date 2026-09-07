#!/usr/bin/env python3
"""case_from_task — emit one judgebench case dir from a historical task record + git.

    python3 bench/tools/case_from_task.py --workspace ~/ws --task 36 --repo ~/ws/app \\
        --reviewed <sha> --id pb-036-r1 --kind feature --area enforcement --difficulty easy \\
        [--repo-name playbook-plugin] [--exclude PATH …] [--notes "…"] [--out bench/corpus/cases]

Writes `<out>/<id>/{case.json, spec.md, diff.patch, truth.json}`:
  spec.md     = `reconstruct_spec(task.md)` — the allowlist filter from bench/lib/package.py;
                remaining `leak_scan` hits are PRINTED and make the exit code 1 so you clean
                the spec by hand (and record why in case.json notes). Files are still written.
  diff.patch  = `git diff <reviewed>^ <reviewed> -- . ':(exclude)…'` minus DEFAULT_EXCLUDES
                (lockfiles, minified/map/dist/node_modules/vendored) minus binary paths (from
                `--numstat`) minus your --exclude paths. Every excluded path is recorded in
                case.json `diff_excludes` so `check_truth` can re-derive the diff byte-for-byte.
  case.json   = repo_base_sha = FULL sha of the REVIEWED commit (the tree WITH the diff — what
                the judge's snapshot is built from), diff_of = "<parent>..<reviewed>".
  truth.json  = skeleton {findings: [], known_rejects: []} — you fill it from the round's triage.
Prints the rendered prompt size (chars/bytes) against the 90k budget. Read-only on the
workspace and the repo; refuses to overwrite an existing case dir.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from bench.tools._common import (DEFAULT_CASES_DIR, PROMPT_BUDGET_CHARS, ToolError,  # noqa: E402
                                 find_task_dir, git, read_text, resolve_commit, task_number,
                                 utf8_stdio)
from bench.lib import package  # noqa: E402

# Matched against the full POSIX path AND the basename (fnmatch: `*` also spans `/`).
DEFAULT_EXCLUDES = ("*lock*.json", "*.lock", "*.min.*", "*.map", "dist/*", "build/*",
                    "node_modules/*", "vendor/*", "*.snap")


def changed_paths(repo: Path, parent: str, reviewed: str) -> list:
    """[(path, is_binary)] from `git diff --numstat` (binary rows are `-\t-\tpath`)."""
    rows = []
    for line in git(repo, "diff", "--numstat", parent, reviewed).splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        if " => " in path:                       # rename row `a/{old => new}.py`; keep the raw form
            path = path.split(" => ")[-1].rstrip("}") if "{" not in path else path
        rows.append((path, added == "-" and deleted == "-"))
    return rows


def compute_excludes(paths: list, user_globs: list) -> list:
    """Exact paths to exclude: default globs ∪ binaries ∪ user globs/paths (sorted)."""
    out = set()
    globs = list(DEFAULT_EXCLUDES) + list(user_globs or [])
    for path, is_binary in paths:
        base = path.rsplit("/", 1)[-1]
        if is_binary:
            out.add(path)
            continue
        for g in globs:
            if fnmatch.fnmatchcase(path, g) or fnmatch.fnmatchcase(base, g) or path == g:
                out.add(path)
                break
    return sorted(out)


def derive_diff(repo: Path, parent: str, reviewed: str, excludes: list) -> str:
    """The exact command `check_truth` re-runs: exclusions are exact pathspecs."""
    args = ["diff", "--no-color", "--no-ext-diff", parent, reviewed, "--", "."]
    args += [f":(exclude){p}" for p in excludes]
    return git(repo, *args)


def prompt_size(spec: str, diff: str) -> tuple:
    tpl, _v, _sha = package.load_template()
    prompt = package.render_prompt(spec, diff, [], tpl)
    return len(prompt), len(prompt.encode("utf-8"))


def build_case(*, workspace: Path, task: str, repo: Path, reviewed: str, case_id: str, kind: str,
               area: str, difficulty: str, repo_name: str, excludes: list, notes: str, out_dir: Path) -> dict:
    tdir = find_task_dir(workspace, task)
    reviewed_sha = resolve_commit(repo, reviewed)
    parent_sha = resolve_commit(repo, reviewed_sha + "^")
    case_dir = Path(out_dir) / case_id
    if case_dir.exists():
        raise ToolError(f"case dir already exists: {case_dir} (cases are frozen; never overwrite)")
    task_md = read_text(tdir / "task.md")
    spec = package.reconstruct_spec(task_md)
    leaks = package.leak_scan(spec)
    paths = changed_paths(repo, parent_sha, reviewed_sha)
    excl = compute_excludes(paths, excludes)
    diff = derive_diff(repo, parent_sha, reviewed_sha, excl)
    chars, nbytes = prompt_size(spec, diff)
    meta = {
        "id": case_id,
        "source": {"workspace": Path(workspace).resolve().name, "task": task_number(task), "repo": repo_name},
        "repo_base_sha": reviewed_sha,
        "diff_of": f"{parent_sha}..{reviewed_sha}",
        "diff_excludes": excl,
        "kind": kind, "area": area, "difficulty": difficulty,
        "truth_version": 1,
        "notes": notes or f"round↔commit mapping: TODO (built from {tdir.name}; reviewed {reviewed_sha[:10]})",
    }
    case_dir.mkdir(parents=True)
    (case_dir / "spec.md").write_text(spec, encoding="utf-8", newline="\n")
    (case_dir / "diff.patch").write_text(diff, encoding="utf-8", newline="\n")
    (case_dir / "case.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
                                        encoding="utf-8", newline="\n")
    (case_dir / "truth.json").write_text(json.dumps({"findings": [], "known_rejects": []}, indent=2) + "\n",
                                         encoding="utf-8", newline="\n")
    return {"case_dir": case_dir, "leaks": leaks, "excluded": excl, "changed": len(paths),
            "chars": chars, "bytes": nbytes, "meta": meta}


def main(argv=None) -> int:
    utf8_stdio()
    ap = argparse.ArgumentParser(prog="case_from_task", description=__doc__.split("\n\n")[0])
    ap.add_argument("--workspace", required=True, type=Path)
    ap.add_argument("--task", required=True)
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--reviewed", required=True, help="the REVIEWED commit (tree with the diff applied)")
    ap.add_argument("--id", required=True, dest="case_id")
    ap.add_argument("--kind", required=True, choices=("feature", "bugfix", "refactor", "docs", "perf"))
    ap.add_argument("--area", required=True, choices=("enforcement", "server", "ui", "tests", "docs"))
    ap.add_argument("--difficulty", required=True, choices=("easy", "medium", "hard"))
    ap.add_argument("--repo-name", default=None, help="case.json source.repo (default: repo dir name)")
    ap.add_argument("--exclude", action="append", default=[], help="extra path or glob to drop from diff.patch")
    ap.add_argument("--notes", default="")
    ap.add_argument("--out", type=Path, default=DEFAULT_CASES_DIR)
    a = ap.parse_args(argv)
    try:
        res = build_case(workspace=a.workspace, task=a.task, repo=a.repo, reviewed=a.reviewed,
                         case_id=a.case_id, kind=a.kind, area=a.area, difficulty=a.difficulty,
                         repo_name=a.repo_name or Path(a.repo).resolve().name, excludes=a.exclude,
                         notes=a.notes, out_dir=a.out)
    except ToolError as exc:
        print(f"error: {exc}")
        return 2
    print(f"wrote {res['case_dir']}")
    print(f"reviewed {res['meta']['repo_base_sha']}  diff_of {res['meta']['diff_of']}")
    print(f"changed paths {res['changed']}, excluded {len(res['excluded'])}: "
          + (", ".join(res["excluded"]) if res["excluded"] else "(none)"))
    over = res["chars"] > PROMPT_BUDGET_CHARS
    print(f"prompt {res['chars']:,} chars / {res['bytes']:,} bytes (budget {PROMPT_BUDGET_CHARS:,})"
          + ("  ** OVER BUDGET **" if over else ""))
    if res["leaks"]:
        print(f"leak_scan hits in spec.md ({len(res['leaks'])}) — clean by hand, record why in notes:")
        for h in res["leaks"]:
            print(f"   leak: {h}")
        return 1
    print("leak_scan: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
