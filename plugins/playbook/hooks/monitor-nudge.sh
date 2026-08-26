#!/bin/bash
# monitor-nudge.sh — hook for injecting monitor nudges into agent context
#
# Registered as a NON-PLUGIN hook in .claude/settings.json.
# Works for both PostToolUse and UserPromptSubmit (reads event name from stdin).
# Current CC: PostToolUse additionalContext works, UserPromptSubmit is broken (bug #12151).
#
# Reads <agent-dir>/monitor/nudge.md (T121 flat layout), where <agent-dir> is
# the per-user lane `.agent/<user>/` when the repo declares one, else `.agent/`.
# The monitor WRITES its nudge into that same lane (launch-monitor resolves
# MONITOR_DIR the same way), so a root-only reader here would leave every nudge
# undelivered on a multi-user repo.
# If non-empty: atomic claim, emit additionalContext, log to chat_log.
# If empty or missing: exit 0 silently (no-op).

set -e

# Skip when invoked from the monitor's own claude session — otherwise the
# monitor's own Bash/Write tool calls would trigger this hook, which would
# consume the monitor's just-written nudge before the front agent ever sees
# it. Launch-monitor exports PLAYBOOK_ROLE=monitor specifically for this.
if [ "${PLAYBOOK_ROLE:-}" = "monitor" ]; then
    exit 0
fi

# Root + lane resolution are inlined rather than sourced from
# scripts/gate-echo-lib.sh: this file is registered as a NON-plugin hook and is
# copied into the project's own .claude/hooks/, so CLAUDE_PLUGIN_ROOT is unset
# and the plugin's scripts/ dir is at no fixed relative path. The contract is
# identical to that library (and to tasks/core.py and provider/paths.py);
# tests/wrapper-multiuser-fixture.sh asserts this copy agrees with them.

# Find project root: legacy `.agent/tasks/` OR multi-user `.agent/<user>/tasks/`.
find_root() {
    local dir="$PWD" sub
    while [ "$dir" != "/" ]; do
        [ -d "$dir/.agent/tasks" ] && echo "$dir" && return
        if [ -d "$dir/.agent" ]; then
            for sub in "$dir/.agent"/*/; do
                [ -d "${sub}tasks" ] && echo "$dir" && return
            done
        fi
        dir="$(dirname "$dir")"
    done
}

PROJECT_DIR=$(find_root)
[ -z "$PROJECT_DIR" ] && exit 0

# Resolve this user's lane. An unusable marker means we cannot tell whose nudge
# outbox to read: exit silently rather than read the shared root. Never fail the
# hook — a nudge is advisory, and taking down every tool call over a bad marker
# would be far worse than a missed nudge.
AGENT_DIR="$PROJECT_DIR/.agent"
if [ -f "$PROJECT_DIR/.agent/current_user" ]; then
    CU=""; CU_EXTRA=""
    # One-line contract — see gate-echo-lib.sh resolve_agent_dir.
    { read -r CU; read -r CU_EXTRA; } < "$PROJECT_DIR/.agent/current_user" 2>/dev/null || true
    CU="${CU%$'\r'}"
    [ -n "$CU_EXTRA" ] && exit 0
    case "$CU" in
        ""|"."|"..") exit 0 ;;
        [a-zA-Z0-9]*) ;;
        *) exit 0 ;;
    esac
    case "$CU" in
        *[!a-zA-Z0-9_.-]*) exit 0 ;;
    esac
    AGENT_DIR="$PROJECT_DIR/.agent/$CU"
fi

# Read stdin to extract hook_event_name (PostToolUse or UserPromptSubmit)
INPUT=$(cat)
export EVENT_NAME=$(echo "$INPUT" | python3 -c "import sys,json; print(json.loads(sys.stdin.read() or '{}').get('hook_event_name','UserPromptSubmit'))" 2>/dev/null || echo "UserPromptSubmit")

SESSION_ID="${PLAYBOOK_SESSION_ID:-pid-$PPID}"
NUDGE_FILE="$AGENT_DIR/monitor/nudge.md"

# Orphan recovery: a crash between this hook's rename-claim (below) and either
# its success `rm` or its failure `ln`-restore STRANDS a
# `nudge.md.delivering.<pid>.<rand>` file. Later invocations only look at
# nudge.md, so that already-claimed nudge is silently lost — breaking the
# at-least-once contract the unique-claim design establishes. Before making this
# invocation's own claim, re-land any stranded claim whose OWNER PROCESS IS DEAD.
# A claim whose pid is still alive is a concurrent in-flight delivery: leave it.
# All arms are `set -e`-safe (guards / `|| true` / `if`), and $$/$RANDOM/kill are
# bash 3.2 builtins (macOS ok). Re-landing uses the SAME atomic `ln` no-clobber
# as the failure path so a stale orphan can never destroy a newer live nudge.
for orphan in "$AGENT_DIR"/monitor/nudge.md.delivering.*; do
    [ -e "$orphan" ] || continue                    # glob had no match
    if [ ! -s "$orphan" ]; then                     # empty stray claim — just drop it
        rm -f "$orphan" 2>/dev/null || true
        continue
    fi
    obase=${orphan##*/nudge.md.delivering.}         # <pid>.<rand>
    opid=${obase%%.*}                               # <pid>
    case "$opid" in
        ''|*[!0-9]*) continue ;;                    # unparseable pid — don't guess, leave it
    esac
    if kill -0 "$opid" 2>/dev/null; then
        continue                                    # owner alive → in-flight, not orphaned
    fi
    # Owner dead → re-land the orphan as nudge.md, atomically and no-clobber.
    if ln "$orphan" "$NUDGE_FILE" 2>/dev/null; then
        rm -f "$orphan" 2>/dev/null || true         # linked → drop the extra name
    elif [ -e "$NUDGE_FILE" ]; then
        rm -f "$orphan" 2>/dev/null || true         # a newer nudge won; drop stale orphan
    else
        # ln unavailable/failing and no nudge.md — non-atomic fallback (as legacy).
        mv "$orphan" "$NUDGE_FILE" 2>/dev/null || rm -f "$orphan" 2>/dev/null || true
    fi
done

# No nudge file or empty — silent exit
[ -f "$NUDGE_FILE" ] || exit 0
[ -s "$NUDGE_FILE" ] || exit 0

# Atomic claim: rename the single nudge.md to a PER-INVOCATION UNIQUE path so
# concurrent hook firings can't collide. With a fixed shared `.delivering` name,
# a second firing's `mv` overwrote a first firing's in-flight claim (or both read
# the same claimed file) — dropping or double-delivering a nudge. A unique
# destination makes `mv` the mutual exclusion: exactly ONE concurrent firing can
# rename the single nudge.md (rename is atomic on the source inode); the losers'
# `mv` finds the source already gone and exits silently. `$$` (this hook
# process's pid) + `$RANDOM` are bash builtins (bash 3.2 / macOS ok); together
# they're unique across concurrent firings and robust to pid reuse.
DELIVERING="$NUDGE_FILE.delivering.$$.$RANDOM"
mv "$NUDGE_FILE" "$DELIVERING" 2>/dev/null || exit 0

# Read content
NUDGE_CONTENT=$(cat "$DELIVERING")

# Skip if content is empty after read (drop the claimed empty file)
[ -z "$NUDGE_CONTENT" ] && { rm -f "$DELIVERING"; exit 0; }

# Emit additionalContext — this is what gets injected into the agent's context.
# The nudge was already CLAIMED (moved) above, so a failed emit (e.g. python3
# missing) used to consume-and-drop it under `set -e`. Build the JSON first, and
# only clear the claim on a SUCCESSFUL emit; on failure RESTORE the file so a
# later session can still deliver it — a nudge is delivered at-least-once, never
# silently lost. (stderr, not stdout, would carry any diagnostic — but here we
# simply leave the nudge for next time.)
if NUDGE_JSON=$(python3 -c "
import json, sys, os
nudge = sys.stdin.read().strip()
event = os.environ.get('EVENT_NAME', 'UserPromptSubmit')
out = {
    'hookSpecificOutput': {
        'hookEventName': event,
        'additionalContext': '[MONITOR] ' + nudge
    }
}
print(json.dumps(out))
" <<< "$NUDGE_CONTENT" 2>/dev/null) && [ -n "$NUDGE_JSON" ]; then
    printf '%s\n' "$NUDGE_JSON"
    rm -f "$DELIVERING"
else
    # Emit failed (e.g. python3 missing): preserve at-least-once — re-land our
    # claim as nudge.md. But NEVER clobber a NEWER nudge the monitor may have
    # written after our claim. A `[ -e ] then mv` check is NOT atomic — the
    # monitor can write newer B between the test and the mv, and mv would
    # overwrite+lose B. `ln` (hard link, no `-f`) is atomic no-replace: it FAILS
    # (EEXIST) if nudge.md already exists, so our OLDER claim can never destroy
    # the monitor's NEWER nudge. On success nudge.md is re-landed; either way the
    # claim file is then dropped. `ln` unavailable/failing for another reason
    # falls back to a best-effort non-atomic restore (this whole branch exists
    # precisely because tools can be missing). All arms are `set -e`-safe.
    if ln "$DELIVERING" "$NUDGE_FILE" 2>/dev/null; then
        rm -f "$DELIVERING" 2>/dev/null || true          # linked → drop the extra name
    elif [ -e "$NUDGE_FILE" ]; then
        rm -f "$DELIVERING" 2>/dev/null || true          # ln EEXIST: a newer nudge won; drop stale claim
    else
        # ln unavailable and no nudge.md — non-atomic fallback, same as legacy.
        mv "$DELIVERING" "$NUDGE_FILE" 2>/dev/null || rm -f "$DELIVERING" 2>/dev/null || true
    fi
    exit 0
fi

# Log to chat_log
LOCAL_LOG="$AGENT_DIR/chat_log.md"
if [ -f "$LOCAL_LOG" ]; then
    TIMESTAMP=$(date -u +"%Y-%m-%d %H:%M:%S UTC")
    {
        echo "---"
        echo ""
        echo "**[MONITOR→$SESSION_ID]** [$TIMESTAMP] ($EVENT_NAME)"
        echo ""
        echo "$NUDGE_CONTENT"
        echo ""
    } >> "$LOCAL_LOG"
fi
