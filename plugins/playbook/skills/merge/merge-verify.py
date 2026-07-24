#!/usr/bin/env python3
"""Run the project's declared post-merge soundness command — or say why it didn't.

Ships in the `merge` skill directory next to `ref-integrity.py` (pure stdlib, no
imports across the skill/package boundary — the skill must work standalone).

The merge skill verifies *the merge*: mind-map integrity, contamination, and code
identity are all repo-agnostic. Whether the *branches* are healthy is the
project's own business, so the command that answers it is declared by the project
in `.agent/config.json`:

    {"merge_verify": {"command": "<what green means for THIS repo>"}}

Four outcomes, one per exit code, so the push gate is mechanical (auto-push iff
exit 0) and no outcome can masquerade as another:

    0  GREEN    the declared command ran and exited 0
    1  FAILED   the declared command ran and exited non-zero (rc in the status line)
    2  BLOCKED  config.json is present but its merge_verify is unusable — surfaced,
                never silently treated as "nothing configured"
    3  SKIPPED  nothing is declared (no file, no key, empty command) — the check
                did not run, and saying so is the whole point

The command is written to a temp script and run as `bash <script>`, never
interpolated into a `-c` string: a command containing quotes (`pytest -k 'not
slow'`) must not change meaning or break the runner.

Usage:
    merge-verify.py            # classify, then run the command if there is one
    merge-verify.py --plan     # classify only, never run (Step 4 background note)
    merge-verify.py -C <dir>   # project root holding .agent/ (default: cwd)
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile

GREEN, FAILED, BLOCKED, SKIPPED = 0, 1, 2, 3

CONFIG_REL = os.path.join(".agent", "config.json")


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
    except OSError:
        return None  # no config.json at all
    try:
        cfg = json.loads(raw)
    except ValueError as exc:
        raise Unusable(f"{CONFIG_REL} is not valid JSON ({exc})")
    if not isinstance(cfg, dict):
        raise Unusable(f"{CONFIG_REL} top-level value is not a JSON object")
    return command_from_config(cfg)


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
        return GREEN

    # Run via a temp script rather than `bash -c <command>`: the command is
    # arbitrary project-owned text and may contain any quoting.
    fd, script = tempfile.mkstemp(prefix="merge-verify-", suffix=".sh")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(command)
            if not command.endswith("\n"):
                fh.write("\n")
        print(f"merge-verify: running — {command}", flush=True)
        rc = subprocess.call(["bash", script], cwd=root or ".")
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
