#!/usr/bin/env python3
"""Drift guard: every `.agent/config.json` key the code HONORS must be
documented — as a real definition — in docs/configuration.md.

The config surface is read across several modules; the reference doc is written
by hand. Nothing kept them in step, so honored keys silently went undocumented.
An impl panel on the first cut of this test found two more (the two below marked
"dynamic") plus a false-green class, so the design is:

  * `HONORED_KEYS` — the AUTHORITATIVE surface, hand-maintained with a cite per
    key. It is authoritative because two keys (`review_context_chars[_stdin]`)
    are read with a *variable* key (`.get(key)` + an `f"config.json {key}"`
    label in core.py), which no literal scan can see. A registry is the only
    honest source of truth for those.
  * `_scan_literal_keys()` — a mechanical scan for the LITERAL-key idioms. Its
    job is not to be the source of truth but to CATCH a newly-added literal key
    that someone forgot to register: the scan must stay a subset of the
    registry, so a new `load_config(...).get("newkey")` fails this test until
    it is registered (and thereby forced through the documentation check).
  * documentation check — a key counts as documented only if it appears as a
    STRUCTURED token: `"key"` inside a ```json fence, or `` `key` `` in a bullet
    or heading. A bare word in prose does NOT count — otherwise the common-word
    key `audit` reads as "documented" off incidental "tasks audit" text while
    its actual schema is absent (the panel's F2).

Run: python3 -m unittest tests.test_config_doc_drift
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PKG = _HERE.parent / "plugins" / "playbook"
_DOC = _HERE.parent / "docs" / "configuration.md"

# The authoritative honored-key surface. Adding a honored key to the code means
# adding it here AND documenting it (a structured token in configuration.md) —
# both are enforced below. Cites are the read sites.
HONORED_KEYS = frozenset({
    "audit",                      # audit.py:106  cfg.get("audit") (nested schema)
    "code_roots",                 # core.py  _code_roots (nested-repo fingerprint)
    "command_guard",              # command_guard.py:248
    "dangerous_commands",         # command_guard.py:253
    "fingerprint_exclude",        # core.py (merge fingerprint)
    "judge_budget_usd",           # core.py:520
    "judge_verify",               # review.py:825
    "merge_verify",               # core.py / merge-verify.py
    "panel_quorum",               # core.py:430
    "panel_required_for",         # core.py:1140
    "review_context_chars",       # core.py:376  .get(key) — DYNAMIC (variable key)
    "review_context_chars_stdin", # core.py:376  .get(key) — DYNAMIC (variable key)
    "review_soft_timeout_secs",   # core.py:647
    "review_timeout_secs",        # core.py:543
    "standing_gates",             # core.py
    "verify",                     # core.py (verify contract)
    "verify_contract_ack",        # audit.py
    "verify_timeout_secs",        # core.py:342
})

# Keys a literal scan can see but that are NOT top-level config.json keys (read
# via variable idioms we deliberately don't chase, or value tokens). Keeping the
# scan⊆registry check honest without chasing false positives.
_SCAN_IGNORE: "frozenset[str]" = frozenset()

_LABEL = re.compile(r'"config\.json ([a-z_]+)"')
_DIRECT = re.compile(r'load_config\([^)]*\)\.get\(\s*["\']([a-z_]+)["\']')
_ASSIGN = re.compile(r'(\b[A-Za-z_]\w*)\s*=\s*(?:load_config|_load_cfg)\(')


def _scan_literal_keys() -> "set[str]":
    """Keys honored through LITERAL-key idioms only (source-label strings,
    load_config(...).get("literal"), and <cfgvar>.get("literal")/["literal"]).
    Deliberately NOT the `"k" in cfgvar` membership form — it matched
    `"all" in <panel_required_for value>`, a value not a key."""
    keys: "set[str]" = set()
    for path in _PKG.rglob("*.py"):
        txt = path.read_text(encoding="utf-8", errors="replace")
        keys.update(m.group(1) for m in _LABEL.finditer(txt))
        keys.update(m.group(1) for m in _DIRECT.finditer(txt))
        for var in {m.group(1) for m in _ASSIGN.finditer(txt)}:
            v = re.escape(var)
            keys.update(m.group(1) for m in re.finditer(
                rf'\b{v}\.get\(\s*["\']([a-z_]+)["\']', txt))
            keys.update(m.group(1) for m in re.finditer(
                rf'\b{v}\[\s*["\']([a-z_]+)["\']', txt))
    return keys - _SCAN_IGNORE


def _is_documented(key: str, doc: str) -> bool:
    """A key is documented only if it appears as a STRUCTURED token, not merely
    as a word in prose:
      * `"key"` on a line inside a fenced ```json block,
      * `` `key` `` in a definition bullet (`- `key``) or a heading, or
      * a defining line that STARTS with `` `key` `` (e.g. "`verify` lives in ...").
    A bare word in running prose ("tasks audit") does NOT count.
    """
    kb = f"`{key}`"
    in_json_fence = False
    for line in doc.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            # A fence marker: open a json fence on ```json, close any fence on ```.
            in_json_fence = stripped.startswith("```json") and not in_json_fence
            continue
        if in_json_fence and re.search(rf'"{re.escape(key)}"', line):
            return True
        if stripped.startswith(("-", "*", "#")) and kb in line:
            return True
        if stripped.startswith(kb):        # a defining line led by `key`
            return True
    return False


class ConfigDocDrift(unittest.TestCase):
    def test_literal_scan_is_subset_of_registry(self):
        # A newly-added literal-key read that nobody registered surfaces here,
        # forcing it into HONORED_KEYS (and thus the documentation check).
        scanned = _scan_literal_keys()
        unregistered = sorted(scanned - HONORED_KEYS)
        self.assertEqual(
            [], unregistered,
            f"code honors literal config key(s) {unregistered} not in "
            "HONORED_KEYS — add them to the registry and document them")

    def test_registry_covers_the_literal_scan(self):
        # Guard the guard: if the scanner regresses and matches fewer keys, the
        # subset check above passes vacuously. Every literal key the scan still
        # sees must be registered (this is the same set today, but pins that the
        # known literal keys never silently drop out of the scan).
        scanned = _scan_literal_keys()
        # every key that IS a literal read today
        known_literal = HONORED_KEYS - {"review_context_chars",
                                        "review_context_chars_stdin"}
        missing = sorted(known_literal - scanned)
        self.assertEqual(
            [], missing,
            f"scanner no longer sees known literal key(s) {missing} — an idiom "
            "broke or a file moved; fix _scan_literal_keys()")

    def test_every_honored_key_is_documented(self):
        doc = _DOC.read_text(encoding="utf-8")
        undocumented = sorted(k for k in HONORED_KEYS if not _is_documented(k, doc))
        self.assertEqual(
            [], undocumented,
            "docs/configuration.md does not DEFINE config.json key(s) the code "
            f"honors (a structured `key`/\"key\" token, not incidental prose): "
            f"{undocumented}")


if __name__ == "__main__":
    unittest.main()
