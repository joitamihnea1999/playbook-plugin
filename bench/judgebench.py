#!/usr/bin/env python3
"""judgebench — dev-only benchmark harness for playbook judge seats.

Runs N candidate judge configurations (provider:model:effort) against the same
frozen historical review inputs and scores them. Lives at the repo root like
`arena/`; it is NOT a `tasks` subcommand and never ships in `plugins/playbook/`.

    python3 bench/judgebench.py corpus validate
    python3 bench/judgebench.py corpus show [<case-id>]
    python3 bench/judgebench.py run --cases all|id,id --candidates a,b --run-id X [--resume] (--fake | --live)
    python3 bench/judgebench.py adjudicate <run-id>
    python3 bench/judgebench.py report <run-id> [--md out.md] [--weights 8,3,1]

Exit codes: 0 ok · 1 completed-with-DNFs · 2 unusable (bad corpus / args).
Real providers are OPT-IN: `run` refuses unless exactly one of --fake/--live is
given, so no test or accidental invocation can spend quota.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from bench.lib import DEFAULT_CORPUS_DIR, DEFAULT_RUNS_DIR  # noqa: E402

EXIT_OK = 0
EXIT_DNF = 1
EXIT_UNUSABLE = 2


class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on usage errors already; keep that contract explicit."""

    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        sys.exit(EXIT_UNUSABLE)


def build_parser() -> argparse.ArgumentParser:
    ap = _Parser(prog="judgebench", description=__doc__.split("\n\n")[0],
                 formatter_class=argparse.RawDescriptionHelpFormatter)
    # Shared location flags, accepted AFTER the subcommand (argparse only binds
    # top-level options before it): `judgebench corpus validate --corpus DIR`.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_DIR,
                        help=f"corpus directory (default {DEFAULT_CORPUS_DIR})")
    common.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR,
                        help=f"runs directory (default {DEFAULT_RUNS_DIR}; gitignored)")
    sub = ap.add_subparsers(dest="cmd", metavar="<command>")

    corpus = sub.add_parser("corpus", help="validate / inspect the frozen corpus")
    csub = corpus.add_subparsers(dest="corpus_cmd", metavar="<action>")
    csub.add_parser("validate", parents=[common],
                    help="validate corpus.json + every case dir")
    show = csub.add_parser("show", parents=[common],
                           help="print the corpus index or one case")
    show.add_argument("case_id", nargs="?", default=None)

    run = sub.add_parser("run", parents=[common], help="run candidates against cases")
    run.add_argument("--cases", required=True,
                     help="'all' or a comma-separated list of case ids")
    run.add_argument("--candidates", required=True,
                     help="comma-separated judge specs (provider:model:effort or alias)")
    run.add_argument("--run-id", required=True)
    run.add_argument("--resume", action="store_true",
                     help="skip (case,candidate) pairs already in result.jsonl")
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--fake", action="store_true",
                      help="scripted FakeRunner — no provider is invoked")
    mode.add_argument("--live", action="store_true",
                      help="REAL provider CLIs (spends quota; operator only)")
    run.add_argument("--soft-timeout", type=int, default=900,
                     help="seconds the judge is told to wind down at (default 900)")
    run.add_argument("--timeout", type=int, default=1200,
                     help="hard wall-clock kill per invocation (default 1200)")
    run.add_argument("--concurrency", type=int, default=2,
                     help="max concurrent candidate invocations per case (default 2)")
    run.add_argument("--fake-script", type=Path, default=None,
                     help="JSON file scripting FakeRunner outputs (default: all ok)")
    run.add_argument("--source-repo", action="append", default=[], metavar="NAME=PATH",
                     help="local checkout for a case's source.repo (live runs snapshot it at "
                          "repo_base_sha); 'playbook-plugin' defaults to this repo")

    adj = sub.add_parser("adjudicate", parents=[common], help="human verdicts for unmatched findings")
    adj.add_argument("run_id")
    adj.add_argument("--auto", action="store_true",
                     help="record deterministic matches only; no terminal prompts")

    rep = sub.add_parser("report", parents=[common], help="comparison table for a run")
    rep.add_argument("run_id")
    rep.add_argument("--md", type=Path, default=None, help="also write markdown here")
    rep.add_argument("--weights", default="8,3,1",
                     help="severity weights Critical,Important,Minor (report-time only)")
    return ap


def cmd_corpus(args) -> int:
    from bench.lib import cases as _cases
    if args.corpus_cmd not in ("validate", "show"):
        build_parser().parse_args(["corpus", "--help"])
        return EXIT_UNUSABLE
    if not args.corpus.is_dir():
        print(f"judgebench: corpus dir not found: {args.corpus}", file=sys.stderr)
        return EXIT_UNUSABLE
    try:
        corpus = _cases.load_corpus(args.corpus)
    except _cases.CorpusError as exc:
        print(f"judgebench: corpus invalid: {exc}", file=sys.stderr)
        return EXIT_UNUSABLE
    if args.corpus_cmd == "validate":
        print(f"corpus v{corpus.version}: {len(corpus.cases)} cases OK")
        return EXIT_OK
    if args.case_id is None:
        for c in corpus.cases:
            print(f"{c.id:<28} {c.kind:<9} {c.area:<12} {c.difficulty:<7} truth v{c.truth_version}")
        return EXIT_OK
    case = corpus.get(args.case_id)
    if case is None:
        print(f"judgebench: no such case {args.case_id!r}", file=sys.stderr)
        return EXIT_UNUSABLE
    print(case.describe())
    return EXIT_OK


def make_runner(*, fake: bool, live: bool, fake_script, source_repos):
    """The ONLY place a LiveRunner is constructed — and only under --live."""
    from bench.lib import runner as _runner
    if fake and not live:
        script = None
        if fake_script:
            import json
            script = json.loads(Path(fake_script).read_text(encoding="utf-8"))
        return _runner.FakeRunner(script)
    if live and not fake:
        from bench.lib import REPO_ROOT
        return _runner.LiveRunner(REPO_ROOT)
    raise ValueError("run needs exactly one of --fake (scripted runner) or --live "
                     "(REAL providers, spends quota)")


def _parse_source_repos(items) -> dict:
    from bench.lib import REPO_ROOT
    repos = {"playbook-plugin": REPO_ROOT}
    for item in items or []:
        name, sep, path = item.partition("=")
        if not sep or not name.strip() or not path.strip():
            raise ValueError(f"--source-repo expects NAME=PATH, got {item!r}")
        repos[name.strip()] = Path(path.strip()).expanduser().resolve()
    return repos


def cmd_run(args) -> int:
    import json
    from bench.lib import cases as _cases, package as _package, records as _records
    from bench.lib import runner as _runner
    if not (args.fake or args.live):
        print("judgebench: run needs exactly one of --fake (scripted runner) or "
              "--live (REAL providers, spends quota)", file=sys.stderr)
        return EXIT_UNUSABLE
    try:
        source_repos = _parse_source_repos(args.source_repo)
        corpus = _cases.load_corpus(args.corpus)
        selected = _cases.select_cases(corpus, args.cases)
        candidates = _runner.parse_candidates(args.candidates)
        runner = make_runner(fake=args.fake, live=args.live, fake_script=args.fake_script,
                             source_repos=source_repos)
    except (_cases.CorpusError, _runner.CandidateError, ValueError, OSError) as exc:
        print(f"judgebench: {exc}", file=sys.stderr)
        return EXIT_UNUSABLE
    run_dir = Path(args.runs_dir) / args.run_id
    labels = [c.label for c in candidates]
    try:
        lock = _records.RunLock(run_dir)
        lock.__enter__()
    except _records.RunLocked as exc:
        print(f"judgebench: {exc}", file=sys.stderr)
        return EXIT_UNUSABLE
    try:
        packages = {c.id: _package.build_package(c, soft_timeout_secs=args.soft_timeout,
                                                 hard_timeout_secs=args.timeout)
                    for c in selected}
        tpl_v, tpl_sha = next(iter(packages.values())).template_version, \
            next(iter(packages.values())).template_sha256
        has_results = any(_records.result_path(run_dir, lb).exists() for lb in labels) or \
            (run_dir / _records.MANIFEST_NAME).exists()
        done, torn = set(), 0
        if args.resume:
            if not (run_dir / _records.MANIFEST_NAME).exists():
                print(f"judgebench: --resume but no manifest in {run_dir}", file=sys.stderr)
                return EXIT_UNUSABLE
            manifest = _records.read_manifest(run_dir)
            problems = _records.check_manifest(manifest, selected_cases=selected, packages=packages,
                                               candidates=candidates,
                                               mode="live" if args.live else "fake",
                                               soft_timeout=args.soft_timeout, hard_timeout=args.timeout)
            if problems:
                print("judgebench: cannot resume — inputs changed since the run started:",
                      file=sys.stderr)
                for pr in problems:
                    print(f"  - {pr}", file=sys.stderr)
                return EXIT_UNUSABLE
            done, torn = _records.completed_pairs(run_dir, labels)
        elif has_results:
            print(f"judgebench: run {args.run_id!r} already has results in {run_dir}; use --resume "
                  "to continue it or pick a new --run-id", file=sys.stderr)
            return EXIT_UNUSABLE
        else:
            manifest = _records.build_manifest(
                run_id=args.run_id, mode="live" if args.live else "fake", corpus=corpus,
                selected_cases=selected, packages=packages, candidates=candidates,
                soft_timeout=args.soft_timeout, hard_timeout=args.timeout,
                concurrency=args.concurrency, source_repos=source_repos,
                template_version=tpl_v, template_sha256=tpl_sha, fake_script=args.fake_script)
            _records.write_manifest(run_dir, manifest)
        if args.live:
            try:
                from tasks.review import _print_background_advisory
                _print_background_advisory(args.timeout)
            except Exception:
                pass
        print(f"run {args.run_id}: {len(selected)} case(s) × {len(candidates)} candidate(s)"
              + (f", resuming ({len(done)} done, {torn} torn line(s) ignored → those pairs re-run)" if args.resume else ""))
        invocations = 0
        counts = {}
        for case in selected:
            skip = {lb for lb in labels if (case.id, lb) in done}
            repo_name = case.source.get("repo", "")
            source_repo = source_repos.get(repo_name)
            if args.live and source_repo is None:
                for cand in candidates:
                    if cand.label in skip:
                        continue
                    inv = _runner.Invocation(status="dnf", raw=f"(error: no local checkout for "
                                             f"source repo {repo_name!r}; pass --source-repo "
                                             f"{repo_name}=PATH)", note="source repo")
                    _persist(run_dir, args.run_id, case, cand, inv, packages[case.id], _records)
                    counts[inv.status] = counts.get(inv.status, 0) + 1
                    invocations += 1
                    print(f"  {case.id:<28} {cand.label:<20} {inv.status}")
                continue
            results = _runner.run_case(case, candidates, runner, packages[case.id],
                                       source_repo=source_repo, soft_timeout=args.soft_timeout,
                                       hard_timeout=args.timeout, concurrency=args.concurrency,
                                       skip=skip)
            for cand, inv in results:
                _persist(run_dir, args.run_id, case, cand, inv, packages[case.id], _records)
                counts[inv.status] = counts.get(inv.status, 0) + 1
                invocations += 1
                print(f"  {case.id:<28} {cand.label:<20} {inv.status}"
                      + (f"  ({inv.note})" if inv.note else ""))
        summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        print(f"done: {invocations} invocations" + (f" ({summary})" if summary else "")
              + f" → {run_dir}")
        bad = sum(v for k, v in counts.items() if k in ("dnf", "timeout", "excluded"))
        return EXIT_DNF if bad else EXIT_OK
    finally:
        lock.__exit__(None, None, None)


def _persist(run_dir, run_id, case, cand, inv, package, _records) -> None:
    raw_rel = _records.write_raw(run_dir, cand.label, case.id, inv.raw)
    rec = _records.make_result_record(run_id, case, cand, inv, raw_rel, package)
    _records.append_result(run_dir, cand.label, rec)
    _records.journal_spend(run_dir, seat=cand.spec, case_id=case.id, duration_ms=inv.duration_ms,
                           status=inv.status, usage=inv.usage)


def cmd_adjudicate(args) -> int:
    from bench.lib import cases as _cases, records as _records, scoring as _scoring
    run_dir = Path(args.runs_dir) / args.run_id
    if not (run_dir / _records.MANIFEST_NAME).is_file():
        print(f"judgebench: no run {args.run_id!r} under {args.runs_dir}", file=sys.stderr)
        return EXIT_UNUSABLE
    try:
        corpus = _cases.load_corpus(args.corpus)
    except _cases.CorpusError as exc:
        print(f"judgebench: corpus invalid: {exc}", file=sys.stderr)
        return EXIT_UNUSABLE
    results = _records.all_results(run_dir)
    if not results:
        print(f"judgebench: run {args.run_id!r} has no result lines yet", file=sys.stderr)
        return EXIT_UNUSABLE
    try:
        counts = _scoring.adjudicate(run_dir, corpus, results, auto_only=args.auto)
    except _records.RunLocked as exc:
        print(f"judgebench: {exc}", file=sys.stderr)
        return EXIT_UNUSABLE
    adj = _scoring.load_adjudication(run_dir)
    pending = sum(1 for rec, f in _scoring.iter_findings(results)
                  if (adj["decisions"].get(_scoring.decision_key(rec["case_id"], rec["label"], f.n))
                      or {}).get("verdict") in _scoring.PENDING_VERDICTS)
    print(f"adjudication {args.run_id}: auto truth={counts['auto_truth']} auto reject="
          f"{counts['auto_reject']} human={counts.get('human', 0)} valid-new added="
          f"{counts.get('valid_new_added', 0)} pending={pending} → {run_dir / _scoring.ADJUDICATION_NAME}")
    return EXIT_OK


def cmd_report(args) -> int:
    from bench.lib import cases as _cases, records as _records, report as _report, scoring as _scoring
    run_dir = Path(args.runs_dir) / args.run_id
    if not (run_dir / _records.MANIFEST_NAME).is_file():
        print(f"judgebench: no run {args.run_id!r} under {args.runs_dir}", file=sys.stderr)
        return EXIT_UNUSABLE
    try:
        weights = _report.parse_weights(args.weights)
        corpus = _cases.load_corpus(args.corpus)
    except (ValueError, _cases.CorpusError) as exc:
        print(f"judgebench: {exc}", file=sys.stderr)
        return EXIT_UNUSABLE
    results = _records.all_results(run_dir)
    if not results:
        print(f"judgebench: run {args.run_id!r} has no result lines yet", file=sys.stderr)
        return EXIT_UNUSABLE
    adj = _scoring.load_adjudication(run_dir)
    manifest = _records.read_manifest(run_dir)
    rep = _report.aggregate(args.run_id, results, adj, corpus, weights=weights, manifest=manifest)
    print(_report.render_text(rep), end="")
    if args.md:
        from tasks.atomic import atomic_write
        atomic_write(Path(args.md), _report.render_markdown(rep))
        print(f"markdown written: {args.md}")
    return EXIT_OK


def main(argv=None) -> int:
    # Force utf-8 stdio: the Windows console defaults to cp1252 and chokes on
    # the → / × glyphs in our summaries (same guard as tasks/cli.py::main).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.cmd is None:
        ap.print_usage(sys.stderr)
        return EXIT_UNUSABLE
    if args.cmd == "corpus":
        if args.corpus_cmd is None:
            ap.parse_args(["corpus", "--help"])
            return EXIT_UNUSABLE
        return cmd_corpus(args)
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "adjudicate":
        return cmd_adjudicate(args)
    if args.cmd == "report":
        return cmd_report(args)
    return EXIT_UNUSABLE


if __name__ == "__main__":
    sys.exit(main())
