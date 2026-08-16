"""1.5.11 audit decision: the SHIPPED judge defaults are all-Claude.

A normal user's `tasks judge` / `tasks panel-review` (no --backend/--models) uses
the shipped models.json default_judge + panel. Those must resolve to the claude
backend, so a Claude-first user never exercises the codex/grok/pi adapter drift
(I16/I17). A project can still opt into other vendors via .agent/models.json,
--backend, or --models — the aliases remain defined.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN = REPO_ROOT / "plugins" / "playbook"
MODELS_JSON = PLUGIN / "provider" / "models.json"
sys.path.insert(0, str(PLUGIN))
from provider.sandbox import resolve_judge_spec  # noqa: E402


class DefaultJudgesAllClaude(unittest.TestCase):
    def setUp(self):
        self.cfg = json.loads(MODELS_JSON.read_text(encoding="utf-8"))

    def test_default_judge_is_claude(self):
        backend, _ = resolve_judge_spec(self.cfg["default_judge"])
        self.assertEqual(backend, "claude",
                         f"default_judge {self.cfg['default_judge']!r} is not claude")

    def test_every_panel_seat_is_claude(self):
        panel = self.cfg["panel"]
        self.assertTrue(panel, "shipped panel is empty")
        for spec in panel:
            backend, _ = resolve_judge_spec(spec)
            self.assertEqual(backend, "claude",
                             f"panel seat {spec!r} resolves to {backend}, not claude")

    def test_other_vendor_aliases_still_available_for_opt_in(self):
        # The vendors are still reachable — just not seated by default.
        self.assertEqual(resolve_judge_spec("gpt")[0], "codex")
        self.assertEqual(resolve_judge_spec("gemini")[0], "agy")


if __name__ == "__main__":
    unittest.main()
