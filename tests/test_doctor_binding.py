#!/usr/bin/env python3
"""Doctor must inspect the install it is RUNNING FROM, not a stray cache (F16).

Field evidence (StrataDB batch 4, task 010): `tasks doctor` reported 4 FAIL
(state-echo-hook / task-gate-hook "missing", gate-text-truncation, session-id
resolver) + 8 WARN (quote-wrapped commands in a grok marketplace-cache copy)
while those same hooks demonstrably enforced the whole session. Root cause:

  * the hooks/resolver checks hunted `~/.claude/plugins/**` by mtime and never
    considered the tree the running module itself belongs to — the one place
    guaranteed to be the code that is executing (the version check was already
    fixed to do exactly this, task 010; the hook checks were missed);
  * `hooks_check_report` scanned every copy any host might load (correct — a
    stale grok copy WAS the firing one in the AloVet bug) but never said which
    copy is the one this CLI runs from, so stray-cache noise was
    indistinguishable from a defect in the live install.

"A health check that cries wolf while the system works trains you to ignore
it" — the journal's words. These tests pin the fix: two install copies
present, doctor reports on the bound one; stray-copy findings are labeled as
such; and a defect in the AUTHORITATIVE copy still warns at full volume (the
negative control — labeling must not soften the real regression).

Run: python3 -m unittest tests.test_doctor_binding
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

from tasks.hooks_check import EXPECTED_HOOKS, authoritative_hooks_path  # noqa: E402

STRAY_MARKER = "not the one this CLI runs from"


def make_project() -> Path:
    tmp = tempfile.mkdtemp()
    proj = Path(tmp) / "proj"
    (proj / ".agent" / "tasks" / "001-x").mkdir(parents=True)
    (proj / ".agent" / "tasks" / "001-x" / "task.md").write_text(
        "# 001 - X\n\n## Status\npending\n", encoding="utf-8")
    return proj


def make_home(*, stray_claude_scripts: bool = False,
              grok_quote_wrapped: bool = False) -> Path:
    """A controlled $HOME so the test never reads the developer's real caches."""
    home = Path(tempfile.mkdtemp()) / "home"
    home.mkdir(parents=True)
    if stray_claude_scripts:
        # An install-cache shell with NO hook scripts inside: the mtime-glob
        # bait. Before the fix, doctor picked this as "the" scripts dir and
        # reported every hook missing.
        (home / ".claude" / "plugins" / "cache" / "mp" / "playbook" / "scripts").mkdir(parents=True)
    if grok_quote_wrapped:
        hooks_dir = home / ".grok" / "marketplace-cache" / "old" / "plugins" / "playbook" / "hooks"
        hooks_dir.mkdir(parents=True)
        obj = {"hooks": {
            ev: [{"hooks": [{"type": "command",
                             "command": f'"${{CLAUDE_PLUGIN_ROOT}}/scripts/{s}"'}]} for s in scripts]
            for ev, scripts in EXPECTED_HOOKS.items()
        }}
        (hooks_dir / "hooks.json").write_text(json.dumps(obj), encoding="utf-8")
    return home


def run_doctor(proj: Path, home: Path, extra_env: dict | None = None) -> str:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PLUGIN)
    env["HOME"] = str(home)
    # Path.home() reads USERPROFILE (not HOME) on Windows, so doctor's grok-cache
    # scan would otherwise search the developer's real profile and miss the
    # fixture. Point both at the controlled home. Harmless on POSIX (uses HOME).
    env["USERPROFILE"] = str(home)
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    env.update(extra_env or {})
    r = subprocess.run([sys.executable, "-m", "tasks.cli", "doctor"],
                       cwd=proj, env=env, capture_output=True, text=True, timeout=120)
    return r.stdout + r.stderr


class DoctorInspectsTheRunningInstall(unittest.TestCase):
    def test_hook_checks_resolve_the_running_tree_not_a_stray_cache(self):
        # Two copies present: the running tree (this repo, hooks intact) and a
        # newer-mtime stray cache with no hooks. Doctor must report on the one
        # it runs from.
        out = run_doctor(make_project(), make_home(stray_claude_scripts=True))
        self.assertIn("[PASS] hooks: state-echo-hook", out)
        self.assertIn("[PASS] hooks: task-gate-hook", out)
        self.assertIn("[PASS] hooks: gate text truncation", out)
        self.assertNotIn("gate-echo-lib.sh not found", out)

    def test_stray_copy_findings_are_labeled_as_not_the_running_install(self):
        out = run_doctor(make_project(), make_home(grok_quote_wrapped=True))
        # The stray grok cache's quote-wrap findings surface — but labeled.
        stray_lines = [l for l in out.splitlines()
                       if "marketplace-cache" in l and "quote-wrapped" in l]
        self.assertTrue(stray_lines, f"stray copy findings missing:\n{out}")
        for line in stray_lines:
            self.assertIn(STRAY_MARKER, line)
        # And the copy this CLI runs from reports no quote-wrap defect.
        own_hooks = str((PLUGIN / "hooks" / "hooks.json").resolve())
        self.assertFalse(
            [l for l in out.splitlines() if own_hooks in l and "quote-wrapped" in l],
            "the running install was reported quote-wrapped")

    def test_defect_in_the_authoritative_copy_still_warns_at_full_volume(self):
        # Negative control: labeling stray copies must not soften a REAL
        # regression in the copy the host actually loads (the AloVet bug).
        fixture = Path(tempfile.mkdtemp()) / "plugroot"
        (fixture / "scripts").mkdir(parents=True)
        for scripts in EXPECTED_HOOKS.values():
            for script in scripts:
                s = fixture / "scripts" / script
                s.write_text("#!/bin/bash\n# GATE_TEXT_STORE marker\n", encoding="utf-8")
                s.chmod(0o755)
        (fixture / "hooks").mkdir()
        obj = {"hooks": {
            ev: [{"hooks": [{"type": "command",
                             "command": f'"${{CLAUDE_PLUGIN_ROOT}}/scripts/{s}"'}]} for s in scripts]
            for ev, scripts in EXPECTED_HOOKS.items()
        }}
        (fixture / "hooks" / "hooks.json").write_text(json.dumps(obj), encoding="utf-8")

        out = run_doctor(make_project(), make_home(),
                         extra_env={"CLAUDE_PLUGIN_ROOT": str(fixture)})
        auth_lines = [l for l in out.splitlines()
                      if str(fixture) in l and "quote-wrapped" in l]
        self.assertTrue(auth_lines, f"authoritative defect not reported:\n{out}")
        for line in auth_lines:
            self.assertNotIn(STRAY_MARKER, line,
                             "a defect in the BOUND copy was softened as stray")


class AuthoritativePathResolution(unittest.TestCase):
    def test_env_plugin_root_wins_when_set(self):
        fixture = Path(tempfile.mkdtemp())
        (fixture / "hooks").mkdir(parents=True)
        (fixture / "hooks" / "hooks.json").write_text("{}", encoding="utf-8")
        p = authoritative_hooks_path(env={"CLAUDE_PLUGIN_ROOT": str(fixture)})
        self.assertEqual(p.resolve(), (fixture / "hooks" / "hooks.json").resolve())

    def test_falls_back_to_the_running_modules_own_tree(self):
        p = authoritative_hooks_path(env={})
        self.assertIsNotNone(p)
        self.assertEqual(p.resolve(), (PLUGIN / "hooks" / "hooks.json").resolve())


# ── PLAN S3 (task 086): the doctor names the plugin copies ───────────────────
import random  # noqa: E402
import shutil  # noqa: E402

from tasks import plugin_copies  # noqa: E402


def _git_ok(cwd, *args):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                          cwd=str(cwd), capture_output=True, text=True, check=True).stdout.strip()


class _CopiesFixture(unittest.TestCase):
    """PLAN S3 (task 086): `tasks doctor` compared a copy with itself and said PASS
    from every copy, while on a directory-marketplace machine the hooks run the
    checkout, the launcher runs the installed cache, and the doctor runs from
    wherever it was invoked. The report names each copy (path + version) and
    PASSes only when they are one release. Hermetic: a fake HOME, a real git repo
    as the directory marketplace, an installed copy, a project wrapper generated
    by the real `create_wrapper`."""

    VERSION = "9.9.1"
    BASE = "copies"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._rebuild(self.BASE)

    def _rebuild(self, base):
        self.root = Path(self._tmp.name) / base
        self.home = self.root / "home"
        self.plugins = self.home / ".claude" / "plugins"
        self.market = self.root / "market"
        self.hook = self.market / "plugins" / "playbook"
        self.project = self.root / "proj"
        self._build()

    # fixture ---------------------------------------------------------------
    def _write(self, path: Path, text: str, mode=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if mode:
            path.chmod(mode)

    def _plugin_tree(self, dest: Path, version: str):
        self._write(dest / ".claude-plugin" / "plugin.json", json.dumps({"name": "playbook", "version": version}))
        self._write(dest / "scripts" / "tasks", '#!/bin/sh\necho "$0"\n', 0o755)
        self._write(dest / "scripts" / "sandbox", "#!/bin/sh\nexit 0\n", 0o755)
        for name in ("gate-echo-lib.sh", "wrapper_resolver.py"):
            shutil.copy2(PLUGIN / "scripts" / name, dest / "scripts" / name)
        self._write(dest / "tasks" / "core.py", f'VERSION = "{version}"\n')

    def _build(self):
        self._write(self.market / ".claude-plugin" / "marketplace.json",
                    json.dumps({"plugins": [{"name": "playbook", "source": "./plugins/playbook"}]}))
        self._plugin_tree(self.hook, self.VERSION)
        _git_ok(self.market, "init", "-q")
        _git_ok(self.market, "add", "-A")
        _git_ok(self.market, "commit", "-qm", "release")
        self.sha = _git_ok(self.market, "rev-parse", "HEAD")
        self.installed = self.plugins / "cache" / "mk" / "playbook" / self.VERSION
        shutil.copytree(self.hook, self.installed)
        self.entries = {
            "playbook@mk": [{"scope": "user", "installPath": str(self.installed),
                             "version": self.VERSION, "gitCommitSha": self.sha,
                             "lastUpdated": "2026-09-24T00:00:00Z"}],
        }
        self._save_registry()
        self._write(self.plugins / "known_marketplaces.json", json.dumps({
            "mk": {"source": {"source": "directory", "path": str(self.market)},
                   "installLocation": str(self.market)}}))
        self.project.mkdir(parents=True)
        (self.project / ".agent" / "tasks").mkdir(parents=True)
        self._generate_wrapper(self.hook)

    def _save_registry(self, shuffle_seed=None):
        items = list(self.entries.items())
        if shuffle_seed is not None:
            random.Random(shuffle_seed).shuffle(items)
        self._write(self.plugins / "installed_plugins.json",
                    json.dumps({"version": 2, "plugins": dict(items)}))

    def _generate_wrapper(self, plugin_root: Path):
        from tests._bashcheck import bash_or_skip
        lib = plugin_root / "scripts" / "gate-echo-lib.sh"
        r = subprocess.run([bash_or_skip(), "-c", 'source "$1"; create_wrapper "$2" tasks', "_",
                            str(lib), str(self.project)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def _report(self, doctor_root=None):
        lines = plugin_copies.report(self.project, home=self.home,
                                     doctor_root=doctor_root or self.hook)
        return "\n".join(f"[{tag}] {text}" for tag, text in lines)

    def _verdict(self, out):
        v = [ln for ln in out.splitlines() if "plugin: copies agree" in ln]
        self.assertEqual(len(v), 1, f"expected exactly one verdict line in:\n{out}")
        return v


class PluginCopies(_CopiesFixture):
    """The 13 tests PLAN S3's Done names."""

    def test_names_hook_launcher_and_installed_copies_with_paths_and_versions(self):
        out = self._report()
        v, sha = self.VERSION, self.sha[:7]
        self.assertIn(f"] plugin: hook copy — {self.hook} v{v} (HEAD {sha})", out)
        self.assertIn(f"] plugin: launcher copy — {self.installed} v{v}", out)
        self.assertIn(f"] plugin: installed copy — {self.installed} v{v} (sha {sha})", out)
        self.assertIn(f"] plugin: doctor copy — {self.hook} v{v}", out)

    def test_passes_only_when_all_three_copies_agree(self):
        verdict = self._verdict(self._report())
        self.assertEqual(len(verdict), 1, verdict)
        self.assertTrue(verdict[0].startswith("[PASS]"), verdict)

    def test_warns_when_the_hook_copy_differs_from_the_installed_commit(self):
        self._write(self.hook / "tasks" / "later.py", "x = 1\n")
        _git_ok(self.market, "add", "-A")
        _git_ok(self.market, "commit", "-qm", "later")
        verdict = self._verdict(self._report())
        self.assertTrue(verdict[0].startswith("[WARN]"), verdict)
        self.assertIn(f"!= installed sha {self.sha[:7]}", verdict[0])

    def test_warns_when_the_hook_copy_is_dirty(self):
        (self.hook / "tasks" / "core.py").write_text('VERSION = "edited"\n', encoding="utf-8")
        verdict = self._verdict(self._report())
        self.assertTrue(verdict[0].startswith("[WARN]") and "uncommitted" in verdict[0], verdict)
        _git_ok(self.market, "checkout", "--", ".")
        self._write(self.hook / "untracked.txt", "new\n")
        verdict = self._verdict(self._report())
        self.assertTrue(verdict[0].startswith("[WARN]") and "uncommitted" in verdict[0], verdict)

    def test_custom_launcher_is_read_never_executed(self):
        sentinel = self.root / "SENTINEL"
        self._write(self.project / ".claude" / "bin" / "tasks",
                    f'#!/bin/sh\ntouch "{sentinel}"\n', 0o755)
        out = self._report()
        self.assertIn("launcher copy — custom launcher — not executed, target unknown", out)
        self.assertTrue(self._verdict(out)[0].startswith("[WARN]"))
        self.assertFalse(sentinel.exists(), "the doctor executed the project's launcher")

    def test_stale_managed_launcher_target_is_unknown(self):
        # an older managed wrapper whose embedded resolver would pick another copy
        other = self.plugins / "cache" / "mk" / "playbook" / "0.0.1"
        self._plugin_tree(other, "0.0.1")
        wrapper = self.project / ".claude" / "bin" / "tasks"
        wrapper.write_text("#!/bin/bash\n# playbook-managed — do not edit\n"
                           f'exec "{other}/scripts/tasks" "$@"\n', encoding="utf-8")
        out = self._report()
        self.assertIn("launcher copy — stale launcher — target unknown, not executed", out)
        self.assertNotIn(f"launcher copy — {other}", out)
        # impl panel round 2 (sol-high 2): the installed line says who chose it
        self.assertIn("chosen by this doctor's resolver; the project's launcher is stale", out)
        self.assertTrue(self._verdict(out)[0].startswith("[WARN]"))

    def test_warns_when_installed_content_differs_from_the_release(self):
        target = self.installed / "tasks" / "core.py"
        st = target.stat()
        data = target.read_bytes()
        target.write_bytes(data.replace(b"9", b"8", 1))          # same size
        os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))    # same mtime
        verdict = self._verdict(self._report())
        self.assertTrue(verdict[0].startswith("[WARN]"), verdict)
        self.assertIn("differs from the release: tasks/core.py", verdict[0])

    def test_warns_when_an_installed_file_is_missing(self):
        (self.installed / "tasks" / "core.py").unlink()
        verdict = self._verdict(self._report())
        self.assertTrue(verdict[0].startswith("[WARN]"), verdict)
        self.assertIn("missing from the install: tasks/core.py", verdict[0])

    def test_runtime_pycache_does_not_break_agreement(self):
        self._write(self.installed / "tasks" / "__pycache__" / "core.cpython-310.pyc", "\0bytecode")
        self._write(self.installed / "scripts" / "stray.pyc", "\0bytecode")
        verdict = self._verdict(self._report())
        self.assertTrue(verdict[0].startswith("[PASS]"), verdict)

    def test_warns_when_manifest_and_entry_versions_differ(self):
        self.entries["playbook@mk"][0]["version"] = "9.9.0"
        self._save_registry()
        verdict = self._verdict(self._report())
        self.assertTrue(verdict[0].startswith("[WARN]"), verdict)
        self.assertIn("!= installed entry v9.9.0", verdict[0])

    def test_warns_when_doctor_runs_from_a_fourth_copy(self):
        fourth = self.root / "fourth" / "playbook"
        shutil.copytree(self.hook, fourth)
        out = self._report(doctor_root=fourth)
        self.assertIn(f"] plugin: doctor copy — {fourth} v{self.VERSION}", out)
        verdict = self._verdict(out)
        self.assertTrue(verdict[0].startswith("[WARN]") and "fourth copy" in verdict[0], verdict)

    def test_selection_matches_the_embedded_resolver(self):
        # a project-scoped entry for THIS project outranks the newer user entry;
        # decoys: another project's entry, another plugin, another marketplace
        pinned = self.plugins / "cache" / "mk" / "playbook" / "9.0.0"
        self._plugin_tree(pinned, "9.0.0")
        elsewhere = self.plugins / "cache" / "mk" / "playbook" / "9.9.9"
        self._plugin_tree(elsewhere, "9.9.9")
        self.entries["playbook@mk"].append({"scope": "project", "installPath": str(pinned),
                                            "version": "9.0.0", "projectPath": str(self.project)})
        self.entries["playbook@mk"].append({"scope": "project", "installPath": str(elsewhere),
                                            "version": "9.9.9", "projectPath": str(self.root / "other")})
        self.entries["other@mk"] = [{"scope": "user", "installPath": str(elsewhere), "version": "1.0"}]
        for seed in (1, 2, 3):
            random.Random(seed).shuffle(self.entries["playbook@mk"])
            self._save_registry(shuffle_seed=seed)
            from tests._bashcheck import bash_or_skip
            # through bash: Windows cannot exec a bash script directly (WinError 193)
            ran = subprocess.run([bash_or_skip(), str(self.project / ".claude" / "bin" / "tasks")],
                                 capture_output=True, text=True,
                                 env=dict(os.environ, HOME=str(self.home))).stdout.strip()
            self.assertEqual(os.path.dirname(os.path.dirname(ran)), str(pinned), ran)
            self.assertIn(f"] plugin: installed copy — {pinned} v9.0.0", self._report())


    def test_paths_with_spaces(self):
        self._rebuild("dir with spaces")
        self.assertIn(" ", str(self.hook))
        out = self._report()
        self.assertIn(f"] plugin: hook copy — {self.hook} v{self.VERSION}", out)
        self.assertIn(f"] plugin: launcher copy — {self.installed} v{self.VERSION}", out)
        self.assertIn(f"] plugin: installed copy — {self.installed} v{self.VERSION}", out)
        self.assertTrue(self._verdict(out)[0].startswith("[PASS]"), out)


class PluginCopiesDegraded(_CopiesFixture):
    """The plan panel's additions (task 086 triage P3/P4/P7-P10/P13): disagreements
    the 13 named tests do not stage, and inputs that are missing or malformed —
    each must end in a WARN, never a traceback and never a PASS."""

    def test_warns_when_an_installed_file_is_extra(self):
        self._write(self.installed / "tasks" / "planted.py", "x = 1\n")
        verdict = self._verdict(self._report())
        self.assertTrue(verdict[0].startswith("[WARN]"), verdict)
        self.assertIn("not in the release: tasks/planted.py", verdict[0])

    @unittest.skipIf(os.name == "nt", "no POSIX execute bit on Windows")
    def test_warns_when_an_installed_execute_bit_differs(self):
        # `scripts/sandbox`, not `scripts/tasks`: without its execute bit the
        # resolver would skip the copy altogether (a different WARN)
        (self.installed / "scripts" / "sandbox").chmod(0o644)
        verdict = self._verdict(self._report())
        self.assertTrue(verdict[0].startswith("[WARN]"), verdict)
        self.assertIn("mode differs from the release: scripts/sandbox", verdict[0])

    def test_warns_when_the_release_commit_is_not_in_the_hook_repo(self):
        self.entries["playbook@mk"][0]["gitCommitSha"] = "f" * 40
        self._save_registry()
        verdict = self._verdict(self._report())
        self.assertTrue(verdict[0].startswith("[WARN]") and "could not verify" in verdict[0], verdict)

    def test_warns_when_the_hook_copy_is_not_a_git_checkout(self):
        plain = self.root / "plain-market"
        shutil.copytree(self.market, plain, ignore=shutil.ignore_patterns(".git"))
        self._write(self.plugins / "known_marketplaces.json", json.dumps({
            "mk": {"source": {"source": "directory", "path": str(plain)},
                   "installLocation": str(plain)}}))
        out = self._report()
        self.assertIn(f"] plugin: hook copy — {plain / 'plugins' / 'playbook'} v{self.VERSION}", out)
        verdict = self._verdict(out)
        self.assertTrue(verdict[0].startswith("[WARN]") and "not a git checkout" in verdict[0], verdict)

    def test_warns_when_two_marketplace_keys_share_the_install(self):
        self.entries["playbook@mk2"] = [dict(self.entries["playbook@mk"][0])]
        self._save_registry()
        out = self._report()
        verdict = self._verdict(out)
        self.assertTrue(verdict[0].startswith("[WARN]") and "ambiguous" in verdict[0], out)

    def test_degraded_registry_inputs_warn_without_a_traceback(self):
        (self.plugins / "installed_plugins.json").write_text("{not json", encoding="utf-8")
        (self.plugins / "known_marketplaces.json").unlink()
        out = self._report()
        for label in ("hook", "launcher", "installed", "doctor"):
            self.assertIn(f"] plugin: {label} copy — ", out)
        self.assertTrue(self._verdict(out)[0].startswith("[WARN]"), out)

    def test_home_wins_over_a_decoy_userprofile(self):
        from unittest import mock
        decoy = self.root / "decoy"
        (decoy / ".claude" / "plugins").mkdir(parents=True)
        (decoy / ".claude" / "plugins" / "installed_plugins.json").write_text(
            json.dumps({"version": 2, "plugins": {}}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(decoy)}):
            lines = plugin_copies.report(self.project, doctor_root=self.hook)
        out = "\n".join(f"[{t}] {x}" for t, x in lines)
        self.assertIn(f"] plugin: installed copy — {self.installed} v{self.VERSION}", out)
        self.assertTrue(self._verdict(out)[0].startswith("[PASS]"), out)

    def test_autocrlf_line_endings_are_not_a_difference(self):
        # Git for Windows usually runs with core.autocrlf=true: working files carry
        # CRLF while the blobs hold LF. Git itself calls those identical, so the
        # report must too — but only line endings: a real edit still WARNs.
        _git_ok(self.market, "config", "core.autocrlf", "true")
        target = self.installed / "tasks" / "core.py"
        target.write_bytes(target.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        self.assertTrue(self._verdict(self._report())[0].startswith("[PASS]"))
        target.write_bytes(target.read_bytes().replace(b"9", b"8", 1))
        verdict = self._verdict(self._report())
        self.assertIn("differs from the release: tasks/core.py", verdict[0])

    def test_a_standard_marketplace_install_can_pass(self):
        # impl panel (opus): a github marketplace runs its hooks from the installed
        # cache (not a git checkout); the release is read from the marketplace clone
        self._write(self.plugins / "known_marketplaces.json", json.dumps({
            "mk": {"source": {"source": "github", "repo": "o/r"},
                   "installLocation": str(self.market)}}))
        # on a standard install the doctor itself runs from the installed copy
        out = self._report(doctor_root=self.installed)
        self.assertIn(f"] plugin: hook copy — {self.installed} v{self.VERSION}", out)
        self.assertTrue(self._verdict(out)[0].startswith("[PASS]"), out)
        (self.installed / "tasks" / "core.py").write_text("tampered\n", encoding="utf-8")
        verdict = self._verdict(self._report(doctor_root=self.installed))
        self.assertIn("differs from the release: tasks/core.py", verdict[0])

    def test_missing_marketplace_metadata_makes_the_hook_copy_unknown(self):
        (self.plugins / "known_marketplaces.json").unlink()
        out = self._report()
        self.assertIn("] plugin: hook copy — unknown (known_marketplaces.json is missing or unreadable)", out)
        self.assertNotIn(f"] plugin: hook copy — {self.installed}", out)
        self.assertTrue(self._verdict(out)[0].startswith("[WARN]"), out)

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_an_extra_symlinked_directory_is_a_difference(self):
        os.symlink(str(self.root), str(self.installed / "tasks" / "linked"))
        verdict = self._verdict(self._report())
        self.assertIn("not in the release: tasks/linked", verdict[0])

    @unittest.skipIf(not hasattr(os, "mkfifo"), "no FIFOs on this platform")
    def test_a_fifo_is_reported_without_being_opened(self):
        os.mkfifo(str(self.installed / "tasks" / "pipe"))
        verdict = self._verdict(self._report())          # would block forever if opened
        self.assertIn("not a regular file (never opened): tasks/pipe", verdict[0])

    def test_a_minus_text_file_keeps_its_bytes(self):
        # impl panel (sol-high, sol-medium): CRLF may only be forgiven where git
        # itself would normalise — a `-text` path is byte-exact
        self._lf_release_file()
        self._write(self.market / ".gitattributes", "*.py -text\n")
        _git_ok(self.market, "config", "core.autocrlf", "true")
        _git_ok(self.market, "add", "-A")
        _git_ok(self.market, "commit", "-qm", "attrs")
        self.sha = _git_ok(self.market, "rev-parse", "HEAD")
        self.entries["playbook@mk"][0]["gitCommitSha"] = self.sha
        self._save_registry()
        self._crlf_installed()
        self.assertIn("differs from the release: tasks/core.py", self._verdict(self._report())[0])

    def test_a_project_module_cannot_run_inside_the_doctor(self):
        # impl panel (sol-high): the resolver ran as `python -` from the project, so
        # a project `glob.py` shadowed the stdlib; `-I` isolates it
        sentinel = self.root / "SHADOWED"
        for name in ("glob.py", "json.py"):
            self._write(self.project / name, f"open({str(sentinel)!r}, 'w').write('x')\n")
        old = os.getcwd()
        os.chdir(self.project)
        try:
            self._report()
        finally:
            os.chdir(old)
        self.assertFalse(sentinel.exists(), "a project module executed inside the doctor")

    def _lf_release_file(self, rel="tasks/core.py"):
        # explicit LF bytes in BOTH copies: `write_text` writes CRLF on Windows, and a
        # CRLF-committed fixture would make the CRLF mutation below a no-op
        data = b'VERSION = "9.9.1"\nX = 1\n'
        (self.hook / rel).write_bytes(data)
        (self.installed / rel).write_bytes(data)

    def _crlf_installed(self, rel="tasks/core.py"):
        target = self.installed / rel
        target.write_bytes(target.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))

    def _recommit(self, msg):
        _git_ok(self.market, "add", "-A")
        _git_ok(self.market, "commit", "-qm", msg)
        return _git_ok(self.market, "rev-parse", "HEAD")

    def test_a_configured_clean_filter_is_never_executed(self):
        # impl panel round 2 (sol-high 1, sol-medium 2): `git hash-object --path`
        # ran configured clean filters — the doctor must execute nothing
        sentinel = self.root / "FILTER-RAN"
        self._lf_release_file()
        _git_ok(self.market, "config", "filter.probe.clean", f"touch '{sentinel}'; cat")
        self._write(self.market / ".gitattributes", "*.py filter=probe\n")
        self.sha = self._recommit("filter attr")
        # age the files and refresh the index, so the spec'd `git status` cleanliness
        # check has nothing to re-hash: what remains is the CONTENT comparison
        import time
        old = time.time() - 60
        for f in self.hook.rglob("*"):
            if f.is_file():
                os.utime(f, (old, old))
        _git_ok(self.market, "status", "--porcelain")
        sentinel.unlink(missing_ok=True)
        self.entries["playbook@mk"][0]["gitCommitSha"] = self.sha
        self._save_registry()
        self._crlf_installed()
        verdict = self._verdict(self._report())
        self.assertFalse(sentinel.exists(), "the doctor executed a git clean filter")
        self.assertIn("differs from the release: tasks/core.py", verdict[0])

    def test_attributes_come_from_the_release_commit_not_the_checkout(self):
        _git_ok(self.market, "config", "core.autocrlf", "true")
        self._lf_release_file()
        self._write(self.market / ".gitattributes", "*.py -text\n")
        release = self._recommit("release: py is -text")
        self._write(self.market / ".gitattributes", "*.py text\n")
        self._recommit("later: py is text")
        self.entries["playbook@mk"][0]["gitCommitSha"] = release
        self._save_registry()
        self._crlf_installed()
        verdict = self._verdict(self._report())
        self.assertIn("differs from the release: tasks/core.py", verdict[0])

    def test_malformed_registry_shapes_still_give_four_lines(self):
        for bad in ('{"plugins": [1]}', '{"plugins": {"playbook@mk": {}}}', '[]', '{"plugins": {"playbook@mk": [7]}}'):
            with self.subTest(registry=bad):
                (self.plugins / "installed_plugins.json").write_text(bad, encoding="utf-8")
                out = self._report()
                for label in ("hook", "launcher", "installed", "doctor"):
                    self.assertIn(f"] plugin: {label} copy — ", out)
                self.assertTrue(self._verdict(out)[0].startswith("[WARN]"), out)

    def test_an_extra_file_is_never_read(self):
        from unittest import mock
        self._write(self.installed / "tasks" / "planted.bin", "x" * 1000)
        hashed = []
        real = plugin_copies._stream_sha

        def spy(path, size):
            hashed.append(Path(path).name)
            return real(path, size)

        with mock.patch.object(plugin_copies, "_stream_sha", spy):
            verdict = self._verdict(self._report())
        self.assertIn("not in the release: tasks/planted.bin", verdict[0])
        self.assertNotIn("planted.bin", hashed, "an extra file was read")

    def test_the_real_doctor_prints_the_four_lines_and_the_verdict(self):
        # impl panel round 3 (opus 1, sonnet 1, grok 3): every other test calls
        # report() in-process — this one runs `tasks doctor` itself, from the
        # fixture's hook copy, so the §5b wiring in diagnostics.py is proven
        tasks_pkg = self.hook / "tasks"
        shutil.rmtree(tasks_pkg)
        shutil.copytree(PLUGIN / "tasks", tasks_pkg, ignore=shutil.ignore_patterns("__pycache__"))
        (tasks_pkg / "core.py").write_text(
            (PLUGIN / "tasks" / "core.py").read_text(encoding="utf-8").replace(
                f'VERSION = "{json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]}"',
                f'VERSION = "{self.VERSION}"'), encoding="utf-8")
        self.sha = self._recommit("the real package")
        shutil.rmtree(self.installed)
        shutil.copytree(self.hook, self.installed)
        self.entries["playbook@mk"][0]["gitCommitSha"] = self.sha
        self._save_registry()
        env = dict(os.environ, PYTHONPATH=str(self.hook), HOME=str(self.home), USERPROFILE=str(self.home))
        env.pop("CLAUDE_PLUGIN_ROOT", None)
        r = subprocess.run([sys.executable, "-m", "tasks.cli", "doctor"], cwd=self.project, env=env,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
        out = r.stdout + r.stderr
        for label in ("hook", "launcher", "installed", "doctor"):
            self.assertIn(f"] plugin: {label} copy — ", out, out[-3000:])
        self.assertIn("[PASS] plugin: copies agree", out, out[-3000:])
        self.assertNotIn("plugin: copies — could not be determined", out)

    def test_a_stray_git_dir_or_hidden_untracked_files_cannot_hide_a_dirty_hook(self):
        from unittest import mock
        _git_ok(self.market, "config", "status.showUntrackedFiles", "no")
        self._write(self.hook / "tasks" / "untracked.py", "x = 1\n")
        decoy = self.root / "decoy-repo"
        decoy.mkdir()
        _git_ok(decoy, "init", "-q")
        with mock.patch.dict(os.environ, {"GIT_DIR": str(decoy / ".git"), "GIT_WORK_TREE": str(decoy)}):
            verdict = self._verdict(self._report())
        self.assertIn("uncommitted changes", verdict[0])

    @unittest.skipIf(os.name == "nt", "no POSIX execute bit on Windows")
    def test_a_non_executable_launcher_warns(self):
        (self.project / ".claude" / "bin" / "tasks").chmod(0o644)
        out = self._report()
        self.assertIn("] plugin: launcher copy — not executable", out)
        self.assertTrue(self._verdict(out)[0].startswith("[WARN]"), out)

    def test_wrong_field_types_and_a_symbolic_sha_degrade_to_warn(self):
        for field, value in (("installPath", 7), ("gitCommitSha", 123), ("version", ["9"]),
                             ("gitCommitSha", "HEAD"), ("gitCommitSha", self.sha[:12])):
            with self.subTest(field=field, value=value):
                entry = dict(self.entries["playbook@mk"][0])
                entry[field] = value
                self._write(self.plugins / "installed_plugins.json",
                            json.dumps({"version": 2, "plugins": {"playbook@mk": [entry]}}))
                out = self._report()
                for label in ("hook", "launcher", "installed", "doctor"):
                    self.assertIn(f"] plugin: {label} copy — ", out)
                self.assertTrue(self._verdict(out)[0].startswith("[WARN]"), out)
                self.assertNotIn("(sha HEAD", out)

    def test_the_resolver_source_is_piped_as_utf8_bytes(self):
        # Windows CI (run 35970907673): piped as text, the resolver was encoded in
        # the locale code page (cp1252 turned its em dash into 0x97) while
        # `python -` decodes source as UTF-8 — a SyntaxError, so no copy resolved.
        from unittest import mock
        seen = {}
        real_run = subprocess.run

        def spy(cmd, *a, **kw):
            if "-" in cmd[1:3]:
                seen["input"], seen["text"] = kw.get("input"), kw.get("text")
            return real_run(cmd, *a, **kw)

        with mock.patch.object(plugin_copies.subprocess, "run", spy):
            plugin_copies.resolve_launcher_script(self.hook, self.project, self.home)
        self.assertIsInstance(seen.get("input"), bytes, seen)
        self.assertFalse(seen.get("text"))
        body = (self.hook / "scripts" / "wrapper_resolver.py").read_text(encoding="utf-8")
        self.assertEqual(seen["input"].decode("utf-8"), body.replace("WRAPPER_NAME", "tasks"))

    def test_parses_a_registry_in_the_captured_real_shape(self):
        # field set captured from a real Claude Code install on 2026-09-24:
        # top {plugins, version=2}; entry {gitCommitSha, installPath, installedAt,
        # lastUpdated, scope, version}; marketplace {installLocation, lastUpdated,
        # source{path, source}}
        self._write(self.plugins / "installed_plugins.json", json.dumps({"version": 2, "plugins": {
            "playbook@mk": [{"scope": "user", "installPath": str(self.installed), "version": self.VERSION,
                             "installedAt": "2026-08-21T14:47:25.000Z", "lastUpdated": "2026-09-23T12:00:00.000Z",
                             "gitCommitSha": self.sha}]}}))
        self._write(self.plugins / "known_marketplaces.json", json.dumps({"mk": {
            "source": {"source": "directory", "path": str(self.market)},
            "installLocation": str(self.market), "lastUpdated": "2026-08-21T14:47:24.988Z"}}))
        out = self._report()
        self.assertIn(f"] plugin: hook copy — {self.hook} v{self.VERSION} (HEAD {self.sha[:7]})", out)
        self.assertTrue(self._verdict(out)[0].startswith("[PASS]"), out)


if __name__ == "__main__":
    unittest.main()
