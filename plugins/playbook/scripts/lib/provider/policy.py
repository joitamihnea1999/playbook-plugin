"""
Shared path-classification helpers for the code-edit gate.

This module is the Python half of the cross-provider "no code without a task"
boundary. The default Claude path enforces via the bash hooks
(scripts/task-gate-hook, sourcing scripts/gate-echo-lib.sh); the opt-in Codex
apply_patch gate enforces via provider.codex_hooks, which imports the helpers
here. The two implementations MUST agree:

  * tests/test_gate_classifier_parity.py pins bash `is_code_file_path` against
    `_is_code_file_path` over a shared vector table;
  * tests/test_gate_path_traversal.py pins the `.agent`/`.claude` management
    exemption (`_is_management_path`) on both sides.

Edit both halves together. These helpers are pure — no side effects, no
filesystem probes (the one deliberate asymmetry is bash's shebang check for an
extensionless EXISTING file, a bash-only superset).
"""

from __future__ import annotations


def _is_management_path(file_path: str) -> bool:
    """Return True if path is genuinely under .agent/ or .claude/ (always allowed
    without a task).

    NEW-1: resolve `..` FIRST (lexical normpath) — a path that merely contains
    the token but traverses back out to a code file (`.agent/../src/main.py`)
    must not be treated as management, or it bypasses the code-edit gate.
    """
    import os
    # normpath collapses `..`/`.` — but on Windows it also RE-INTRODUCES `\`
    # (undoing the pre-replace), so `split("/")` would then see one element and
    # miss a genuine `.agent`/`.claude` component, blocking real management edits.
    # Re-normalize separators to `/` after normpath. No-op on POSIX (os.sep=="/").
    norm = os.path.normpath(file_path.replace("\\", "/")).replace(os.sep, "/")
    parts = norm.split("/")
    return ".agent" in parts or ".claude" in parts


_CODE_EXTENSIONS = {
    # Programming languages (union of both prior lists ∪ common extras — closes
    # the codex hole where .php/.vue/.swift/… went ungated).
    ".py", ".ts", ".js", ".mjs", ".cjs", ".tsx", ".jsx", ".sh", ".bash", ".go",
    ".rs", ".rb", ".java", ".c", ".cpp", ".h", ".hpp", ".swift", ".kt", ".kts",
    ".dart", ".cs", ".php", ".r", ".m", ".mm", ".scala", ".zig", ".lua", ".ex",
    ".exs", ".ml", ".mli", ".tf", ".vue", ".svelte", ".ipynb",
    # Config / markup / schema treated as code (strict, owner decision 1.5.18).
    ".css", ".scss", ".less", ".html", ".sql", ".yaml", ".yml", ".toml",
    ".proto", ".graphql", ".gradle",
}
# Docs / data / binaries — never code, even inside a code dir. (.json/.yaml
# split is deliberate: .yaml/.toml drive behavior and are gated; .json stays
# data, matching the pre-1.5.17 exemption.)
_DOC_DATA_EXTENSIONS = {
    ".md", ".txt", ".json", ".png", ".svg", ".jpg", ".jpeg", ".gif", ".ico",
    ".webp", ".pdf", ".lock", ".csv",
}
_CODE_DIRS = {"scripts", "bin", "src", "hooks", "lib", "cmd"}


def _is_code_file_path(file_path: str) -> bool:
    """Return True if path looks like a code file (should require active task).

    F2 (1.5.18): this MUST agree with the bash `is_code_file_path` in
    scripts/gate-echo-lib.sh — the default Claude path enforces via that hook, the
    opt-in codex apply_patch gate enforces via this, and "no code without a task"
    has to mean the same thing under every provider.
    tests/test_gate_classifier_parity.py pins the agreement over a shared vector
    table; edit both together. The one deliberate asymmetry: the bash hook adds a
    shebang check for an extensionless EXISTING file (it can read the working
    tree; this pre-decision sees a patch, not always a file) — a bash-only
    superset, never a hole.

    Algorithm: extension decides first (code -> gate; doc/data -> exempt); an
    undecided extension gates iff a path component is a known code dir.
    """
    import os
    if not file_path:
        return False
    # rstrip trailing CR/LF so a malformed path ending in a newline classifies
    # like the bash side (whose `$(…)` ext extraction drops it) — 1.5.20 parity.
    norm = file_path.replace("\\", "/").rstrip("\r\n")
    _, ext = os.path.splitext(norm)
    ext = ext.lower()
    if ext in _CODE_EXTENSIONS:
        return True
    if ext in _DOC_DATA_EXTENSIONS:
        return False
    parts = set(norm.split("/"))
    return bool(parts & _CODE_DIRS)
