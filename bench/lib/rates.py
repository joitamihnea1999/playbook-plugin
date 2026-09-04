"""API-equivalent dollar rates — SECONDARY METADATA ONLY (plan §12).

Non-authoritative, dated, and deliberately sparse: a rate is listed only where
the plan's 2026-09-04 research recorded one. Everything else is `None` and the
report prints "n/a" — the bench never estimates a number it does not have.
Observed quota decrement is not machine-readable for any provider; operators
may note usage-page readings by hand in `manifest.json::manual_quota_notes`.
"""
from __future__ import annotations

AS_OF = "2026-09-04"

POOLS = {"claude": "anthropic", "codex": "openai", "grok": "xai", "agy": "google", "pi": "other"}

# (backend, variant prefix) → USD per 1M tokens {in, out}. Longest prefix wins.
RATES = {
    ("agy", "gemini-3.8-flash"): {"in_per_m": 0.75, "out_per_m": 3.75,
                                  "note": "published API rate through 2026-12-31 (plan §26)"},
}


def pool_of(backend: str) -> str:
    return POOLS.get(backend, "other")


def rate_for(backend: str, variant) -> "dict | None":
    v = (variant or "").split(":")[0]          # strip the effort suffix
    best = None
    for (b, prefix), r in RATES.items():
        if b == backend and v.startswith(prefix):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, r)
    return best[1] if best else None


def estimate_usd(backend: str, variant, usage) -> "float | None":
    """USD for one invocation from KNOWN token usage and a rate on file; None
    whenever either is missing (never estimated)."""
    if not isinstance(usage, dict) or usage.get("status") != "known":
        return None
    r = rate_for(backend, variant)
    if r is None:
        return None
    try:
        return usage["in"] / 1e6 * r["in_per_m"] + usage["out"] / 1e6 * r["out_per_m"]
    except (KeyError, TypeError):
        return None
