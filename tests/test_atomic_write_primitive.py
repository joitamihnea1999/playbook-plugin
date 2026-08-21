"""The one atomic-write primitive (tasks.atomic.atomic_write): the properties
every consequence-relevant writer inherits by routing through it.

Covers, per the PB-CONFIG-ATOMIC-DURABILITY contract:
  - torn-read safety under a concurrent reader (the same hammer the pre-existing
    tasks.core._atomic_write test uses, now against the shared primitive);
  - concurrent writers never yield a half-merged file (whole-version loss only);
  - permission preservation across a rewrite, and a sane umask-masked mode for a
    brand-new file (NOT mkstemp's private 0600);
  - temp cleanup when the write itself fails (interruption simulation), so an
    interrupt leaves neither a torn target nor a stray .tmp;
  - text newline control (default translation vs newline="" byte preservation)
    and raw bytes support.

Windows note: os.replace cannot swap a file another handle holds open without
FILE_SHARE_DELETE (which CPython does not set), so the live concurrent-reader
hammers are POSIX-only; the CI windows lane still exercises os.replace via every
migrated writer and the non-concurrent cases here.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "plugins/playbook"))
from tasks.atomic import atomic_write  # noqa: E402

_CONTENT_A = "# task\n" + ("A" * 200_000) + "\nend\n"
_CONTENT_B = "# task\n" + ("B" * 200_000) + "\nend\n"
_ITERS = 300


class AtomicWritePrimitive(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "file.md"

    # ── basic content / encoding ────────────────────────────────────────────
    def test_writes_text(self):
        atomic_write(self.path, "héllo\nworld\n")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "héllo\nworld\n")

    def test_overwrites_existing(self):
        self.path.write_text("old", encoding="utf-8")
        atomic_write(self.path, "new")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "new")

    def test_writes_bytes_verbatim(self):
        atomic_write(self.path, b"\x00\x01\x02rawbytes")
        self.assertEqual(self.path.read_bytes(), b"\x00\x01\x02rawbytes")

    def test_newline_empty_preserves_crlf(self):
        # newline="" disables translation: a CRLF payload survives byte-for-byte.
        atomic_write(self.path, "a\r\nb\r\n", newline="")
        self.assertEqual(self.path.read_bytes(), b"a\r\nb\r\n")

    def test_leaves_no_temp_files_on_success(self):
        atomic_write(self.path, "x")
        strays = [p.name for p in self.dir.iterdir() if p.name != "file.md"]
        self.assertEqual(strays, [], f"unexpected leftovers: {strays}")

    # ── permissions ─────────────────────────────────────────────────────────
    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_preserves_existing_mode(self):
        self.path.write_text("v1", encoding="utf-8")
        os.chmod(self.path, 0o640)
        atomic_write(self.path, "v2")
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o640,
                         "rewrite must preserve the target's permission bits")

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_new_file_is_not_private_0600(self):
        # A brand-new file must get the normal umask-masked mode, not mkstemp's
        # 0600 — else a shared config would be unreadable by a second user.
        atomic_write(self.path, "fresh")
        umask = os.umask(0)
        os.umask(umask)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o666 & ~umask)

    # ── interruption / cleanup ──────────────────────────────────────────────
    def test_temp_cleaned_up_when_write_raises(self):
        # A write that fails (here: a non-str/bytes payload → TypeError from
        # fh.write) must not touch the target and must leave no stray temp.
        self.path.write_text("intact", encoding="utf-8")
        with self.assertRaises(TypeError):
            atomic_write(self.path, 12345)  # int -> fh.write(int) raises TypeError
        self.assertEqual(self.path.read_text(encoding="utf-8"), "intact",
                         "failed write must not touch the target")
        strays = [p.name for p in self.dir.iterdir() if p.name != "file.md"]
        self.assertEqual(strays, [], f"temp not cleaned up: {strays}")

    def test_kill_between_write_and_replace_cleans_temp(self):
        # THE interruption scenario: a KeyboardInterrupt (BaseException, not
        # OSError) lands after the bytes are written but before os.replace. The
        # target must still hold the old bytes and no .tmp must survive — proving
        # the cleanup handler catches BaseException, not merely Exception.
        self.path.write_text("intact", encoding="utf-8")
        real_replace = os.replace

        def kill(*a, **k):
            raise KeyboardInterrupt("^C between write and replace")

        os.replace = kill
        try:
            with self.assertRaises(KeyboardInterrupt):
                atomic_write(self.path, "new-content-never-committed")
        finally:
            os.replace = real_replace
        self.assertEqual(self.path.read_text(encoding="utf-8"), "intact",
                         "interrupted write must leave the old file intact")
        strays = [p.name for p in self.dir.iterdir() if p.name != "file.md"]
        self.assertEqual(strays, [], f"temp not cleaned up after interrupt: {strays}")

    # ── concurrency (POSIX) ─────────────────────────────────────────────────
    @unittest.skipIf(sys.platform == "win32",
                     "Windows os.replace cannot swap a file held open by a reader")
    def test_never_tears_under_concurrent_reader(self):
        self.path.write_text(_CONTENT_A, encoding="utf-8")
        stop = threading.Event()
        torn = [0]

        def reader():
            while not stop.is_set():
                try:
                    data = self.path.read_text(encoding="utf-8")
                except (OSError, ValueError):
                    torn[0] += 1
                    continue
                if data not in (_CONTENT_A, _CONTENT_B):
                    torn[0] += 1

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        try:
            for i in range(_ITERS):
                atomic_write(self.path, _CONTENT_B if i % 2 else _CONTENT_A,
                             fsync=False)
        finally:
            stop.set()
            t.join(timeout=5)
        self.assertEqual(torn[0], 0, f"{torn[0]} torn read(s) — not atomic")

    @unittest.skipIf(sys.platform == "win32",
                     "Windows os.replace cannot swap a file held open by a reader")
    def test_concurrent_writers_never_half_merge(self):
        # Two writers racing on one path: the reader must only ever see one of
        # the two WHOLE payloads, never a line from each interleaved.
        self.path.write_text(_CONTENT_A, encoding="utf-8")
        stop = threading.Event()
        bad = [0]

        def writer(content):
            for _ in range(_ITERS):
                if stop.is_set():
                    return
                atomic_write(self.path, content, fsync=False)

        def reader():
            while not stop.is_set():
                try:
                    data = self.path.read_text(encoding="utf-8")
                except (OSError, ValueError):
                    bad[0] += 1
                    continue
                if data not in (_CONTENT_A, _CONTENT_B):
                    bad[0] += 1

        threads = [
            threading.Thread(target=writer, args=(_CONTENT_A,)),
            threading.Thread(target=writer, args=(_CONTENT_B,)),
            threading.Thread(target=reader, daemon=True),
        ]
        for t in threads:
            t.start()
        threads[0].join(timeout=30)
        threads[1].join(timeout=30)
        stop.set()
        threads[2].join(timeout=5)
        self.assertEqual(bad[0], 0, f"{bad[0]} half-merged read(s) under concurrent writers")


if __name__ == "__main__":
    unittest.main()
