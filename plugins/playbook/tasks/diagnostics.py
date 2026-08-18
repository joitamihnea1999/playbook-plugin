"""Mechanical health checks: the `doctor` and `audit` arms.

Boundary: read-only diagnosis. `doctor` inspects the RUNNING install (its own
tree via __file__, CLAUDE_PLUGIN_ROOT, hook copies, session dirs via shared's
one liveness policy, config/model/README advisories via the leaf libs) and
only reports — no runtime resolution or enforcement changes here. `audit`
runs the pre-panel sweeps (tasks.audit) and records the receipt. Imports
stdlib + tasks.core + tasks.shared + leaf libs; never a command module
(design-1.5.9.md §4).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from tasks.core import resolve_agent_dir, resolve_session_id
from tasks.shared import (
    _merge_verify_issues, _merge_verify_untracked, _own_session_id,
    _session_is_dead, find_project_root,
)


def _resolver_parity_verdict(has_root: bool, py_sid: str, bash_sid: str) -> tuple[bool, str]:
    """Decide whether the Python and bash session-id resolvers agree — the
    split-brain guard, made hermetic (1.5.17).

    `has_root` True means a real agent ancestor is on the process tree, so both
    resolvers walk it and MUST converge on the same pid → require exact equality.
    False (a detached/background run with no agent ancestry) means each falls
    back to a process-LOCAL `pid-<getppid()>` that legitimately differs between
    the Python process and the bash subprocess; requiring exact equality there
    false-FAILed under a detached suite (Obs-A). In that case the meaningful
    invariant is that both produce the same fallback SHAPE (`pid-…`) — the
    production split-brain guarantee is env-authoritative (both honor
    PLAYBOOK_SESSION_ID, always set by the launchers) and is unaffected here.
    """
    if has_root:
        agree = py_sid == bash_sid and py_sid.startswith("pid-")
        return agree, (f"both → {py_sid}" if agree
                       else f"MISMATCH py={py_sid!r} bash={bash_sid!r}")
    agree = py_sid.startswith("pid-") and bash_sid.startswith("pid-")
    return agree, (f"both → pid- fallback (py={py_sid}, bash={bash_sid}); no agent "
                   f"ancestry, so exact pid is process-local"
                   if agree else f"non-pid fallback py={py_sid!r} bash={bash_sid!r}")


def cmd_audit(cmd_args):
    """The `tasks audit` arm — body moved verbatim from cli.py (1.5.9 split)."""
    # P6: mechanical pre-panel sweeps — catch the stale/zombie/half-merged
    # stuff a grep can find before a judge spends a token on it. Records a
    # receipt into task.md and exits non-zero on real breakage so a review
    # can't proceed over a red audit.
    project_path = find_project_root()
    from tasks.audit import run_audit, format_audit_receipt
    # Optional task arg (else the active task) for the receipt destination.
    task_arg = next((a for a in cmd_args if a.isdigit()), None)
    agent_dir = resolve_agent_dir(project_path)
    task_file = None
    if task_arg:
        m = list((agent_dir / "tasks").glob(f"{task_arg.zfill(3)}-*/task.md"))
        task_file = m[0] if m else None
    else:
        sf = agent_dir / "sessions" / resolve_session_id() / "current_state"
        if sf.exists():
            active = sf.read_text(encoding="utf-8").strip()
            m = list((agent_dir / "tasks").glob(f"{active}-*/task.md"))
            task_file = m[0] if m else None

    print("Running pre-panel audit...", flush=True)
    audit = run_audit(project_path)
    for r in audit["results"]:
        n = len([ln for ln in r["output"].splitlines() if ln.strip()])
        tag = {"clean": "CLEAN", "findings": f"FINDINGS({n})", "error": "ERROR"}[r["status"]]
        print(f"  [{tag}] {r['name']} — {r['why']}", flush=True)
    if task_file:
        import subprocess as _sp
        from tasks.core import upsert_task_section
        try:
            _head = _sp.run(["git", "rev-parse", "HEAD"], cwd=project_path,
                            capture_output=True, text=True).stdout.strip()
        except (OSError, _sp.SubprocessError):
            _head = ""
        receipt = format_audit_receipt(audit, head_sha=_head)
        upsert_task_section(task_file, "Pre-Panel Audit", receipt)
        print(f"  Receipt recorded in {task_file.relative_to(project_path)}")
    print(f"\nAUDIT {'PASS' if audit['passed'] else 'FAIL'}", flush=True)
    if not audit["passed"]:
        print("  Fix the error-severity findings (or a broken sweep) before "
              "reviewing — a red audit means mechanically-detectable issues remain.",
              file=sys.stderr)
        sys.exit(1)


def cmd_doctor(cmd_args):
    """The `tasks doctor` arm — body moved verbatim from cli.py (1.5.9 split).

    `--verbose` enumerates every finding in every install copy it can see;
    without it, a foreign copy of a different version collapses to one line
    (see hooks_check.hooks_check_report).
    """
    verbose = "--verbose" in (cmd_args or []) or "-v" in (cmd_args or [])
    project_path = find_project_root()
    passed = 0
    failed = 0
    warned = 0

    def iter_hook_commands(node):
        if isinstance(node, dict):
            command = node.get("command")
            if isinstance(command, str):
                yield command
            for value in node.values():
                yield from iter_hook_commands(value)
        elif isinstance(node, list):
            for item in node:
                yield from iter_hook_commands(item)

    def check(name: str, ok: bool, detail: str = ""):
        nonlocal passed, failed
        status = "PASS" if ok else "FAIL"
        msg = f"  [{status}] {name}"
        if detail:
            msg += f" — {detail}"
        print(msg)
        if ok:
            passed += 1
        else:
            failed += 1

    def warn(name: str, detail: str = ""):
        # Non-fatal advisory: surfaced but never counts as a failed check.
        nonlocal warned
        msg = f"  [WARN] {name}"
        if detail:
            msg += f" — {detail}"
        print(msg)
        warned += 1

    print("tasks doctor\n")

    # 1. Project structure
    agent_tasks = resolve_agent_dir(project_path) / "tasks"
    check("project: tasks/ exists", agent_tasks.exists())
    claude_md = project_path / "CLAUDE.md"
    check("project: CLAUDE.md exists", claude_md.exists())
    mind_map = project_path / "MIND_MAP.md"
    check("project: MIND_MAP.md exists", mind_map.exists())

    # 1b. Optional per-install config (.agent/config.json). Advisory only:
    # a missing/malformed file or bad value falls back to defaults at runtime,
    # so these are warnings, not failures.
    import json as _json
    cfg_path = project_path / ".agent" / "config.json"
    if cfg_path.exists():
        try:
            _cfg = _json.loads(cfg_path.read_text(encoding="utf-8", errors="replace"))
        except (ValueError, OSError) as e:
            # "defaults used" is true for the review knobs but NOT for
            # merge_verify: an unparseable config makes the merge skill
            # BLOCK, so say so when the file was trying to declare one.
            _extra = ""
            try:
                if "merge_verify" in cfg_path.read_text(encoding="utf-8", errors="replace"):
                    _extra = ("; the merge skill will BLOCK on this rather "
                              "than skip its verify step")
            except OSError:
                pass
            warn("config: .agent/config.json parses",
                 f"invalid JSON ({e}); defaults used{_extra}")
            _cfg = None
        if isinstance(_cfg, dict):
            # Validate through the runtime's own parser and report the
            # runtime's own defaults, so doctor can never call a value
            # clean that the runtime ignores, nor name a fallback that
            # is no longer the fallback.
            from tasks.core import DEFAULT_JUDGE_BUDGET_USD as _DEF_BUDGET
            from tasks.core import DEFAULT_REVIEW_SOFT_TIMEOUT_SECS as _DEF_SOFT
            from tasks.core import DEFAULT_REVIEW_TIMEOUT_SECS as _DEF_HARD
            from tasks.core import _parse_timeout as _pt
            _jb = _cfg.get("judge_budget_usd")
            if _jb is not None:
                try:
                    _ok = float(_jb) >= 0
                except (TypeError, ValueError):
                    _ok = False
                if not _ok:
                    warn("config: judge_budget_usd", f"{_jb!r} not a non-negative number; default ${_DEF_BUDGET} used")
            _rt = _cfg.get("review_timeout_secs")
            if _rt is not None:
                # 0 / "unlimited" = no hard kill; a positive int = hang safety.
                try:
                    _pt(_rt)
                    _ok = True
                except (TypeError, ValueError):
                    _ok = False
                if not _ok:
                    warn("config: review_timeout_secs",
                         f'{_rt!r} not a positive integer or an unlimited form '
                         f'(0/"unlimited"); default {_DEF_HARD}s used')
            _st = _cfg.get("review_soft_timeout_secs")
            if _st is not None:
                try:
                    _pt(_st)
                    _ok = True
                except (TypeError, ValueError):
                    _ok = False
                if not _ok:
                    warn("config: review_soft_timeout_secs",
                         f'{_st!r} not a positive integer or an unlimited form '
                         f'(0/"unlimited"); default {_DEF_SOFT}s used')
            # merge_verify — the post-merge soundness command the merge skill
            # runs (skills/merge/merge-verify.py). Advisory here, but worth
            # surfacing early: at merge time an unusable declaration BLOCKS
            # the merge's verify step rather than being ignored, so a typo
            # found by doctor is a typo found cheaply.
            for _m in _merge_verify_issues(_cfg):
                warn("config: merge_verify", _m)
            for _m in _merge_verify_untracked(project_path, _cfg):
                warn("config: merge_verify", _m)
        elif _cfg is not None:
            warn("config: .agent/config.json shape", "top-level value is not a JSON object; ignored")

    # 1c. Judge pins (.agent/models.json + shipped panel) — advisory only.
    # Cheap checks: adapter presence + codex cache/effort validation; NO
    # live probes in doctor (that's `tasks models check`).
    try:
        from tasks.models_check import bad_pins, check_pins
        models_path = project_path / ".agent" / "models.json"
        if not models_path.exists():
            warn("models: .agent/models.json", "absent — shipped panel used; "
                 "create with `tasks models select`")
        _report = check_pins(project_path, probe=False)
        for _e in bad_pins(_report):
            warn(f"models: pin '{_e['spec']}'", f"{_e['verdict']} — {_e['detail']}; "
                 f"refresh with `tasks models select`")
    except Exception as e:  # doctor must never crash on an advisory check
        warn("models: pin check ran", f"skipped ({e})")

    # 1d. README drift (task 017) — maintainer-only advisory. Silently a
    # no-op outside a plugin source checkout / dogfood workspace.
    try:
        from tasks.readme_drift import readme_drift
        for _msg in readme_drift(project_path):
            warn("readme: audit drift", _msg)
    except Exception as e:  # doctor must never crash on an advisory check
        warn("readme: drift check ran", f"skipped ({e})")

    # 1e. Gate-logging health across ALL lanes (bug report #4). state-echo
    # writes `**[G<task>:…]**` per gate transition into each lane's chat_log;
    # if those stop while tasks keep completing, retro attribution silently
    # degrades. Scan every lane — NOT just resolve_agent_dir's current one —
    # because the reported case is one dev running doctor while a PEER's lane
    # is the broken one (task 018 panel T7). Advisory; never crashes doctor.
    try:
        from tasks.gate_logging import done_task_numbers, gate_logging_gap
        from tasks.core import _agent_lanes
        for lane_user, lane_rel in _agent_lanes(project_path):
            chat_log = project_path / lane_rel / "chat_log.md"
            if not chat_log.is_file():
                continue
            text = chat_log.read_text(encoding="utf-8", errors="replace")
            done = done_task_numbers(project_path / lane_rel / "tasks")
            gap = gate_logging_gap(text, done)
            if gap:
                label = lane_user or "(root)"
                warn(f"gate-logging: lane '{label}'", gap)
    except Exception as e:  # advisory — doctor must never crash here
        warn("gate-logging: lane scan ran", f"skipped ({e})")

    # 1f. Hook command quoting (task 019 / field bug AloVet 2026-07-20).
    # Every hooks.json `command` was quote-wrapped, which grok resolves as
    # a literal path -> command-not-found -> all six hooks fail-open. Scan
    # the copies the host actually loads (CLAUDE_PLUGIN_ROOT, the copy next
    # to this module, the workspace source tree, and grok's own ~/.grok
    # copies), not just the source tree — a clean checkout is not proof the
    # running install is clean. Missing copies are silently skipped.
    # Advisory; never crashes doctor.
    try:
        from tasks.hooks_check import hooks_check_report
        for _label, _detail in hooks_check_report(project_path, verbose=verbose):
            warn(_label, _detail)
    except Exception as e:  # advisory — doctor must never crash here
        warn("hooks: command-quoting check ran", f"skipped ({e})")

    # 1g. Grok always-trusted global enforcement file (task 020).
    # Absolute script pins go stale on upgrade/move → fail-open. Also flag
    # a missing file when AGENTS.md exists (Grok bootstrap present).
    try:
        from tasks.hooks_check import grok_enforcement_report, grok_enforcement_issues
        agents_md = project_path / "AGENTS.md"
        issues = grok_enforcement_issues()
        # Only warn "missing" when the project looks Grok-bootstrapped;
        # always warn on stale/broken paths if the file exists.
        if issues:
            missing_only = all(i.startswith("missing ") for i in issues)
            if not missing_only or agents_md.is_file():
                for _label, _detail in grok_enforcement_report():
                    warn(_label, _detail)
    except Exception as e:  # advisory — doctor must never crash here
        warn("hooks: grok enforcement check ran", f"skipped ({e})")

    # 1h. Environment recommendations (1.5.15) — the optional tools that make
    # playbook run smoothly/optimally: extra panel vendors, sandbox containment,
    # the verify command's own tooling, command logging. Suggest-only, never a
    # FAIL — a thinner setup still works, it just isn't the full experience.
    try:
        from tasks.environment import environment_report, suggestions
        for _i in suggestions(environment_report(project_path)):
            hint = f" — {_i['hint']}" if _i["hint"] else ""
            warn(f"env: {_i['name']}", f"{_i['why']}{hint}")
    except Exception as e:  # advisory — doctor must never crash here
        warn("env: recommendations check ran", f"skipped ({e})")

    # 2. Unicode
    stdout_enc = getattr(sys.stdout, "encoding", "unknown") or "unknown"
    check("unicode: stdout encoding", "utf" in stdout_enc.lower(), stdout_enc)

    # 3. Dead session dirs left by crashed sessions.
    #
    # Uses _session_is_dead, the same predicate _gc_dead_sessions deletes by,
    # so doctor cannot report a session the GC would keep (or vice versa).
    # It used to flag any pointer older than 24h with no liveness check and
    # no self-exclusion, which after task 027 is exactly the false-positive
    # class this task removed: a live session on a multi-day task is the
    # NORMAL case, not a fault to report.
    #
    # In practice this now reports only what the GC could NOT reclaim —
    # _gc_dead_sessions runs at the CLI entry point, so by the time doctor
    # looks, every deletable dead dir is already gone. A non-empty list
    # therefore means the sweep is being blocked (permissions, read-only
    # mount), which is worth surfacing precisely because the sweep itself
    # is deliberately silent about failures (fail-open).
    agent_dir = resolve_agent_dir(project_path)
    stale = []
    sessions_dir = agent_dir / "sessions"
    if sessions_dir.exists():
        cutoff = time.time() - 86400
        own_session = _own_session_id()
        for session_dir in sorted(sessions_dir.iterdir()):
            if session_dir.is_symlink() or not session_dir.is_dir():
                continue
            if _session_is_dead(session_dir, own_session, cutoff):
                stale.append(session_dir.name)
    check("session: no dead session dirs", len(stale) == 0,
          f"dead: {', '.join(stale)}" if stale else "clean")

    # 4. Hooks — check .claude/hooks/ (installed) or src/hooks/ (dev repo)
    hooks_dirs = [project_path / "scripts", project_path / ".claude" / "hooks", project_path / "src" / "hooks"]
    # On a plugin install the hook scripts live at ${CLAUDE_PLUGIN_ROOT}/scripts
    # (wired via the plugin's hooks.json), not in the project tree. Resolve
    # that dir too so doctor doesn't false-negative "missing" on every
    # plugin install even though the gates demonstrably fire.
    #
    # F16 (batch-4): resolve the RUNNING code's own scripts dir — the same
    # tree the version check reads (block 5, task 010) and the copy the
    # daily `tasks` wrapper resolved to. Without it, doctor hunted
    # ~/.claude/plugins by mtime and could inspect a DIFFERENT install than
    # the one executing: 4 FAIL (hooks "missing", truncation, resolver)
    # while every hook demonstrably enforced all session. The home glob
    # stays only as a last resort for layouts where the module has no
    # sibling scripts/ (dev src/ checkouts).
    _plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    _resolved_install = False
    if _plugin_root and (Path(_plugin_root) / "scripts").is_dir():
        hooks_dirs.append(Path(_plugin_root) / "scripts")
        _resolved_install = True
    _own_scripts = Path(__file__).resolve().parent.parent / "scripts"
    if _own_scripts.is_dir():
        hooks_dirs.append(_own_scripts)
        _resolved_install = True
    if not _resolved_install:
        _plugins_home = Path.home() / ".claude" / "plugins"
        if _plugins_home.exists():
            _found = sorted(_plugins_home.glob("**/playbook/scripts"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
            if _found:
                hooks_dirs.append(_found[0])
    for hook_name in ["state-echo-hook", "task-gate-hook"]:
        found = False
        for hooks_dir in hooks_dirs:
            hook_path = hooks_dir / hook_name
            if hook_path.exists():
                executable = os.access(hook_path, os.X_OK)
                check(f"hooks: {hook_name}", executable,
                      f"found at {hooks_dir.name}/" + ("" if executable else " but not executable"))
                found = True
                break
        if not found:
            check(f"hooks: {hook_name}", False, "missing")

    # 4b. Check ~/.claude/settings.json for stale hook entries pointing to nonexistent paths
    user_settings = Path.home() / ".claude" / "settings.json"
    stale_hooks = []
    if user_settings.exists():
        import json as _json
        try:
            settings = _json.loads(user_settings.read_text(encoding="utf-8"))
            for cmd in iter_hook_commands(settings.get("hooks", {})):
                for token in cmd.split():
                    p = Path(token)
                    if p.suffix in (".sh", "") and len(p.parts) > 2 and not p.exists():
                        stale_hooks.append(str(p))
        except (ValueError, KeyError):
            pass
    check("hooks: no stale entries in ~/.claude/settings.json",
          len(stale_hooks) == 0,
          f"stale paths: {', '.join(stale_hooks[:3])}" if stale_hooks else "clean")

    # 5. Plugin version — read the RUNNING code's own manifest (same tree as
    # this module), not a global glob: with several cached plugin versions
    # the glob's [0] is readdir-order nondeterministic (task 010). Dev
    # layout (src/tasks/) has no sibling manifest -> sorted glob fallback.
    from tasks.core import VERSION as code_version
    installed_version = None
    own_manifest = Path(__file__).resolve().parent.parent / ".claude-plugin" / "plugin.json"
    if own_manifest.is_file():
        plugin_json_paths = [own_manifest]
    else:
        plugin_json_paths = sorted(Path.home().glob(".claude/plugins/**/playbook/.claude-plugin/plugin.json"))
    if plugin_json_paths:
        import json as _json2
        try:
            pdata = _json2.loads(plugin_json_paths[0].read_text(encoding="utf-8"))
            installed_version = pdata.get("version", "unknown")
        except (ValueError, OSError):
            installed_version = "unreadable"
    if installed_version:
        version_ok = installed_version == code_version
        check("plugin: version matches code", version_ok,
              f"installed={installed_version}, code={code_version}" + ("" if version_ok else " — run /upgrade"))
    else:
        check("plugin: installed", False, "no plugin found")

    # 6. Python version
    import platform
    py_ver = platform.python_version()
    major, minor = sys.version_info[:2]
    check("python: version >= 3.8", major >= 3 and minor >= 8, py_ver)

    # 7. write_text encoding (check installed plugin scripts)
    import re as _re
    # 1.5.9 split: scan the WHOLE tasks package, not [cli.py, core.py]. The
    # command arms live across modules now, and the old sys.modules[__name__]
    # resolution would have silently shrunk this check to diagnostics.py +
    # core.py. Output is identical at the split (every module scans clean —
    # judge-verified); tests/test_doctor_encoding_scan.py pins both the PASS
    # and that a planted unencoded call still fails the check.
    _pkg_dir = Path(__file__).resolve().parent
    unencoded = 0
    for src_file in sorted(_pkg_dir.glob("*.py")):
        if src_file.exists():
            content = src_file.read_text(encoding="utf-8")
            # Find all write_text/read_text calls (may span multiple lines)
            for m in _re.finditer(r'\.(write_text|read_text)\(', content):
                # Find the matching closing paren
                start = m.end()
                depth = 1
                pos = start
                while pos < len(content) and depth > 0:
                    if content[pos] == '(':
                        depth += 1
                    elif content[pos] == ')':
                        depth -= 1
                    pos += 1
                call_body = content[start:pos]
                if "encoding=" not in call_body:
                    unencoded += 1
    check("encoding: write_text/read_text have encoding=", unencoded == 0,
          f"{unencoded} unencoded calls" if unencoded else "all encoded")

    # 8. Gate echo truncation
    has_truncation = False
    for hd in hooks_dirs:
        echo_hook = hd / "state-echo-hook"
        if echo_hook.exists():
            hook_content = echo_hook.read_text(encoding="utf-8")
            has_truncation = "cut -c" in hook_content or "GATE_TEXT_STORE" in hook_content
            break
    check("hooks: gate text truncation", has_truncation,
          "prevents recursive duplication" if has_truncation else "gate text may grow unbounded")

    # 9. Session-id resolver consistency (split-brain regression guard).
    # Python and bash must produce identical session_ids without PLAYBOOK_SESSION_ID,
    # otherwise hooks and CLI look in different .agent/sessions/ directories.
    gate_lib = None
    for hd in hooks_dirs + [project_path / "scripts"]:
        cand = hd / "gate-echo-lib.sh"
        if cand.exists():
            gate_lib = cand
            break
    if gate_lib and (sys.platform == "win32" or os.name == "nt"):
        # Windows: the process-walk is skipped by both resolvers (disjoint
        # MSYS vs native PID namespaces, see find_agent_root_pid). Two
        # assertions: (1) the env-set path honors PLAYBOOK_SESSION_ID;
        # (2) the env-UNSET path returns the shared constant
        # 'pid-win-fallback' and gate-echo-lib.sh carries the same literal
        # — that constant is the only thing preventing split-brain when the
        # env var doesn't propagate. We deliberately don't shell out to
        # bash: MSYS path resolution is unreliable when bash.exe is spawned
        # from native Python, which would produce a spurious MISMATCH; the
        # static literal check covers the bash side instead.
        probe = "pid-doctor-probe"
        saved = os.environ.get("PLAYBOOK_SESSION_ID")
        os.environ["PLAYBOOK_SESSION_ID"] = probe
        try:
            py_sid = resolve_session_id()
        finally:
            if saved is None:
                os.environ.pop("PLAYBOOK_SESSION_ID", None)
            else:
                os.environ["PLAYBOOK_SESSION_ID"] = saved
        check("session-id: Python ≡ bash resolver", py_sid == probe,
              "env-authoritative on Windows (ancestor scan skipped)"
              if py_sid == probe else f"Python ignored PLAYBOOK_SESSION_ID: {py_sid!r}")
        saved = os.environ.pop("PLAYBOOK_SESSION_ID", None)
        try:
            py_fallback = resolve_session_id()
        finally:
            if saved is not None:
                os.environ["PLAYBOOK_SESSION_ID"] = saved
        bash_has_const = "pid-win-fallback" in gate_lib.read_text(
            encoding="utf-8", errors="replace")
        fallback_ok = py_fallback == "pid-win-fallback" and bash_has_const
        check("session-id: env-unset fallback converges", fallback_ok,
              "both resolvers use constant 'pid-win-fallback'"
              if fallback_ok else
              f"Python fallback {py_fallback!r}; bash literal present: {bash_has_const}"
              " — split-brain risk when PLAYBOOK_SESSION_ID is unset")
    elif gate_lib:
        import subprocess as _sub
        from tasks.core import find_agent_root_pid
        saved = os.environ.pop("PLAYBOOK_SESSION_ID", None)
        try:
            find_agent_root_pid.cache_clear()
            has_root = find_agent_root_pid() is not None
            py_sid = resolve_session_id()
            env = {k: v for k, v in os.environ.items() if k != "PLAYBOOK_SESSION_ID"}
            r = _sub.run(["bash", "-c", f"source '{gate_lib.as_posix()}' && resolve_session_id"],
                         capture_output=True, text=True, env=env, timeout=5)
            bash_sid = r.stdout.strip()
        finally:
            if saved is not None:
                os.environ["PLAYBOOK_SESSION_ID"] = saved
        agree, detail = _resolver_parity_verdict(has_root, py_sid, bash_sid)
        check("session-id: Python ≡ bash resolver", agree, detail)
    else:
        check("session-id: Python ≡ bash resolver", False, "gate-echo-lib.sh not found")

    # Summary
    total = passed + failed
    summary = f"\n{passed}/{total} checks passed"
    if failed:
        summary += f" ({failed} failed)"
    if warned:
        summary += f" ({warned} warning{'s' if warned != 1 else ''})"
    print(summary)

    # I5: exit non-zero when any check FAILED, so `tasks doctor && deploy` (or
    # any CI gate) sees the failure. cmd_doctor was dispatched with no exit
    # wrapping and printed FAIL lines while still exiting 0 — a false green.
    # Warnings alone do NOT fail (they are advisory by design).
    sys.exit(1 if failed else 0)
