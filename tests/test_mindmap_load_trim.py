#!/usr/bin/env python3
"""Point tests for `_load_mind_map`'s over-budget trim (node-aware, not line-based).

The bug these pin: mind-map nodes are conventionally ONE long line each
(`^[N] **Title** - prose…`), so the old 60%-head/40%-tail trim cut on line
boundaries and therefore shed whole subsystem chapters instead of shedding prose
evenly — and reported only "N lines omitted", so nothing downstream could tell
which chapters were gone. Measured on a real 120,253-char / 19-node map: the
judge received 19,387 chars containing nodes [0], [17], [18] and nothing else,
while every judge prompt asserts "the MIND_MAP.md is provided".

So the invariants are: whole nodes only, node [0] always, every omitted id named,
and a mid-file node survives when the budget allows it (the case that failed).

Pure stdlib unittest (no hypothesis — honors the T135 stdlib-only invariant).

Run: python3 tests/test_mindmap_load_trim.py   (from claude-playbook-plugin/)
"""
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

# Import the helpers from the tasks package (cli.py guards its dispatch under __main__).
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "plugins/playbook"))
from tasks.cli import (  # noqa: E402
    _load_mind_map, _trim_mind_map_by_node, _trim_mind_map_by_lines,
)

PREAMBLE = (
    "# Mind Map — testproj\n\n"
    "> **For AI Agents:** read routing nodes [0]-[2] first, then follow [N] links.\n\n"
)
NODE_RE = re.compile(r"^\[(\d+)\]", re.MULTILINE)


def one_line_node(nid: int, chars: int = 6000) -> str:
    """A node in the real shape: one very long line, then a blank line."""
    body = f"prose-for-node-{nid} " * ((chars // 20) + 1)
    return f"[{nid}] **Subsystem {nid}** - {body[:chars]}\n\n"


def map_of(n_nodes: int, node_chars: int = 6000, preamble: str = PREAMBLE) -> str:
    return preamble + "".join(one_line_node(i, node_chars) for i in range(n_nodes))


def ids_in(text: str) -> list[int]:
    return [int(m) for m in NODE_RE.findall(text)]


def omitted_ids_named(text: str) -> list[int]:
    """The ids the notice claims were dropped."""
    m = re.search(r"nodes omitted — ([^.]*)\.", text)
    return [int(x) for x in re.findall(r"\[(\d+)\]", m.group(1))] if m else []


class TestNodeAwareTrim(unittest.TestCase):
    """19 single-line nodes over budget — the reported shape."""

    def setUp(self):
        self.source = map_of(19)                      # ~114 KB, 19 nodes
        self.max_chars = 25000
        self.assertGreater(len(self.source), self.max_chars)
        self.out = _trim_mind_map_by_node(self.source, self.max_chars)
        self.node_lines = {
            nid: re.search(rf"(?m)^\[{nid}\] .*$", self.source).group(0)
            for nid in range(19)
        }

    def test_within_budget(self):
        self.assertLessEqual(len(self.out), self.max_chars)

    def test_only_whole_nodes(self):
        # Every node that appears at all appears in full — no half-chapters.
        for nid in ids_in(self.out):
            self.assertIn(self.node_lines[nid], self.out, f"node [{nid}] is partial")

    def test_node_zero_always_present(self):
        self.assertEqual(ids_in(self.out)[0], 0)
        self.assertIn(self.node_lines[0], self.out)

    def test_every_omitted_id_is_named(self):
        kept = set(ids_in(self.out))
        expected_omitted = [n for n in range(19) if n not in kept]
        self.assertEqual(omitted_ids_named(self.out), expected_omitted)
        # And the count in the notice matches the ids it lists.
        self.assertIn(f"{len(expected_omitted)} of 19 nodes omitted", self.out)

    def test_notice_says_the_map_is_incomplete_and_how_to_recover(self):
        # The judge prompts claim the map "is provided" — the text has to contradict
        # that itself, or the omission stays invisible.
        self.assertIn("NOT the full map", self.out)
        self.assertIn("grep '^\\[N\\]' MIND_MAP.md", self.out)

    def test_notice_precedes_the_nodes(self):
        # First thing after the header, so a reader learns the map is partial before
        # reading it as if it were whole.
        self.assertLess(self.out.index("MIND MAP TRIMMED"), self.out.index("\n[0] "))

    def test_preamble_kept(self):
        self.assertTrue(self.out.startswith(PREAMBLE.rstrip("\n")))

    def test_more_nodes_than_the_line_trim_delivered(self):
        # Regression scale check: the old trim shed 16 of 19 chapters at this budget.
        old = _trim_mind_map_by_lines(self.source, self.max_chars)
        self.assertGreater(len(ids_in(self.out)), len(ids_in(old)))
        self.assertNotIn("lines omitted", self.out)


class TestMidFileNodeSurvives(unittest.TestCase):
    """The regression: a mid-file node the budget can afford must land.

    On the reported shape (19 nodes of ~6 KB, 25 KB budget) the line-based trim
    delivered nodes [0], [1], [18] in 15,857 chars — it dropped node [2] while
    leaving 9 KB of the budget unspent, because snapping to a line boundary inside
    a 6 KB node throws away everything between the boundary and the budget. Node
    [2] is the assertion; the unspent budget is the reason.
    """

    def setUp(self):
        self.source = map_of(19, node_chars=6000)
        self.max_chars = 25000

    def test_mid_file_node_survives_when_budget_allows(self):
        out = _trim_mind_map_by_node(self.source, self.max_chars)
        kept = ids_in(out)
        self.assertLess(len(kept), 19, "budget must actually bind for this to test anything")
        self.assertIn(2, kept, f"mid-file node [2] fits the budget but was dropped; kept {kept}")

    def test_budget_is_not_left_unspent(self):
        out = _trim_mind_map_by_node(self.source, self.max_chars)
        old = _trim_mind_map_by_lines(self.source, self.max_chars)
        self.assertGreater(len(out), len(old))
        # Room for one more whole node means a node was dropped for no reason.
        smallest_node = min(len(one_line_node(n, 6000)) for n in range(19))
        self.assertLess(self.max_chars - len(out), smallest_node)

    def test_line_trim_is_still_the_thing_being_fixed(self):
        # Guards the comparison above from going vacuous: if someone rewires
        # `_trim_mind_map_by_lines` to be node-aware, this test says so out loud
        # rather than letting the regression above start passing for free.
        old = _trim_mind_map_by_lines(self.source, self.max_chars)
        self.assertNotIn(2, ids_in(old))
        self.assertNotIn("nodes omitted", old)

    def test_a_short_late_node_lands_even_after_a_fat_early_one(self):
        # `↗` nodes are short; a fat node early must not starve them.
        source = PREAMBLE + one_line_node(0, 800) + one_line_node(1, 40000) + one_line_node(2, 800)
        out = _trim_mind_map_by_node(source, 10000)
        self.assertEqual(ids_in(out), [0, 2])
        self.assertEqual(omitted_ids_named(out), [1])


class TestFallbackToLineTrim(unittest.TestCase):
    def test_no_node_markers_falls_back(self):
        prose = "".join(f"line {i} " + "x" * 200 + "\n" for i in range(400))
        self.assertIsNone(_trim_mind_map_by_node(prose, 5000))

    def test_unmatched_fence_falls_back_rather_than_guessing(self):
        # `_node_starts` reports an open fence; node boundaries are then unsafe.
        source = map_of(5, 6000) + "```\n" + one_line_node(9, 6000)
        self.assertIsNone(_trim_mind_map_by_node(source, 12000))

    def test_fenced_node_marker_is_not_a_node(self):
        # A `[9]` inside a closed fence is an example, not a chapter: it is neither
        # counted, nor omittable on its own — it travels with the node it sits in.
        fenced = "```\n[9] this is an example, not a node\n```\n\n"
        source = (
            PREAMBLE + one_line_node(0, 3000) + fenced
            + one_line_node(1, 3000) + one_line_node(2, 3000)
        )
        out = _trim_mind_map_by_node(source, 7000)
        self.assertIn("of 3 nodes omitted", out)          # 3 nodes, not 4
        self.assertNotIn(9, omitted_ids_named(out))
        self.assertIn(fenced, out)                        # kept with node [0]'s span


class TestPathologicalBudgets(unittest.TestCase):
    def test_budget_below_one_node_keeps_node_zero_and_says_it_was_cut(self):
        source = map_of(19, node_chars=6000)
        out = _trim_mind_map_by_node(source, 800)
        self.assertLessEqual(len(out), 800)
        self.assertIn("[0]", out)
        self.assertEqual(omitted_ids_named(out), list(range(1, 19)))

    def test_first_node_is_flagged_when_cut_mid_node(self):
        source = PREAMBLE + one_line_node(0, 6000) + one_line_node(1, 6000)
        out = _trim_mind_map_by_node(source, 1200)
        self.assertIn("CUT MID-NODE", out)
        self.assertLessEqual(len(out), 1200)

    def test_preamble_yields_to_the_routing_node(self):
        big_preamble = "# Mind Map\n\n> " + "header prose " * 400 + "\n\n"
        source = big_preamble + one_line_node(0, 1000) + one_line_node(1, 1000)
        out = _trim_mind_map_by_node(source, 2000)
        self.assertIn(0, ids_in(out))
        self.assertNotIn("header prose", out)
        # …and says so: the header carries the editing rules and the ownership map,
        # so dropping it silently is the same class of bug as dropping a node.
        self.assertIn("header (editing rules, ownership map) was dropped", out)

    def test_dropped_preamble_is_named_even_when_no_node_is_omitted(self):
        # The one case where the node list alone would report a complete map.
        big_preamble = "# Mind Map\n\n> " + "header prose " * 400 + "\n\n"
        source = big_preamble + one_line_node(0, 500)
        self.assertGreater(len(source), 3000)
        out = _trim_mind_map_by_node(source, 3000)
        self.assertEqual(ids_in(out), [0])
        self.assertEqual(omitted_ids_named(out), [])
        self.assertIn("was dropped", out)


class TestLoadMindMap(unittest.TestCase):
    """The public entry point, including the env override."""

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
        self.assertIsNone(_load_mind_map(self.root))

    def test_under_budget_is_byte_identical(self):
        content = map_of(3, node_chars=500)
        self._write(content)
        self.assertEqual(_load_mind_map(self.root), content)

    def test_over_budget_trims_by_node(self):
        self._write(map_of(19))
        out = _load_mind_map(self.root)
        self.assertLessEqual(len(out), 25000)
        self.assertIn("MIND MAP TRIMMED", out)
        self.assertEqual(ids_in(out)[0], 0)

    def test_env_override_zero_suppresses(self):
        self._write(map_of(19))
        os.environ["PLAYBOOK_MINDMAP_MAX"] = "0"
        self.assertIsNone(_load_mind_map(self.root))

    def test_env_override_changes_how_many_nodes_land(self):
        # Not a workaround for the old bug, but it must at least be honoured.
        self._write(map_of(19, node_chars=2000))
        os.environ["PLAYBOOK_MINDMAP_MAX"] = "8000"
        small = ids_in(_load_mind_map(self.root))
        os.environ["PLAYBOOK_MINDMAP_MAX"] = "24000"
        large = ids_in(_load_mind_map(self.root))
        self.assertLess(len(small), len(large))
        self.assertEqual(small[0], 0)

    def test_negative_env_override_suppresses_instead_of_slicing_backwards(self):
        self._write(map_of(19))
        os.environ["PLAYBOOK_MINDMAP_MAX"] = "-1"
        self.assertIsNone(_load_mind_map(self.root))

    def test_real_fixture_map(self):
        fixture = _HERE / "fixtures" / "airingvet_fcac0d6_MAIN.md"
        self._write(fixture.read_text(encoding="utf-8"))
        os.environ["PLAYBOOK_MINDMAP_MAX"] = "12000"
        out = _load_mind_map(self.root)
        self.assertLessEqual(len(out), 12000)
        kept = ids_in(out)
        self.assertEqual(kept, sorted(kept))
        named = omitted_ids_named(out)
        self.assertTrue(named)
        self.assertFalse(set(kept) & set(named))
        for nid in kept:
            line = re.search(rf"(?m)^\[{nid}\] .*$", fixture.read_text(encoding="utf-8")).group(0)
            self.assertIn(line, out, f"node [{nid}] is partial")


if __name__ == "__main__":
    unittest.main(verbosity=2)
