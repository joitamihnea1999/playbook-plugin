"""Branch-merge preparation and audit: the `prepare-merge` machinery and the
`merge-doctor` arm.

Boundary: everything that readies one branch's Playbook state to merge into
another — task renumbering with live-session guards (`_session_is_live` is
deliberately a SEPARATE, stricter pid-only policy from shared's
`_session_is_dead`; do not unify them), chat-log MID re-sequencing, the
mind-map node-collision report (via mindmap's byte-exact `_parse_nodes`) —
plus the thin `merge-doctor` delegate to `tasks.core.run_merge_doctor`.
Imports stdlib + tasks.core + tasks.shared + tasks.mindmap; never a command
module (design-1.5.9.md §4).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from tasks.atomic import atomic_write
from tasks.core import resolve_agent_dir, run_merge_doctor
from tasks.mindmap import _parse_nodes
from tasks.shared import find_project_root


def _read_text_lossy(path: Path) -> tuple[str, bool]:
    """Read UTF-8 text, tolerating non-UTF-8 bytes, and report whether the
    decode was lossy (N3).

    A chat_log/task file rewritten from an `errors="replace"` decode silently
    turns any non-UTF-8 byte into U+FFFD. Switching to `surrogateescape` would
    only relocate the failure to the next strict-encode site (a crash instead of
    a corruption) — a worse trade for a merge that must not blow up. So the
    behavior stays crash-safe, but the caller can now WARN instead of losing
    bytes silently. Returns (text, was_lossy)."""
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8"), False
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace"), True


def _cmd_prepare_merge(project_path: Path, target: str, dry_run: bool) -> None:
    """Prepare current branch's Playbook state to merge cleanly into target."""
    import subprocess
    import re as _re

    agent_dir = resolve_agent_dir(project_path)

    # --- Shared: merge base ---
    try:
        merge_base = subprocess.check_output(
            ["git", "-C", str(project_path), "merge-base", "HEAD", target],
            text=True,
        ).strip()
    except subprocess.CalledProcessError:
        print(f"Error: could not compute merge base with '{target}'. Is '{target}' a valid branch?",
              file=sys.stderr)
        sys.exit(1)

    # --- Step 1: Task renumbering (placeholder) ---
    _prepare_merge_tasks(project_path, agent_dir, target, merge_base, dry_run)

    # --- Step 2: Chat log re-sequencing (placeholder) ---
    _prepare_merge_chatlog(project_path, agent_dir, target, merge_base, dry_run)

    # --- Step 3: MIND_MAP collision report (placeholder) ---
    _prepare_merge_mindmap(project_path, target, merge_base)

    if dry_run:
        print("(dry-run — no files written)")


def _renumbered_dir_name(old_name: str, new_num: int) -> str:
    """Rename a `NNN-slug` task dir to `new_num`, PRESERVING the zero-pad width
    (C2). The single source of truth for both the `--dry-run` preview and the
    real rename — computing them separately let the preview lie about the
    action (`002-feat-two` → preview `003`, action `302`). Slicing by the
    *unpadded* number's width (`str(new_num) + old_name[len(str(old_num)):]`)
    corrupted every task < 100: it kept the padding zeros as data
    (`002` → `302`, `099` → `1009`). Replace the WHOLE leading digit run.
    """
    import re
    m = re.match(r"^(\d+)(.*)$", old_name)
    if not m:
        # No leading number — nothing sane to renumber; leave as-is.
        return old_name
    digits, rest = m.group(1), m.group(2)
    # Preserve the original prefix width (canonical layout is 3-digit); a
    # larger new number naturally widens (format never truncates).
    return f"{new_num:0{len(digits)}d}" + rest


def _git_ls_tasks(project_path: Path, ref: str, agent_dir: Path) -> dict[int, str]:
    """Return {task_number: dir_name} for tasks present at git ref. Empty dict if path absent."""
    import subprocess
    import re
    agent_dir_rel = str(agent_dir.relative_to(project_path))
    tasks_path = agent_dir_rel + "/tasks/"
    try:
        out = subprocess.check_output(
            ["git", "-C", str(project_path), "ls-tree", "--name-only", ref, tasks_path],
            text=True, stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return {}
    result: dict[int, str] = {}
    for entry in out.splitlines():
        name = entry.rstrip("/").split("/")[-1]
        m = re.match(r"^(\d+)-", name)
        if m:
            result[int(m.group(1))] = name
    return result


def _prepare_merge_tasks(project_path: Path, agent_dir: Path, target: str,
                         merge_base: str, dry_run: bool) -> None:
    import re

    base_tasks = _git_ls_tasks(project_path, merge_base, agent_dir)
    target_tasks = _git_ls_tasks(project_path, target, agent_dir)

    # Current tasks: scan working tree directly
    tasks_dir = agent_dir / "tasks"
    current_tasks: dict[int, str] = {}
    if tasks_dir.exists():
        for d in tasks_dir.iterdir():
            if d.is_dir():
                m = re.match(r"^(\d+)-", d.name)
                if m:
                    current_tasks[int(m.group(1))] = d.name

    new_on_current = {n: name for n, name in current_tasks.items() if n not in base_tasks}
    new_on_target = {n: name for n, name in target_tasks.items() if n not in base_tasks}
    collisions = set(new_on_current) & set(new_on_target)

    if not collisions:
        print("Tasks: no collisions — already clean.")
        return

    # Assign new numbers starting after the highest number on target (across all tasks, not just new)
    max_target = max(target_tasks) if target_tasks else 0
    rename_map: dict[int, int] = {}
    next_num = max_target + 1
    for old_num in sorted(collisions):
        rename_map[old_num] = next_num
        next_num += 1

    print("Tasks: " + str(len(collisions)) + " collision(s) to renumber: "
          + ", ".join(f"T{n}→T{rename_map[n]}" for n in sorted(collisions)))

    if dry_run:
        for old_num in sorted(rename_map):
            old_name = current_tasks[old_num]
            new_name = _renumbered_dir_name(old_name, rename_map[old_num])
            print(f"  [dry-run] rename {old_name} → {new_name}")
        return

    # Abort before any mutation if a live session holds a task being renumbered
    sessions_dir = agent_dir / "sessions"
    if sessions_dir.exists():
        for session_dir in sessions_dir.iterdir():
            if not session_dir.is_dir():
                continue
            state_file = session_dir / "current_state"
            if not state_file.exists():
                continue
            try:
                task_num = int(state_file.read_text(encoding="utf-8").strip())
            except ValueError:
                continue
            if task_num in rename_map and _session_is_live(session_dir):
                print(
                    f"Error: session {session_dir.name} is live and holds task T{task_num}. "
                    "Stop that session before running prepare-merge.",
                    file=sys.stderr,
                )
                sys.exit(1)

    _rename_colliding_tasks(project_path, agent_dir, current_tasks, rename_map)
    _rewrite_task_refs(project_path, agent_dir, rename_map)


def _rename_colliding_tasks(project_path: Path, agent_dir: Path,
                             current_tasks: dict[int, str], rename_map: dict[int, int]) -> None:
    import subprocess
    import re
    tasks_dir = agent_dir / "tasks"

    for old_num in sorted(rename_map):
        new_num = rename_map[old_num]
        old_name = current_tasks[old_num]
        # Pad-preserving, UNIFIED with the dry-run preview (C2).
        new_name = _renumbered_dir_name(old_name, new_num)
        old_rel = str((tasks_dir / old_name).relative_to(project_path))
        new_rel = str((tasks_dir / new_name).relative_to(project_path))

        result = subprocess.run(
            ["git", "-C", str(project_path), "mv", old_rel, new_rel],
            capture_output=True,
        )
        if result.returncode != 0:
            # Fallback for untracked dirs
            (tasks_dir / old_name).rename(tasks_dir / new_name)

        # Rewrite H1 title: "# 133 - ..." → "# 135 - ..."
        task_md = tasks_dir / new_name / "task.md"
        if task_md.exists():
            text = task_md.read_text(encoding="utf-8")
            text = re.sub(
                rf"^# {old_num}(?=[\s\-]|$)",
                f"# {new_num}",
                text,
                count=1,
                flags=re.MULTILINE,
            )
            atomic_write(task_md, text)

    # Clear chat_log_offset for all sessions — stale after renames + upcoming ref rewrite
    sessions_dir = agent_dir / "sessions"
    if sessions_dir.exists():
        for session_dir in sessions_dir.iterdir():
            if session_dir.is_dir():
                offset_file = session_dir / "chat_log_offset"
                if offset_file.exists():
                    offset_file.unlink()


def _session_is_live(session_dir: Path) -> bool:
    """Return True if the session's process is still running."""
    name = session_dir.name
    if name.startswith("pid-"):
        try:
            pid = int(name[4:])
            os.kill(pid, 0)
            return True
        except (ValueError, OSError):
            return False
    return False


def _rewrite_task_refs(project_path: Path, agent_dir: Path, rename_map: dict[int, int]) -> None:
    import re

    def _apply(text: str) -> str:
        # Process in descending order of old number to avoid cascading (T13 inside T133)
        for old_num in sorted(rename_map, reverse=True):
            new_num = rename_map[old_num]
            text = re.sub(rf"\bT{old_num}\b", f"T{new_num}", text)
            text = re.sub(rf"\btask {old_num}\b", f"task {new_num}", text, flags=re.IGNORECASE)
            text = re.sub(rf"\b{old_num}(?=-[a-z])", str(new_num), text)
            text = re.sub(rf"\bG{old_num}:(\d+)\b", rf"G{new_num}:\1", text)
        return text

    # Rewrite all task.md files (including non-colliding tasks that reference old numbers)
    tasks_dir = agent_dir / "tasks"
    if tasks_dir.exists():
        for task_dir in tasks_dir.iterdir():
            if task_dir.is_dir():
                task_md = task_dir / "task.md"
                if task_md.exists():
                    original = task_md.read_text(encoding="utf-8")
                    updated = _apply(original)
                    if updated != original:
                        atomic_write(task_md, updated)

    # Rewrite chat_log.md
    chat_log = agent_dir / "chat_log.md"
    if chat_log.exists():
        original, lossy = _read_text_lossy(chat_log)
        updated = _apply(original)
        if updated != original:
            if lossy:
                print(f"[playbook] warning: {chat_log} has non-UTF-8 bytes; the "
                      f"rewrite normalizes them to U+FFFD (N3).", file=sys.stderr)
            atomic_write(chat_log, updated)

    # Rewrite current_state in dead sessions; live sessions were already rejected upstream
    sessions_dir = agent_dir / "sessions"
    if sessions_dir.exists():
        for session_dir in sessions_dir.iterdir():
            if not session_dir.is_dir():
                continue
            state_file = session_dir / "current_state"
            if not state_file.exists():
                continue
            try:
                task_num = int(state_file.read_text(encoding="utf-8").strip())
            except ValueError:
                continue
            if task_num in rename_map:
                atomic_write(state_file, str(rename_map[task_num]) + "\n")


def _prepare_merge_chatlog(project_path: Path, agent_dir: Path, target: str,
                           merge_base: str, dry_run: bool) -> None:
    import subprocess
    import re

    agent_dir_rel = str(agent_dir.relative_to(project_path))

    def _git_show_text(ref: str, rel_path: str) -> str:
        try:
            return subprocess.check_output(
                ["git", "-C", str(project_path), "show", f"{ref}:{rel_path}"],
                text=True, stderr=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError:
            return ""

    def _last_mid(text: str) -> int:
        mids = re.findall(r"\*\*\[M(\d+)\]\*\*", text)
        return max(int(m) for m in mids) if mids else 0

    chat_log_rel = agent_dir_rel + "/chat_log.md"
    base_last = _last_mid(_git_show_text(merge_base, chat_log_rel))
    target_last = _last_mid(_git_show_text(target, chat_log_rel))

    chat_log = agent_dir / "chat_log.md"
    if not chat_log.exists():
        print("Chat log: not found — skipping.")
        return

    current_text, _chat_lossy = _read_text_lossy(chat_log)
    new_mids = [int(m) for m in re.findall(r"\*\*\[M(\d+)\]\*\*", current_text) if int(m) > base_last]

    if not new_mids:
        print("Chat log: no new entries beyond merge base — already clean.")
        return

    # Idempotency: if new entries already start beyond target's last MID, we're done
    if min(new_mids) > target_last:
        print("Chat log: new entries already positioned beyond target's last MID — already clean.")
        return

    offset = target_last - base_last
    if offset <= 0:
        print("Chat log: target has not advanced beyond merge base — no re-sequencing needed.")
        return

    def _reseq(m: "re.Match[str]") -> str:
        mid = int(m.group(1))
        if mid > base_last:
            width = max(len(m.group(1)), len(str(mid + offset)))
            return f"**[M{mid + offset:0{width}d}]**"
        return m.group(0)

    updated = re.sub(r"\*\*\[M(\d+)\]\*\*", _reseq, current_text)
    new_highest = max(new_mids) + offset

    print(f"Chat log: re-sequencing {len(new_mids)} new entr{'y' if len(new_mids)==1 else 'ies'} "
          f"(offset +{offset}, new highest M{new_highest}).")

    if dry_run:
        return

    if _chat_lossy:
        print(f"[playbook] warning: {chat_log} has non-UTF-8 bytes; the "
              f"re-sequence normalizes them to U+FFFD (N3).", file=sys.stderr)
    atomic_write(chat_log, updated)
    # chat_log_counter is the DATA file of the chat-log-hook flock protocol
    # (scripts/chat-log-hook locks chat_log_counter.lock and rewrites this file
    # in place under that lock). Left as a plain in-place write on purpose: an
    # os.replace here would swap the inode out from under a concurrent locked
    # incrementer, breaking the increment's mutual exclusion. Do not "atomic-ize".
    (agent_dir / "chat_log_counter").write_text(str(new_highest) + "\n", encoding="utf-8")


def _prepare_merge_mindmap(project_path: Path, target: str, merge_base: str) -> None:
    import subprocess
    import hashlib

    def _git_show_text(ref: str, rel_path: str) -> str:
        try:
            return subprocess.check_output(
                ["git", "-C", str(project_path), "show", f"{ref}:{rel_path}"],
                text=True, stderr=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError:
            return ""

    def _h(text: str) -> str:
        return hashlib.md5(text.encode()).hexdigest()

    collision_found = False
    for filename in ("MIND_MAP.md", "MIND_MAP_OVERFLOW.md"):
        base_nodes = _parse_nodes(_git_show_text(merge_base, filename))
        target_nodes = _parse_nodes(_git_show_text(target, filename))
        cur_file = project_path / filename
        current_nodes = _parse_nodes(cur_file.read_text(encoding="utf-8") if cur_file.exists() else "")

        all_ids = set(base_nodes) | set(target_nodes) | set(current_nodes)
        changed_current = {n for n in all_ids if _h(current_nodes.get(n, "")) != _h(base_nodes.get(n, ""))}
        changed_target = {n for n in all_ids if _h(target_nodes.get(n, "")) != _h(base_nodes.get(n, ""))}
        collisions = sorted(changed_current & changed_target)
        if not collisions:
            continue

        collision_found = True
        print(f"\n{filename}: {len(collisions)} node collision(s) requiring manual synthesis:")
        for node_id in collisions:
            tgt = target_nodes.get(node_id, "(absent on target)")
            cur = current_nodes.get(node_id, "(absent on current)")
            print(f"\n  ── [{node_id}] {target} (target) ──")
            for line in tgt.splitlines():
                print(f"  {line}")
            print(f"\n  ── [{node_id}] HEAD (current) ──")
            for line in cur.splitlines():
                print(f"  {line}")

    if not collision_found:
        print("MIND_MAP: no node collisions.")


def cmd_prepare_merge(cmd_args):
    """The `tasks prepare-merge` arm — body moved verbatim from cli.py (1.5.9 split)."""
    project_path = find_project_root()
    target = "main"
    dry_run = False
    remaining = list(cmd_args)
    while remaining:
        a = remaining.pop(0)
        if a == "--target" and remaining:
            target = remaining.pop(0)
        elif a == "--dry-run":
            dry_run = True
        else:
            print(f"Unknown argument: {a}", file=sys.stderr)
            sys.exit(1)
    _cmd_prepare_merge(project_path, target, dry_run)


def cmd_merge_doctor(cmd_args):
    """The `tasks merge-doctor` arm — body moved verbatim from cli.py (1.5.9 split)."""
    if cmd_args and cmd_args[0] in ("--help", "-h"):
        print("Usage: tasks merge-doctor <source-branch> [target-branch]")
        print()
        print("  Audits a cross-namespace merge for per-user contamination,")
        print("  stranded conflict markers, and legacy .agent/ paths.")
        print("  Inspects the in-progress merge (MERGE_HEAD present) or the most")
        print("  recent merge commit reachable from HEAD. <source>/<target> are")
        print("  used for cross-comparison only — no branch is checked out.")
        print()
        print("  Exit: 1 if any actionable findings, else 0.")
        sys.exit(0)
    if not cmd_args:
        print("Usage: tasks merge-doctor <source-branch> [target-branch]", file=sys.stderr)
        print("  Audits a cross-namespace merge for per-user contamination,", file=sys.stderr)
        print("  stranded conflict markers, and legacy .agent/ paths.", file=sys.stderr)
        print("  Run 'tasks merge-doctor --help' for details.", file=sys.stderr)
        sys.exit(2)
    source = cmd_args[0]
    target = cmd_args[1] if len(cmd_args) > 1 else "main"
    project_path = find_project_root()
    if not (project_path / ".git").exists():
        print(f"Error: not a git repository: {project_path}", file=sys.stderr)
        sys.exit(2)
    # Validate both refs up front. A bad ref otherwise collapses to an empty
    # user set in _md_user_dirs and the doctor can exit 0 ("SAFE") while
    # silently skipping the intended cross-branch comparison.
    import subprocess as _sp
    for _ref in (source, target):
        if _sp.run(["git", "rev-parse", "--verify", "--quiet", f"{_ref}^{{commit}}"],
                   cwd=str(project_path), capture_output=True).returncode != 0:
            print(f"Error: not a valid git ref: {_ref}", file=sys.stderr)
            print("Usage: tasks merge-doctor <source-branch> [target-branch]", file=sys.stderr)
            sys.exit(2)
    findings = run_merge_doctor(project_path, source, target)
    sys.exit(1 if findings else 0)
