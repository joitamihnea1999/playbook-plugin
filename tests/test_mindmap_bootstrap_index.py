#!/usr/bin/env python3
"""Point tests for the bootstrap INDEX loader (`_bootstrap_mind_map`).

The problem it fixes: `tasks bootstrap` dumped the whole MIND_MAP.md (bounded
only by `_load_mind_map`'s 25000-char trim) into context every session — for a
50-node map that is thousands of resident tokens of subsystem prose a given
task never reads. And when the 25k trim DID bind, it named omitted nodes by
bare id (`[47]`), so the agent could not tell which omitted node to fetch.

So the invariants are: a small map is returned byte-for-byte (no round-trips
worth saving); a large map returns the first `routing` nodes in FULL plus a
one-line TITLED TOC of every other node (title, not bare id — that is what
lets the agent grep the right one) plus the recovery grep; the index is much
smaller than the full map; and the judge path (`_load_mind_map`) is untouched.

Pure stdlib unittest (honors the stdlib-only invariant).

Run: python3 tests/test_mindmap_bootstrap_index.py   (from claude-playbook-plugin/)
"""
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "plugins/playbook"))
from tasks.mindmap import (  # noqa: E402
    _bootstrap_mind_map, _mind_map_toc, _node_title, _load_mind_map,
)

PREAMBLE = (
    "# Mind Map — testproj\n\n"
    "> **For AI Agents:** read routing nodes [1]-[5] first, then follow [N] links.\n\n"
)
NODE_RE = re.compile(r"^\[(\d+)\]", re.MULTILINE)


def node(nid: int, title: str = "", chars: int = 400) -> str:
    """A node in the real shape: `[N] **Title** - one long line`, then blank."""
    title = title or f"Subsystem {nid}"
    body = f"prose-for-node-{nid} " * ((chars // 20) + 1)
    return f"[{nid}] **{title}** - {body[:chars]}\n\n"


def map_of(ids, node_chars: int = 400, preamble: str = PREAMBLE) -> str:
    return preamble + "".join(node(i, chars=node_chars) for i in ids)


def ids_in(text: str) -> list[int]:
    return [int(m) for m in NODE_RE.findall(text)]


class TestNodeTitle(unittest.TestCase):
    def test_bold_title(self):
        self.assertEqual(_node_title("[7] **Sandbox Containment** - prose here\n"),
                         (7, "Sandbox Containment"))

    def test_no_bold_falls_back_to_pre_dash_text(self):
        self.assertEqual(_node_title("[7] Sandbox containment - prose here\n"),
                         (7, "Sandbox containment"))

    def test_no_bold_no_dash_is_capped(self):
        nid, title = _node_title("[7] " + "x" * 200 + "\n")
        self.assertEqual(nid, 7)
        self.assertEqual(len(title), 60)


class TestToc(unittest.TestCase):
    def test_one_titled_line_per_node(self):
        src = map_of([1, 2, 3])
        toc = _mind_map_toc(src)
        self.assertEqual(
            toc, "[1] Subsystem 1\n[2] Subsystem 2\n[3] Subsystem 3")

    def test_none_when_no_nodes(self):
        self.assertIsNone(_mind_map_toc("just prose, no [N] markers\n"))

    def test_fenced_node_is_not_indexed(self):
        src = (PREAMBLE + node(1) + "```\n[9] example, not a node\n```\n\n" + node(2))
        toc = _mind_map_toc(src)
        self.assertIn("[1] Subsystem 1", toc)
        self.assertIn("[2] Subsystem 2", toc)
        self.assertNotIn("[9]", toc)

    def test_none_on_open_fence(self):
        src = PREAMBLE + node(1) + "```\n" + node(9)
        self.assertIsNone(_mind_map_toc(src))


class TestBootstrapLoader(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.pop("PLAYBOOK_MINDMAP_MAX", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()
        if self._env is None:
            os.environ.pop("PLAYBOOK_MINDMAP_MAX", None)
        else:
            os.environ["PLAYBOOK_MINDMAP_MAX"] = self._env

    def _write(self, content: str) -> None:
        (self.root / "MIND_MAP.md").write_text(content, encoding="utf-8")

    def test_missing_file(self):
        self.assertIsNone(_bootstrap_mind_map(self.root))

    def test_small_map_returned_whole(self):
        content = map_of([1, 2, 3], node_chars=300)
        self.assertLess(len(content), 8000)
        self._write(content)
        self.assertEqual(_bootstrap_mind_map(self.root), content)

    def test_large_map_is_indexed(self):
        # 40 nodes of ~400 chars ≈ 18 KB — comfortably over the 8000 budget.
        content = map_of(range(1, 41), node_chars=400)
        self.assertGreater(len(content), 8000)
        self._write(content)
        out = _bootstrap_mind_map(self.root)

        # Routing nodes [1]-[5] present IN FULL (their prose survives).
        for nid in range(1, 6):
            self.assertIn(f"prose-for-node-{nid} ", out, f"routing node [{nid}] not full")
        # A late node: TITLE indexed, PROSE absent — the whole point.
        self.assertIn("[37] Subsystem 37", out)
        self.assertNotIn("prose-for-node-37 ", out)
        # The index announces itself and how to recover a node.
        self.assertIn("MIND MAP INDEX", out)
        self.assertIn("[1]-[5]", out)
        self.assertIn("grep '^\\[N\\]' MIND_MAP.md", out)
        # Every node is still reachable: routing (full) + indexed (titled) == all.
        self.assertEqual(sorted(set(ids_in(out))), list(range(1, 41)))

    def test_index_is_far_smaller_than_the_full_dump(self):
        content = map_of(range(1, 41), node_chars=400)
        self._write(content)
        out = _bootstrap_mind_map(self.root)
        self.assertLess(len(out), len(content) // 2)

    def test_env_zero_suppresses(self):
        self._write(map_of(range(1, 41)))
        os.environ["PLAYBOOK_MINDMAP_MAX"] = "0"
        self.assertIsNone(_bootstrap_mind_map(self.root))

    def test_env_negative_suppresses(self):
        self._write(map_of(range(1, 41)))
        os.environ["PLAYBOOK_MINDMAP_MAX"] = "-1"
        self.assertIsNone(_bootstrap_mind_map(self.root))

    def test_few_fat_nodes_over_budget_fall_back_not_indexed(self):
        # Fewer nodes than `routing`, but over budget: a handful of nodes IS the
        # overview — nothing to index — so defer to the whole-node trim.
        content = map_of([1, 2, 3], node_chars=6000)
        self.assertGreater(len(content), 8000)
        self._write(content)
        out = _bootstrap_mind_map(self.root)
        self.assertNotIn("MIND MAP INDEX", out)
        self.assertEqual(out, _load_mind_map(self.root))

    def test_non_node_map_falls_back_without_crashing(self):
        prose = "# notes\n\n" + "".join(f"line {i} " + "x" * 200 + "\n" for i in range(200))
        self.assertGreater(len(prose), 8000)
        self._write(prose)
        out = _bootstrap_mind_map(self.root)
        self.assertNotIn("MIND MAP INDEX", out)
        self.assertEqual(out, _load_mind_map(self.root))

    def test_real_fixture_indexes_every_non_routing_node_by_title(self):
        fixture = _HERE / "fixtures" / "airingvet_fcac0d6_MAIN.md"
        src = fixture.read_text(encoding="utf-8")
        self.assertGreater(len(src), 8000)
        self._write(src)
        out = _bootstrap_mind_map(self.root)
        self.assertIn("MIND MAP INDEX", out)
        all_ids = ids_in(src)
        # Every node id is still referenced (routing full + rest titled).
        self.assertEqual(sorted(set(ids_in(out))), sorted(set(all_ids)))
        # And the index really is smaller than the source.
        self.assertLess(len(out), len(src))


if __name__ == "__main__":
    unittest.main(verbosity=2)
