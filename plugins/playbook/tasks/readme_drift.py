"""README drift detection (task 017) — maintainer-only advisory check.

Warns when the playbook-plugin SOURCE repo's user-facing surface has moved
past the last README audit baseline (docs/readme-audit-baseline.json, written
by the maintainer skill at .claude/skills/readme-audit/SKILL.md).

Fires ONLY in maintainer contexts, detected by two signals:
  1. module-path walk — this module itself lives inside a plugin source
     checkout (the working-copy CLI was run directly), or
  2. child-checkout scan — the current project root has a plugin source
     checkout as a direct child (the dogfooding-workspace layout, where the
     daily CLI resolves to the installed cache copy so signal 1 never fires).

A "plugin source checkout" must have plugins/playbook/.claude-plugin/plugin.json,
README.md, and .git at its root, and must NOT live under ~/.claude/plugins:
the marketplaces/ tree there is a full git clone of the repo (content-
indistinguishable from a dev checkout), and installed cache copies have no
.git and no repo-root files, so ordinary host installs can never match.

The trigger is baseline-coverage, not commit recency: a README-only typo
commit cannot reset it — only re-running the audit (which moves the baseline
commit) does. All failures are soft; callers additionally wrap in try/except
(doctor's "must never crash on an advisory check" contract).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

SKILL_REL = ".claude/skills/readme-audit/SKILL.md"
BASELINE_REL = "docs/readme-audit-baseline.json"

# Paths whose commits count as "user-facing surface moved" when the baseline
# predates them. The baseline file's covered_paths wins when present; this is
# the documented default the skill writes.
DEFAULT_COVERED_PATHS = [
    "plugins/playbook/commands",
    "plugins/playbook/skills",
    "plugins/playbook/provider",
    "plugins/playbook/tasks",
    "plugins/playbook/hooks",
    "plugins/playbook/scripts",
]


def _is_source_repo(root: Path) -> bool:
    # SKILL_REL doubles as the maintainer marker: a vendored/upstream clone
    # without the audit skill must never be nagged toward a path it lacks
    # (impl-panel V4).
    return (
        (root / "plugins" / "playbook" / ".claude-plugin" / "plugin.json").is_file()
        and (root / "README.md").is_file()
        and (root / ".git").exists()
        and (root / SKILL_REL).is_file()
    )


def _under(path: Path, ancestor: Path) -> bool:
    try:
        path.resolve().relative_to(ancestor.resolve())
        return True
    except (ValueError, OSError):
        return False


def find_source_repo(
    project_path: Path | None = None,
    module_file: Path | None = None,
    plugins_home: Path | None = None,
) -> Path | None:
    """Locate the plugin source checkout, or None outside maintainer contexts.

    Parameterized (module_file, plugins_home, project_path) so tests can fake
    every layout: dev checkout, installed cache, marketplaces clone, host
    project, dogfood workspace.
    """
    module = Path(module_file) if module_file else Path(__file__)
    home = Path(plugins_home) if plugins_home else Path.home() / ".claude" / "plugins"

    # Signal 1: this module lives inside a source checkout.
    try:
        for ancestor in module.resolve().parents:
            if _is_source_repo(ancestor):
                if not _under(ancestor, home):
                    return ancestor
                break  # under ~/.claude/plugins → marketplaces clone, excluded
    except OSError:
        pass

    # Signal 2: the project root carries a source checkout as a direct child.
    if project_path is not None:
        try:
            for manifest in sorted(
                Path(project_path).glob("*/plugins/playbook/.claude-plugin/plugin.json")
            ):
                root = manifest.parents[3]
                if _is_source_repo(root) and not _under(root, home):
                    return root
        except OSError:
            pass

    return None


def readme_drift(
    project_path: Path | None = None,
    module_file: Path | None = None,
    plugins_home: Path | None = None,
) -> list[str]:
    """Advisory messages about README/docs drift; [] outside maintainer contexts."""
    repo = find_source_repo(project_path, module_file, plugins_home)
    if repo is None:
        return []
    # .as_posix(): this path is interpolated into a user-facing "read and follow
    # <path>" instruction; native str(Path) would render it with backslashes on
    # Windows. Forward slashes are a stable, portable instruction. No-op on POSIX.
    skill = (repo / SKILL_REL).as_posix()
    baseline_path = repo / BASELINE_REL

    if not baseline_path.is_file():
        return [
            f"no README audit baseline yet — run the audit once: read and follow {skill}"
        ]

    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8", errors="replace"))
        if not isinstance(baseline, dict):
            raise ValueError("baseline is not a JSON object")
        sha = str(baseline.get("audited_commit") or "")
        version = str(baseline.get("version") or "?")
        raw_paths = baseline.get("covered_paths") or []
        # Must be a list of strings: a bare string would char-iterate into
        # bogus pathspecs, other types TypeError past this net (impl-panel V6).
        if not isinstance(raw_paths, list):
            raise ValueError("covered_paths is not a list")
        paths = [p for p in raw_paths if isinstance(p, str) and p]
    except (ValueError, TypeError, OSError):
        return [
            f"README audit baseline unreadable ({BASELINE_REL}) — re-run the audit: {skill}"
        ]
    if not sha:
        return [
            f"README audit baseline incomplete (no audited_commit) — re-run the audit: {skill}"
        ]
    if not paths:
        paths = list(DEFAULT_COVERED_PATHS)

    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "log", "--oneline", f"{sha}..HEAD", "--", *paths],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []  # no git / timeout — advisory check stays silent
    if proc.returncode != 0:
        return [
            f"README audit baseline commit {sha[:12]} unresolvable (rebase? shallow clone?)"
            f" — consider re-running the audit: {skill}"
        ]

    count = sum(1 for line in proc.stdout.splitlines() if line.strip())
    if count == 0:
        return []
    return [
        f"README may be stale: {count} commit(s) touched user-facing paths since the"
        f" last audit (v{version}, {sha[:12]}) — read and follow {skill}"
    ]
