"""I12 (verification-report-1.5.9): `_capture_recent_chat` must parse the modern
chat-log header that carries a `(provider/pid)` suffix.

The regex required the header to end at `` `\\w+` `` immediately before the
newline, but the chat-log producer has appended a ` (provider/pid)` suffix since
1.4.3. Two sibling parsers were fixed; this third was not — so the "Recent Chat
auto-captured at activation" feature (and the Chat-Log-Research design gate that
depends on it) was silently DEAD on any lane whose hook writes the suffix.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "plugins/playbook"))
from tasks.lifecycle import _capture_recent_chat  # noqa: E402

MODERN = """# Project Chat Log

---

**[M001]** [2026-08-15 10:00:00 UTC] `HOST` (claude/pid-123)

first modern message

---

**[M002]** [2026-08-15 10:00:05 UTC] `HOST` (codex/pid-123)

second modern message
"""

LEGACY = """# Project Chat Log

---

**[M001]** [2026-08-15 10:00:00 UTC] `HOST`

legacy message
"""


class CaptureRecentChat(unittest.TestCase):
    def _capture(self, content):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        (d / ".agent").mkdir()
        (d / ".agent" / "chat_log.md").write_text(content, encoding="utf-8")
        return _capture_recent_chat(d)

    def test_modern_header_with_provider_suffix_is_captured(self):
        got = self._capture(MODERN)
        blob = "\n".join(got)
        self.assertIn("first modern message", blob,
                      "modern (provider/pid) entries were not captured (I12)")
        self.assertIn("second modern message", blob)

    def test_legacy_header_still_captured(self):
        # Negative control: the pre-suffix format must still parse.
        got = self._capture(LEGACY)
        self.assertIn("legacy message", "\n".join(got))


if __name__ == "__main__":
    unittest.main()
