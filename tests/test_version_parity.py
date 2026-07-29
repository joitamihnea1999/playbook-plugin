"""The version is written in more than one place, so a test has to hold them equal.

`plugins/playbook/.claude-plugin/plugin.json` is what Claude Code reads to name
the installed plugin; `plugins/playbook/tasks/core.py:VERSION` is what the CLI
reports. Nothing derives one from the other — a release bumps both by hand.

Until now the only check was `tasks doctor`, which (a) lives outside the suite,
(b) only compares them when run from a working copy, and (c) nobody is obliged
to run. The evidence that hand-maintenance fails silently is in the repo:
`scripts/lib/tasks/core.py` has read `VERSION = "1.4.1"` for four releases.
That copy is dead code for this fork and parked for deletion, so it is asserted
as *known-stale* rather than equal — if someone revives that tree, this test
tells them it needs a version policy first.

Written for task 026, whose whole subject was a version number that had drifted
from what it labelled.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLAYBOOK = REPO_ROOT / "plugins" / "playbook"
MANIFEST = PLAYBOOK / ".claude-plugin" / "plugin.json"
CORE = PLAYBOOK / "tasks" / "core.py"
DEAD_MIRROR_CORE = PLAYBOOK / "scripts" / "lib" / "tasks" / "core.py"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
README = REPO_ROOT / "README.md"

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
VERSION_ASSIGN = re.compile(r'^VERSION\s*=\s*"([^"]+)"', re.MULTILINE)


def manifest_version() -> str:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))["version"]


def core_version(path: Path) -> str:
    m = VERSION_ASSIGN.search(path.read_text(encoding="utf-8"))
    assert m, f"no VERSION assignment in {path}"
    return m.group(1)


class TestVersionParity(unittest.TestCase):
    def test_manifest_and_cli_agree(self):
        self.assertEqual(
            manifest_version(), core_version(CORE),
            f"{MANIFEST.relative_to(REPO_ROOT)} and {CORE.relative_to(REPO_ROOT)} "
            f"disagree. A release must bump BOTH — `.claude/bin/tasks` execs the "
            f"installed plugin, so only `tasks doctor` run from a working copy "
            f"would otherwise catch this.",
        )

    def test_version_is_semver(self):
        for label, v in (("plugin.json", manifest_version()), ("core.py", core_version(CORE))):
            with self.subTest(source=label):
                self.assertRegex(v, SEMVER, f"{label} version {v!r} is not X.Y.Z")

    def test_changelog_documents_the_current_version(self):
        """A bump without a changelog entry is how a release becomes untraceable.

        Task 026 exists because two different code states shipped under one
        version; the entry is what tells a user which one they have.
        """
        version = manifest_version()
        self.assertIn(
            f"## [{version}]", CHANGELOG.read_text(encoding="utf-8"),
            f"CHANGELOG.md has no `## [{version}]` section — either the bump is "
            f"undocumented or the entry was written under a different heading",
        )

    def test_readme_audit_stamp_matches_the_current_version(self):
        """The stamp claims which version the docs were audited against."""
        stamp = re.search(r"<!-- readme-audit: v(\S+) @ (\S+) -->",
                          README.read_text(encoding="utf-8"))
        self.assertIsNotNone(stamp, "README.md has lost its readme-audit stamp")
        self.assertEqual(
            stamp.group(1), manifest_version(),
            "the README audit stamp names a different version than plugin.json — "
            "a version bump has to carry the stamp with it, or the docs advertise "
            "an audit of a version that no longer exists",
        )

    def test_dead_mirror_is_known_stale_not_silently_diverging(self):
        """Documents a divergence instead of pretending it isn't there.

        `scripts/lib/tasks/` is unreachable for this fork (the Codex hooks
        bootstrap from `scripts/lib/provider/`) and is parked for deletion, so
        its VERSION is deliberately NOT kept in sync. This test fails if it ever
        changes — at which point the tree is being maintained again and needs to
        join `test_manifest_and_cli_agree` instead.
        """
        if not DEAD_MIRROR_CORE.exists():
            self.skipTest("mirror deleted — the parked cleanup happened")
        self.assertEqual(
            core_version(DEAD_MIRROR_CORE), "1.4.1",
            "the dead scripts/lib/tasks/ mirror's VERSION moved. Either it is "
            "live again (then include it in the parity assertion) or it should be "
            "deleted, but it must not drift quietly.",
        )


if __name__ == "__main__":
    unittest.main()
