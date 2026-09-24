# claude-playbook: project-scoped command logging (bash)
# Sourced via BASH_ENV — logs commands in playbook projects to the current
# user's lane: .agent/bash_history, or .agent/<user>/bash_history.
# Purpose: forensic post-mortem record ("what did the agent actually run?")

# PLAN S5b (task 088): a shell that asks not to be logged installs no trap at
# all. The Claude Code statusline is the reason (~710 renders a day, ~40 simple
# commands each). This file is sourced BEFORE a script's first line, so the knob
# must be in the environment the shell starts with (e.g. a statusLine command of
# `PLAYBOOK_NO_BASHLOG=1 bash ~/.claude/statusline.sh`); exporting it inside a
# script covers only the shells that script starts. A script NAMED statusline is
# skipped in the callback below either way. "" and "0" mean "log".
case "${PLAYBOOK_NO_BASHLOG:-}" in
    ""|0) ;;
    *) return 0 ;;
esac

_cpb_log_cmd() {
    # Hook shells are implementation machinery, not user/agent Bash tool calls.
    # BASH_ENV is sourced before bash assigns the script name to $0, so this
    # check must live in the DEBUG callback (where $0 is final), not at source
    # time.  It avoids both history noise and the expensive walk/date fork for
    # every hook-internal command. Real Bash tool shells keep $0 as bash/sh.
    # The script's own NAME only: `${0##*/}` then `##*\\` also strips a Git Bash
    # backslash path (`bash C:\…\statusline.sh`, Windows lane CI 36000444073)
    # without matching a directory such as `…\statusline-tests\run.sh` (panel
    # r1). ONE line, ending in the S17 marker: the wrapper fixture's negative
    # control deletes the marked line, and any statement left before the
    # `$BASH_COMMAND` case would reset the stale status that control needs.
    local _me="${0##*/}"; case "${_me##*\\}" in *-hook|statusline|statusline.sh|statusline-*) return 0 ;; esac  # PB-S17-FAST-PATH

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

    # PLAN S5b: each distinct command text is logged ONCE per shell process.
    # A DEBUG trap fires for every simple command, so a 5-iteration loop wrote
    # its header and body five times each (11 lines for `for …; do …; done; …`;
    # 195,168 × 3 rows on 2026-09-21). The text is the unexpanded source, so a
    # loop body is the same string every iteration. Bash 3.2 has no associative
    # arrays: a \x1f-delimited string, reset past 64 KiB.
    # The key holds the working directory too (panel r2): the destination lane
    # follows $PWD, so one shell running the same text in two projects logs it
    # in both. Any text containing `tasks` is never deduped (panel r1/r2):
    # retro and timeline build task windows from those lines, and a quoted or
    # backslash path (`"$B/tasks" work 7`) must count too — over-logging is the
    # safe direction.
    local _key=$'\x1f'"$PWD"$'\x1e'"$BASH_COMMAND"$'\x1f'
    case "$BASH_COMMAND" in
        *tasks*) _key="" ;;
    esac
    if [[ -n "$_key" ]]; then
        case "${_CPB_SEEN:-}" in
            *"$_key"*) return 0 ;;
        esac
    fi

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
            # The decision rests on exactly two filesystem facts: the marker,
            # and whether root .agent/tasks/ is a directory. Nothing about
            # .agent/'s children is consulted.
            #   valid marker                       -> the validated user lane
            #   no marker, root .agent/tasks/      -> the root IS a lane
            #   anything else                      -> owner unknown; skip
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
                # No marker, no root tasks/: owner unknown, so the shared
                # root is not elected. Stricter than lanes_without_marker,
                # which still answers the root — a missing forensic log, not
                # contamination, healed by anything creating .agent/tasks/.
                # No glob remains here, so no host option can move this.
                return 0
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
            # PLAN S5b: a history past 50 MB is rotated before the first write a
            # shell makes to it (the 2026-09-21 file had reached 136 MB). Checked
            # once per process PER LANE (panel r2: a process-wide flag skipped a
            # second project), so one shell can overshoot by what it writes.
            local _rk=$'\x1f'"$_lane"$'\x1f'
            case "${_CPB_ROTATE_CHECKED:-}" in
                *"$_rk"*) ;;
                *)
                _CPB_ROTATE_CHECKED="${_CPB_ROTATE_CHECKED:-}$_rk"
                # `find -size +Nc` is one stat on every platform (a `wc -c`
                # may read the whole file), so the check-to-move window stays
                # small. Two shells crossing 50 MB at the same instant can
                # still both rotate; the second archive then holds the few
                # lines written in between — archived, not lost (disclosed).
                local _big=""
                if [[ -f "$_lane/bash_history" ]]; then
                    _big=$(find "$_lane/bash_history" -prune -size +52428800c 2>/dev/null) || _big=""
                fi
                if [[ -n "$_big" ]]; then
                    local _arch
                    _arch="$_lane/bash_history.archived-$(date '+%Y%m%d-%H%M%S')-$$" || _arch="$_lane/bash_history.archived-$$"
                    # The `tasks work|new` lines are carried into the fresh file
                    # (panel r2): retro opens each task's window at its EARLIEST
                    # activation and reads only the live file, so archiving the
                    # active task's activation made its window vanish. `-a`: a
                    # history can hold stray binary bytes.
                    if mv -f "$_lane/bash_history" "$_arch" 2>/dev/null; then
                        { grep -a -E ' [|] [A-Za-z0-9_]+ [|] .*tasks[[:space:]]+(work|new)' "$_arch" >> "$_lane/bash_history"; } 2>/dev/null || true
                    fi
                fi
                ;;
            esac
            { echo "$(date '+%Y-%m-%d %H:%M:%S') | AGENT | $_cmd" >> "$_lane/bash_history"; } 2>/dev/null || return 0
            if [[ -n "$_key" ]]; then
                _CPB_SEEN="${_CPB_SEEN:-}$_key"
                if [[ ${#_CPB_SEEN} -gt 65536 ]]; then
                    _CPB_SEEN="$_key"
                fi
            fi
            break
        fi
        _dir="${_dir%/*}"
        [[ -n "$_dir" ]] || _dir="/"
    done
    return 0
}
set -o history
trap '_cpb_log_cmd' DEBUG
