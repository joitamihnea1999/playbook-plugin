"""CLI entry point for standalone tasks management — DISPATCH ONLY.

Boundary (the 1.5.9 split, design-1.5.9.md): this module parses argv, runs
the session-GC sweep, and routes each command to its owning module — nothing
else. Command bodies live in: tasks/lifecycle.py (work/close/new/blocked/
parked/freehand), tasks/review.py (panel + single-judge), tasks/history.py
(context/intent/timeline/tagger/tag/retro/log), tasks/diagnostics.py
(doctor/audit), tasks/project_setup.py (init/bootstrap), tasks/mindmap.py
(mindmap-sync + map parsing), tasks/merge_prep.py (prepare-merge/
merge-doctor); shared helpers in tasks/shared.py. The trivial list/status/
models delegates stay inline. Dispatch branches import lazily (house style;
also keeps startup flat and cycles impossible). The if/elif chain's shape is
load-bearing: tests/test_cli_dispatch.py parses it against COMMANDS, and the
readme-audit skill greps it to count subcommands. `python3 -m tasks.cli` is
the shipped entry (scripts/tasks execs it) — main() stays here forever.
"""
from __future__ import annotations

import sys
from tasks.core import list_tasks, task_status
from tasks.shared import find_project_root, _gc_dead_sessions

# Every top-level command the dispatcher accepts, aliases included. Pinned two
# ways by tests/test_cli_dispatch.py: this tuple must equal the dispatch
# chain's literals, and every entry must reach its arm through a real
# `python3 -m tasks.cli <cmd>` invocation — so a module peel can never orphan
# an arm silently. Keep it in dispatch order.
COMMANDS = (
    "work", "new", "init", "bootstrap", "list", "ls", "panel-review",
    "models", "plan-review", "impl-review", "judge", "context", "intent",
    "timeline", "tagger", "tag", "retro", "status", "audit", "blocked",
    "parked", "freehand", "doctor", "environment", "detect-verify", "merge-doctor",
    "mindmap-sync", "log", "prepare-merge", "compact", "recall",
)


def print_usage():
    from tasks.template import usage_text
    print(usage_text())


def main():
    # Force utf-8 on Windows where the default console encoding (cp1252) chokes on → and emoji.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help", "help"):
        print_usage()
        return

    _gc_dead_sessions(find_project_root())

    cmd = args[0]
    cmd_args = args[1:]

    if cmd == "work":
        from tasks.lifecycle import cmd_work
        cmd_work(cmd_args)

    elif cmd == "new":
        from tasks.lifecycle import cmd_new
        cmd_new(cmd_args)

    elif cmd == "init":
        from tasks.project_setup import cmd_init
        cmd_init(cmd_args)

    elif cmd == "bootstrap":
        from tasks.project_setup import cmd_bootstrap
        cmd_bootstrap(cmd_args)

    elif cmd in ("list", "ls"):
        project_path = find_project_root()
        pending_only = "--pending" in cmd_args
        list_tasks(project_path, pending_only=pending_only)

    elif cmd == "panel-review":
        from tasks.review import cmd_panel_review
        cmd_panel_review(cmd_args)

    elif cmd == "models":
        # Model-availability discovery + panel selection (task 012; detect/set 1.5.14).
        # `tasks models check [--no-probe]` audits every models.json pin;
        # `tasks models detect [--json]` inventories installed agents + models;
        # `tasks models select [--no-probe]` interactively rewrites the panel;
        # `tasks models set --panel … --default-judge …` writes it non-interactively.
        from tasks.models_check import cli_models
        sys.exit(cli_models(cmd_args, find_project_root()))

    elif cmd in ("plan-review", "impl-review", "judge"):
        from tasks.review import cmd_single_review
        cmd_single_review(cmd, cmd_args)

    elif cmd == "context":
        from tasks.history import cmd_context
        cmd_context(cmd_args)

    elif cmd == "intent":
        from tasks.history import cmd_intent
        cmd_intent(cmd_args)

    elif cmd == "timeline":
        from tasks.history import cmd_timeline
        cmd_timeline(cmd_args)

    elif cmd == "tagger":
        from tasks.history import cmd_tagger
        cmd_tagger(cmd_args)

    elif cmd == "tag":
        from tasks.history import cmd_tag
        cmd_tag(cmd_args)

    elif cmd == "retro":
        from tasks.history import cmd_retro
        cmd_retro(cmd_args)

    elif cmd == "status":
        project_path = find_project_root()
        task_status(project_path)

    elif cmd == "audit":
        from tasks.diagnostics import cmd_audit
        cmd_audit(cmd_args)

    elif cmd == "blocked":
        from tasks.lifecycle import cmd_blocked
        cmd_blocked(cmd_args)

    elif cmd == "parked":
        from tasks.lifecycle import cmd_parked
        cmd_parked(cmd_args)

    elif cmd == "freehand":
        from tasks.lifecycle import cmd_freehand
        cmd_freehand(cmd_args)

    elif cmd == "doctor":
        from tasks.diagnostics import cmd_doctor
        cmd_doctor(cmd_args)

    elif cmd == "environment":
        # Advisory: which optional tools would improve this setup + how to get
        # them (extra panel vendors, sandbox containment, verify tooling,
        # command logging). Never fails — informational (1.5.15).
        from tasks.environment import cli_environment
        sys.exit(cli_environment(cmd_args, find_project_root()))

    elif cmd == "detect-verify":
        # Deterministic suggestion of a project's full verify command (typecheck
        # AND tests AND lint) for /playbook:init to confirm with the user (1.5.19).
        from tasks.verify_detect import cli_detect_verify
        sys.exit(cli_detect_verify(cmd_args, find_project_root()))

    elif cmd == "merge-doctor":
        from tasks.merge_prep import cmd_merge_doctor
        cmd_merge_doctor(cmd_args)

    elif cmd == "mindmap-sync":
        from tasks.mindmap import cmd_mindmap_sync
        cmd_mindmap_sync(cmd_args)

    elif cmd == "log":
        from tasks.history import cmd_log
        cmd_log(cmd_args)

    elif cmd == "prepare-merge":
        from tasks.merge_prep import cmd_prepare_merge
        cmd_prepare_merge(cmd_args)

    elif cmd == "compact":
        # Move agent-marked cold review narrative out of a bloated task.md into
        # task-archive.md (verbatim), keeping the hot trace reviewable (1.5.21).
        from tasks.compact import cmd_compact
        cmd_compact(cmd_args)

    elif cmd == "recall":
        # Cross-tier mind-map retrieval: fetch a node (main + overflow) by id, or
        # locate node ids by keyword — the fetch half of the bootstrap index (1.5.22).
        from tasks.mindmap import cmd_recall
        cmd_recall(cmd_args)

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print_usage()
        sys.exit(1)


if __name__ == "__main__":
    main()
