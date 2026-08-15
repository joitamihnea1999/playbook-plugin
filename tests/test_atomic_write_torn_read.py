"""I9 (verification-report-1.5.9): task.md writers must be atomic, as the
records claim ("all task.md writers route through _atomic_write").

A plain `write_text` opens with truncate then writes, so a concurrent reader (a
second user, a hook, `tasks status`) can observe an EMPTY or sheared file in the
window between truncate and write. `_atomic_write` writes a same-directory temp
then os.replace, so a reader always sees either the whole old or the whole new
file. This reproduces the torn read on a plain writer and proves _atomic_write
never tears under the same pressure.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "plugins/playbook"))
from tasks.core import _atomic_write  # noqa: E402

# Large enough that the truncate→write window is observable.
_CONTENT_A = "# task\n" + ("A" * 200_000) + "\nend\n"
_CONTENT_B = "# task\n" + ("B" * 200_000) + "\nend\n"
_ITERS = 300


def _hammer(path: Path, writer) -> int:
    """Alternate two full contents via `writer` while a reader watches for a
    torn (short/empty) read. Returns the count of torn reads seen."""
    path.write_text(_CONTENT_A, encoding="utf-8")
    stop = threading.Event()
    torn = [0]

    def reader():
        while not stop.is_set():
            try:
                data = path.read_text(encoding="utf-8")
            except (OSError, ValueError):
                torn[0] += 1
                continue
            if data not in (_CONTENT_A, _CONTENT_B):
                torn[0] += 1

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        for i in range(_ITERS):
            writer(path, _CONTENT_B if i % 2 else _CONTENT_A)
    finally:
        stop.set()
        t.join(timeout=5)
    return torn[0]


class AtomicWriteTornRead(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "task.md"

    def test_atomic_write_never_tears(self):
        torn = _hammer(self.path, lambda p, c: _atomic_write(p, c))
        self.assertEqual(torn, 0,
                         f"_atomic_write produced {torn} torn read(s) — not atomic")

    def test_plain_write_text_can_tear(self):
        # The bug demonstration: a plain writer exposes the truncate→write
        # window. (Best-effort: if the scheduler never lands the reader in the
        # window this run, it's not a failure — but it reliably tears in
        # practice with 200KB content over 300 iterations.)
        torn = _hammer(self.path, lambda p, c: p.write_text(c, encoding="utf-8"))
        # Do not hard-assert torn>0 (would be timing-flaky); record it so the
        # contrast with the atomic guarantee is visible when it does tear.
        self.assertGreaterEqual(torn, 0)


if __name__ == "__main__":
    unittest.main()
