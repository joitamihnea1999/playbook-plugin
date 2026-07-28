"""The provider mirror must stay byte-identical to the canonical tree.

`plugins/playbook/provider/` is canonical. `plugins/playbook/scripts/lib/provider/`
is a mirror that exists because the Codex hook scripts bootstrap their imports
from `scripts/lib/` — so the mirror is what actually executes on the enforcement
path, while unit tests import the canonical package.

That split is invisible until it bites: during task 022, mutating the canonical
`codex_hooks.py` left every hook-subprocess test passing (they ran the mirror),
and mutating only the mirror left every in-process test passing. A forgotten
sync therefore ships a working test suite and a broken gate.

This test is the guard. It also catches the easy miss: adding a NEW file to
`provider/` (task 022 added `paths.py`) and forgetting to mirror it.
"""

from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CANONICAL = REPO_ROOT / "plugins" / "playbook" / "provider"
MIRROR = REPO_ROOT / "plugins" / "playbook" / "scripts" / "lib" / "provider"

SYNC_CMD = (
    "rsync -a --delete --exclude='__pycache__' "
    "plugins/playbook/provider/ plugins/playbook/scripts/lib/provider/"
)


def _tracked_files(root: Path) -> dict[str, str]:
    """Map relative path → sha256, skipping build artifacts."""
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if "__pycache__" in rel.parts or path.suffix == ".pyc":
            continue
        out[str(rel)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


class TestProviderMirrorSync(unittest.TestCase):
    def setUp(self) -> None:
        self.canonical = _tracked_files(CANONICAL)
        self.mirror = _tracked_files(MIRROR)

    def test_both_trees_exist_and_are_non_empty(self):
        self.assertTrue(CANONICAL.is_dir(), f"missing canonical tree: {CANONICAL}")
        self.assertTrue(MIRROR.is_dir(), f"missing mirror tree: {MIRROR}")
        self.assertGreater(len(self.canonical), 0, "canonical provider tree is empty")

    def test_no_file_is_missing_from_the_mirror(self):
        missing = sorted(set(self.canonical) - set(self.mirror))
        self.assertEqual(
            missing, [],
            f"present in provider/ but not mirrored: {missing}\nSync with:\n  {SYNC_CMD}",
        )

    def test_mirror_has_no_extra_files(self):
        extra = sorted(set(self.mirror) - set(self.canonical))
        self.assertEqual(
            extra, [],
            f"stale files in the mirror: {extra}\nSync with:\n  {SYNC_CMD}",
        )

    def test_every_mirrored_file_is_byte_identical(self):
        differing = sorted(
            rel for rel, digest in self.canonical.items()
            if rel in self.mirror and self.mirror[rel] != digest
        )
        self.assertEqual(
            differing, [],
            f"content differs from provider/: {differing}\nSync with:\n  {SYNC_CMD}",
        )

    def test_paths_module_is_present_in_both(self):
        # The lane resolver is imported by codex_hooks, which runs from the
        # mirror — a missing copy breaks enforcement, not just a test.
        self.assertIn("paths.py", self.canonical)
        self.assertIn("paths.py", self.mirror)


if __name__ == "__main__":
    unittest.main()
