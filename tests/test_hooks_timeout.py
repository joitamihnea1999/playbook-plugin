"""I14 (verification-report-1.5.9): hooks.json timeouts are in SECONDS.

Every command hook carried `"timeout": 5000` — 5000 seconds = 83 minutes, a
milliseconds-era leftover for the intended 5 s. Claude Code hook timeouts are in
seconds (default 600 for command hooks), so a wedged hook (e.g. an unbounded
write_log_append copying a multi-GB file) blocked the tool call for up to 83
minutes instead of seconds. This pins every declared hook timeout to a sane
seconds value so the 5000 mistake can't come back.
"""
import json
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_JSON = REPO_ROOT / "plugins" / "playbook" / "hooks" / "hooks.json"

# A hook must respond in seconds, not minutes. The Claude Code command-hook
# default is 600s; anything near that is already generous for these hooks.
_SANE_MAX_SECONDS = 60


class HooksTimeout(unittest.TestCase):
    def test_all_declared_timeouts_are_sane_seconds(self):
        data = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
        offenders = []
        for event, blocks in data.get("hooks", {}).items():
            for block in blocks:
                for h in block.get("hooks", []):
                    if "timeout" in h:
                        t = h["timeout"]
                        if not isinstance(t, int) or t <= 0 or t > _SANE_MAX_SECONDS:
                            offenders.append((event, h.get("command", "?"), t))
        self.assertEqual(offenders, [],
                         f"hook timeout(s) out of sane seconds range: {offenders}")


if __name__ == "__main__":
    unittest.main()
