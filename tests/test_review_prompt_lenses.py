"""Judge prompts and the shipped task template must not drift between copies.

Two copies exist for each of these, and both are hand-maintained (there is no
build step):

  * `tasks/template.py` is canonical; `scripts/lib/tasks/template.py` is the
    mirror. The mirror as a whole HAS diverged (task 022's lane wording was
    never propagated), but the judge/review prompt region is the part that is
    kept in lockstep — a judge reached through one entry point must not get
    different review instructions than one reached through the other.
  * `scripts/base-template.md` and `skills/tasks/base-template.md` are two
    copies of one file; new tasks would otherwise differ depending on which
    surface loaded the template.

The hostile-sequence lens arrived as a contributed patch (cristi / ai-ring-vet,
2026-07-21) that had to be applied at four prompt sites per file. "Applied at
some of the sites" is the realistic failure, so assert the count, not presence.
"""

from __future__ import annotations

import ast
import importlib
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLAYBOOK = REPO_ROOT / "plugins" / "playbook"
CANONICAL = PLAYBOOK / "tasks" / "template.py"
MIRROR = PLAYBOOK / "scripts" / "lib" / "tasks" / "template.py"
TEMPLATE_COPIES = (
    PLAYBOOK / "scripts" / "base-template.md",
    PLAYBOOK / "skills" / "tasks" / "base-template.md",
)

# The four prompt builders the lens must appear in.
PROMPT_BUILDERS = (
    "plan_review_prompt",
    "impl_review_prompt",
    "panel_plan_review_prompt",
    "panel_impl_review_prompt",
)


def load_template_module(path: Path, name: str = ""):
    """Load one `tasks/template.py` copy, faithfully and in isolation.

    The mirror cannot be reached with a plain `from tasks import template`:
    both copies live in a package called `tasks`, so whichever parent directory
    is on sys.path first wins and the other never executes. Nor can it be
    loaded by file path alone — `template.py` does `from tasks.core import
    PLAYBOOKS`, so it needs its OWN package root importable.

    So: put this copy's package root first on sys.path, purge any already
    imported `tasks*` modules, import fresh, then restore both. The returned
    module keeps working after the purge (its globals are already bound), and
    the next call gets a clean slate — which is what makes "render the mirror"
    mean the mirror and not a cached canonical.

    Rendering the mirror is the point: count-and-substring checks pass on a
    mirror whose rendered numbering is broken.
    """
    package_root = path.parent.parent  # the dir containing the `tasks` package
    saved_path = list(sys.path)
    saved_modules = {
        key: mod for key, mod in sys.modules.items()
        if key == "tasks" or key.startswith("tasks.")
    }
    for key in saved_modules:
        del sys.modules[key]
    sys.path.insert(0, str(package_root))
    try:
        module = importlib.import_module("tasks.template")
        loaded_from = Path(module.__file__).resolve()
        assert loaded_from == path.resolve(), (
            f"loaded the wrong copy: wanted {path}, got {loaded_from}"
        )
        return module
    finally:
        sys.path[:] = saved_path
        for key in [k for k in sys.modules if k == "tasks" or k.startswith("tasks.")]:
            del sys.modules[key]
        sys.modules.update(saved_modules)


class TestHostileSequenceLens(unittest.TestCase):
    def test_present_at_all_four_sites_in_both_copies(self):
        for path in (CANONICAL, MIRROR):
            text = path.read_text(encoding="utf-8")
            self.assertEqual(
                text.count("Hostile sequences"), 4,
                f"{path.relative_to(REPO_ROOT)}: expected the lens at all 4 prompt "
                f"sites, found {text.count('Hostile sequences')}",
            )

    def test_each_named_builder_contains_the_lens(self):
        """Count alone can't tell you WHICH four sites got it."""
        for path in (CANONICAL, MIRROR):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            funcs = {
                n.name: ast.unparse(n)
                for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef)
            }
            for name in PROMPT_BUILDERS:
                self.assertIn(name, funcs, f"{path.name}: {name} missing")
                self.assertIn(
                    "Hostile sequences", funcs[name],
                    f"{path.relative_to(REPO_ROOT)}: {name} lacks the lens",
                )

    def test_prompts_do_not_advertise_a_stale_lens_count(self):
        """The prompt says how many lenses it has; adding one must update that.

        The contributed patch left "five lenses" in place while inserting a
        sixth — a prompt that contradicts its own list.
        """
        for path in (CANONICAL, MIRROR):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("five lenses", text, f"{path.name}: stale lens count")
            self.assertEqual(text.count("six lenses"), 4, f"{path.name}")

    def test_lenses_are_numbered_one_to_six_in_rendered_prompts(self):
        """Render the real prompts — in BOTH copies — and check the numbering.

        Rendering the mirror is not redundant: a mirror with a duplicated "(5)",
        a gap in the sequence, or a missing escape hatch satisfies every
        count-and-substring check above while handing judges a malformed prompt.
        """
        for label, path in (("canonical", CANONICAL), ("mirror", MIRROR)):
            module = load_template_module(path, f"_tmpl_{label}")
            for name in PROMPT_BUILDERS:
                rendered = getattr(module, name)("task.md")
                with self.subTest(copy=label, prompt=name):
                    # Scope the numbering check to the lens list: these prompts
                    # end with a SECOND numbered list (the "then edit task.md"
                    # instructions), so counting markers across the whole
                    # prompt would flag correct text.
                    self.assertIn("lenses:", rendered, f"{label}/{name}: no lens list")
                    lens_list = rendered.split("lenses:", 1)[1].split(
                        "Be specific and adversarial", 1
                    )[0]
                    for n in range(1, 7):
                        self.assertEqual(
                            lens_list.count(f"({n})"), 1,
                            f"{label}/{name}: lens ({n}) is not listed exactly once "
                            f"— a duplicated or missing number a range check would miss",
                        )
                    self.assertNotIn("(7)", lens_list, f"{label}/{name}: stray 7th lens")
                    self.assertIn("Hostile sequences", rendered)
                    # The escape hatch keeps the lens from forcing vacuous
                    # findings on the many tasks that touch no shared state.
                    self.assertIn("no shared or persisted state", rendered)


class TestPreReviewGateReachesRealTasks(unittest.TestCase):
    """The gate must be asserted where tasks are actually BORN.

    This class exists because the first version of these tests asserted only
    the two `base-template.md` files — and an impl panel proved those are read
    by no code at all (`grep -rn base-template` over the plugin returns
    nothing). `tasks new` renders `pre_review()` from `tasks/template.py` via
    `core.py`'s `render_template`. The markdown copies are documentation.

    So: assert the RENDERED output, and treat the .md files as docs that must
    not contradict it.
    """

    GATE = "update the OWNING subsystem node"
    OLD_GATE = "MIND_MAP.md updated if new insights emerged"

    def test_rendered_task_carries_the_gate_in_both_copies(self):
        for label, path in (("canonical", CANONICAL), ("mirror", MIRROR)):
            module = load_template_module(path, f"_tmpl_gate_{label}")
            with self.subTest(copy=label, surface="pre_review"):
                self.assertIn(self.GATE, module.pre_review())
                self.assertNotIn(self.OLD_GATE, module.pre_review())
            # Every task type that has a Pre-review section must carry it.
            for task_type in ("bugfix", "feature", "research", "review"):
                rendered = module.render_template(1, "probe", task_type)
                with self.subTest(copy=label, task_type=task_type):
                    self.assertIn("## Pre-review", rendered)
                    self.assertIn(self.GATE, rendered)
                    self.assertNotIn(self.OLD_GATE, rendered)

    def test_quick_template_is_deliberately_exempt(self):
        """`quick` has no Pre-review section at all — assert that, don't assume.

        If a future change gives the quick template a Pre-review section, this
        test fails and forces a decision instead of silently shipping the old
        wording there.
        """
        module = load_template_module(CANONICAL, "_tmpl_quick")
        rendered = module.render_quick_template(1, "probe")
        self.assertNotIn("## Pre-review", rendered)
        self.assertNotIn(self.OLD_GATE, rendered)

    def test_docs_do_not_contradict_the_runtime_gate(self):
        module = load_template_module(CANONICAL, "_tmpl_docs")
        runtime_line = next(
            line for line in module.pre_review().splitlines() if "MIND_MAP.md" in line
        )
        for path in TEMPLATE_COPIES:
            with self.subTest(path=path.name):
                self.assertIn(
                    runtime_line, path.read_text(encoding="utf-8"),
                    f"{path.name} documents a different gate than pre_review() emits",
                )

    def test_gate_text_names_nothing_unrunnable(self):
        """A template ships to every project — it must not name a script that
        only exists inside the plugin's merge skill, with no PATH entry and no
        `tasks` subcommand. (Five judges flagged exactly that.)"""
        module = load_template_module(CANONICAL, "_tmpl_runnable")
        gate = module.pre_review()
        for unrunnable in ("ref-integrity.py", "mindmap-sync"):
            self.assertNotIn(unrunnable, gate)


class TestTaskTemplateCopiesAgree(unittest.TestCase):
    def test_the_two_base_templates_are_identical(self):
        a, b = (p.read_text(encoding="utf-8") for p in TEMPLATE_COPIES)
        self.assertEqual(
            a, b,
            "scripts/base-template.md and skills/tasks/base-template.md have "
            "diverged — new tasks would differ depending on which surface "
            "loaded the template",
        )

    def test_mind_map_gate_names_the_owning_node_rule(self):
        """The vague version let every task append its own node to the map."""
        for path in TEMPLATE_COPIES:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("MIND_MAP.md updated if new insights emerged", text)
                self.assertIn("OWNING subsystem node", text)
                self.assertIn("never one node per task", text)


if __name__ == "__main__":
    unittest.main()
