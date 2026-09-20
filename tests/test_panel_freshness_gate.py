#!/usr/bin/env python3
"""F18 — irreversible closes resolve panel staleness explicitly (blind-judge
reviewed design: design-1.5.6.md + judge-f18-design.md, all findings built).

Three layers:

  * `tree_state_fingerprint` coverage (judge C1: the old fingerprint was
    PROVABLY blind to edits in untracked files — where batches 4 and 5 put
    their post-panel fixes) + config-declared excludes (judge C2: standing-
    gate outputs like journal/NNN.md must be excludable or the gate blocks
    100% of well-behaved closes on the flagship project);
  * the pure gate decision (`freshness_gate_decision`);
  * the real CLI close path: block matrix, narrow override, receipt clauses
    (judge F4: a missing stamp is RECORDED, not silently skipped; judge F5:
    a shared --reason under --force attributes to force).

Run: python3 -m unittest tests.test_panel_freshness_gate
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN = _HERE.parent / "plugins/playbook"
sys.path.insert(0, str(PLUGIN))

from tasks.core import (  # noqa: E402
    _repo_fingerprint_material,
    freshness_gate_decision,
    tree_state_fingerprint,
)

BLOCK_MARKER = "code state changed after the newest impl panel"
CLAUSE = "**Panel tree-state:**"


def _git(d, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   cwd=d, check=True, capture_output=True)


def _repo() -> Path:
    d = Path(tempfile.mkdtemp())
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    (d / "code.py").write_text("x = 1\n", encoding="utf-8")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "seed")
    return d


class FingerprintCoverage(unittest.TestCase):
    def test_untracked_file_edit_moves_the_fingerprint(self):
        # Judge C1, reproduced red against the 1.5.3 fingerprint: porcelain
        # names an untracked file but not its content; diff HEAD skips it.
        d = _repo()
        (d / "new_module.py").write_text("VERSION = 1\n", encoding="utf-8")
        fp1 = tree_state_fingerprint(d)
        (d / "new_module.py").write_text("VERSION = 2\n", encoding="utf-8")
        fp2 = tree_state_fingerprint(d)
        self.assertNotEqual(fp1, fp2,
                            "fingerprint is blind to untracked-file edits — "
                            "the batch-4/5 post-panel fix shape")

    def test_untracked_file_inside_new_directory_covered(self):
        # porcelain without -uall shows only `?? dir/` — the file inside is
        # invisible unless untracked enumeration is per-file.
        d = _repo()
        (d / "pkg").mkdir()
        (d / "pkg" / "mod.py").write_text("a = 1\n", encoding="utf-8")
        fp1 = tree_state_fingerprint(d)
        (d / "pkg" / "mod.py").write_text("a = 2\n", encoding="utf-8")
        fp2 = tree_state_fingerprint(d)
        self.assertNotEqual(fp1, fp2)

    def test_agent_dir_still_excluded(self):
        # Negative control: the original exclusion must survive the rewrite —
        # triage edits between panel and close are the designed flow.
        d = _repo()
        (d / ".agent" / "tasks" / "001-t").mkdir(parents=True)
        fp1 = tree_state_fingerprint(d)
        (d / ".agent" / "tasks" / "001-t" / "task.md").write_text("- [x] g\n",
                                                                  encoding="utf-8")
        fp2 = tree_state_fingerprint(d)
        self.assertEqual(fp1, fp2)

    def test_config_declared_exclude_is_honored(self):
        # Judge C2: owner-declared bookkeeping (StrataDB journal/) must be
        # excludable, or the irreversible gate fires on every close.
        d = _repo()
        (d / ".agent").mkdir()
        (d / ".agent" / "config.json").write_text(
            json.dumps({"fingerprint_exclude": ["journal/"]}), encoding="utf-8")
        (d / "journal").mkdir()
        fp1 = tree_state_fingerprint(d)
        (d / "journal" / "011.md").write_text("shipped\n", encoding="utf-8")
        fp2 = tree_state_fingerprint(d)
        self.assertEqual(fp1, fp2, "declared exclude did not suppress journal noise")
        # and a real code edit still moves it (exclusion is narrow)
        (d / "code.py").write_text("x = 3\n", encoding="utf-8")
        self.assertNotEqual(fp2, tree_state_fingerprint(d))

    def test_malformed_exclude_skipped_loudly_not_silently(self):
        d = _repo()
        (d / ".agent").mkdir()
        (d / ".agent" / "config.json").write_text(
            json.dumps({"fingerprint_exclude": ["ok/", 7, ""]}), encoding="utf-8")
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            fp = tree_state_fingerprint(d)
        self.assertTrue(fp, "malformed entries must not kill the fingerprint")
        self.assertIn("fingerprint_exclude", buf.getvalue(),
                      "malformed entries must be skipped LOUDLY")

    @unittest.skipIf(os.name == "nt", "mkfifo / symlink-follow are POSIX-only")
    def test_untracked_symlink_to_fifo_does_not_hang_fingerprint(self):
        # R1/P1 (1.5.39): the untracked-content hash used a bare read_bytes(),
        # which FOLLOWS an untracked symlink into a FIFO and blocks forever (git
        # lists the symlink `?? link`; a bare FIFO it skips, so the symlink is
        # the live vector). The freshness fingerprint runs on every panel and
        # every high-consequence close, so this is a PERMANENT denial of the
        # close path. _safe_hash_regular's O_NOFOLLOW must reject the symlink →
        # a deterministic "unreadable" leaf, never a hang.
        import threading
        d = _repo()
        fifo = d / "real.pipe"
        os.mkfifo(fifo)
        os.symlink(fifo, d / "linktofifo")     # untracked symlink → FIFO
        result: dict = {}

        def run():
            result["fp"] = tree_state_fingerprint(d)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout=15)
        self.assertFalse(t.is_alive(),
                         "fingerprint hung following an untracked symlink into a "
                         "FIFO — the untracked hash must not follow into a pipe")
        self.assertTrue(result.get("fp"),
                        "fingerprint returned empty after the symlink-to-FIFO")
        # deterministic: a second computation agrees (no time/randomness leak)
        self.assertEqual(result["fp"], tree_state_fingerprint(d))

    def test_oversize_untracked_content_is_cap_bounded(self):
        # R1/P1: a huge untracked file must not be read whole (OOM). With a tiny
        # cap, two same-size untracked files that share size but differ in
        # content past the cap must fingerprint IDENTICALLY (a size-bearing
        # `toolarge:<size>` marker) — proving the read never touches the bytes.
        # RED on the old full-read code, which sees the differing content.
        import tasks.core as core
        d = _repo()
        cap = 16
        sentinel = object()
        orig = getattr(core, "_FINGERPRINT_HASH_CAP", sentinel)
        core._FINGERPRINT_HASH_CAP = cap

        def restore():
            if orig is sentinel:
                if hasattr(core, "_FINGERPRINT_HASH_CAP"):
                    del core._FINGERPRINT_HASH_CAP
            else:
                core._FINGERPRINT_HASH_CAP = orig
        self.addCleanup(restore)

        head = b"A" * cap
        (d / "big.bin").write_bytes(head + b"11111111")     # oversize, size cap+8
        fp1 = tree_state_fingerprint(d)
        (d / "big.bin").write_bytes(head + b"22222222")     # same size, content differs
        fp2 = tree_state_fingerprint(d)
        self.assertEqual(fp1, fp2,
                         "fingerprint read past the cap — a huge untracked file "
                         "would be read whole (OOM risk)")
        # boundary control: a file AT/under the cap is content-hashed again, so
        # dropping to cap bytes moves the fingerprint (the cap is a ceiling, not
        # a blindfold for normal files).
        (d / "big.bin").write_bytes(head)                   # exactly cap bytes → hashed
        self.assertNotEqual(fp2, tree_state_fingerprint(d),
                            "a file at/under the cap must be content-hashed")

    @unittest.skipIf(os.name == "nt", "'\"' is illegal in Windows filenames")
    def test_special_char_untracked_filename_content_is_tracked(self):
        # R2/P2 (1.5.39): git C-quotes a special-char untracked filename in the
        # default porcelain (a"b.py → `?? "a\"b.py"`); the old `.strip('"')`
        # parse left the internal escape intact → a wrong path → `unreadable`,
        # so content edits to such a file were INVISIBLE to the fingerprint (a
        # stale panel could read FRESH). The `-z` enumeration resolves the real
        # path, so its content is now hashed and edits move the fp.
        d = _repo()
        weird = d / 'a"b.py'
        weird.write_text("VERSION = 1\n", encoding="utf-8")
        fp1 = tree_state_fingerprint(d)
        weird.write_text("VERSION = 2\n", encoding="utf-8")
        fp2 = tree_state_fingerprint(d)
        self.assertNotEqual(fp1, fp2,
                            "content edit to a C-quoted untracked filename is "
                            "invisible to the fingerprint (porcelain mis-parse)")

    @unittest.skipIf(os.name == "nt", "CR is illegal in Windows filenames")
    def test_carriage_return_untracked_filename_content_is_tracked(self):
        # R2 impl-panel (codex:sol Important): a raw CR byte in a `-z` path is
        # mangled to LF by `text=True` universal-newline translation → wrong
        # path → `unreadable` → edits invisible. The `-z` output MUST be read as
        # bytes and os.fsdecode'd. (git default-quotes the CR as `\r`, but `-z`
        # emits the raw byte — this is exactly the text-mode trap.)
        d = _repo()
        weird = d / "a\rb.py"
        weird.write_text("V = 1\n", encoding="utf-8")
        fp1 = tree_state_fingerprint(d)
        weird.write_text("V = 2\n", encoding="utf-8")
        fp2 = tree_state_fingerprint(d)
        self.assertNotEqual(fp1, fp2,
                            "content edit to a CR-in-name untracked file is "
                            "invisible (text-mode newline translation)")

    @unittest.skipIf(os.name == "nt", "non-UTF-8 bytes are illegal in Windows filenames")
    def test_non_utf8_untracked_filename_content_is_tracked(self):
        # R2 impl-panel (sonnet #2): a POSIX filename may carry arbitrary
        # non-UTF-8 bytes; the `-z` bytes → os.fsdecode(surrogateescape) path
        # must round-trip so its content is hashed, not `unreadable`.
        d = _repo()
        raw = os.path.join(os.fsencode(str(d)), b"a\xffb.py")
        try:
            with open(raw, "wb") as f:
                f.write(b"V = 1\n")
        except OSError:
            # macOS (APFS/HFS+) enforces valid UTF-8 filenames and rejects a
            # raw \xff byte (Errno 92, Illegal byte sequence); the surrogateescape
            # round-trip is only exercisable where the FS accepts such a name.
            self.skipTest("filesystem rejects non-UTF-8 filenames")
        fp1 = tree_state_fingerprint(d)
        with open(raw, "wb") as f:
            f.write(b"V = 2\n")
        fp2 = tree_state_fingerprint(d)
        self.assertNotEqual(fp1, fp2,
                            "content edit to a non-UTF-8-named untracked file is "
                            "invisible (surrogateescape round-trip broken)")

    def test_unicode_untracked_filename_content_is_tracked(self):
        # R2 impl-panel (codex:sol #2): a Unicode filename is octal-escaped by
        # git's default porcelain on ALL platforms (café.py → `"caf\303\251.py"`)
        # and is a LEGAL filename on Windows too — so this case exercises the
        # un-quoting path cross-platform (unlike the CR/`"`/non-UTF-8 cases that
        # can only exist on POSIX).
        d = _repo()
        weird = d / "café.py"
        weird.write_text("V = 1\n", encoding="utf-8")
        fp1 = tree_state_fingerprint(d)
        weird.write_text("V = 2\n", encoding="utf-8")
        fp2 = tree_state_fingerprint(d)
        self.assertNotEqual(fp1, fp2,
                            "content edit to a Unicode-named untracked file is "
                            "invisible (octal-escape un-quoting broken)")

    @unittest.skipIf(os.name == "nt", "leading/trailing spaces are trimmed by Windows")
    def test_whitespace_untracked_filename_content_is_tracked(self):
        # R2 impl-panel round-6 (codex:sol, empirically confirmed): git QUOTES a
        # leading/trailing-whitespace filename (`?? " lead.py"`) WITHOUT escapes,
        # so the legacy `.strip().strip('"')` recovers it intact — such a name is
        # byte-identical (NOT an exception). This pins that it is content-tracked.
        d = _repo()
        weird = d / " lead.py"
        weird.write_text("V = 1\n", encoding="utf-8")
        fp1 = tree_state_fingerprint(d)
        weird.write_text("V = 2\n", encoding="utf-8")
        fp2 = tree_state_fingerprint(d)
        self.assertNotEqual(fp1, fp2,
                            "content edit to a leading-whitespace untracked file "
                            "is invisible to the fingerprint")

    def test_z_failure_makes_the_material_unavailable(self):
        # R2 (1.5.39) made a `-z` failure fall back to the quote-blind legacy
        # parse because an EMPTY fingerprint was fail-OPEN at the gate. Task 060
        # (plan panel codex-high #3): the empty fingerprint now BLOCKS a
        # high-consequence close as UNREADABLE, so unavailable material is the
        # honest answer — a weaker parse would hide quoted names silently.
        import tasks.core as core
        from unittest import mock
        d = _repo()
        (d / "new.py").write_text("V = 1\n", encoding="utf-8")
        real_run = subprocess.run
        seen = {"z": False}

        def fake_run(cmd, *a, **k):
            if isinstance(cmd, (list, tuple)) and "-z" in cmd:
                seen["z"] = True
                return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"")
            return real_run(cmd, *a, **k)

        with mock.patch.object(core.subprocess, "run", side_effect=fake_run):
            fp = tree_state_fingerprint(d)
        self.assertTrue(seen["z"], "the -z status was never attempted")
        self.assertEqual(fp, "", "a failed -z read must yield NO fingerprint (UNREADABLE at "
                                 "close), never a quote-blind digest")

    def test_z_exception_makes_the_material_unavailable(self):
        # Same contract for a `-z` subprocess EXCEPTION (R2 round-4 vector):
        # caught, and the fingerprint is unavailable — never a crash, never a
        # weaker parse.
        import tasks.core as core
        from unittest import mock
        d = _repo()
        (d / "new.py").write_text("V = 1\n", encoding="utf-8")
        real_run = subprocess.run
        seen = {"z": False}

        def fake_run(cmd, *a, **k):
            if isinstance(cmd, (list, tuple)) and "-z" in cmd:
                seen["z"] = True
                raise OSError("boom: -z launch failed")
            return real_run(cmd, *a, **k)

        with mock.patch.object(core.subprocess, "run", side_effect=fake_run):
            fp = tree_state_fingerprint(d)
        self.assertTrue(seen["z"], "the -z status was never attempted")
        self.assertEqual(fp, "")

    def test_toplevel_failure_makes_the_material_unavailable(self):
        # Task 060 (plan panel codex-medium #5 / grok #4): a failed
        # `--show-toplevel` must NOT fall back to `repo_path` (that recreates the
        # subdir doubled-prefix false FRESH) — unavailable material instead.
        import tasks.core as core
        from unittest import mock
        d = _repo()
        real_run = subprocess.run

        def fake_run(cmd, *a, **k):
            if isinstance(cmd, (list, tuple)) and "--show-toplevel" in cmd:
                return subprocess.CompletedProcess(cmd, 128, stdout=b"", stderr=b"fatal")
            return real_run(cmd, *a, **k)

        with mock.patch.object(core.subprocess, "run", side_effect=fake_run):
            self.assertEqual(tree_state_fingerprint(d), "")

    def test_toplevel_crlf_output_is_handled(self):
        # Git-for-Windows can print `C:/…\r\n`: drop one `\n` then one `\r`,
        # never `.strip()` (a space-suffixed repo path must survive).
        import tasks.core as core
        from unittest import mock
        d = _repo()
        (d / "new.py").write_text("V = 1\n", encoding="utf-8")
        real_run = subprocess.run

        def fake_run(cmd, *a, **k):
            r = real_run(cmd, *a, **k)
            if isinstance(cmd, (list, tuple)) and "--show-toplevel" in cmd and r.returncode == 0:
                out = r.stdout.rstrip(b"\n") + b"\r\n"
                return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr=b"")
            return r

        with mock.patch.object(core.subprocess, "run", side_effect=fake_run):
            fp1 = tree_state_fingerprint(d)
            (d / "new.py").write_text("V = 2\n", encoding="utf-8")
            fp2 = tree_state_fingerprint(d)
        self.assertTrue(fp1 and fp2)
        self.assertNotEqual(fp1, fp2, "CRLF toplevel broke untracked resolution")


class NestedCodeRoots(unittest.TestCase):
    """C2: the outer fingerprint is blind to a code-only edit inside a nested
    (gitignored) checkout — the real-world blind spot (this workspace and
    HowFar-v2 keep their code in a gitignored nested repo). `code_roots` folds
    each nested repo's HEAD+porcelain+diff+untracked into the fingerprint."""

    def _outer_with_nested(self):
        """Outer repo with a gitignored nested git repo at `sub/`. Editing files
        inside `sub/` must not move the OUTER porcelain/diff/untracked at all —
        which is exactly why the outer fingerprint is blind to it."""
        d = _repo()
        (d / ".gitignore").write_text("sub/\n", encoding="utf-8")
        _git(d, "add", "-A")
        _git(d, "commit", "-qm", "ignore sub")
        sub = d / "sub"
        sub.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=sub, check=True)
        (sub / "app.py").write_text("y = 1\n", encoding="utf-8")
        _git(sub, "add", "-A")
        _git(sub, "commit", "-qm", "seed nested")
        (d / ".agent").mkdir(exist_ok=True)
        return d, sub

    def _cfg(self, d, obj):
        (d / ".agent").mkdir(exist_ok=True)
        (d / ".agent" / "config.json").write_text(json.dumps(obj), encoding="utf-8")

    def test_nested_edit_moves_fingerprint_when_registered(self):
        # THE core fix. code_roots names `sub`; a code-only edit inside `sub`
        # (tracked file, and separately an untracked one) must move the outer fp.
        d, sub = self._outer_with_nested()
        self._cfg(d, {"code_roots": ["sub"]})
        fp1 = tree_state_fingerprint(d)
        self.assertTrue(fp1)
        (sub / "app.py").write_text("y = 2\n", encoding="utf-8")  # dirty tracked
        self.assertNotEqual(fp1, tree_state_fingerprint(d),
                            "registered nested edit did not move the fingerprint")
        # a NEW commit in the nested repo (HEAD moves) also registers
        _git(sub, "add", "-A")
        _git(sub, "commit", "-qm", "bump")
        fp_committed = tree_state_fingerprint(d)
        self.assertNotEqual(fp1, fp_committed)
        # and an untracked file inside the nested repo, by content
        (sub / "extra.py").write_text("Z = 1\n", encoding="utf-8")
        fp_unt1 = tree_state_fingerprint(d)
        (sub / "extra.py").write_text("Z = 2\n", encoding="utf-8")
        self.assertNotEqual(fp_unt1, tree_state_fingerprint(d),
                            "untracked nested content is not hashed")

    @unittest.skipIf(os.name == "nt", "'\"' is illegal in Windows filenames")
    def test_nested_special_char_untracked_content_is_tracked(self):
        # R2 impl-panel round-2 (sonnet #2): the C-quote fix lives in the shared
        # `_repo_fingerprint_material` called with strict=True for code_roots, so
        # a special-char untracked file INSIDE a nested repo must also be content-
        # tracked (the ledger's "applies equally to outer and nested" claim).
        d, sub = self._outer_with_nested()
        self._cfg(d, {"code_roots": ["sub"]})
        weird = sub / 'a"b.py'
        weird.write_text("V = 1\n", encoding="utf-8")
        fp1 = tree_state_fingerprint(d)
        weird.write_text("V = 2\n", encoding="utf-8")
        fp2 = tree_state_fingerprint(d)
        self.assertNotEqual(fp1, fp2,
                            "special-char untracked content inside a nested "
                            "code_roots repo is invisible to the fingerprint")

    def test_unset_code_roots_is_byte_identical(self):
        # Unset (and empty-list) must be EXACTLY today's behavior: a nested edit
        # leaves the outer fingerprint unchanged, and unset == [] byte-for-byte.
        d, sub = self._outer_with_nested()
        # no config at all
        fp_no_cfg = tree_state_fingerprint(d)
        (sub / "app.py").write_text("y = 999\n", encoding="utf-8")
        self.assertEqual(fp_no_cfg, tree_state_fingerprint(d),
                         "with code_roots unset a nested edit must be invisible "
                         "(today's behavior) — the byte-identical guarantee")
        # empty list is identical to unset
        (sub / "app.py").write_text("y = 1\n", encoding="utf-8")  # restore
        self._cfg(d, {"code_roots": []})
        self.assertEqual(fp_no_cfg, tree_state_fingerprint(d),
                         "code_roots: [] must equal unset")

    def test_missing_or_non_repo_root_is_deterministic_not_crash(self):
        # A configured root that does not exist / is not a git repo must not
        # crash the fingerprint and must be deterministic across calls.
        d, sub = self._outer_with_nested()
        self._cfg(d, {"code_roots": ["does-not-exist"]})
        fp1 = tree_state_fingerprint(d)
        self.assertTrue(fp1)
        self.assertEqual(fp1, tree_state_fingerprint(d))
        # a plain (non-git) directory root
        (d / "plaindir").mkdir()
        self._cfg(d, {"code_roots": ["plaindir"]})
        fp2 = tree_state_fingerprint(d)
        self.assertTrue(fp2)
        self.assertEqual(fp2, tree_state_fingerprint(d))

    def test_invalid_entries_skipped_loudly(self):
        # Absolute paths, `..` traversal, and non-strings are rejected LOUDLY;
        # the fingerprint is still produced from the valid remainder.
        import contextlib
        import io
        d, sub = self._outer_with_nested()
        self._cfg(d, {"code_roots": ["sub", "/etc", "../escape", 7, ""]})
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            fp = tree_state_fingerprint(d)
        self.assertTrue(fp, "invalid entries must not kill the fingerprint")
        err = buf.getvalue()
        self.assertIn("code_roots", err, "invalid entries must be skipped LOUDLY")
        # the valid `sub` still took effect
        (sub / "app.py").write_text("y = 7\n", encoding="utf-8")
        self.assertNotEqual(fp, tree_state_fingerprint(d))

    def test_unset_matches_legacy_oracle(self):
        # Impl-panel F1: pin that the OUTER computation is byte-identical to the
        # pre-feature formula — not just unset==[]. An INDEPENDENT reimplementation
        # of the legacy head+porcelain+diff+untracked algorithm must equal the
        # live unset fingerprint, so any concatenation reorder in
        # _repo_fingerprint_material turns this red (it would otherwise pass the
        # whole suite while silently shifting every stamp → one-time mass-STALE).
        import hashlib
        d = _repo()
        (d / "code.py").write_text("x = 5\n", encoding="utf-8")   # dirty tracked
        (d / "unt...tracked.py").write_text("NEW = 1\n", encoding="utf-8")  # untracked
        _git(d, "add", "code.py")
        _git(d, "commit", "-qm", "second")
        (d / "code.py").write_text("x = 6\n", encoding="utf-8")   # dirty again

        exclude = [":(exclude).agent"]
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=d,
                              capture_output=True, text=True).stdout.strip()
        porcelain = subprocess.run(
            ["git", "status", "--porcelain", "-uall", "--", ".", *exclude],
            cwd=d, capture_output=True, text=True).stdout
        diff = subprocess.run(["git", "diff", "HEAD", "--", ".", *exclude],
                              cwd=d, capture_output=True, text=True).stdout
        ud = hashlib.sha256()
        for line in sorted(porcelain.splitlines()):
            if not line.startswith("?? "):
                continue
            rel = line[3:].strip().strip('"')
            fhash = hashlib.sha256((d / rel).read_bytes()).hexdigest()
            ud.update(f"{rel}\0{fhash}\n".encode("utf-8", "replace"))
        oracle = hashlib.sha256(
            (head + porcelain + diff + ud.hexdigest()).encode("utf-8", "replace")
        ).hexdigest()[:12]
        self.assertEqual(oracle, tree_state_fingerprint(d),
                         "outer fingerprint drifted from the legacy formula — "
                         "byte-identical guarantee broken")

    def test_strict_requires_own_repo_toplevel(self):
        # Impl-panel F2: a plain subdirectory of the outer repo is NOT its own
        # repo. strict mode must return None (→ <absent>), not silently
        # fingerprint the ANCESTOR repo git walks up to.
        d, sub = self._outer_with_nested()
        (d / "plaindir").mkdir()
        (d / "plaindir" / "f.py").write_text("q = 1\n", encoding="utf-8")
        exclude = [":(exclude).agent"]
        self.assertIsNone(
            _repo_fingerprint_material(d / "plaindir", exclude, strict=True),
            "a plain subdir must be <absent>, not the ancestor repo")
        # the real nested repo IS its own toplevel → real material
        self.assertIsNotNone(
            _repo_fingerprint_material(sub, exclude, strict=True))
        # and via the full fingerprint, a plain-subdir root does not adopt the
        # outer repo's identity under a nested label
        self._cfg(d, {"code_roots": ["plaindir"]})
        # deterministic + truthy (folds an <absent> marker, not ancestor bytes)
        self.assertTrue(tree_state_fingerprint(d))
        self.assertEqual(tree_state_fingerprint(d), tree_state_fingerprint(d))

    def test_fingerprint_exclude_applies_inside_nested_roots(self):
        # Impl-panel N1: the outer exclusion set (`.agent` + fingerprint_exclude)
        # is honored inside each nested root too — documented behavior, pinned so
        # it can't silently change. A `journal/` exclude blinds journal/ in the
        # nested repo, while a real code edit there still moves the fingerprint.
        d, sub = self._outer_with_nested()
        self._cfg(d, {"code_roots": ["sub"], "fingerprint_exclude": ["journal/"]})
        (sub / "journal").mkdir()
        fp1 = tree_state_fingerprint(d)
        (sub / "journal" / "1.md").write_text("shipped\n", encoding="utf-8")
        self.assertEqual(fp1, tree_state_fingerprint(d),
                         "fingerprint_exclude did not propagate into the nested root")
        # exclusion is narrow — a real nested code edit still moves it
        (sub / "app.py").write_text("y = 3\n", encoding="utf-8")
        self.assertNotEqual(fp1, tree_state_fingerprint(d))

    @unittest.skipUnless(hasattr(os, "symlink"), "requires os.symlink")
    def test_symlink_loop_root_does_not_crash(self):
        # Impl-panel N5: a code_roots entry that is a symlink LOOP makes
        # Path.resolve() raise RuntimeError on some platforms; the fingerprint
        # must skip it deterministically, never traceback.
        d, sub = self._outer_with_nested()
        try:
            os.symlink(d / "loop", d / "loop")   # self-referential loop
        except (OSError, NotImplementedError):
            self.skipTest("symlink not permitted here")
        self._cfg(d, {"code_roots": ["loop", "sub"]})
        fp = tree_state_fingerprint(d)            # must not raise
        self.assertTrue(fp)
        self.assertEqual(fp, tree_state_fingerprint(d))
        # the valid sibling `sub` still took effect
        (sub / "app.py").write_text("y = 9\n", encoding="utf-8")
        self.assertNotEqual(fp, tree_state_fingerprint(d))

    @unittest.skipUnless(hasattr(os, "symlink"), "requires os.symlink")
    def test_symlink_root_escaping_the_tree_is_skipped(self):
        # Impl-panel F2: a code_roots entry that is lexically clean ("link", no
        # `..`, relative) but SYMLINKS to a git repo OUTSIDE the project must be
        # refused loudly — never fingerprint outside the tree.
        import contextlib
        import io
        outside = Path(tempfile.mkdtemp())              # a repo outside the tree
        subprocess.run(["git", "init", "-q"], cwd=outside, check=True)
        (outside / "secret.py").write_text("s = 1\n", encoding="utf-8")
        _git(outside, "add", "-A")
        _git(outside, "commit", "-qm", "outside")
        d, sub = self._outer_with_nested()
        try:
            os.symlink(outside, d / "link")
        except (OSError, NotImplementedError):
            self.skipTest("symlink not permitted here")
        self._cfg(d, {"code_roots": ["link"]})
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            fp_escape = tree_state_fingerprint(d)
        self.assertIn("resolves outside the project", buf.getvalue(),
                      "symlink escape must be skipped LOUDLY")
        # editing the OUTSIDE repo must not move our fingerprint at all
        (outside / "secret.py").write_text("s = 2\n", encoding="utf-8")
        self.assertEqual(fp_escape, tree_state_fingerprint(d),
                         "fingerprint was steered to hash outside the tree")


def _symlink_or_skip(tc, target, link):
    """Windows: os.symlink exists but needs a privilege — probe, don't hasattr."""
    if not hasattr(os, "symlink"):
        tc.skipTest("requires os.symlink")
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as e:
        tc.skipTest(f"symlinks unavailable here: {e}")


class FingerprintRobustness060(unittest.TestCase):
    """Task 060 (parked T019/T023/T036, owner batch 2026-09-20): the freshness
    fingerprint must tell the truth in the bounded cases the panels found."""

    def test_subdir_project_untracked_content_edit_moves_fingerprint(self):
        # T023 #1: porcelain paths are repo-TOPLEVEL-relative (probed: from
        # `<repo>/sub`, `git status --porcelain -- .` prints `?? sub/new.py`), but
        # the untracked content was resolved against `repo_path` → doubled prefix
        # → `unreadable` → the SAME digest for content A and B (false FRESH).
        # Creation already moves the folded porcelain string, so the instrument
        # must EDIT an existing untracked file (plan-panel opus #1).
        d = _repo()
        sub = d / "sub"
        sub.mkdir()
        (sub / "new.py").write_text("A = 1\n", encoding="utf-8")
        fp_a = tree_state_fingerprint(sub)
        (sub / "new.py").write_text("A = 2\n", encoding="utf-8")
        fp_b = tree_state_fingerprint(sub)
        self.assertTrue(fp_a and fp_b, "subdir project must still fingerprint")
        self.assertNotEqual(fp_a, fp_b,
                            "subdir project: untracked content edit is invisible (false FRESH)")

    @unittest.skipIf(os.name == "nt", "non-UTF-8 bytes are illegal in Windows filenames")
    def test_quotepath_false_non_utf8_name_does_not_crash(self):
        # T023 #2: with `core.quotePath=false` git emits RAW non-UTF-8 path bytes
        # on the readable porcelain; the strict `text=True` read raised an
        # uncaught UnicodeDecodeError → the whole close/panel crashed.
        d = _repo()
        raw = os.path.join(os.fsencode(str(d)), b"a\xffb.py")
        try:
            with open(raw, "wb") as f:
                f.write(b"V = 1\n")
        except OSError:
            self.skipTest("filesystem rejects non-UTF-8 filenames")
        _git(d, "config", "core.quotePath", "false")
        try:
            fp = tree_state_fingerprint(d)
        except UnicodeDecodeError as e:
            self.fail(f"quotePath=false + non-UTF-8 name crashed the fingerprint: {e}")
        self.assertRegex(fp, r"^[0-9a-f]{12}$")

    @unittest.skipIf(os.name == "nt", "non-UTF-8 bytes are illegal in Windows filenames")
    def test_two_non_utf8_tracked_names_do_not_collide(self):
        # Plan-panel codex ×2: surrogate-decoded names must survive the FINAL
        # material encode — with `errors="replace"` both `a\xff.py` and
        # `a\xfe.py` serialize as `a?.py`, so the same edit under either name
        # hashes identically (a false FRESH across a rename).
        d = _repo()
        base = os.fsencode(str(d))
        try:
            for name in (b"a\xff.py", b"a\xfe.py"):
                with open(os.path.join(base, name), "wb") as f:
                    f.write(b"V = 1\n")
        except OSError:
            self.skipTest("filesystem rejects non-UTF-8 filenames")
        _git(d, "config", "core.quotePath", "false")
        _git(d, "add", "-A")
        _git(d, "commit", "-qm", "two raw names")
        with open(os.path.join(base, b"a\xff.py"), "wb") as f:
            f.write(b"V = 2\n")
        fp_ff = tree_state_fingerprint(d)
        with open(os.path.join(base, b"a\xff.py"), "wb") as f:
            f.write(b"V = 1\n")
        with open(os.path.join(base, b"a\xfe.py"), "wb") as f:
            f.write(b"V = 2\n")
        fp_fe = tree_state_fingerprint(d)
        self.assertNotEqual(fp_ff, fp_fe,
                            "distinct non-UTF-8 names collapsed to the same fingerprint")

    def test_truncated_index_yields_empty_fingerprint_not_a_clean_tree(self):
        # T019 F4-outer: `git status`/`git diff` exit 128 on a truncated index
        # with EMPTY output while `rev-parse HEAD` still succeeds → the outer
        # material hashed HEAD + "" + "" — a clean-looking fingerprint equal to
        # the stamp of a clean panel (false FRESH). rc must be read: → "".
        d = _repo()
        clean_fp = tree_state_fingerprint(d)
        idx = d / ".git" / "index"
        idx.write_bytes(idx.read_bytes()[:3])
        fp = tree_state_fingerprint(d)
        self.assertEqual(fp, "", f"broken index must yield NO fingerprint, got {fp!r} "
                                 f"(clean was {clean_fp!r})")

    def test_missing_head_yields_empty_fingerprint(self):
        d = _repo()
        (d / ".git" / "HEAD").unlink()
        self.assertEqual(tree_state_fingerprint(d), "")

    def test_untracked_symlink_retarget_moves_fingerprint(self):
        # T036 r8 opus #2: an untracked symlink hashed as the constant
        # `unreadable` (O_NOFOLLOW), so a RETARGET as the sole change kept the
        # fingerprint equal (false FRESH). Token it by link text, like
        # `_dirty_path_content_map` already does.
        d = _repo()
        (d / "a.py").write_text("same\n", encoding="utf-8")
        (d / "b.py").write_text("same\n", encoding="utf-8")
        _git(d, "add", "-A")
        _git(d, "commit", "-qm", "two same files")
        _symlink_or_skip(self, "a.py", d / "link.py")
        fp1 = tree_state_fingerprint(d)
        (d / "link.py").unlink()
        os.symlink("b.py", d / "link.py")
        fp2 = tree_state_fingerprint(d)
        self.assertTrue(fp1 and fp2)
        self.assertNotEqual(fp1, fp2, "untracked symlink retarget is invisible (false FRESH)")

    def test_untracked_symlink_to_fifo_still_does_not_hang(self):
        # W5 must not touch `_safe_hash_regular`: a symlink INTO a FIFO is
        # tokened by link text and never opened.
        if os.name == "nt":
            self.skipTest("mkfifo is POSIX-only")
        d = _repo()
        os.mkfifo(d / "pipe")
        _symlink_or_skip(self, "pipe", d / "link.py")
        import threading
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("fp", tree_state_fingerprint(d)))
        t.daemon = True
        t.start()
        t.join(20)
        self.assertFalse(t.is_alive(), "fingerprint hung on an untracked symlink → FIFO")
        self.assertRegex(out.get("fp", ""), r"^[0-9a-f]{12}$")

    def test_code_root_symlink_repoint_to_same_head_clone_moves_fingerprint(self):
        # T036 r8 codex:sol #2: a `code_root` scope is keyed by its config NAME, so
        # repointing the symlink to a same-HEAD, clean clone leaves the material
        # identical. The root and both clones are gitignored so the OUTER tree
        # is blind to the repoint by construction — only the resolved identity
        # can catch it.
        d = _repo()
        (d / ".gitignore").write_text("root\ncloneA/\ncloneB/\n", encoding="utf-8")
        _git(d, "add", "-A")
        _git(d, "commit", "-qm", "ignore roots")
        a = d / "cloneA"
        a.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=a, check=True)
        (a / "app.py").write_text("y = 1\n", encoding="utf-8")
        _git(a, "add", "-A")
        _git(a, "commit", "-qm", "seed")
        subprocess.run(["git", "clone", "-q", str(a), str(d / "cloneB")], check=True,
                       capture_output=True)
        (d / ".agent").mkdir()
        (d / ".agent" / "config.json").write_text(json.dumps({"code_roots": ["root"]}),
                                                  encoding="utf-8")
        _symlink_or_skip(self, "cloneA", d / "root")
        fp1 = tree_state_fingerprint(d)
        (d / "root").unlink()
        os.symlink("cloneB", d / "root")
        fp2 = tree_state_fingerprint(d)
        self.assertTrue(fp1 and fp2)
        self.assertNotEqual(fp1, fp2, "code_root repoint to a same-HEAD clone is invisible")

    def test_owner_exclude_covers_behavioral_classification(self):
        # W6 helper: bookkeeping prose under an exclude is allowed (the documented
        # `journal/` use); code / json / scripts / extension-less are source; a
        # git error counts as covers (unknown = fail closed).
        from tasks.core import owner_exclude_covers_behavioral
        d = _repo()
        for rel, body in {"journal/log.md": "x\n", "journal/notes.rst": "x\n",
                          "gen/out.json": "{}\n", "gen/run.py": "x=1\n",
                          "gen/Makefile": "all:\n", "gen/README.md": "x\n"}.items():
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            (d / rel).write_text(body, encoding="utf-8")
        _git(d, "add", "-A")
        _git(d, "commit", "-qm", "fixture")
        self.assertEqual(owner_exclude_covers_behavioral(d, {}), (False, []))
        self.assertEqual(owner_exclude_covers_behavioral(d, {"fingerprint_exclude": ["journal/"]}),
                         (False, []))
        covers, hits = owner_exclude_covers_behavioral(d, {"fingerprint_exclude": ["gen/"]})
        self.assertTrue(covers)
        self.assertEqual(hits, ["gen/Makefile", "gen/out.json", "gen/run.py"])
        # impl-panel r1 (sonnet/codex-high): `.txt` is NOT bookkeeping-shaped —
        # requirements.txt / CMakeLists.txt change behaviour.
        (d / "gen" / "requirements.txt").write_text("requests==2.0\n", encoding="utf-8")
        _git(d, "add", "-A")
        _git(d, "commit", "-qm", "reqs")
        covers, hits = owner_exclude_covers_behavioral(d, {"fingerprint_exclude": ["gen/"]})
        self.assertIn("gen/requirements.txt", hits)
        # impl-panel r1 (codex-high #1): a STAGED DELETION removes the path from
        # `ls-files --cached`; the exclude still hides the `D` line, so the check
        # must also see what HEAD has under the exclude.
        _git(d, "rm", "-q", "gen/run.py")
        covers, hits = owner_exclude_covers_behavioral(d, {"fingerprint_exclude": ["gen/"]})
        self.assertIn("gen/run.py", hits, "staged deletion under an exclude escaped the check")
        # untracked source under an exclude counts too (--others)
        (d / "journal" / "helper.py").write_text("x\n", encoding="utf-8")
        covers, hits = owner_exclude_covers_behavioral(d, {"fingerprint_exclude": ["journal/"]})
        self.assertEqual((covers, hits), (True, ["journal/helper.py"]))
        # git error → covers
        (d / ".git" / "HEAD").unlink()
        covers, hits = owner_exclude_covers_behavioral(d, {"fingerprint_exclude": ["journal/"]})
        self.assertTrue(covers)
        self.assertTrue(any("git error" in h for h in hits), hits)

    def test_subdir_project_tail_cert_sees_a_dirty_code_edit(self):
        # impl-panel r1 (opus): `_dirty_path_content_map` resolved porcelain paths
        # against `repo_path` too, so in a SUBDIR project every dirty path tokened
        # as the doubled-prefix `absent` at BOTH F0 and close — a content edit to
        # an already-dirty code file was invisible to the tail-cert delta while a
        # committed doc explained the STALE, and the behavioural edit certified as
        # docs-only.
        from tasks.core import build_panel_snapshot, tail_cert_delta
        d = _repo()
        sub = d / "sub"
        (sub / "docs").mkdir(parents=True)
        (sub / "app.py").write_text("v = 1\n", encoding="utf-8")
        (sub / "docs" / "note.md").write_text("a\n", encoding="utf-8")
        _git(d, "add", "-A")
        _git(d, "commit", "-qm", "sub project")
        (sub / "app.py").write_text("v = 2\n", encoding="utf-8")        # dirty at F0
        fp0 = tree_state_fingerprint(sub)
        snap = build_panel_snapshot(sub, fp0)
        self.assertIsNotNone(snap)
        self.assertNotEqual(snap["scopes"][""]["dirty"].get("sub/app.py",
                            snap["scopes"][""]["dirty"].get("app.py")), "absent",
                            "dirty code tokened as absent in a subdir project")
        (sub / "app.py").write_text("v = 3\n", encoding="utf-8")        # code edit after F0
        (sub / "docs" / "note.md").write_text("b\n", encoding="utf-8")
        _git(d, "add", "sub/docs/note.md")
        _git(d, "commit", "-qm", "doc after panel")
        can, beh, non = tail_cert_delta(sub, snap, fp0)
        self.assertTrue(any(p.endswith("app.py") for p in beh),
                        f"subdir dirty code edit invisible to tail-cert: can={can} beh={beh} non={non}")

    def test_subdir_project_under_tests_does_not_classify_code_as_tests(self):
        # impl-panel r2 (codex-high #1): dirty-map keys and F0..HEAD diff paths were
        # TOPLEVEL-relative, so a project rooted at `<repo>/tests/product` saw
        # `tests/product/app.py` — a `tests/` segment → non-behavioral → a code
        # edit certified. Paths must be PROJECT-relative before classification.
        from tasks.core import build_panel_snapshot, tail_cert_delta
        d = _repo()
        proj = d / "tests" / "product"
        (proj / "docs").mkdir(parents=True)
        (proj / "app.py").write_text("v = 1\n", encoding="utf-8")
        (proj / "docs" / "n.md").write_text("a\n", encoding="utf-8")
        _git(d, "add", "-A")
        _git(d, "commit", "-qm", "project under tests/")
        (proj / "app.py").write_text("v = 2\n", encoding="utf-8")            # dirty at F0
        fp0 = tree_state_fingerprint(proj)
        snap = build_panel_snapshot(proj, fp0)
        self.assertIn("app.py", snap["scopes"][""]["dirty"], "dirty-map keys must be project-relative")
        (proj / "app.py").write_text("v = 3\n", encoding="utf-8")            # code edit after F0
        (proj / "docs" / "n.md").write_text("b\n", encoding="utf-8")
        _git(d, "add", "tests/product/docs/n.md")
        _git(d, "commit", "-qm", "doc after panel")
        can, beh, non = tail_cert_delta(proj, snap, fp0)
        self.assertIn("app.py", beh, f"code under a tests/-rooted project certified: can={can} beh={beh} non={non}")
        # committed code edit is also project-relative in the F0..HEAD leg
        (proj / "lib.py").write_text("z = 1\n", encoding="utf-8")
        _git(d, "add", "-A")
        _git(d, "commit", "-qm", "code commit")
        can, beh, non = tail_cert_delta(proj, snap, fp0)
        self.assertIn("lib.py", beh)

    def test_tail_cert_refuses_code_that_passed_through_an_exclude(self):
        # impl-panel r2 (grok #1): a `.py` committed under a standing exclude
        # (`journal/`) after F0 and deleted again before close never enters the
        # F0..HEAD delta (the diff carries `:(exclude)`), and the close-time
        # ls-files/ls-tree check sees nothing — a docs touch would certify.
        from tasks.core import build_panel_snapshot, tail_cert_delta
        d = _repo()
        (d / "journal").mkdir()
        (d / "docs").mkdir()
        (d / "journal" / "log.md").write_text("x\n", encoding="utf-8")
        _git(d, "add", "-A")
        _git(d, "commit", "-qm", "journal")
        (d / ".agent").mkdir()
        (d / ".agent" / "config.json").write_text(json.dumps({"fingerprint_exclude": ["journal/"]}),
                                                  encoding="utf-8")
        fp0 = tree_state_fingerprint(d)
        snap = build_panel_snapshot(d, fp0)
        self.assertIsNotNone(snap)
        (d / "journal" / "sneak.py").write_text("import os\n", encoding="utf-8")
        _git(d, "add", "-A")
        _git(d, "commit", "-qm", "code through the exclude")
        _git(d, "rm", "-q", "journal/sneak.py")
        _git(d, "commit", "-qm", "and gone again")
        (d / "docs" / "note.md").write_text("doc\n", encoding="utf-8")
        _git(d, "add", "-A")
        _git(d, "commit", "-qm", "docs")
        can, beh, non = tail_cert_delta(d, snap, fp0)
        self.assertFalse(can, "code that passed through an exclude between F0 and close certified")

    def test_tail_cert_refuses_code_through_an_exclude_in_a_tests_rooted_subdir(self):
        # impl-panel r3 (grok #2): the through-walk emits toplevel-relative names;
        # for a project at `<repo>/tests/product` an un-stripped
        # `tests/product/journal/sneak.py` would classify as the test tree.
        from tasks.core import build_panel_snapshot, tail_cert_delta
        d = _repo()
        proj = d / "tests" / "product"
        (proj / "journal").mkdir(parents=True)
        (proj / "docs").mkdir()
        (proj / "journal" / "log.md").write_text("x\n", encoding="utf-8")
        _git(d, "add", "-A")
        _git(d, "commit", "-qm", "project under tests/")
        (proj / ".agent").mkdir()
        (proj / ".agent" / "config.json").write_text(
            json.dumps({"fingerprint_exclude": ["journal/"]}), encoding="utf-8")
        fp0 = tree_state_fingerprint(proj)
        snap = build_panel_snapshot(proj, fp0)
        self.assertIsNotNone(snap)
        (proj / "journal" / "sneak.py").write_text("import os\n", encoding="utf-8")
        _git(d, "add", "-A")
        _git(d, "commit", "-qm", "code through the exclude")
        _git(d, "rm", "-q", "tests/product/journal/sneak.py")
        _git(d, "commit", "-qm", "gone again")
        (proj / "docs" / "note.md").write_text("doc\n", encoding="utf-8")
        _git(d, "add", "-A")
        _git(d, "commit", "-qm", "docs")
        can, beh, non = tail_cert_delta(proj, snap, fp0)
        self.assertFalse(can, "code through an exclude certified in a tests/-rooted subdir project")

    def test_tail_cert_refuses_when_exclude_covers_source(self):
        from tasks.core import build_panel_snapshot, tail_cert_delta
        d = _repo()
        (d / "src").mkdir()
        (d / "src" / "x.py").write_text("v = 1\n", encoding="utf-8")
        (d / "docs").mkdir()
        _git(d, "add", "-A")
        _git(d, "commit", "-qm", "src")
        (d / ".agent").mkdir()
        (d / ".agent" / "config.json").write_text(
            json.dumps({"fingerprint_exclude": ["src/"]}), encoding="utf-8")
        fp = tree_state_fingerprint(d)
        snap = build_panel_snapshot(d, fp)
        self.assertIsNotNone(snap)
        (d / "docs" / "note.md").write_text("doc\n", encoding="utf-8")
        can, beh, non = tail_cert_delta(d, snap, fp)
        self.assertFalse(can, "tail-cert certified a docs delta while src/ was hidden by the exclude")

    def test_snapshot_records_identity_and_tail_cert_refuses_a_repoint(self):
        from tasks.core import build_panel_snapshot, tail_cert_delta
        d = _repo()
        (d / ".gitignore").write_text("root\ncloneA/\ncloneB/\n", encoding="utf-8")
        (d / "docs").mkdir()
        _git(d, "add", "-A")
        _git(d, "commit", "-qm", "ignore roots")
        a = d / "cloneA"
        a.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=a, check=True)
        (a / "app.py").write_text("y = 1\n", encoding="utf-8")
        _git(a, "add", "-A")
        _git(a, "commit", "-qm", "seed")
        subprocess.run(["git", "clone", "-q", str(a), str(d / "cloneB")], check=True,
                       capture_output=True)
        (d / ".agent").mkdir()
        (d / ".agent" / "config.json").write_text(json.dumps({"code_roots": ["root"]}),
                                                  encoding="utf-8")
        _symlink_or_skip(self, "cloneA", d / "root")
        fp = tree_state_fingerprint(d)
        snap = build_panel_snapshot(d, fp)
        self.assertEqual(snap["scopes"]["root"]["identity"], "cloneA")
        self.assertEqual(snap["scopes"][""]["identity"], ".")
        (d / "root").unlink()
        os.symlink("cloneB", d / "root")
        (d / "docs" / "note.md").write_text("doc\n", encoding="utf-8")
        can, beh, non = tail_cert_delta(d, snap, fp)
        self.assertFalse(can, "tail-cert must refuse a repointed code_root")


class GateDecision(unittest.TestCase):
    KW = dict(risk="irreversible", panel_required=True, evidence_carries=True,
              round_fp="a" * 12, now_fp="b" * 12, force=False,
              stale_ok=False, stale_reason=None)

    def test_stale_irreversible_blocks(self):
        allowed, why = freshness_gate_decision(**self.KW)
        self.assertFalse(allowed)
        self.assertIn("re-run", why)
        self.assertIn("--stale-panel-ok", why)

    def test_override_without_reason_refused(self):
        allowed, why = freshness_gate_decision(**{**self.KW, "stale_ok": True})
        self.assertFalse(allowed)
        self.assertIn("--reason", why)

    def test_override_with_reason_allows(self):
        allowed, _ = freshness_gate_decision(
            **{**self.KW, "stale_ok": True, "stale_reason": "journal only"})
        self.assertTrue(allowed)

    def test_gate_scope_negative_controls(self):
        # Each condition individually off → allowed (the gate is narrow).
        # NOTE (T1, 2026-08-23): {"risk": "assertive"} and — after panel finding
        # O1 — {"risk": "unclassified"} MOVED OUT of this allowed set. Both now
        # block on a stale carrying panel (see test_stale_assertive_blocks /
        # test_stale_unclassified_blocks); only `reversible` stays advisory.
        # Rewriting, not deleting, the old contract.
        # Task 060: {"round_fp": ""} and {"now_fp": ""} MOVED OUT too — an
        # absent fingerprint on a carrying high-consequence panel is NO-STAMP /
        # UNREADABLE and blocks (see test_absent_fingerprint_blocks); the one
        # remaining allow for an absent stamp is a project with no git at all.
        for tweak in ({"risk": "reversible"},
                      {"panel_required": False}, {"evidence_carries": False},
                      {"round_fp": "", "git_available": False},
                      {"now_fp": "a" * 12}, {"force": True}):
            allowed, _ = freshness_gate_decision(**{**self.KW, **tweak})
            self.assertTrue(allowed, f"gate overreached with {tweak}")

    def test_absent_fingerprint_blocks(self):
        # Task 060 (plan panel codex ×2, grok #2): NO-STAMP (git repo, no stamp)
        # and UNREADABLE (stamp, no fingerprint now) block a carrying
        # high-consequence close; the message names the cause, not "code changed";
        # --stale-panel-ok needs a reason; --force still bypasses.
        for tweak, word in (({"round_fp": ""}, "NO STAMP"), ({"now_fp": ""}, "UNREADABLE")):
            allowed, why = freshness_gate_decision(**{**self.KW, **tweak})
            self.assertFalse(allowed, f"absent fingerprint allowed with {tweak}")
            self.assertIn(word, why)
            self.assertNotIn("code state changed", why)
            allowed, why = freshness_gate_decision(**{**self.KW, **tweak, "stale_ok": True})
            self.assertFalse(allowed)
            self.assertIn("--reason", why)
            allowed, _ = freshness_gate_decision(
                **{**self.KW, **tweak, "stale_ok": True, "stale_reason": "reviewed by hand"})
            self.assertTrue(allowed)
            allowed, _ = freshness_gate_decision(**{**self.KW, **tweak, "force": True})
            self.assertTrue(allowed)

    def test_stale_assertive_blocks(self):
        # T1: an assertive close resting on a stale panel must block, because a
        # claim signed off by a panel that predates the code is a claim about
        # code that was never reviewed. Same message shape as irreversible.
        allowed, why = freshness_gate_decision(
            **{**self.KW, "risk": "assertive"})
        self.assertFalse(allowed, "assertive stale close was NOT blocked")
        self.assertIn("re-run", why)
        self.assertIn("--stale-panel-ok", why)
        # grok#3: pin the message CONTENT — the risk class and BOTH fingerprints
        # (worklist: "blocks the close with a message naming both fingerprints").
        # A revert to the old hardcoded "risk is irreversible" or a dropped
        # fingerprint must fail here, not stay green on the marker alone.
        self.assertIn("assertive", why)
        self.assertIn(self.KW["round_fp"], why)
        self.assertIn(self.KW["now_fp"], why)

    def test_assertive_override_without_reason_refused(self):
        allowed, why = freshness_gate_decision(
            **{**self.KW, "risk": "assertive", "stale_ok": True})
        self.assertFalse(allowed)
        self.assertIn("--reason", why)

    def test_assertive_override_with_reason_allows(self):
        # Narrow escape (same as irreversible): --stale-panel-ok --reason.
        allowed, _ = freshness_gate_decision(
            **{**self.KW, "risk": "assertive", "stale_ok": True,
               "stale_reason": "docs-only delta, diff reviewed"})
        self.assertTrue(allowed)

    def test_assertive_force_allows(self):
        # Blunt escape (same as irreversible): --force bypasses close policy.
        allowed, _ = freshness_gate_decision(
            **{**self.KW, "risk": "assertive", "force": True})
        self.assertTrue(allowed)

    def test_stale_unclassified_blocks(self):
        # Panel finding O1: an unset `## Risk` is held to the high-consequence
        # bar everywhere else (close_decision, panel_required "all"), so it must
        # ALSO block on a stale carrying panel — otherwise blanking the field is
        # strictly more lenient on freshness than honest classification, the
        # 1.5.32 "cheapest path through the strictest gate" fail-open reopened.
        allowed, why = freshness_gate_decision(
            **{**self.KW, "risk": "unclassified"})
        self.assertFalse(allowed, "unclassified stale close was NOT blocked")
        self.assertIn("re-run", why)


class ClosePathMatrix(unittest.TestCase):
    """End-to-end through the real CLI."""

    def _setup(self, *, risk: str, panel_cfg, round_head: str = "Impl",
               verdict: str = "PASS", stamp: bool = True,
               change_after: bool = True, extra_round: "str | None" = None,
               code_roots=None, nested_change: bool = False,
               extra_cfg: "dict | None" = None,
               tracked_files: "dict | None" = None):
        d = _repo()
        (d / ".agent").mkdir(exist_ok=True)
        sub = None
        if tracked_files:
            for rel, content in tracked_files.items():
                fpath = d / rel
                fpath.parent.mkdir(parents=True, exist_ok=True)
                fpath.write_text(content, encoding="utf-8")
            _git(d, "add", "-A")
            _git(d, "commit", "-qm", "tracked fixture files")
        if code_roots is not None:
            # gitignored nested git repo(s) — the code_roots dogfood shape.
            (d / ".gitignore").write_text(
                "".join(f"{r}/\n" for r in code_roots), encoding="utf-8")
            _git(d, "add", "-A")
            _git(d, "commit", "-qm", "ignore nested roots")
            for r in code_roots:
                sub = d / r
                sub.mkdir(parents=True, exist_ok=True)
                subprocess.run(["git", "init", "-q"], cwd=sub, check=True)
                (sub / "app.py").write_text("y = 1\n", encoding="utf-8")
                _git(sub, "add", "-A")
                _git(sub, "commit", "-qm", "seed nested")
        cfg = {}
        if panel_cfg is not None:
            cfg["panel_required_for"] = panel_cfg
        if code_roots is not None:
            cfg["code_roots"] = code_roots
        if extra_cfg:
            cfg.update(extra_cfg)
        if cfg:
            (d / ".agent" / "config.json").write_text(
                json.dumps(cfg), encoding="utf-8")
        td = d / ".agent" / "tasks" / "001-t"
        td.mkdir(parents=True)
        (td / "task.md").write_text(
            f"# 001 - T\n\n## Status\npending\n\n## Risk\n{risk}\n\n"
            "## Work Plan\n- [x] G1: do it\n", encoding="utf-8")
        env = dict(os.environ, PYTHONPATH=str(PLUGIN), PLAYBOOK_SESSION_ID="pid-f18")
        r = subprocess.run([sys.executable, "-m", "tasks.cli", "work", "1"],
                           cwd=d, env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        fp = tree_state_fingerprint(d)
        stamp_line = f"**Tree-state:** {fp}\n" if stamp else ""
        rounds = (f"# Panel {round_head} Review — task 1\n\n"
                  f"**PANEL VERDICT: {verdict}** — 4/4, quorum 3\n"
                  f"{stamp_line}\nbody\n")
        if extra_round:
            rounds = extra_round + "\n" + rounds
        (td / "judge.md").write_text(rounds, encoding="utf-8")
        if change_after:
            if nested_change:
                # A CODE-ONLY edit INSIDE the nested root — nothing in the outer
                # tree moves. Only a code_roots-aware fingerprint catches it.
                assert sub is not None, "nested_change requires code_roots"
                (sub / "app.py").write_text("y = 42\n", encoding="utf-8")
            else:
                (d / "code.py").write_text("x = 99\n", encoding="utf-8")
        return d, td, env

    def _close(self, d, env, *flags):
        return subprocess.run(
            [sys.executable, "-m", "tasks.cli", "work", "done", *flags],
            cwd=d, env=env, capture_output=True, text=True, timeout=60)

    def _receipt(self, td) -> str:
        return (td / "task.md").read_text(encoding="utf-8")

    def test_irreversible_stale_blocks(self):
        d, td, env = self._setup(risk="irreversible", panel_cfg="all")
        r = self._close(d, env)
        self.assertNotIn("Task 001 done.", r.stdout)
        self.assertIn(BLOCK_MARKER, r.stderr)
        self.assertIn("pending", self._receipt(td))

    def test_override_needs_reason_then_closes_with_recorded_reason(self):
        d, td, env = self._setup(risk="irreversible", panel_cfg="all")
        r = self._close(d, env, "--stale-panel-ok")
        self.assertNotIn("Task 001 done.", r.stdout)
        r = self._close(d, env, "--stale-panel-ok", "--reason", "journal only, diff reviewed")
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        receipt = self._receipt(td)
        self.assertIn(CLAUSE, receipt)
        self.assertIn("STALE", receipt)
        self.assertIn('accepted: "journal only, diff reviewed"', receipt)

    def test_irreversible_fresh_closes_with_fresh_clause(self):
        d, td, env = self._setup(risk="irreversible", panel_cfg="all",
                                 change_after=False)
        r = self._close(d, env)
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        self.assertIn("FRESH", self._receipt(td))

    def test_reversible_stale_closes_with_stale_clause(self):
        # Advisory behavior unchanged below the gate; the record is new.
        # (T1: reversible stays advisory — the negative control that the gate
        # did not widen to every risk class.)
        d, td, env = self._setup(risk="reversible", panel_cfg="all")
        r = self._close(d, env)
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        self.assertIn("STALE", self._receipt(td))
        self.assertIn("tree-state mismatch", r.stdout)  # console note kept

    def test_assertive_stale_blocks(self):
        # T1: the real close path blocks an assertive stale close, same as
        # irreversible. Red against pre-T1 code (assertive closed with a STALE
        # receipt clause and no block).
        d, td, env = self._setup(risk="assertive", panel_cfg="all")
        r = self._close(d, env)
        self.assertNotIn("Task 001 done.", r.stdout)
        self.assertIn(BLOCK_MARKER, r.stderr)
        self.assertIn("risk is assertive", r.stderr)  # grok#3: names the class
        self.assertIn("pending", self._receipt(td))

    def test_assertive_fresh_closes_with_fresh_clause(self):
        d, td, env = self._setup(risk="assertive", panel_cfg="all",
                                 change_after=False)
        r = self._close(d, env)
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        self.assertIn("FRESH", self._receipt(td))

    def test_assertive_nested_code_only_edit_blocks(self):
        # C2 GUARANTEE PROOF: with code_roots naming the nested repo, a code-only
        # edit INSIDE it after the panel stamp reads STALE and blocks the
        # assertive close. Red against pre-C2 code: the fingerprint saw only the
        # outer tree, so the nested edit was invisible → silently FRESH → close.
        d, td, env = self._setup(risk="assertive", panel_cfg="all",
                                 code_roots=["sub"], nested_change=True)
        r = self._close(d, env)
        self.assertNotIn("Task 001 done.", r.stdout)
        self.assertIn(BLOCK_MARKER, r.stderr)
        self.assertIn("pending", self._receipt(td))

    def test_assertive_nested_fresh_closes(self):
        # Negative control: code_roots set, but NO post-panel nested edit — the
        # stamp still matches, so the assertive close goes through FRESH. Proves
        # the block above is caused by the nested edit, not merely by code_roots.
        d, td, env = self._setup(risk="assertive", panel_cfg="all",
                                 code_roots=["sub"], change_after=False)
        r = self._close(d, env)
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        self.assertIn("FRESH", self._receipt(td))

    def test_assertive_stale_panel_ok_closes_with_recorded_reason(self):
        # T1: the narrow escape works for assertive too (user decision
        # 2026-08-23 — same two escapes as irreversible).
        d, td, env = self._setup(risk="assertive", panel_cfg="all")
        r = self._close(d, env, "--stale-panel-ok")
        self.assertNotIn("Task 001 done.", r.stdout)  # reason required
        r = self._close(d, env, "--stale-panel-ok", "--reason",
                        "docs-only delta, diff reviewed")
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        receipt = self._receipt(td)
        self.assertIn(CLAUSE, receipt)
        self.assertIn("STALE", receipt)
        self.assertIn('accepted: "docs-only delta, diff reviewed"', receipt)

    def test_assertive_force_bypasses_and_attributes_reason_to_force(self):
        d, td, env = self._setup(risk="assertive", panel_cfg="all")
        r = self._close(d, env, "--force", "--reason", "emergency close")
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        receipt = self._receipt(td)
        self.assertIn("Forced close, reason:", receipt)
        self.assertIn("emergency close", receipt)
        self.assertIn("STALE", receipt)
        self.assertNotIn('accepted: "emergency close"', receipt)

    def test_unclassified_stale_blocks_under_all(self):
        # Panel finding O1, end-to-end: under panel_required_for "all" an unset
        # `## Risk` resting on a stale carrying panel must block, or blanking
        # the field is a freshness bypass of the T1 guarantee. Red pre-O1
        # (unclassified closed advisory-only).
        d, td, env = self._setup(risk="unclassified", panel_cfg="all")
        r = self._close(d, env)
        self.assertNotIn("Task 001 done.", r.stdout)
        self.assertIn(BLOCK_MARKER, r.stderr)
        self.assertIn("pending", self._receipt(td))

    def test_reversible_stale_still_advisory_under_all(self):
        # O1 negative control: the gate did NOT widen to reversible — a truly
        # reversible task still closes with only the advisory STALE clause.
        d, td, env = self._setup(risk="reversible", panel_cfg="all")
        r = self._close(d, env)
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        self.assertIn("STALE", self._receipt(td))

    def test_default_list_config_asymmetry_is_pinned(self):
        # Panel finding O2: pin the SEEDED-default behavior, not just "all".
        # Under panel_required_for=["assertive","irreversible"] the freshness
        # gate fires for assertive (panel required) but is INERT for
        # unclassified (panel NOT required → resolve_panel_required False), so
        # `unclassified` does not hit the freshness BLOCK. This is the boundary
        # a future edit to resolve_panel_required or the init seed could shift
        # undetected — the documented scope limit of the O1 fix.
        DEFAULT = ["assertive", "irreversible"]
        # assertive → freshness block fires
        d, td, env = self._setup(risk="assertive", panel_cfg=DEFAULT)
        r = self._close(d, env)
        self.assertNotIn("Task 001 done.", r.stdout)
        self.assertIn(BLOCK_MARKER, r.stderr)
        # unclassified → gate inert (panel not required for it by default)
        d2, td2, env2 = self._setup(risk="unclassified", panel_cfg=DEFAULT)
        r2 = self._close(d2, env2)
        self.assertNotIn(BLOCK_MARKER, r2.stderr)

    def test_irreversible_stale_without_policy_is_advisory(self):
        # Judge A4: a voluntary panel in a no-policy project must not block.
        d, td, env = self._setup(risk="irreversible", panel_cfg=None)
        r = self._close(d, env)
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        self.assertIn("STALE", self._receipt(td))

    def test_no_impl_round_gets_panel_evidence_block_not_freshness(self):
        # Judge C3 / design A3: exactly one block message.
        d, td, env = self._setup(risk="irreversible", panel_cfg="all",
                                 round_head="Plan")
        r = self._close(d, env)
        self.assertNotIn("Task 001 done.", r.stdout)
        self.assertIn("panel review required by policy", r.stderr)
        self.assertNotIn(BLOCK_MARKER, r.stderr)

    def test_newest_impl_fail_round_skips_freshness_gate(self):
        # Judge C3 case 1: a stamped FAIL round cannot carry the close, so the
        # freshness gate must stay silent and the evidence block must fire.
        d, td, env = self._setup(risk="irreversible", panel_cfg="all",
                                 verdict="FAIL")
        r = self._close(d, env)
        self.assertNotIn("Task 001 done.", r.stdout)
        self.assertIn("panel review required by policy", r.stderr)
        self.assertNotIn(BLOCK_MARKER, r.stderr)

    def test_replan_over_stamped_impl_skips_freshness_gate(self):
        # Judge C3 case 2: newest round is plan → evidence does not carry.
        d, td, env = self._setup(risk="irreversible", panel_cfg="all",
                                 round_head="Plan", verdict="PASS",
                                 extra_round=None)
        # build: plan round newest (written by _setup), stamped impl BELOW it
        fp = tree_state_fingerprint(d)
        jm = td / "judge.md"
        jm.write_text(jm.read_text(encoding="utf-8")
                      + f"\n# Panel Impl Review — task 1\n\n"
                        f"**PANEL VERDICT: PASS** — 4/4, quorum 3\n"
                        f"**Tree-state:** {fp}\n\nolder impl\n", encoding="utf-8")
        r = self._close(d, env)
        self.assertNotIn("Task 001 done.", r.stdout)
        self.assertIn("panel review required by policy", r.stderr)
        self.assertNotIn(BLOCK_MARKER, r.stderr)

    def test_missing_stamp_blocks_high_consequence_and_is_recorded(self):
        # Judge F4 (1.5.6): a missing stamp must leave a RECORD. Task 060 (plan
        # panel codex ×2): it must also BLOCK a carrying high-consequence close —
        # with an unreadable git AT CLOSE now blocking, a missing stamp (git
        # broken at PANEL time, or a hand-edited round) would be the cheaper path
        # through the strict gate. Same two exits as STALE.
        d, td, env = self._setup(risk="irreversible", panel_cfg="all",
                                 stamp=False)
        r = self._close(d, env)
        self.assertNotIn("Task 001 done.", r.stdout, "NO-STAMP must not close irreversible")
        self.assertIn("no stamp", (r.stdout + r.stderr).lower())
        r = self._close(d, env, "--stale-panel-ok", "--reason", "stamp lost, delta reviewed by hand")
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        self.assertIn("no stamp recorded", self._receipt(td))

    def test_missing_stamp_reversible_still_advisory(self):
        d, td, env = self._setup(risk="reversible", panel_cfg="all", stamp=False)
        r = self._close(d, env)
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        self.assertIn("no stamp recorded", self._receipt(td))

    def test_owner_exclude_covering_source_blocks_the_close(self):
        # T036 r6 codex:sol #1 + plan-panel (4 judges, Critical): an owner
        # `fingerprint_exclude` that covers a SOURCE path filters it out of the
        # material; the exclude-set hash is unchanged since F0, so an edit under
        # it reads FRESH and the close never reaches tail_cert_delta. The gate
        # itself must refuse: a high-consequence carrying close blocks with an
        # EXCLUDE-COVERS-CODE verdict until the config is fixed.
        d, td, env = self._setup(risk="assertive", panel_cfg=["assertive"],
                                 change_after=False,
                                 tracked_files={"src/x.py": "v = 1\n"},
                                 extra_cfg={"fingerprint_exclude": ["src/"]})
        (d / "src" / "x.py").write_text("v = 2\n", encoding="utf-8")   # hidden edit
        r = self._close(d, env)
        self.assertNotIn("Task 001 done.", r.stdout,
                         f"an exclude hiding source read FRESH and closed: {r.stdout} {r.stderr}")
        self.assertIn("EXCLUDE", r.stdout + r.stderr)
        self.assertIn("src/", r.stdout + r.stderr)
        r = self._close(d, env, "--stale-panel-ok", "--reason", "src/ exclusion reviewed by hand")
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        self.assertIn("EXCLUDE", self._receipt(td))

    def test_owner_exclude_staged_deletion_of_source_blocks_the_close(self):
        # impl-panel r1 (codex-high #1): `git rm src/x.py` after the panel removes
        # the path from the index/worktree; the exclude hides the `D` line from
        # the fingerprint, and a worktree-only check sees no hit → FRESH close.
        d, td, env = self._setup(risk="assertive", panel_cfg=["assertive"],
                                 change_after=False,
                                 tracked_files={"src/x.py": "v = 1\n"},
                                 extra_cfg={"fingerprint_exclude": ["src/"]})
        _git(d, "rm", "-q", "src/x.py")
        r = self._close(d, env)
        self.assertNotIn("Task 001 done.", r.stdout,
                         f"staged deletion under an exclude closed FRESH: {r.stdout} {r.stderr}")
        self.assertIn("EXCLUDE", r.stdout + r.stderr)

    def test_missing_stamp_blocks_when_git_dir_is_external(self):
        # impl-panel r1 (codex-medium #1): `git_available` was a `.git` ancestor
        # probe; a worktree driven by GIT_DIR/GIT_WORK_TREE has no `.git` inside
        # the project, so a missing stamp read as "not a git project" → advisory.
        import shutil
        d, td, env = self._setup(risk="irreversible", panel_cfg="all", stamp=False)
        gitdir = Path(tempfile.mkdtemp()) / "external.git"
        shutil.move(str(d / ".git"), str(gitdir))
        env["GIT_DIR"] = str(gitdir)
        env["GIT_WORK_TREE"] = str(d)
        r = self._close(d, env)
        self.assertNotIn("Task 001 done.", r.stdout,
                         f"NO-STAMP bypassed via an external GIT_DIR: {r.stdout} {r.stderr}")
        self.assertIn("no stamp", (r.stdout + r.stderr).lower())

    def test_owner_exclude_semantics_are_existence_based(self):
        # impl-panel r2 (opus #1/#2): the block is on the EXISTENCE of code-shaped
        # paths under an owner exclude — the fingerprint cannot see changes there,
        # so such a config is unverifiable until fixed — not on a post-F0 change.
        # Pin both shapes so a later "relaxation" cannot flip silently.
        for label, edit in (("stable excluded source, no edit", False),
                            ("changed excluded source", True)):
            with self.subTest(label):
                d, td, env = self._setup(risk="assertive", panel_cfg=["assertive"],
                                         change_after=False,
                                         tracked_files={"src/x.py": "v = 1\n"},
                                         extra_cfg={"fingerprint_exclude": ["src/"]})
                if edit:
                    (d / "src" / "x.py").write_text("v = 2\n", encoding="utf-8")
                r = self._close(d, env)
                self.assertNotIn("Task 001 done.", r.stdout, f"{label}: {r.stdout} {r.stderr}")
                self.assertIn("EXCLUDE-COVERS-CODE", r.stdout + r.stderr)

    @unittest.skipIf(os.name == "nt", "non-UTF-8 bytes are illegal in Windows filenames")
    def test_owner_exclude_override_survives_a_non_utf8_source_name(self):
        # impl-panel r2 (codex-high #4): the EXCLUDE-COVERS-CODE receipt/console
        # text carried surrogate-decoded raw names; the strict UTF-8 receipt write
        # raised UnicodeEncodeError on `--stale-panel-ok`, leaving the task pending.
        d, td, env = self._setup(risk="assertive", panel_cfg=["assertive"],
                                 change_after=False,
                                 tracked_files={"src/x.py": "v = 1\n"},
                                 extra_cfg={"fingerprint_exclude": ["src/"]})
        raw = os.path.join(os.fsencode(str(d)), b"src", b"a\xffb.py")
        try:
            with open(raw, "wb") as f:
                f.write(b"V = 1\n")
        except OSError:
            self.skipTest("filesystem rejects non-UTF-8 filenames")
        _git(d, "add", "-A")
        _git(d, "commit", "-qm", "raw name under the exclude")
        r = self._close(d, env)
        self.assertNotIn("Task 001 done.", r.stdout)
        r = self._close(d, env, "--stale-panel-ok", "--reason", "raw-named source reviewed by hand")
        self.assertIn("Task 001 done.", r.stdout, f"override crashed on a non-UTF-8 name: {r.stderr}")
        rec = self._receipt(td)
        self.assertIn("EXCLUDE-COVERS-CODE", rec)
        self.assertIn("src/a", rec)

    def test_failed_verify_does_not_spend_a_tail_cert_judge(self):
        # Task 073 finding C13: the close printed "[FAIL exit 1]" for the declared
        # verify, then STILL ran the paid single-judge tail certification, then
        # blocked on the failed verify anyway. A close that will block regardless
        # must not spend a judge.
        d, td, env = self._setup(risk="assertive", panel_cfg=["assertive"],
                                 change_after=False,
                                 tracked_files={"docs/note.md": "a\n"},
                                 extra_cfg={"verify": {"_always": ["false"]},
                                            "default_judge": "nosuchprovider"})
        (d / "docs" / "note.md").write_text("b\n", encoding="utf-8")
        r = self._close(d, env)
        self.assertNotIn("Task 001 done.", r.stdout)
        self.assertIn("verification failed", r.stdout + r.stderr)
        self.assertNotIn("tail certification", r.stdout + r.stderr,
                         "a tail-cert judge was attempted although verify had already failed")

    def test_owner_exclude_covering_only_docs_does_not_block(self):
        d, td, env = self._setup(risk="assertive", panel_cfg=["assertive"],
                                 change_after=False,
                                 tracked_files={"journal/log.md": "entry\n"},
                                 extra_cfg={"fingerprint_exclude": ["journal/"]})
        (d / "journal" / "log.md").write_text("entry 2\n", encoding="utf-8")
        r = self._close(d, env)
        self.assertIn("Task 001 done.", r.stdout, r.stderr)

    def test_unreadable_git_at_close_blocks_instead_of_reading_fresh(self):
        # T019 F4-outer end to end: a PASS panel stamped on a CLEAN tree, no code
        # change, then the index is corrupted before the close. Today the outer
        # material ignores rc, hashes HEAD + "" + "" → equal to the clean stamp →
        # FRESH close. With rc read the fingerprint is "" and the gate must block
        # with a verdict that names git, not "code changed".
        d, td, env = self._setup(risk="assertive", panel_cfg=["assertive"],
                                 change_after=False)
        idx = d / ".git" / "index"
        idx.write_bytes(idx.read_bytes()[:3])
        r = self._close(d, env)
        self.assertNotIn("Task 001 done.", r.stdout,
                         f"unreadable git at close must not read FRESH: {r.stdout} {r.stderr}")
        self.assertIn("UNREADABLE", r.stdout + r.stderr)
        r = self._close(d, env, "--stale-panel-ok", "--reason", "git index rebuilt by hand")
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        self.assertIn("UNREADABLE", self._receipt(td))

    def test_force_bypasses_and_attributes_reason_to_force(self):
        # Judge F5 / design A8: --force keeps whole-policy semantics; the
        # shared --reason lands as the FORCED reason, and the freshness clause
        # still records STALE (Leg 1 is unconditional).
        d, td, env = self._setup(risk="irreversible", panel_cfg="all")
        r = self._close(d, env, "--force", "--reason", "emergency close")
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        receipt = self._receipt(td)
        self.assertIn("Forced close, reason:", receipt)
        self.assertIn("emergency close", receipt)
        self.assertIn("STALE", receipt)
        self.assertNotIn('accepted: "emergency close"', receipt)


if __name__ == "__main__":
    unittest.main()
