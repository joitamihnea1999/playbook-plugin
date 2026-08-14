"""Chat-log history and attribution: the `context`, `intent`, `timeline`,
`tagger`, `tag`, `retro`, and `log` arms.

Boundary: every command that READS the per-lane conversation record
(chat_log.md + bash_history) to attribute, tag, extract, or summarize it —
span-based context with the F2 timestamp-window fallback, the vertical-retro
intent extractions (delegating to tasks.intent), task-transition timelines,
span tagging, retro-task generation (delegating to tasks.retro), and the
compact log view. Nothing here writes task state except `tag` (attribution
spans into chat_log.md) and `retro` (a generated retro task). Imports stdlib
+ tasks.core + tasks.shared + leaf libs; never a command module
(design-1.5.9.md §4).
"""
from __future__ import annotations

import sys
from pathlib import Path
from tasks.core import resolve_agent_dir
from tasks.shared import find_project_root


def cmd_context(cmd_args):
    """The `tasks context` arm — body moved verbatim from cli.py (1.5.9 split)."""
    if not cmd_args:
        print("Error: 'context' requires a task number", file=sys.stderr)
        print("Usage: tasks context <number>", file=sys.stderr)
        sys.exit(1)

    task_num = cmd_args[0]
    if task_num.isdigit():
        task_num = task_num.zfill(3)
    project_path = find_project_root()

    chat_log = resolve_agent_dir(project_path) / "chat_log.md"
    if not chat_log.exists():
        print("No .agent/chat_log.md found.", file=sys.stderr)
        sys.exit(1)

    import re
    open_tag = re.compile(r'^<!--\s*T' + re.escape(task_num) + r'\s*-->$')
    close_tag = re.compile(r'^<!--\s*/T' + re.escape(task_num) + r'\s*-->$')

    spans = []
    current_span = []
    inside = False
    for line in chat_log.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not inside and open_tag.match(stripped):
            inside = True
            continue
        elif inside and close_tag.match(stripped):
            spans.append("\n".join(current_span))
            current_span = []
            inside = False
            continue
        if inside:
            current_span.append(line)

    # Handle unclosed span at end of file
    if inside and current_span:
        spans.append("\n".join(current_span))

    if not spans:
        # F2 (untagged projects): `<!-- TNNN -->` spans are written only by
        # `tasks tag`, which nothing runs automatically — so this path was
        # blind for EVERY task on most projects while gate entries +
        # bash_history held everything needed to attribute messages. Fall
        # back to timestamp-window attribution (the same fallback `tasks
        # intent`'s chat layer uses); still fail loudly when nothing is
        # attributable. stdout stays pure messages — the provenance note
        # goes to stderr.
        fallback_msgs = []
        try:
            _n = int(task_num)
        except ValueError:
            _n = None
        if _n is not None:
            from tasks.retro import build_task_windows, extract_chatlog
            _bash_history = resolve_agent_dir(project_path) / "bash_history"
            _windows = build_task_windows(
                chat_log, _bash_history if _bash_history.exists() else None)
            if _n in _windows:
                fallback_msgs = [m for m in extract_chatlog(chat_log, _windows)
                                 if m.get("task") == _n]
        if not fallback_msgs:
            print(f"No attributed messages for task {task_num}.", file=sys.stderr)
            sys.exit(1)
        print(f"note: no <!-- T{task_num} --> tags in chat_log.md; messages "
              "attributed via timestamp window (gate entries + bash_history). "
              "Run `tasks tag` to persist attribution.", file=sys.stderr)
        _max_line = 200
        for m in fallback_msgs:
            text = " ".join(m["text"].split())
            if len(text) > _max_line:
                text = text[:_max_line] + "..."
            print(f"[M{m['id']:03d}] {text}")
        # spans is empty: the tagged-span output loop below is a no-op.

    # Token-efficient output: strip markdown boilerplate, one line per message
    import re as _re
    max_line = 200
    msg_header = _re.compile(r'^\*\*\[(M\d+)\]\*\*.*')
    gate_header = _re.compile(r'^\*\*\[G\d+:\d+\]\*\*.*')
    for span in spans:
        msg_id = None
        msg_lines = []
        in_gate = False
        for line in span.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped == "---":
                in_gate = False
                continue
            if gate_header.match(stripped):
                in_gate = True
                continue
            if in_gate:
                continue
            m = msg_header.match(stripped)
            if m:
                # Flush previous message
                if msg_id and msg_lines:
                    text = " ".join(msg_lines)
                    if len(text) > max_line:
                        text = text[:max_line] + "..."
                    print(f"[{msg_id}] {text}")
                msg_id = m.group(1)
                msg_lines = []
            else:
                msg_lines.append(stripped)
        # Flush last message
        if msg_id and msg_lines:
            text = " ".join(msg_lines)
            if len(text) > max_line:
                text = text[:max_line] + "..."
            print(f"[{msg_id}] {text}")

def cmd_intent(cmd_args):
    """The `tasks intent` arm — body moved verbatim from cli.py (1.5.9 split)."""
    # Vertical retro: 4 blind intent extractions over one task's layers.
    if not cmd_args:
        print("Error: 'intent' requires a task number", file=sys.stderr)
        print("Usage: tasks intent <number> [--chat-file P] [--base REF --head REF] "
              "[--collect-only] [--timeout S]", file=sys.stderr)
        sys.exit(1)

    task_num = cmd_args[0]
    if task_num.isdigit():
        task_num = task_num.zfill(3)
    chat_file = base = head = None
    collect_only = False
    # None = not yet resolved; the real default comes from tasks.core so
    # `tasks intent` honours the same review knobs as plan/impl review
    # instead of pinning its own 300s. --timeout still overrides.
    timeout_secs = None
    i = 1
    while i < len(cmd_args):
        a = cmd_args[i]
        if a == "--chat-file" and i + 1 < len(cmd_args):
            chat_file = Path(cmd_args[i + 1]); i += 2
        elif a == "--base" and i + 1 < len(cmd_args):
            base = cmd_args[i + 1]; i += 2
        elif a == "--head" and i + 1 < len(cmd_args):
            head = cmd_args[i + 1]; i += 2
        elif a == "--collect-only":
            collect_only = True; i += 1
        elif a == "--timeout" and i + 1 < len(cmd_args):
            timeout_secs = int(cmd_args[i + 1]); i += 2
        else:
            print(f"Error: unknown option for intent: {a}", file=sys.stderr)
            sys.exit(1)

    if bool(base) != bool(head):
        print("Error: --base and --head must be given together (an explicit range)",
              file=sys.stderr)
        sys.exit(1)

    from tasks.intent import (
        collect_all, run_extractions, make_default_runner,
        write_run, find_task_dir, new_run_id, last_intent_entry, LAYERS,
    )
    project_path = find_project_root()
    from tasks.core import format_timeout_label
    if timeout_secs is None:
        from tasks.core import resolve_review_timeout
        timeout_secs = resolve_review_timeout(project_path)
    agent_dir = resolve_agent_dir(project_path)
    task_dir = find_task_dir(agent_dir / "tasks", task_num)
    if task_dir is None:
        print(f"Error: no task {task_num} under {agent_dir / 'tasks'}", file=sys.stderr)
        sys.exit(1)

    slices = collect_all(project_path, agent_dir, task_dir, task_num,
                         chat_file=chat_file, base=base, head=head)
    print(f"Intent review — task {task_num} ({task_dir.name})")
    for layer in LAYERS:
        s = slices[layer]
        print(f"  {layer:7} {'✓' if s.available else '✗'}  {s.provenance}")
    avail = [l for l in LAYERS if slices[l].available]
    if not avail:
        print("Error: no available evidence on any layer — nothing to infer. "
              "Pass --chat-file and/or --base/--head.", file=sys.stderr)
        sys.exit(1)

    run_id = new_run_id()
    if collect_only:
        from tasks.intent import build_prompt
        reports = {l: (build_prompt(slices[l]) if slices[l].available
                       else f"# Intent inferred from {l}\n\n_(no evidence — "
                            f"{slices[l].provenance})_\n") for l in LAYERS}
        print("\n(--collect-only: wrote prompts, skipped model calls)")
    else:
        print(f"\nRunning {len(avail)} blind extraction(s) "
              f"(default judge, {format_timeout_label(timeout_secs)} each)...", flush=True)
        reports = run_extractions(slices, make_default_runner(
            project_path, timeout_secs=timeout_secs))

    run_dir = write_run(task_dir, slices, reports, run_id=run_id)
    rel = run_dir.relative_to(project_path)
    print(f"\nReports written: {rel}/")
    print(f"Grading sheet:   {rel}/review.md")
    prior = last_intent_entry(project_path / "INTENT.md", task_num)
    if prior:
        print("Prior validated intent exists — reconcile as a DELTA against INTENT.md.")
    print("\nNext: read review.md with the user, grade the seams, then append "
          "vetted intent to INTENT.md (the /intent command drives this).")

def cmd_timeline(cmd_args):
    """The `tasks timeline` arm — body moved verbatim from cli.py (1.5.9 split)."""
    project_path = find_project_root()
    bash_history = resolve_agent_dir(project_path) / "bash_history"
    if not bash_history.exists():
        print("No .agent/bash_history found.", file=sys.stderr)
        sys.exit(1)

    import re
    # Match: timestamp | AGENT/SCRIPT | tasks work/new/done ...
    pattern = re.compile(
        r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| \w+ \| '
        r'(?:.*/)?(tasks (?:work|new) .+)$'
    )
    seen = set()
    for line in bash_history.read_text(encoding="utf-8", errors="replace").splitlines():
        m = pattern.match(line)
        if m:
            cmd = m.group(2)
            # Deduplicate AGENT+SCRIPT echoes (same command within 2 lines)
            if cmd not in seen:
                seen.add(cmd)
                print(f"{m.group(1)}  {cmd}")
            else:
                seen.discard(cmd)

def cmd_tagger(cmd_args):
    """The `tasks tagger` arm — body moved verbatim from cli.py (1.5.9 split)."""
    project_path = find_project_root()
    chat_log = resolve_agent_dir(project_path) / "chat_log.md"
    bash_history = resolve_agent_dir(project_path) / "bash_history"
    if not chat_log.exists():
        print("No .agent/chat_log.md found.", file=sys.stderr)
        sys.exit(1)
    if not bash_history.exists():
        print("No .agent/bash_history found.", file=sys.stderr)
        sys.exit(1)

    import re

    # 1. Parse messages from chat_log.md: (timestamp, msg_id, text)
    msg_header = re.compile(
        r'^\*\*\[(M\d+)\]\*\* \[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC\]'
    )
    gate_header = re.compile(r'^\*\*\[G\d+:\d+\]\*\*')
    entries = []  # (timestamp_str, sort_key, display_line)
    max_line = 200

    msg_id = None
    msg_ts = None
    msg_lines = []
    in_gate = False

    def flush_msg():
        if msg_id and msg_lines:
            text = " ".join(msg_lines)
            if len(text) > max_line:
                text = text[:max_line] + "..."
            entries.append((msg_ts, 0, f"[{msg_id}] {text}"))

    for line in chat_log.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "---":
            in_gate = False
            continue
        if gate_header.match(stripped):
            in_gate = True
            continue
        if in_gate:
            continue
        m = msg_header.match(stripped)
        if m:
            flush_msg()
            msg_id = m.group(1)
            msg_ts = m.group(2)
            msg_lines = []
        elif stripped.startswith("<!--"):
            continue  # skip attribution tags / comments
        else:
            msg_lines.append(stripped)

    flush_msg()

    # 2. Parse task transitions from bash_history
    task_pattern = re.compile(
        r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| \w+ \| '
        r'(?:.*/)?(tasks (?:work|new) .+)$'
    )
    seen = set()
    for line in bash_history.read_text(encoding="utf-8", errors="replace").splitlines():
        m = task_pattern.match(line)
        if m:
            task_cmd = m.group(2)
            if task_cmd not in seen:
                seen.add(task_cmd)
                entries.append((m.group(1), 1, f"--- {task_cmd} ---"))
            else:
                seen.discard(task_cmd)

    # 3. Sort by timestamp, then task transitions before messages (sort_key: 1 before 0)
    #    Actually: task transitions AFTER messages at same timestamp makes more sense
    #    But transitions should come BEFORE subsequent messages — sort_key 1 means
    #    transitions sort after messages at same second. That's fine: the transition
    #    happened between messages.
    entries.sort(key=lambda e: (e[0], e[1]))

    # 4. Output
    for _, _, display in entries:
        print(display)

def cmd_tag(cmd_args):
    """The `tasks tag` arm — body moved verbatim from cli.py (1.5.9 split)."""
    dry_run = "--dry-run" in cmd_args
    project_path = find_project_root()
    chat_log = resolve_agent_dir(project_path) / "chat_log.md"
    bash_history = resolve_agent_dir(project_path) / "bash_history"
    if not chat_log.exists():
        print("No .agent/chat_log.md found.", file=sys.stderr)
        sys.exit(1)
    if not bash_history.exists():
        print("No .agent/bash_history found.", file=sys.stderr)
        sys.exit(1)

    import re
    from bisect import bisect_right

    # 1. Build sorted task transition list from bash_history
    #    Each entry: (timestamp, active_task_or_None)
    task_pattern = re.compile(
        r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| \w+ \| '
        r'(?:.*/)?(tasks (?:work|new) .+)$'
    )
    work_re = re.compile(r'tasks work (\d+)')
    transitions = []  # [(timestamp, task_num_or_None)]
    seen = set()
    for line in bash_history.read_text(encoding="utf-8", errors="replace").splitlines():
        m = task_pattern.match(line)
        if m:
            task_cmd = m.group(2)
            if task_cmd not in seen:
                seen.add(task_cmd)
            else:
                seen.discard(task_cmd)
                continue
            ts = m.group(1)
            if "work done" in task_cmd:
                transitions.append((ts, None))
            else:
                wm = work_re.search(task_cmd)
                if wm:
                    transitions.append((ts, wm.group(1).zfill(3)))
    transitions.sort(key=lambda t: t[0])
    trans_times = [t[0] for t in transitions]

    def active_task_at(ts):
        """Return task number active at timestamp ts, or None."""
        idx = bisect_right(trans_times, ts) - 1
        if idx < 0:
            # F2 (first-task attribution): messages before the first
            # activation are the project seed — the mandate that produced
            # the first task. Attribute them to the first task ever
            # activated instead of dropping them.
            return next((t for _, t in transitions if t is not None), None)
        return transitions[idx][1]

    # 2. Scan chat_log.md, find message headers with timestamps,
    #    insert tags at task transition points
    msg_header = re.compile(
        r'^(\*\*\[(M\d+)\]\*\* \[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC\])'
    )
    # Also detect existing tags to avoid double-tagging
    existing_tag = re.compile(r'^<!--\s*/?T\d+\s*-->$')

    lines = chat_log.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    output = []
    current_tag = None  # currently open tag (task number)
    tags_inserted = 0

    for line in lines:
        stripped = line.strip()
        # Skip existing attribution tags (we'll rewrite them)
        if existing_tag.match(stripped):
            continue

        m = msg_header.match(stripped)
        if m:
            msg_id = m.group(2)
            msg_ts = m.group(3)
            task = active_task_at(msg_ts)

            if task != current_tag:
                # Close previous tag if open
                if current_tag is not None:
                    output.append(f"<!-- /T{current_tag} -->\n")
                    output.append("\n")
                    tags_inserted += 1
                # Open new tag if task is active
                if task is not None:
                    output.append(f"<!-- T{task} -->\n")
                    output.append("\n")
                    tags_inserted += 1
                current_tag = task

        output.append(line)

    # Close final tag if still open
    if current_tag is not None:
        output.append(f"\n<!-- /T{current_tag} -->\n")
        tags_inserted += 1

    if dry_run:
        print(f"Would insert {tags_inserted} tags into chat_log.md")
        # Show first few transitions
        current_tag = None
        for line in output:
            stripped = line.strip()
            if existing_tag.match(stripped):
                print(f"  {stripped}")
    else:
        chat_log.write_text("".join(output), encoding="utf-8")
        print(f"Inserted {tags_inserted} tags into chat_log.md")

def cmd_retro(cmd_args):
    """The `tasks retro` arm — body moved verbatim from cli.py (1.5.9 split)."""
    project_path = find_project_root()
    # Parse --since N flag
    since = 0
    i = 0
    while i < len(cmd_args):
        if cmd_args[i] == "--since" and i + 1 < len(cmd_args):
            try:
                since = int(cmd_args[i + 1])
            except ValueError:
                print(f"Error: --since requires a number", file=sys.stderr)
                sys.exit(1)
            i += 2
        else:
            i += 1

    from tasks.retro import (
        extract_tasks, extract_chatlog, extract_mindmap,
        build_task_windows,
    )

    tasks_dir = resolve_agent_dir(project_path) / "tasks"
    chatlog_path = resolve_agent_dir(project_path) / "chat_log.md"
    bash_history_path = resolve_agent_dir(project_path) / "bash_history"
    mindmap_path = project_path / "MIND_MAP.md"

    # Extract data
    tasks = extract_tasks(tasks_dir, since=since)
    task_windows = build_task_windows(chatlog_path, bash_history_path)
    chatlog = extract_chatlog(chatlog_path, task_windows)
    mindmap = extract_mindmap(mindmap_path)

    if not tasks:
        print("No tasks found in window.", file=sys.stderr)
        sys.exit(1)

    # Run structural analysis passes
    from tasks.retro import (
        analyze_intent_health, analyze_garbage,
        generate_retro_task,
    )
    health = analyze_intent_health(tasks)
    gc = analyze_garbage(tasks)

    # Generate the retro task.md — a cognitive program
    retro_content = generate_retro_task(
        tasks=tasks, chatlog=chatlog, mindmap=mindmap,
        health=health, gc=gc,
    )

    # Create as a new task
    from tasks.core import _next_task_number, _slugify
    tasks_dir_path = resolve_agent_dir(project_path) / "tasks"
    task_num = _next_task_number(tasks_dir_path)
    first = tasks[0]["number"]
    last = tasks[-1]["number"]
    slug = f"retro-{first:03d}-{last:03d}"
    folder_name = f"{task_num:03d}-{slug}"
    task_dir = tasks_dir_path / folder_name
    task_dir.mkdir(parents=True)
    task_file = task_dir / "task.md"
    task_file.write_text(retro_content, encoding="utf-8")

    print(f"Created: {task_file.relative_to(project_path)}")
    print(f"Retro task T{task_num:03d} — {len(tasks)} tasks in window, "
          f"{len(chatlog)} chat messages, {len(mindmap)} mind map nodes")
    print(f"Next: tasks work {task_num}")

def cmd_log(cmd_args):
    """The `tasks log` arm — body moved verbatim from cli.py (1.5.9 split)."""
    # tasks log [N] [--width W]
    # Compact one-line-per-message view of chat_log.md (no gate cruft).
    # N: show only the last N messages (default: all).
    # --width: crop each message body to W chars (default 500).
    import re
    cmd_args = sys.argv[2:]
    last_n = None
    width = 500
    i = 0
    while i < len(cmd_args):
        a = cmd_args[i]
        if a == "--width" and i + 1 < len(cmd_args):
            width = max(10, int(cmd_args[i + 1])); i += 2
        elif a.isdigit():
            last_n = int(a); i += 1
        else:
            i += 1
    project_path = find_project_root()
    chat_log = resolve_agent_dir(project_path) / "chat_log.md"
    if not chat_log.exists():
        print("Error: .agent/chat_log.md not found", file=sys.stderr)
        sys.exit(1)
    text = chat_log.read_text(encoding="utf-8", errors="replace")
    blocks = text.split("\n---\n")
    lines = []
    for block in blocks:
        # Entry header format grew a ` (provider/pid)` suffix with multi-provider
        # tagging (commit 0fca4b0), e.g. `**[M12]** [… UTC] `HOST` (claude/pid-9)`.
        # The suffix must be OPTIONAL (legacy entries lack it, and requiring it
        # made `tasks log` silently print nothing — bug report #5b) and CAPTURED
        # (its provider token is the real agent; the backticked field is now just
        # `HOST`). Prefer the suffix provider; fall back to the backticked name.
        m = re.match(
            r'\*\*(\[M\d+\])\*\* \[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}):\d{2} UTC\] '
            r'`(\w+)`(?:\s*\(([^)/]+)/[^)]*\))?\s*\n+(.*)',
            block.strip(), re.DOTALL
        )
        if m:
            mid, ts, role, provider, body = m.groups()
            agent = provider or role
            body = " ".join(body.split())
            if len(body) > width:
                body = body[:width - 1] + "…"
            lines.append(f"{mid} {ts} {agent:<6} {body}")
    if last_n is not None:
        lines = lines[-last_n:]
    for line in lines:
        print(line)
