"""POSIX per-element argv byte guard (upstream issue #10).

Context is budgeted in CHARACTERS (`MAX_CONTEXT_CHARS`), but three adapters —
grok, antigravity, pi — deliver that context inside a single `argv` element. On
Linux one `argv` element is capped at `MAX_ARG_STRLEN = 32 * PAGE_SIZE` BYTES
(131,072 on a 4 KB-page host; 524,288 where pages are 16 KB). A character budget
cannot bound a byte-limited channel: past ~1.29 bytes/char, a 100,000-char context
overflows the transport and `execve` fails with a cryptic `E2BIG` post-mortem.

The adapters already carry a guard — but it is Windows-only (`os.name == "nt"`),
counts CHARACTERS, and sums the whole command line. All three are correct for
Windows (which caps a whole-command-line UTF-16 count) and wrong for POSIX (which
caps EACH element, in bytes). This module adds the POSIX half: measure each
element in bytes, take the MAX (not the sum), and refuse loudly BEFORE dispatch so
a knowable condition stops being reported as an environmental spawn failure.

Pure stdlib; mirrored into scripts/lib/provider/ like the rest of the package.
"""
from __future__ import annotations

import os


def max_arg_bytes() -> int:
    """The per-element argv byte cap for this platform: 32 * PAGE_SIZE. Derived
    from `SC_PAGESIZE` rather than hardcoded 131,072 so it stays correct where
    pages differ (arm64 16 KB pages → 524,288). Falls back to a 4 KB page."""
    page = os.sysconf("SC_PAGESIZE") if hasattr(os, "sysconf") else 4096
    return 32 * page


def argv_byte_error(agent_args, backend: str) -> "str | None":
    """Return an error string if ANY single argv element exceeds the POSIX
    per-element byte cap, else None.

    No-op on Windows: its whole-command-line CHARACTER cap is a different limit,
    still guarded separately by each adapter. Uses `max` over elements, not `sum`
    — the POSIX limit is per element, so a fixture of many small args totalling
    far over the cap is fine while one oversized element is fatal."""
    if os.name == "nt":
        return None
    limit = max_arg_bytes()
    worst = max((len(a.encode("utf-8")) for a in agent_args), default=0)
    if worst < limit:
        return None
    return (
        f"(error: {backend} judge context is {worst:,} bytes in a single argv "
        f"element; this platform caps one element at {limit:,} bytes "
        f"(MAX_ARG_STRLEN = 32 * PAGE_SIZE) and {backend} reads its prompt from "
        f"argv — shrink the context (lower MAX_CONTEXT_CHARS) or use a "
        f"stdin-capable backend such as claude or codex)"
    )
