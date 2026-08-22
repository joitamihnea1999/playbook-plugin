"""
Provider harness — cross-provider adapter layer.

Concrete adapters (Claude, Codex, Antigravity/agy, Grok, pi) implement the
ProviderAdapter ABC: identity, bootstrap, hook install, interactive/headless
launch, capability detection, and chat-log capture. Enforcement itself runs in
the provider hooks — Claude's bash hooks are the authoritative path, and the
opt-in Codex apply_patch gate lives in codex_hooks.py; both gates share the
path classifiers in policy.py.

Layout:
    capabilities.py  — ProviderCapabilities, SessionFacts
    policy.py        — shared gate path classifiers (parity-pinned against bash)
    adapter.py       — ProviderAdapter ABC
    adapters/        — concrete adapters
    codex_hooks.py   — Codex hooks feature + apply_patch gate
"""

from .capabilities import ProviderCapabilities, SessionFacts
from .adapter import ProviderAdapter

__all__ = [
    "ProviderCapabilities",
    "SessionFacts",
    "ProviderAdapter",
]
