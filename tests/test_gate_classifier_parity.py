#!/usr/bin/env python3
"""Cross-provider parity for the code-file gate classifier (F2, 1.5.17).

"No code without a task" must mean the SAME thing under every provider. Two
implementations decide what counts as a code file:

  * bash  `is_code_file_path`  in scripts/gate-echo-lib.sh  (the default Claude
    path — the PreToolUse task-gate-hook sources it)
  * Python `_is_code_file_path` in provider/policy.py       (the opt-in codex
    apply_patch gate, via provider.codex_hooks.apply_patch_pre_decision)

Before this test they disagreed in both directions: bash gated ~20 language
extensions codex didn't (a real HOLE — .php/.vue/.swift edits went ungated under
codex), and codex gated .css/.html/.sql/.yaml/.yml/.toml that bash didn't. The
invariant here is a PROPERTY over a fixture table: for every path,
    bash_is_code(path) == python_is_code(path) == expected
so a future edit to one list alone re-opens a parity gap loudly.

The one deliberate asymmetry (NOT tested for agreement): the bash hook adds a
shebang check for an extensionless EXISTING file — it can read the working tree,
the codex pre-decision sees a patch and not always a file. Every vector here is
a NON-existent path, so bash's shebang branch never fires and the comparison is
pure string classification.

Pure stdlib unittest. Run: python3 -m unittest tests.test_gate_classifier_parity
"""
import subprocess
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN = _HERE.parent / "plugins" / "playbook"
sys.path.insert(0, str(PLUGIN))
from provider.policy import _is_code_file_path  # noqa: E402

GATE_LIB = PLUGIN / "scripts" / "gate-echo-lib.sh"

# (path, expected_is_code). Paths must NOT exist on disk (see module docstring).
VECTORS = [
    # Language extensions — gated on BOTH now (codex used to miss most of these).
    ("src/main.py", True), ("app.ts", True), ("a.jsx", True), ("m.go", True),
    ("lib.rs", True), ("app.php", True), ("widget.vue", True), ("c.svelte", True),
    ("Main.swift", True), ("K.kt", True), ("s.scala", True), ("q.zig", True),
    ("u.lua", True), ("mod.ex", True), ("t.exs", True), ("o.ml", True),
    ("infra.tf", True), ("obj.mm", True), ("x.R", True), ("n.ipynb", True),
    ("h.hpp", True), ("d.dart", True), ("p.cs", True),
    # Config / markup — STRICT: gated on both (bash used to allow these).
    ("styles.css", True), ("index.html", True), ("schema.sql", True),
    ("config.yaml", True), ("deploy.yml", True), ("pyproject.toml", True),
    # Docs / data / binaries — never code, even inside a code dir.
    ("README.md", False), ("data.json", False), ("logo.png", False),
    ("notes.txt", False), ("icon.svg", False), ("photo.jpeg", False),
    ("src/README.md", False), ("scripts/notes.md", False), ("x/lib/data.csv", False),
    ("poetry.lock", False),
    # Undecided extension / extensionless -> code iff a code-dir component.
    ("src/app.ini", True), ("cmd/tool.env", True), ("src/deploy", True),
    ("lib/util.cfg", True), ("hooks/pre", True),
    ("tool.unknownext", False), ("Makefile", False), (".gitignore", False),
    ("config/app.ini", False), ("a/mysrc/b.xyz", False),
    # Case-insensitive extension match (parity with Python's .lower()).
    ("STYLES.CSS", True), ("a/b/c.JSON", False), ("X.Py", True),
    # Backslash paths normalize to forward slashes on both.
    ("src\\main.py", True), ("data\\notes.md", False),
    # 1.5.20 extension additions (strict-consistent: preprocessors/modules/schema).
    ("app.mjs", True), ("m.cjs", True), ("style.scss", True), ("style.less", True),
    ("api.proto", True), ("q.graphql", True), ("build.gradle", True),
    # 1.5.20 leading-dot parity: a dots-then-name basename has NO extension on
    # both surfaces (matches Python os.path.splitext), so it falls to the dir
    # rule — these used to diverge (bash saw ".py"/".toml", python saw none).
    ("..py", False), ("...toml", False), ("weird/...css", False),
    ("src/...css", True), ("bin/..js", True),
]


def bash_is_code(path: str) -> bool:
    r = subprocess.run(
        ["bash", "-c", f"source '{GATE_LIB.as_posix()}' && is_code_file_path \"$1\"", "_", path],
        capture_output=True, text=True)
    return r.returncode == 0


class GateClassifierParity(unittest.TestCase):
    def test_bash_and_python_agree_with_expected(self):
        for path, expected in VECTORS:
            with self.subTest(path=path):
                b = bash_is_code(path)
                p = _is_code_file_path(path)
                self.assertEqual(b, p, f"bash/python disagree on {path!r}: bash={b} py={p}")
                self.assertEqual(p, expected, f"{path!r}: expected is_code={expected}, got {p}")

    def test_table_is_not_trivially_one_sided(self):
        # Negative control: a table that was all-True (or all-False) could pass a
        # broken classifier. Assert both outcomes are well represented.
        vals = [e for _, e in VECTORS]
        self.assertGreater(sum(vals), 10, "too few code vectors")
        self.assertGreater(len(vals) - sum(vals), 10, "too few non-code vectors")


if __name__ == "__main__":
    unittest.main(verbosity=2)
