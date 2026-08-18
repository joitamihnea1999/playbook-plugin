"""I18 (verification-report-1.5.9): JSONL/stream readers must not assume every
decoded value is a dict.

`sensor.read_new_events` (and the vendor transcript readers) called `.get()` on
`json.loads` output with no dict check, so a line that is `null`/`[]`/a string,
or a record with `"message": null`, raised AttributeError. In the monitor's
incremental reader the crash happens BEFORE the offset is returned, so every
subsequent poll re-reads the poison line and the monitor is PERMANENTLY WEDGED.
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
MONITOR_LIB = _HERE.parent / "plugins/playbook/scripts/monitor-lib"
_spec = importlib.util.spec_from_file_location("sensor", MONITOR_LIB / "sensor.py")
sensor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sensor)


def _line(obj):
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


class MonitorPoisonLine(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.jsonl = Path(self._tmp.name) / "session.jsonl"

    def _write(self, *objs_or_raw):
        with open(self.jsonl, "wb") as f:
            for o in objs_or_raw:
                f.write(o if isinstance(o, bytes) else _line(o))

    def test_poison_lines_do_not_wedge_the_sensor(self):
        self._write(
            {"type": "user", "message": {"content": "hello"}},
            b"null\n",                              # decodes to None
            b"[]\n",                                # decodes to a list
            b"\"just a string\"\n",                 # decodes to a str
            {"type": "user", "message": None, "content": "still here"},  # null message
            {"type": "assistant",
             "message": {"content": [{"type": "text", "text": "done"}],
                         "stop_reason": "end_turn"}},
        )
        total = self.jsonl.stat().st_size
        # Must not raise, and must consume the WHOLE file so the poison line is
        # never re-read (the wedge).
        events, new_offset = sensor.read_new_events(self.jsonl, 0)
        self.assertEqual(new_offset, total,
                         "offset did not advance past the poison line (wedge risk)")
        # The valid records still parsed.
        kinds = [e["type"] for e in events]
        self.assertIn("user", kinds)
        self.assertIn("turn_end", kinds)

    def test_valid_stream_still_parses(self):
        # Negative control: a clean stream returns its events unchanged.
        self._write(
            {"type": "user", "message": {"content": "hi"}},
            {"type": "assistant",
             "message": {"content": [{"type": "text", "text": "ok"}],
                         "stop_reason": "end_turn"}},
        )
        events, off = sensor.read_new_events(self.jsonl, 0)
        self.assertEqual(off, self.jsonl.stat().st_size)
        self.assertTrue(any(e["type"] == "user" for e in events))


if __name__ == "__main__":
    unittest.main()
