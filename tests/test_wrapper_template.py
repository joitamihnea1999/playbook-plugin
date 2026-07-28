"""The wrapper template's heredocs must survive a strict `$( )` parser.

`create_wrapper` in `gate-echo-lib.sh` builds `.claude/bin/<name>` from a
heredoc inside a command substitution. Git Bash / MSYS bash 5.2 terminates a
heredoc at a body line that merely *starts with* the delimiter — it does not
require an exact match — so a delimiter that is a prefix of any body line cuts
the template short. The body's own `WRAPPER_DIR="$(…)"` line did exactly that
to the old `WRAPPER` delimiter: every wrapper regenerated on Windows was six
lines long instead of ninety.

That is a severe silent failure, because `session-start-hook` regenerates
`tasks`, `sandbox` and all four `playbook-*` wrappers on EVERY session start —
so the truncation takes out `.claude/bin/tasks`, the CLI that arms the gate
hook. Reported by cristi (ai-ring-vet, Git Bash MSYS 5.2.26) on 2026-07-21.

The bug does not reproduce on a permissive parser (macOS bash 3.2 captures the
body fine either way), so a platform-specific behavioral test would pass here
and rot. These tests pin the *structural* invariant instead, which holds on
every platform:

    no line of a heredoc body may start with that heredoc's own delimiter

It is deliberately scope-aware. The wrapper body contains a NESTED heredoc
(`<<'PYRESOLVE'`), whose terminator is a legitimate body line starting with
`PYRESOLVE`. A naive "no line starts with any delimiter" check would flag it,
be judged noise, and get deleted.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "plugins" / "playbook" / "scripts"
GATE_ECHO_LIB = SCRIPTS / "gate-echo-lib.sh"

# All the forms bash accepts, because the MSYS prefix-termination bug does not
# care whether the delimiter was quoted: `<<EOF`, `<<'EOF'`, `<<"EOF"`, each
# optionally `<<-` and optionally with space after the operator. Restricting
# this to the quoted form (the first version did) made the "repo-wide" check
# skip most of the shipped heredocs — `task-gate-hook`, `state-echo-hook`,
# `stop-hook`, `monitor-lib/bootstrap.sh` and `playbook-pi` all use `<<EOF`.
HEREDOC_OPEN = re.compile(
    r"""<<(?P<dash>-?)\s*(?:'(?P<sq>[A-Za-z_][A-Za-z0-9_]*)'"""
    r"""|"(?P<dq>[A-Za-z_][A-Za-z0-9_]*)\""""
    r"""|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))"""
)


def _delimiter(match: re.Match) -> tuple[str, bool]:
    """(delimiter, tab_stripping_allowed) for a heredoc-open match."""
    name = match.group("sq") or match.group("dq") or match.group("bare")
    return name, bool(match.group("dash"))


def heredoc_blocks(text: str):
    """Yield (delimiter, [body lines]) for each heredoc, innermost-aware.

    Walks line by line keeping a stack of open delimiters, so a nested heredoc
    is attributed to itself rather than to its parent.

    Terminator matching follows bash exactly: the delimiter must sit at column
    zero, and leading TABS are stripped only when the heredoc was opened with
    `<<-`. The first version compared `line.strip() == delim`, which is wrong in
    the unsafe direction — an indented line equal to the delimiter closed the
    heredoc early, so every body line after it went unscanned and the invariant
    check went quietly vacuous over exactly the region it is meant to police.
    """
    # stack entries: (delimiter, body_lines, tabs_stripped)
    stack: list[tuple[str, list[str], bool]] = []
    for line in text.splitlines():
        if stack:
            delim, _, tabs_stripped = stack[-1]
            candidate = line.lstrip("\t") if tabs_stripped else line
            if candidate == delim:
                closed_delim, closed_body, _ = stack.pop()
                # The terminator belongs to any STILL-open (outer) heredoc as
                # ordinary text — bash would cut the outer one here too if this
                # line started with the outer delimiter — so attribute it upward.
                for _, body, _ in stack:
                    body.append(line)
                yield closed_delim, closed_body
                continue
        for _, body, _ in stack:
            body.append(line)
        opened = HEREDOC_OPEN.search(line)
        if opened:
            name, dash = _delimiter(opened)
            stack.append((name, [], dash))
    for delim, body, _ in stack:
        yield delim, body


class TestHeredocDelimiterInvariant(unittest.TestCase):
    def assert_no_delimiter_prefix(self, text: str, source: str) -> int:
        checked = 0
        for delim, body in heredoc_blocks(text):
            checked += 1
            for lineno, line in enumerate(body, start=1):
                self.assertFalse(
                    line.startswith(delim),
                    f"{source}: heredoc <<'{delim}' has a body line starting with "
                    f"its own delimiter (body line {lineno}: {line!r}). MSYS bash "
                    f"5.2 will terminate the heredoc there and truncate the output. "
                    f"Rename the delimiter so it is not a prefix of any body line.",
                )
        return checked

    def test_gate_echo_lib_heredocs_are_safe(self):
        text = GATE_ECHO_LIB.read_text(encoding="utf-8")
        checked = self.assert_no_delimiter_prefix(text, "gate-echo-lib.sh")
        # Guard against the parser silently finding nothing to check.
        self.assertGreaterEqual(checked, 2, "expected at least the wrapper + PYRESOLVE heredocs")

    def test_every_shipped_heredoc_is_safe(self):
        """The invariant is cheap, so hold it repo-wide rather than at one site.

        The floor assertion counts parsed BLOCKS, not files. Counting files was
        the earlier bug: a file containing `<<` counted as "checked" even when
        the pattern matched none of its heredocs, so a regex that recognised
        nothing would still have reported broad coverage.
        """
        blocks = 0
        files_with_heredocs = 0
        for path in sorted(SCRIPTS.rglob("*")):
            if not path.is_file() or path.suffix not in ("", ".sh"):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if "<<" not in text:
                continue
            files_with_heredocs += 1
            blocks += self.assert_no_delimiter_prefix(
                text, str(path.relative_to(REPO_ROOT))
            )
        # Every file that mentions `<<` must yield at least one parsed block on
        # average; a regex that stopped matching a whole form would trip this.
        self.assertGreaterEqual(
            blocks, files_with_heredocs,
            f"parsed only {blocks} heredoc blocks across {files_with_heredocs} "
            f"files containing '<<' — the opener pattern is missing a form",
        )
        self.assertGreater(blocks, 10, f"suspiciously few heredocs parsed: {blocks}")

    def test_unquoted_heredocs_are_actually_scanned(self):
        """Guards the coverage gap directly: `<<EOF` must be seen.

        The shipped hooks use the unquoted form, and the MSYS prefix bug applies
        to it identically. The first version of this file matched only `<<'EOF'`
        while claiming repo-wide coverage.
        """
        sample = "cat <<EOF\nEOF_SUFFIX line\nEOF\n"
        blocks = dict(heredoc_blocks(sample))
        self.assertIn("EOF", blocks, "unquoted heredoc was not parsed at all")
        with self.assertRaises(AssertionError):
            self.assert_no_delimiter_prefix(sample, "sample")

    def test_terminator_must_be_at_column_zero(self):
        """An indented delimiter is body text, not a terminator (plain `<<`).

        If the walker closed here, everything after it would go unscanned —
        which is how the check would go vacuous over the region it polices.
        """
        sample = (
            "cat <<EOF\n"
            "    EOF\n"            # indented → NOT a terminator for plain <<
            "EOF_collision here\n"  # must still be scanned, and must be a finding
            "EOF\n"
        )
        blocks = dict(heredoc_blocks(sample))
        self.assertIn("    EOF", blocks["EOF"], "indented line was treated as terminator")
        self.assertIn("EOF_collision here", blocks["EOF"], "body after it went unscanned")
        with self.assertRaises(AssertionError):
            self.assert_no_delimiter_prefix(sample, "sample")

    def test_dash_heredoc_strips_only_tabs(self):
        """`<<-` strips leading TABS (not spaces) before matching."""
        tabbed = "cat <<-EOF\n\tbody\n\tEOF\n"
        self.assertEqual(dict(heredoc_blocks(tabbed))["EOF"], ["\tbody"])
        spaced = "cat <<-EOF\n    EOF\nEOF\n"
        self.assertIn("    EOF", dict(heredoc_blocks(spaced))["EOF"])

    def test_nested_pyresolve_terminator_is_not_flagged(self):
        """Scope-awareness check: PYRESOLVE's own terminator must not be a finding.

        If this ever fails, the parser has flattened the nesting and the
        invariant test would start reporting a false positive on correct code.
        """
        text = GATE_ECHO_LIB.read_text(encoding="utf-8")
        blocks = dict(heredoc_blocks(text))
        self.assertIn("PYRESOLVE", blocks)
        self.assertIn("END_WRAPPER_TEMPLATE", blocks)
        outer = blocks["END_WRAPPER_TEMPLATE"]
        self.assertTrue(
            any(line == "PYRESOLVE" for line in outer),
            "the outer body should still contain PYRESOLVE's terminator line",
        )

    def test_mutation_reintroducing_the_bug_is_caught(self):
        """The invariant test must not be vacuous.

        Rename the delimiter back to `WRAPPER` (a prefix of the body's
        `WRAPPER_DIR=` line) and assert the check goes red.
        """
        text = GATE_ECHO_LIB.read_text(encoding="utf-8")
        mutant = text.replace("END_WRAPPER_TEMPLATE", "WRAPPER")
        self.assertNotEqual(mutant, text, "mutation did not apply")
        with self.assertRaises(AssertionError):
            self.assert_no_delimiter_prefix(mutant, "mutant")


class TestGeneratedWrapperIsComplete(unittest.TestCase):
    """The truncation symptom itself.

    `wrapper-atomicity-fixture.sh` only asserts the wrapper is non-empty (task
    009's concern was a 0-byte file from a killed write). A six-line truncated
    wrapper passes that check while being useless, so assert the content.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _generate(self, lib: Path, name: str = "tasks") -> str:
        project = self.tmp / name
        (project / ".claude" / "bin").mkdir(parents=True)
        (project / ".agent" / "tasks").mkdir(parents=True)
        script = f'source "{lib}"\ncreate_wrapper "{project}" {name}\n'
        proc = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        self.assertEqual(proc.returncode, 0, f"create_wrapper failed: {proc.stderr}")
        return (project / ".claude" / "bin" / name).read_text(encoding="utf-8")

    def test_wrapper_contains_its_whole_template(self):
        content = self._generate(GATE_ECHO_LIB)
        self.assertTrue(content.startswith("#!/bin/bash"), "missing shebang")
        self.assertIn("# playbook-managed", content)
        # The line that truncated the old heredoc — it must be present…
        self.assertIn('WRAPPER_DIR="$(cd "$(dirname "$0")" && pwd -P)"', content)
        # …along with everything AFTER it, which is what got cut.
        self.assertIn("import glob, json, os, sys", content)
        self.assertIn("installed_plugins.json", content)
        self.assertTrue(
            content.rstrip().endswith('exec "$SCRIPT" "$@"'),
            f"wrapper does not end with its exec line — truncated?\n"
            f"last 3 lines: {content.rstrip().splitlines()[-3:]}",
        )
        # A truncated wrapper was ~6 lines; a whole one is ~90.
        self.assertGreater(len(content.splitlines()), 50)

    def test_wrapper_name_is_substituted(self):
        content = self._generate(GATE_ECHO_LIB, "sandbox")
        self.assertIn("/scripts/sandbox", content)
        self.assertNotIn("WRAPPER_NAME", content)

    def test_a_truncated_template_is_actually_detected(self):
        """Negative control for the completeness assertions above.

        The MSYS truncation cannot be reproduced on a permissive parser, so
        reproduce its OUTPUT instead: a lib whose heredoc closes right after the
        `WRAPPER_DIR=` line, which is exactly where the old delimiter was cut.
        The completeness checks must reject the result — otherwise they are
        asserting nothing and Windows breakage ships green again.
        """
        text = GATE_ECHO_LIB.read_text(encoding="utf-8")
        marker = 'WRAPPER_DIR="$(cd "$(dirname "$0")" && pwd -P)"\n'
        head, sep, tail = text.partition(marker)
        self.assertTrue(sep, "template marker line not found — test needs updating")
        # Close the heredoc immediately after the marker, then resume after the
        # real terminator so the rest of the library still parses.
        _, _, after = tail.partition("END_WRAPPER_TEMPLATE\n")
        mutant_text = head + marker + "END_WRAPPER_TEMPLATE\n" + after

        mutant_lib = self.tmp / "gate-echo-lib-truncated.sh"
        mutant_lib.write_text(mutant_text, encoding="utf-8")
        content = self._generate(mutant_lib, "tasks-trunc")

        self.assertNotIn("import glob, json, os, sys", content,
                         "mutation did not actually truncate the template")
        with self.assertRaises(AssertionError):
            self.assertTrue(content.rstrip().endswith('exec "$SCRIPT" "$@"'))
        with self.assertRaises(AssertionError):
            self.assertGreater(len(content.splitlines()), 50)


if __name__ == "__main__":
    unittest.main()
