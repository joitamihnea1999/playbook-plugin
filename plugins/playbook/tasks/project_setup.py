"""Project setup and session start: the `init` and `bootstrap` arms.

Boundary: `init` prepares a PROJECT (lane-aware .agent/tasks, MIND_MAP.md,
CLAUDE.md, duplicate-hook warnings, optional provider bootstrap files —
mechanical merge work lives in scripts/claude-md-merge.py, invoked by
scripts/init, not here); `bootstrap` briefs a SESSION (identity preamble,
indexed mind map via mindmap._bootstrap_mind_map, pending tasks, judge-pin and
README-drift nudges, CLI reference). Imports stdlib + tasks.core +
tasks.shared + tasks.mindmap + tasks.template/readme_drift; never a command
module (design-1.5.9.md §4).
"""
from __future__ import annotations

import sys
from pathlib import Path
from tasks.atomic import atomic_write
from tasks.core import list_tasks, require_lane_marker, resolve_agent_dir
from tasks.mindmap import _bootstrap_mind_map
from tasks.shared import find_project_root


def cmd_init(cmd_args):
    """The `tasks init` arm — body moved verbatim from cli.py (1.5.9 split)."""
    # Parse provider-specific init flags (additive on top of normal init)
    provider = None
    install_provider_hooks = False
    remaining_init_args = []
    i = 0
    while i < len(cmd_args):
        if cmd_args[i] == "--provider" and i + 1 < len(cmd_args):
            provider = cmd_args[i + 1]
            i += 2
        elif cmd_args[i] == "--hooks":
            install_provider_hooks = True
            i += 1
        else:
            remaining_init_args.append(cmd_args[i])
            i += 1
    cmd_args = remaining_init_args

    # Target directory: argument or cwd
    target = Path(cmd_args[0]).resolve() if cmd_args else Path.cwd()
    if not target.exists():
        print(f"Error: directory not found: {target}", file=sys.stderr)
        sys.exit(1)

    title = target.name.replace("-", " ").replace("_", " ").title()
    print(f"Initializing project: {target.name}")

    # Refuse on the fresh-clone shape rather than mint a phantom root lane.
    require_lane_marker(target, "tasks init")
    # Create .agent/tasks/ (or .agent/<user>/tasks/ in multi-user mode)
    tasks_dir = resolve_agent_dir(target) / "tasks"
    existed = tasks_dir.exists()
    tasks_dir.mkdir(parents=True, exist_ok=True)
    print(f"  {tasks_dir.relative_to(target)}  {'exists' if existed else 'created'}")

    # Create MIND_MAP.md
    mind_map = target / "MIND_MAP.md"
    if not mind_map.exists():
        atomic_write(mind_map, f"""# {title}

## Architecture

(describe your project architecture here)
""")
        print("  MIND_MAP.md    created")
    else:
        print("  MIND_MAP.md    exists")

    # Create CLAUDE.md
    claude_md = target / "CLAUDE.md"
    if not claude_md.exists():
        from tasks.template import claude_md as claude_md_template
        atomic_write(claude_md, claude_md_template(title))
        print("  CLAUDE.md      created")
    else:
        print("  CLAUDE.md      exists")

    # Check for duplicate hook registrations
    settings_file = target / ".claude" / "settings.json"
    if settings_file.exists():
        import json
        try:
            settings = json.loads(settings_file.read_text(encoding="utf-8"))
            if "hooks" in settings:
                hook_events = list(settings["hooks"].keys())
                print(f"  ⚠ .claude/settings.json has local hook registrations: {', '.join(hook_events)}")
                print(f"    These may duplicate plugin hooks (hooks/hooks.json) — causing double writes.")
                print(f"    Fix: remove the 'hooks' key from .claude/settings.json")
        except (json.JSONDecodeError, KeyError):
            pass

    # Check for stale .claude/hooks/ directory
    local_hooks = target / ".claude" / "hooks"
    if local_hooks.is_dir():
        hook_files = [f.name for f in local_hooks.iterdir() if f.is_file()]
        if hook_files:
            print(f"  ⚠ .claude/hooks/ contains {len(hook_files)} hook scripts: {', '.join(hook_files)}")
            print(f"    These are stale copies — canonical hooks live in scripts/ (resolved via plugin).")
            print(f"    Fix: remove .claude/hooks/ directory")

    # --provider: install provider-specific bootstrap file (additive)
    if provider:
        _PROVIDER_MAP = {"codex": "CodexAdapter", "antigravity": "AntigravityAdapter", "pi": "PiAdapter", "grok": "GrokAdapter"}
        if provider not in _PROVIDER_MAP:
            print(f"Error: unknown provider '{provider}'. Choose: codex, antigravity, grok, pi", file=sys.stderr)
            sys.exit(1)
        import importlib
        adapter_cls_name = _PROVIDER_MAP[provider]
        mod = importlib.import_module(f"provider.adapters.{provider}")
        adapter_cls = getattr(mod, adapter_cls_name)
        bootstrap_file = {"codex": "AGENTS.md", "antigravity": "GEMINI.md", "pi": "AGENTS.md", "grok": "AGENTS.md"}[provider]
        bs_path = target / bootstrap_file
        already_existed = bs_path.exists()
        adapter = adapter_cls("init", target)
        adapter.install_bootstrap(target)
        print(f"  {bootstrap_file:<15}{'exists' if already_existed else 'created'}")
        # Grok: always install global enforcement hooks (task 020). On spaced
        # project paths, project/plugin hooks never schedule — the always-
        # trusted ~/.grok/hooks/playbook-enforcement.json is the only reliable
        # channel. --hooks remains required for other providers.
        if install_provider_hooks or provider == "grok":
            adapter.install_hooks(target)
            if provider == "grok" and not install_provider_hooks:
                print("  grok hooks   auto-installed (required on Grok; pass --hooks to be explicit)")
    elif install_provider_hooks:
        print("Error: --hooks requires --provider codex, antigravity, grok, or pi", file=sys.stderr)
        sys.exit(1)


def cmd_bootstrap(cmd_args):
    """The `tasks bootstrap` arm — body moved verbatim from cli.py (1.5.9 split)."""
    project_path = find_project_root()

    # Identity preamble
    from tasks.template import identity_preamble, mind_map_header
    print(identity_preamble())
    print()

    # Mind Map — index (routing nodes in full + a titled TOC of the rest) once
    # the map is over the bootstrap budget, else the full map. Orientation reads
    # an index and greps what the task touches; a judge (review.py) still gets
    # the fuller `_load_mind_map` trim, because auditing needs whole nodes.
    mm_content = _bootstrap_mind_map(project_path)
    if mm_content:
        print("=== MIND MAP (MIND_MAP.md) ===")
        print(mind_map_header())
        print()
        print(mm_content.rstrip())
        print()

    # Pending tasks
    print("=== PENDING TASKS ===")
    list_tasks(project_path, pending_only=True)

    # Judge-pin nudge (task 012): covers projects that predate the models
    # maintenance loop. Presence check only — no probes at session start.
    if not (project_path / ".agent" / "models.json").exists():
        print()
        print("NOTE: no .agent/models.json — judge panel uses the plugin's shipped")
        print("defaults, which drift as providers retire models. Relay to the user:")
        print("pin per-machine judges via `tasks models check` + `tasks models select`.")

    # README drift nudge (task 017): maintainer-only — silently a no-op
    # outside a plugin source checkout / dogfood workspace. Advisory, so
    # bootstrap must never crash on it.
    try:
        from tasks.readme_drift import readme_drift
        _drift = readme_drift(project_path)
        if _drift:
            print()
            for _msg in _drift:
                print(f"NOTE: {_msg}")
    except Exception:
        pass

    # CLI reference — shown last so mind map + tasks aren't buried
    from tasks.template import cli_reference
    print()
    print("=== CLI REFERENCE ===")
    print(cli_reference())
