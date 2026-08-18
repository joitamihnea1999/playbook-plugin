"""
Pure policy functions — map (capabilities, facts, event) -> Decision.

These functions have no side effects. They do not write files, call hooks,
or probe the environment. All inputs are loaded by the adapter before calling.

Integration: spec-only in T111. These are not called by any hook today.
T112 will wire hook scripts to call these via a thin Python entry point.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Literal, Optional

from .capabilities import ProviderCapabilities, SessionFacts
from .events import MessageEvent, ToolEvent, StopEvent


@dataclass(frozen=True)
class Decision:
    """
    Output of a policy evaluation — what should happen next.

    "allow"  — proceed normally, no intervention.
    "warn"   — proceed but surface a message to the agent (stdout).
    "block"  — prevent the action; hook exits with code 2 (Claude PreToolUse).
               For providers without hard-block capability, block degrades to warn.
    "skip"   — capability absent for this provider; do nothing silently.
               Distinct from allow: skip means "this hook point doesn't exist here",
               not "this action is approved".
    """
    action: Literal["allow", "warn", "block", "skip"]
    message: Optional[str] = None

    @classmethod
    def allow(cls) -> "Decision":
        return cls(action="allow")

    @classmethod
    def warn(cls, message: str) -> "Decision":
        return cls(action="warn", message=message)

    @classmethod
    def block(cls, message: str) -> "Decision":
        return cls(action="block", message=message)

    @classmethod
    def skip(cls) -> "Decision":
        """Capability absent — do nothing. Not an approval, just a no-op."""
        return cls(action="skip")


def evaluate_message(
    caps: ProviderCapabilities,
    facts: SessionFacts,
    event: MessageEvent,
) -> Decision:
    """
    Evaluate a user message before or as it is processed.

    Called from: UserPromptSubmit hook (Claude), or after read_new_messages()
    delivers a message (Codex/agy file-based path).

    Side effects: none. The adapter is responsible for writing to chat_log.md.

    Fallback when caps.has_user_prompt_hook is False:
        Return Decision.skip() — message capture happens via the file-based
        read_new_messages() path instead. No enforcement at this point.

    Current policy: always allow (message capture is not a gate point).
    Future: could warn if message references forbidden patterns, or if no
    active task exists and the message looks like a code change request.
    """
    if not caps.has_user_prompt_hook:
        return Decision.skip()

    # Message capture is a recording concern, not a gate. Always allow.
    return Decision.allow()


def evaluate_tool_call(
    caps: ProviderCapabilities,
    facts: SessionFacts,
    event: ToolEvent,
) -> Decision:
    """
    Evaluate a tool call before it executes.

    Called from: PreToolUse hook (Claude only today).

    NOT called for Codex (no scriptable pre-tool hook — prefix_rule approval
    only) or agy (hook model unverified). If called for a provider where
    caps.has_pre_tool_hook is False, return Decision.skip().

    Current policy stub — full logic lives in task-gate-hook bash until T112:
        - If no active task and tool touches a code file: block.
        - Otherwise: allow.

    The bash hook (task-gate-hook) is the authoritative implementation today.
    This stub defines the intended Python interface for T112 wiring.
    """
    if not caps.has_pre_tool_hook:
        return Decision.skip()

    code_tools = {"Edit", "Write", "MultiEdit"}
    if event.tool_name in code_tools and facts.active_task_number is None:
        # Allow edits to task-management directories without an active task
        if _is_management_path(event.file_path):
            return Decision.allow()
        if _is_code_file_path(event.file_path):
            return Decision.block(
                "No active task. Run `.claude/bin/tasks work <N>` before editing code."
            )

    return Decision.allow()


def _is_management_path(file_path: str) -> bool:
    """Return True if path is genuinely under .agent/ or .claude/ (always allowed
    without a task).

    NEW-1: resolve `..` FIRST (lexical normpath) — a path that merely contains
    the token but traverses back out to a code file (`.agent/../src/main.py`)
    must not be treated as management, or it bypasses the code-edit gate.
    """
    import os
    norm = os.path.normpath(file_path.replace("\\", "/"))
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


def evaluate_stop(
    caps: ProviderCapabilities,
    facts: SessionFacts,
    event: StopEvent,
) -> Decision:
    """
    Evaluate session end — the universal enforcement point.

    Called from: Stop hook (Claude), AfterAgent (agy, unverified),
    or session-end equivalent for other providers.

    This is the ONLY enforcement point available across all providers that
    have any hook support. If a provider lacks this, Playbook is advisory only.

    Fallback when caps.has_stop_hook is False:
        Return Decision.skip() and log a warning — enforcement was not possible.
        Do not silently omit: the user should know enforcement was bypassed.

    Current policy stub — checks for open gates in active task:
        - If active task has unchecked gates: warn (or block if hard enforcement).
        - If no active task: allow (session may have been purely exploratory).
    """
    if not caps.has_stop_hook:
        return Decision.skip()

    if facts.active_task_number is None:
        return Decision.allow()

    # Full gate-checking logic will be wired in T112.
    # Stub: always allow at stop to avoid disrupting existing Claude behavior.
    # The bash stop-hook is authoritative until T112.
    return Decision.allow()
