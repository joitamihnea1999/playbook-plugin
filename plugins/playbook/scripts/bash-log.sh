# claude-playbook: project-scoped command logging (bash)
# Sourced via BASH_ENV — logs commands in playbook projects to the current
# user's lane: .agent/bash_history, or .agent/<user>/bash_history.
# Purpose: forensic post-mortem record ("what did the agent actually run?")

_cpb_log_cmd() {
    # Hook shells are implementation machinery, not user/agent Bash tool calls.
    # BASH_ENV is sourced before bash assigns the script name to $0, so this
    # check must live in the DEBUG callback (where $0 is final), not at source
    # time.  It avoids both history noise and the expensive walk/date fork for
    # every hook-internal command. Real Bash tool shells keep $0 as bash/sh.
    case "${0##*/}" in *-hook) return 0 ;; esac

    # Filter shell internals and CC infrastructure noise.
    #
    # Every exit from this function MUST be `return 0`, never bare `return`:
    # inside a DEBUG trap a bare `return` propagates the *stale* `$?` of the
    # previously executed command, and a DEBUG trap returning non-zero kills
    # a `set -e` shell. Since these arms match exactly the commands hooks run
    # constantly (`[ -d …`, `[[ …`), a bare return here silently killed every
    # `set -e` PostToolUse hook (state-echo-hook → gate logging dead; field
    # report 2026-07-21, reproduced on bash 3.2 and 5.2).
    case "$BASH_COMMAND" in
        *shell-snapshots*|"pwd -P"*|"case \$- in"*|return|"[["*) return 0 ;;
        "[ -d "*|"[ -f "*|"[ -n "*|"[ -z "*|"[ ! "*) return 0 ;;
        HIST*=*|PATH=*|"set -o"*|"shopt "*|"trap "*|"export PATH"*) return 0 ;;
        source*|.) return 0 ;;
    esac

    # Walk up from $PWD looking for .agent/ directory
    local _dir="$PWD"
    while [[ "$_dir" != "/" ]]; do
        if [[ -d "$_dir/.agent" ]]; then
            # Log into THIS user's lane, which is the same file `tasks retro`
            # and `tasks context` read (cli.py resolve_agent_dir()/bash_history).
            # Writing the root while the CLI reads a lane makes the history
            # invisible and mixes users' commands together.
            #
            # Pure builtins only: this runs in a DEBUG trap on every single
            # command, so a `cat`/`tr` subshell here is a per-command fork.
            # `read` also trims surrounding whitespace, matching the .strip()
            # in tasks/core.py.
            #
            # The lane starts UNKNOWN. Defaulting it to "$_dir/.agent" and
            # only reassigning inside the marker branch is what made a fresh
            # clone of a multi-user repo — lanes present, the gitignored
            # marker absent — append every command to the SHARED root
            # .agent/bash_history (PB-LANE-RESOLUTION, Critical).
            #
            # The four answers, identical to lanes_without_marker's verdict in
            # every shape a supported surface can produce (test_provider_multiuser
            # .py pins the logger as a fourth implementation of that table). Known
            # exception, pre-existing and unreachable via supported flows: a
            # DOT-named lane. Globbing skips dotfiles, so this and gate-echo-lib.sh
            # see no lane where Python's iterdir() reports one. Phase 4 reconciles
            # the two families; do not "fix" it here — the shell copies agree.
            #   valid marker                       -> the validated user lane
            #   no marker, root .agent/tasks/      -> the root IS a lane
            #   no marker, no per-user lane        -> the root IS a lane
            #   no marker, a per-user lane exists  -> owner unknown; skip
            #   invalid or unusable marker         -> owner unknown; skip
            local _lane=""
            if [[ -f "$_dir/.agent/current_user" ]]; then
                local _u="" _extra=""
            # One-line contract, same as tasks/core.py / provider/paths.py /
            # gate-echo-lib.sh: strip a trailing CR (CRLF markers), reject a
            # second content line (`alice\n../evil` must not become lane
            # `alice`), and `|| true` because `read` returns 1 on a marker with
            # no trailing newline — unguarded that trips errexit inside this
            # per-command DEBUG trap.
                { read -r _u; read -r _extra; } < "$_dir/.agent/current_user" 2>/dev/null || true
                _u="${_u%$'\r'}"
                [[ -n "$_extra" ]] && return 0
                # An unusable marker means we cannot know which lane owns this
                # command. Skip logging entirely — falling back to the shared
                # root is the cross-user contamination this lane model exists
                # to prevent. Never `exit`: this is the user's live shell.
                case "$_u" in
                    ""|"."|"..") return 0 ;;
                    [a-zA-Z0-9]*) ;;
                    *) return 0 ;;
                esac
                case "$_u" in
                    *[!a-zA-Z0-9_.-]*) return 0 ;;
                esac
                _lane="$_dir/.agent/$_u"
            elif [[ -d "$_dir/.agent/tasks" ]]; then
                # No marker, but root .agent/tasks/ means the root IS itself a
                # legitimate lane (the legacy and mixed layouts). Refusing it
                # here would kill logging for every single-user project.
                _lane="$_dir/.agent"
            else
                # No marker, no root lane. Ownership is unknown ONLY if a
                # per-user lane actually exists — that is the fresh-clone shape
                # the Critical fix is about. With no lane there is nobody to
                # contaminate, and `resolve_agent_dir` answers the root, so
                # skipping here would write nothing while `tasks retro` and
                # `tasks context` read `<root>/.agent/bash_history`: silent
                # forensic loss, not safety. Reachable shape: `.agent/config.json`
                # is documented as committable and git tracks no empty `tasks/`,
                # so a clone of a single-user project arrives exactly like this.
                #
                # A per-user lane is what provider/paths.py::lanes_without_marker
                # counts: a child of .agent/ that itself contains tasks/. This
                # loop is the last thing tried, so it never runs in the common
                # marker case or in any legacy/mixed project.
                #
                # Pure builtins: globbing forks nothing.
                #
                # This scan is this file's ONLY glob, and this file is SOURCED
                # into the user's shell, so it inherits that shell's globbing
                # options — the `-d` guard below cannot protect against them,
                # because bash expands the glob BEFORE the guard ever runs.
                # Under `shopt -s failglob` an unmatched glob is an ERROR, and
                # `.agent/` with no children at all is an in-policy shape
                # (a fresh single-user clone). Measured on bash 5.2: the host
                # shell prints "no match" once per command into stderr that
                # hook output feeds back to the agent, the scan is abandoned so
                # the lane stays empty and logging is silently lost, and under
                # `set -e` the host shell DIES. So failglob is neutralised
                # across the scan and restored to its exact prior state.
                #
                # `shopt -q/-s/-u` are builtins and fork nothing; capturing
                # `$(shopt -p failglob)` would fork a subshell per command,
                # which is what this DEBUG-trap file exists to avoid, so `-q`
                # is used as the fork-free equivalent of that idiom. The test
                # MUST sit inside `if`: a bare `shopt -q` on an unset option
                # returns 1 and would itself kill a `set -e` host shell.
                #
                # The restore happens BEFORE either exit path, so the scan
                # leaves no residue in the user's shell options.
                #
                # With failglob off, an unmatched glob stays literal in bash
                # and `<dir>/.agent/*/tasks` is not a directory; under
                # `shopt -s nullglob` it vanishes and the loop body never runs.
                # Both give the same verdict, so "no children" is handled with
                # or without nullglob, and nullglob is left untouched.
                local _sub _failglob=0 _lanefound=0
                if shopt -q failglob; then _failglob=1; shopt -u failglob; fi
                for _sub in "$_dir/.agent"/*; do
                    if [[ -d "$_sub/tasks" ]]; then _lanefound=1; break; fi
                done
                if (( _failglob )); then shopt -s failglob; fi
                if (( _lanefound )); then return 0; fi
                _lane="$_dir/.agent"
            fi
            [[ -d "$_lane" ]] || return 0
            local _cmd="${BASH_COMMAND//$'\n'/\\n}"
            # `|| return 0`: an append failure (perms, disk, history path
            # replaced by a directory) is a failing simple command inside the
            # trap — under the host shell's `set -e` that would kill it the
            # same way a bare return does.
            #
            # The brace group matters: `echo … >> file 2>/dev/null` does NOT
            # silence a failure to OPEN the file, because bash reports that
            # before it applies `2>/dev/null`. Since this runs per command, the
            # unguarded form prints "Is a directory" once for EVERY command in
            # every hook shell — and hook stderr/stdout is fed back to the
            # agent. Redirecting the group suppresses it properly.
            { echo "$(date '+%Y-%m-%d %H:%M:%S') | AGENT | $_cmd" >> "$_lane/bash_history"; } 2>/dev/null || return 0
            break
        fi
        _dir="${_dir%/*}"
        [[ -n "$_dir" ]] || _dir="/"
    done
    return 0
}
set -o history
trap '_cpb_log_cmd' DEBUG
