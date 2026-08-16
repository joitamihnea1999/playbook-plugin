"""I15 + I19 (verification-report-1.5.9): skill docs must describe surfaces that
actually ship and mechanisms that actually run.

I15: the judge skill's only mechanism was `Task(...)`, which `init` puts on
`permissions.deny` — so the skill couldn't run in an initialized project and
never named the sanctioned CLI path.

I19: the monitor skill documented `monitor.py`, `.pid`, `/monitor off`, and a
`pids/<sid>/` layout — none of which ship (launch-monitor + bootstrap.sh +
sensor.py + a flat `<agent-dir>/monitor/` do). An agent following it ran
nonexistent files.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN = REPO_ROOT / "plugins" / "playbook"
JUDGE = PLUGIN / "skills" / "judge" / "SKILL.md"
MONITOR = PLUGIN / "skills" / "monitor" / "SKILL.md"
MONITOR_LIB = PLUGIN / "scripts" / "monitor-lib"
CONFIG_DOC = REPO_ROOT / "docs" / "configuration.md"
CMD_PLAYBOOK = PLUGIN / "commands" / "playbook.md"
PLAYBOOKS_README = PLUGIN / "scripts" / "playbooks-README.md"


class DocDriftFixed(unittest.TestCase):
    """1.5.11-audit doc drifts stay closed."""

    def test_configuration_alias_example_is_on_schema(self):
        text = CONFIG_DOC.read_text(encoding="utf-8")
        # The alias VALUE must be a list, not a bare string the parser drops.
        self.assertIn('"aliases": {"opus": ["claude"', text,
                      "configuration.md alias example is off-schema (copy-paste trap)")

    def test_commands_playbook_uses_namespaced_init_and_lists_fix(self):
        text = CMD_PLAYBOOK.read_text(encoding="utf-8")
        self.assertIn("/playbook:init", text)
        self.assertNotIn("Run `/init`", text, "bare /init collides with the builtin")
        self.assertIn("**Fix**", text, "Fix pattern missing from commands/playbook.md")

    def test_playbooks_readme_has_no_ghost_src_path(self):
        text = PLAYBOOKS_README.read_text(encoding="utf-8")
        self.assertNotIn("src/tasks/template.py", text,
                         "playbooks-README references a non-existent src/ path")


class JudgeSkillCliPath(unittest.TestCase):
    def test_names_the_deny_caveat_and_cli_path(self):
        text = JUDGE.read_text(encoding="utf-8")
        self.assertIn("permissions.deny", text,
                      "judge skill never warns that Task is on the deny-list (I15)")
        self.assertIn("plan-review", text,
                      "judge skill never names the sanctioned vendor-judge CLI (I15)")


class MonitorSkillMatchesReality(unittest.TestCase):
    def test_shipped_monitor_files_exist(self):
        for name in ("launch-monitor", "bootstrap.sh", "sensor.py"):
            self.assertTrue((MONITOR_LIB / name).exists(),
                            f"monitor-lib is missing {name}")

    def test_skill_references_the_real_launcher_not_a_ghost(self):
        text = MONITOR.read_text(encoding="utf-8")
        self.assertIn("launch-monitor", text,
                      "monitor skill doesn't name the real launcher (I19)")
        self.assertIn(".claude/bin/monitor start", text,
                      "monitor skill doesn't give the real start command (I19)")

    def test_skill_does_not_present_ghost_files_as_real(self):
        # A ghost file may only appear in a NEGATION ("there is no monitor.py").
        # Any other mention is drift. `monitor.py` and `pids/<...>/` were the
        # ghosts the report named.
        for line in MONITOR.read_text(encoding="utf-8").splitlines():
            for ghost in ("monitor.py", "pids/", "/monitor off", "sandbox.sb"):
                if ghost in line and not re.search(r"\b(no|not|there is no)\b", line):
                    self.fail(f"monitor skill presents ghost {ghost!r} as real: {line!r}")


if __name__ == "__main__":
    unittest.main()
