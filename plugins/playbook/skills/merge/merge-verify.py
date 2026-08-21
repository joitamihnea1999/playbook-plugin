#!/usr/bin/env python3
"""Run the project's declared post-merge soundness command — or say why it didn't.

Ships in the `merge` skill directory next to `ref-integrity.py` (pure stdlib; it
`import`s nothing across the skill/package boundary — the skill must work
standalone. Its ONE cross-boundary reach is a path-load of the shared bash
resolver, `tasks/bash_resolver.py`, done relative to this file so it needs no
`tasks` package on sys.path; see `_bash_resolver`).

The merge skill verifies *the merge*: mind-map integrity, contamination, and code
identity are all repo-agnostic. Whether the *branches* are healthy is the
project's own business, so the command that answers it is declared by the project
in `.agent/config.json`:

    {"merge_verify": {"command": "<what green means for THIS repo>"}}

Outcomes are one per exit code, so the push gate is mechanical (auto-push iff
exit 0) and no outcome can masquerade as another:

    0  GREEN       the declared command ran and exited 0
    1  FAILED      the declared command ran and exited non-zero (rc in the status line)
    2  BLOCKED     config.json is present but its merge_verify is unusable —
                   surfaced, never silently treated as "nothing configured"
    3  SKIPPED     nothing is declared (no file, no key, empty command) — the
                   check did not run, and saying so is the whole point
    4  CONFIGURED  --plan only: a command exists but was deliberately not run

A command's own exit status never leaks into these codes: a suite that exits 2
still reports FAILED(1), so a config verdict can't be forged by a test runner.

The command is written to a temp script and run as `bash <script>`, never
interpolated into a `-c` string: a command containing quotes (`pytest -k 'not
slow'`) must not change meaning or break the runner. The script runs under
`set -e -o pipefail` so a failing *early* step fails the gate — bash otherwise
reports only the last command's status, which would pass a red suite followed by
a successful cleanup line.

Usage:
    merge-verify.py            # classify, then run the command if there is one
    merge-verify.py --plan     # classify only, never run (Step 4 background note)
    merge-verify.py -C <dir>   # project root holding .agent/ (default: cwd)
"""

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

GREEN, FAILED, BLOCKED, SKIPPED = 0, 1, 2, 3

# THE single "which bash, and is it usable" policy lives in product code
# (tasks/bash_resolver.py). This skill runs STANDALONE — invoked from an
# arbitrary repo with no `tasks` package on sys.path, and path-loaded by the
# close gate — so it cannot `import tasks.*`; it reaches the resolver by loading
# that one file relative to itself. Cached per process.
_BASH_RESOLVER = None


def _bash_resolver():
    """The one bash resolver, path-loaded so this skill stays standalone.

    On a broken/partial install (the file is missing or corrupt) synthesize a
    fail-CLOSED stand-in whose `usable_bash()` reports no bash, so a missing
    resolver degrades to "no usable bash" rather than a traceback — the same
    posture as every other no-bash path here.
    """
    global _BASH_RESOLVER
    if _BASH_RESOLVER is None:
        try:
            path = Path(__file__).resolve().parents[2] / "tasks" / "bash_resolver.py"
            spec = importlib.util.spec_from_file_location("_playbook_bash_resolver", path)
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot load {path}")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as exc:
            class _Unavailable:
                _why = f"bash resolver unavailable: {exc}"

                def usable_bash(self):
                    return None, self._why
            mod = _Unavailable()
        _BASH_RESOLVER = mod
    return _BASH_RESOLVER
# `--plan` classifies without running, so it must not be able to return the one
# code the push gate accepts. Only an actual run may yield GREEN.
CONFIGURED = 4

CONFIG_REL = os.path.join(".agent", "config.json")

# Prepended to the declared command. `set -e` makes an early failing step fail
# the gate; `pipefail` stops a trailing `| tail` from swallowing the real status.
PREAMBLE = "set -e\nset -o pipefail\n"


class Unusable(Exception):
    """config.json exists but its merge_verify cannot be honored (→ BLOCKED)."""


def command_from_config(cfg):
    """Return the declared command from an already-parsed config dict.

    Split out from `resolve_command` so `tasks doctor` can apply these exact
    rules to the config it has already read — the shape rules must not drift
    between what doctor warns about and what a merge actually enforces.
    """
    if "merge_verify" not in cfg:
        return None  # config.json exists but declares no verify command
    spec = cfg["merge_verify"]
    if not isinstance(spec, dict):
        raise Unusable(
            f"merge_verify must be an object like "
            f'{{"command": "..."}}, got {type(spec).__name__}'
        )
    if "command" not in spec:
        # An empty object is a deliberate "declare nothing"; a populated one
        # missing `command` is almost always a misspelled key, and silently
        # skipping a typo is the failure this tool exists to prevent.
        if not spec:
            return None
        raise Unusable(
            "merge_verify has no `command` key (found: "
            + ", ".join(sorted(map(str, spec))) + ")"
        )
    command = spec["command"]
    if not isinstance(command, str):
        raise Unusable(
            f"merge_verify.command must be a string, got {type(command).__name__}"
        )
    if not command.strip():
        return None  # explicitly declared as empty ⇒ same as undeclared
    return command


def resolve_command(project_root):
    """Return the declared command string, or None when nothing is declared.

    Raises Unusable for a present-but-broken declaration. The asymmetry is
    deliberate: an absent declaration is a legitimate project choice, while a
    malformed one is a mistake in committed policy that must not read as "the
    project declined to verify".
    """
    path = os.path.join(project_root, CONFIG_REL)
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except (FileNotFoundError, NotADirectoryError):
        return None  # no config.json at all — a legitimate project choice
    except OSError as exc:
        # It EXISTS but can't be read (permissions, a directory in its place, an
        # I/O error). The project did declare something; saying "nothing was
        # declared" would be a false statement that also unblocks nothing.
        raise Unusable(f"{CONFIG_REL} exists but cannot be read ({exc.strerror})")
    try:
        cfg = json.loads(raw)
    except ValueError as exc:
        raise Unusable(f"{CONFIG_REL} is not valid JSON ({exc})")
    if not isinstance(cfg, dict):
        raise Unusable(f"{CONFIG_REL} top-level value is not a JSON object")
    return command_from_config(cfg)


def _write_script(command):
    """Write `command` to a temp bash script under the fail-fast preamble and
    return its path. Shared by the streaming runner (main) and the capturing
    runner (run_command_capture) so both honor the SAME discipline: set -e makes
    an early failing step fail; pipefail stops a trailing `| tail` swallowing the
    real status. Caller unlinks."""
    fd, script = tempfile.mkstemp(prefix="verify-", suffix=".sh")
    # newline="\n": without it, Windows text-mode translates the preamble/command
    # `\n`→`\r\n`, and git-bash then runs a CRLF script whose `set -e`/`set -o
    # pipefail` and command lines are CR-corrupted — even a green command exits
    # non-zero, failing the gate. No-op on POSIX.
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(PREAMBLE)
        fh.write(command)
        if not command.endswith("\n"):
            fh.write("\n")
    return script


def run_command_capture(command, project_root=".", timeout_secs=None):
    """Run one declared command and return (rc, combined_output).

    Same execution contract as `main`'s runner (temp script, set -e/pipefail, exit
    status can't be forged) but CAPTURES stdout+stderr instead of streaming, so a
    caller — the `tasks work done` evidence gate — can put a line of output into a
    receipt. This is the one runner both the merge push-gate and the close gate
    share; the shape of "what green means" lives here and nowhere else.

    `timeout_secs` (None = unlimited) is a hard ceiling: a hung suite must not
    hang the close forever — in headless use that is a silent deadlock. A timeout
    returns rc 124 (the conventional timeout code, non-zero, so the gate reads
    FAILED) with the marker FIRST in the output so a receipt's first line names
    the timeout, then whatever the command had written."""
    bash, why = _bash_resolver().usable_bash()
    if bash is None:
        # Fail CLOSED: with no bash to run it, the command did NOT run, and an
        # unrun verify is not a pass. Non-zero rc so the gate reads FAILED.
        return 126, (f"(no usable bash to run the verify command: {why} — "
                     "command NOT run, cannot certify green)")
    script = _write_script(command)
    try:
        proc = subprocess.run(
            [bash, script], cwd=project_root or ".",
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", timeout=timeout_secs,
        )
        return proc.returncode, (proc.stdout or "")
    except subprocess.TimeoutExpired as exc:
        raw = exc.stdout or ""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        marker = (f"(timed out after {timeout_secs}s — command killed; a verify "
                  "that cannot finish is FAILED, not verified)")
        return 124, marker + ("\n" + raw if raw else "")
    finally:
        try:
            os.unlink(script)
        except OSError:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Run the project's declared post-merge verify command.")
    ap.add_argument("-C", "--project-root", default=".",
                    help="project root containing .agent/ (default: cwd)")
    ap.add_argument("--plan", action="store_true",
                    help="classify only; never run the command")
    args = ap.parse_args(argv)

    root = args.project_root
    try:
        command = resolve_command(root)
    except Unusable as exc:
        print(f"merge-verify: BLOCKED — {exc}")
        print("  Fix .agent/config.json (or remove the merge_verify key to skip "
              "the soundness check deliberately). Not pushing.")
        return BLOCKED

    if command is None:
        print("merge-verify: SKIPPED — no merge_verify.command declared in "
              f"{CONFIG_REL}")
        print("  The merge itself is still verified (mind-map integrity, "
              "contamination, code identity); this project just declares no "
              "post-merge soundness command, so branch health was NOT checked.")
        return SKIPPED

    if args.plan:
        print(f"merge-verify: CONFIGURED — {command}")
        print("  (classification only — nothing ran, so this does NOT satisfy "
              "the push gate)")
        return CONFIGURED

    # Run via a temp script rather than `bash -c <command>`: the command is
    # arbitrary project-owned text and may contain any quoting. _write_script
    # applies the fail-fast preamble (set -e / pipefail) so an early failing step
    # fails the gate — a green stamp on a red tree is the whole defect this
    # prevents.
    bash, why = _bash_resolver().usable_bash()
    if bash is None:
        # Fail CLOSED: no bash means the command cannot run, and a verify that
        # cannot run is not GREEN. Report FAILED so the push gate refuses.
        print(f"merge-verify: FAILED — no usable bash to run the command: {why}")
        return FAILED
    script = _write_script(command)
    try:
        print(f"merge-verify: running — {command}", flush=True)
        rc = subprocess.call([bash, script], cwd=root or ".")
    finally:
        try:
            os.unlink(script)
        except OSError:
            pass

    if rc == 0:
        print("merge-verify: GREEN — declared command exited 0")
        return GREEN
    print(f"merge-verify: FAILED — declared command exited {rc}")
    return FAILED


if __name__ == "__main__":
    sys.exit(main())
