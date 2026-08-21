#!/usr/bin/env bash
# Fixture for the state-echo-hook gate-logging write-failure warning (task 018 /
# bug report #4). A write that fails AFTER the AGENT_WRITABLE probe (e.g. a
# Windows AV lock on the counter mv, or a non-writable sessions dir) used to be
# swallowed by `set -e` — the hook died mid-write, the gate_key froze, and gate
# logging stopped with NO warning. The fix guards the writes (fail-open) and
# surfaces a loud warning, suppressed inside a sandboxed judge.
#
# Run: bash claude-playbook-plugin/tests/gate-logging-failure-fixture.sh
# Exit 0 if all scenarios pass, non-zero on first failure.

set -uo pipefail   # NOT -e: we intentionally provoke write failures

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPTS="$HERE/../plugins/playbook/scripts"
HOOK="$SCRIPTS/state-echo-hook"

PASS=0
FAIL=0
pass() { echo "  PASS  $*"; PASS=$((PASS+1)); }
fail() { echo "  FAIL  $*"; FAIL=$((FAIL+1)); }

# Scenarios 1-2 provoke the write failure with `chmod 500` on the session dir.
# That has no effect on Windows/git-bash — a dir stays writable to its owner — so
# no failure occurs, the warning never fires, and the assertions that depend on a
# real failure are unreachable there. Guard them win-only (unreachable on
# Linux/macOS, where uname is Linux/Darwin).
case "$(uname -s 2>/dev/null)" in
    MINGW*|MSYS*|CYGWIN*) IS_WINDOWS=1 ;;
    *)                    IS_WINDOWS=0 ;;
esac

echo "=== gate-logging write-failure fixture ==="

# Build a minimal playbook project with task 001 ACTIVE (session state points at
# it) — gate logging only runs for an active task, so the write-failure path is
# only reachable here.
SID="pid-test"
make_project() {
    local root="$1"
    mkdir -p "$root/.agent/tasks/001-x"
    printf '# 001 - X\n\n## Status\npending\n\n## Design Phase\n- [ ] a gate\n' \
        > "$root/.agent/tasks/001-x/task.md"
    : > "$root/.agent/chat_log.md"
    mkdir -p "$root/.agent/sessions/$SID"
    printf '001\n' > "$root/.agent/sessions/$SID/current_state"
}

# Lock the session dir read-only (r-x): current_state stays readable so the task
# is still active, but the counter mv/write fails — the exact post-probe failure.
lock_session() { chmod 500 "$1/.agent/sessions/$SID"; }
unlock_session() { chmod 700 "$1/.agent/sessions/$SID"; }

# Run the hook against $root with a synthetic PostToolUse payload; echo stdout.
run_hook() {
    local root="$1"; shift
    ( cd "$root" && printf '{"tool_name":"Read","tool_input":{"file_path":"x"}}' \
        | PLAYBOOK_SESSION_ID="pid-test" "$@" bash "$HOOK" 2>/dev/null )
}

# --- Scenario 1: post-probe write failure surfaces the warning ---------------
WORK1="$(mktemp -d)"; trap 'rm -rf "$WORK1"' EXIT
make_project "$WORK1"
# .agent is writable (probe passes) but the session dir is read-only, so the
# counter write fails AFTER the probe — the exact post-probe failure (T6).
lock_session "$WORK1"
OUT1="$(run_hook "$WORK1")"
RC1=$?
unlock_session "$WORK1"   # restore so cleanup works

if [ "$RC1" -eq 0 ]; then
    pass "hook exits 0 despite write failure (did not die under set -e)"
else
    fail "hook exited $RC1 — set -e killed it mid-write (the original bug)"
fi
if [ "$IS_WINDOWS" = 1 ]; then
    pass "write-failure warning skipped (windows: chmod 500 cannot make a dir unwritable to its owner, so no failure is provoked)"
elif printf '%s' "$OUT1" | grep -q "gate-logging write FAILED"; then
    pass "write failure surfaces the loud warning"
else
    fail "no warning emitted on write failure — output: $OUT1"
fi

# --- Scenario 2: warning suppressed inside a sandboxed judge -----------------
WORK2="$(mktemp -d)"
make_project "$WORK2"
lock_session "$WORK2"
OUT2="$(run_hook "$WORK2" env PLAYBOOK_SANDBOXED=1)"
unlock_session "$WORK2"; rm -rf "$WORK2"
if [ "$IS_WINDOWS" = 1 ]; then
    pass "sandbox-suppression skipped (windows: no write failure to suppress — see above)"
elif printf '%s' "$OUT2" | grep -q "gate-logging write FAILED"; then
    fail "warning leaked into a sandboxed judge (PLAYBOOK_SANDBOXED=1) — would spam verdicts"
else
    pass "warning suppressed when PLAYBOOK_SANDBOXED=1"
fi

# --- Scenario 3: healthy writes emit NO warning (no false positive) ----------
WORK3="$(mktemp -d)"
make_project "$WORK3"
OUT3="$(run_hook "$WORK3")"
rm -rf "$WORK3"
if printf '%s' "$OUT3" | grep -q "gate-logging write FAILED"; then
    fail "warning fired on a healthy writable project (false positive)"
else
    pass "no warning when writes succeed"
fi

echo ""
echo "  $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
