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

    adj = sub.add_parser("adjudicate", parents=[common], help="human verdicts for unmatched findings")
    adj.add_argument("run_id")

    rep = sub.add_parser("report", parents=[common], help="comparison table for a run")
    rep.add_argument("run_id")
    rep.add_argument("--md", type=Path, default=None, help="also write markdown here")
    rep.add_argument("--weights", default="8,3,1",
                     help="severity weights Critical,Important,Minor (report-time only)")
    return ap


def _not_yet(what: str) -> int:
    print(f"judgebench: {what} is not implemented in this step", file=sys.stderr)
    return EXIT_UNUSABLE


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


def cmd_run(args) -> int:
    if not (args.fake or args.live):
        print("judgebench: run needs exactly one of --fake (scripted runner) or "
              "--live (REAL providers, spends quota)", file=sys.stderr)
        return EXIT_UNUSABLE
    return _not_yet("run")


def cmd_adjudicate(args) -> int:
    return _not_yet("adjudicate")


def cmd_report(args) -> int:
    return _not_yet("report")


def main(argv=None) -> int:
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
