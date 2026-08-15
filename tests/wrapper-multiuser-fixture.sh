#!/usr/bin/env bash
# Multi-user lane fixture for the provider wrappers (task 022).
#
# The wrappers are the entry point that PROVISIONS session state, so if they
# resolve the wrong lane every downstream surface reads an empty session:
# `tasks work <N>` writes `.agent/<user>/sessions/<id>/current_state` while a
# root-only wrapper created `.agent/sessions/<id>/` — split-brain, and gate
# enforcement silently sees "no active task".
#
# Each wrapper is executed for real against scratch repos with the provider
# binary PATH-shimmed to a recorder, across the layouts that matter:
#
#   legacy       .agent/tasks/                  → root lane (must not regress)
#   multi-user   .agent/<user>/tasks/ + marker  → the per-user lane
#   subdir       launched from deep inside      → same root, same lane
#   fresh clone  lanes but NO marker            → fail loud, create NOTHING
#   invalid      marker with a slash/empty/…    → fail loud, create NOTHING
#   non-playbook no .agent at all               → still launches (bare CLI)
#   orphaned     wrapper without gate-echo-lib  → fail loud, don't launch
#
# Run from anywhere: `bash playbook-plugin/tests/wrapper-multiuser-fixture.sh`.
# Exits 0 if every scenario passes, non-zero otherwise.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPTS="$HERE/../plugins/playbook/scripts"

PASS=0
FAIL=0
pass() { echo "  PASS  $*"; PASS=$((PASS+1)); }
fail() { echo "  FAIL  $*"; FAIL=$((FAIL+1)); }

assert_eq() {
    local got="$1" want="$2" label="$3"
    if [ "$got" = "$want" ]; then
        pass "$label"
    else
        fail "$label — expected [$want], got [$got]"
    fi
}

assert_contains() {
    local haystack="$1" needle="$2" label="$3"
    if printf '%s' "$haystack" | grep -qF "$needle"; then
        pass "$label"
    else
        fail "$label — expected to find '$needle'"
        echo "----- output start -----"; printf '%s\n' "$haystack"; echo "----- output end -----"
    fi
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ── Provider binary shims ────────────────────────────────────────────────────
# Each records the env the wrapper handed it, so we can assert on PROJECT_ROOT
# without launching a real agent. "launched" in the marker file is the proof the
# wrapper reached its exec (as opposed to failing loud before it).
SHIM_BIN="$WORK/bin"
mkdir -p "$SHIM_BIN"
for prov in codex grok agy pi; do
    cat > "$SHIM_BIN/$prov" <<'EOF'
#!/bin/bash
echo "launched root=${PLAYBOOK_PROJECT_ROOT:-} session=${PLAYBOOK_SESSION_ID:-} provider=${PLAYBOOK_PROVIDER:-}"
EOF
    chmod +x "$SHIM_BIN/$prov"
done
# launch-monitor ends in `exec sandbox-exec … claude …`. If a future change let
# it get that far under test, an interactive claude would hang the suite
# forever (it did, once). Shim both so reaching exec is a fast, visible event
# rather than a hang.
for stub in claude sandbox-exec; do
    cat > "$SHIM_BIN/$stub" <<'EOF'
#!/bin/bash
echo "REACHED_EXEC $(basename "$0")" >&2
exit 97
EOF
    chmod +x "$SHIM_BIN/$stub"
done
export PATH="$SHIM_BIN:$PATH"

# playbook-pi refuses to launch unless the model is allow-listed, and it copies
# its provider config into the lane. Pin the model to the first shipped id so
# the wrapper reaches exec in these scenarios too.
PI_MODEL="$(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
ms = d.get("providers", {}).get("omlx", {}).get("models", [])
print(ms[0]["id"] if ms else "")
' "$SCRIPTS/playbook-pi-omlx-models.json")"

# run_wrapper PROVIDER CWD [ARGS...] → sets globals OUT and RC.
#
# Deliberately NOT "echo the output and let the caller capture it": wrapping this
# in $( ) puts the whole function in a subshell, so the RC it assigns is thrown
# away and every exit-code assertion silently compares against a stale 0. That
# is the vacuous-green failure this fixture exists to catch, so it must not be
# how the fixture itself is wired.
OUT=""
RC=0
run_wrapper() {
    local prov="$1" cwd="$2"; shift 2
    set +e
    OUT="$(cd "$cwd" && "$SCRIPTS/playbook-$prov" "$@" 2>&1)"
    RC=$?
    set -e
}

# Wrappers whose invocation needs no extra args, plus pi's model arg.
wrapper_args() {
    case "$1" in
        pi) printf '%s' "$PI_MODEL" ;;
        *)  printf '' ;;
    esac
}

# session_dirs AGENT_SUBPATH → count of pid-* dirs under that lane
count_sessions() {
    find "$1" -type d -name 'pid-*' 2>/dev/null | wc -l | tr -d ' '
}

# Build a scratch project of a given shape.
#   $1 = dir, $2 = shape, $3 = marker content (optional)
build_project() {
    local dir="$1" shape="$2" marker="${3:-}"
    mkdir -p "$dir"
    case "$shape" in
        legacy)      mkdir -p "$dir/.agent/tasks" ;;
        multi-user)  mkdir -p "$dir/.agent/alice/tasks" ;;
        mixed)       mkdir -p "$dir/.agent/tasks" "$dir/.agent/alice/tasks" ;;
        non-playbook) : ;;
    esac
    [ -n "$marker" ] && printf '%s\n' "$marker" > "$dir/.agent/current_user"
    return 0
}

PROVIDERS="codex grok agy pi"

echo "=== S1: legacy layout provisions the ROOT lane (no regression) ==="
{
    for prov in $PROVIDERS; do
        d="$WORK/s1-$prov"; build_project "$d" legacy
        run_wrapper "$prov" "$d" $(wrapper_args "$prov"); out="$OUT"
        assert_eq "$RC" "0" "S1/$prov exits 0"
        assert_contains "$out" "launched root=$d" "S1/$prov exports PROJECT_ROOT"
        assert_eq "$(count_sessions "$d/.agent")" "1" "S1/$prov provisions exactly one session dir"
        assert_eq "$(find "$d/.agent/sessions" -type d -name 'pid-*' 2>/dev/null | wc -l | tr -d ' ')" "1" \
            "S1/$prov session dir is at the ROOT lane (.agent/sessions/pid-*)"
    done
}

echo "=== S2: multi-user layout provisions the PER-USER lane (the bug) ==="
{
    for prov in $PROVIDERS; do
        d="$WORK/s2-$prov"; build_project "$d" multi-user alice
        run_wrapper "$prov" "$d" $(wrapper_args "$prov"); out="$OUT"
        assert_eq "$RC" "0" "S2/$prov exits 0"
        assert_contains "$out" "launched root=$d" "S2/$prov exports PROJECT_ROOT"
        assert_eq "$(find "$d/.agent/alice/sessions" -type d -name 'pid-*' 2>/dev/null | wc -l | tr -d ' ')" "1" \
            "S2/$prov provisions .agent/alice/sessions/pid-*"
        # The regression that motivated the task: a root sessions/ dir here means
        # the wrapper ignored the marker.
        assert_eq "$(find "$d/.agent/sessions" -type d -name 'pid-*' 2>/dev/null | wc -l | tr -d ' ')" "0" \
            "S2/$prov creates NO root-lane session dir"
    done
}

echo "=== S3: launched from a deep subdirectory, root + lane still correct ==="
{
    for prov in $PROVIDERS; do
        d="$WORK/s3-$prov"; build_project "$d" multi-user alice
        mkdir -p "$d/src/deep/nested"
        run_wrapper "$prov" "$d/src/deep/nested" $(wrapper_args "$prov"); out="$OUT"
        assert_eq "$RC" "0" "S3/$prov exits 0 from subdir"
        assert_contains "$out" "launched root=$d" "S3/$prov walks up to the project root"
        assert_eq "$(find "$d/.agent/alice/sessions" -type d -name 'pid-*' 2>/dev/null | wc -l | tr -d ' ')" "1" \
            "S3/$prov still lands in the alice lane"
    done
}

echo "=== S4: fresh clone (lanes, NO marker) fails loud and creates nothing ==="
{
    for prov in $PROVIDERS; do
        d="$WORK/s4-$prov"; build_project "$d" multi-user   # note: no marker
        run_wrapper "$prov" "$d" $(wrapper_args "$prov"); out="$OUT"
        assert_eq "$RC" "1" "S4/$prov exits 1"
        assert_contains "$out" "no .agent/current_user marker" "S4/$prov explains the missing marker"
        assert_contains "$out" "current_user" "S4/$prov prints the fix command"
        assert_eq "$(count_sessions "$d/.agent")" "0" "S4/$prov creates NO session dir anywhere"
        # Must not have reached exec.
        case "$out" in *launched*) fail "S4/$prov launched the agent anyway" ;; *) pass "S4/$prov did not launch the agent" ;; esac
    done
}

echo "=== S5: invalid marker fails loud and creates nothing ==="
{
    # One case per validation branch in resolve_agent_dir.
    for bad in "../evil" "" "." ".." "-dash" "has space"; do
        d="$WORK/s5-$(echo "$bad" | tr -c 'a-zA-Z0-9' '_')"
        build_project "$d" legacy
        printf '%s\n' "$bad" > "$d/.agent/current_user"
        run_wrapper codex "$d"; out="$OUT"
        assert_eq "$RC" "1" "S5[$bad] exits 1"
        assert_contains "$out" "invalid username" "S5[$bad] names the problem"
        assert_eq "$(count_sessions "$d/.agent")" "0" "S5[$bad] creates NO session dir"
    done
}

echo "=== S6: mixed layout (root tasks AND lanes, no marker) keeps working ==="
{
    # Root .agent/tasks/ is a legitimate lane, so the fresh-clone guard must NOT
    # fire here — this shape works today and must keep working.
    d="$WORK/s6"; build_project "$d" mixed
    run_wrapper codex "$d"; out="$OUT"
    assert_eq "$RC" "0" "S6 mixed layout still launches"
    assert_eq "$(find "$d/.agent/sessions" -type d -name 'pid-*' | wc -l | tr -d ' ')" "1" \
        "S6 mixed layout provisions the root lane"
}

echo "=== S7: non-playbook directory still launches the bare CLI ==="
{
    d="$WORK/s7"; build_project "$d" non-playbook
    run_wrapper codex "$d"; out="$OUT"
    assert_eq "$RC" "0" "S7 exits 0 outside a playbook project"
    assert_contains "$out" "launched root=" "S7 launches with an empty project root"
}

echo "=== S8: wrapper separated from gate-echo-lib.sh fails loud ==="
{
    d="$WORK/s8"; build_project "$d" legacy
    cp "$SCRIPTS/playbook-codex" "$WORK/orphan-codex"
    set +e
    out="$(cd "$d" && "$WORK/orphan-codex" 2>&1)"; rc=$?
    set -e
    assert_eq "$rc" "1" "S8 exits 1 when the hook library is missing"
    assert_contains "$out" "gate-echo-lib.sh" "S8 names the missing library"
    case "$out" in *launched*) fail "S8 launched the agent un-provisioned" ;; *) pass "S8 did not launch the agent" ;; esac
}

echo "=== S9: wrapper lane agrees with what the tasks CLI reads ==="
{
    # The actual bug is writer/reader divergence, so assert the wrapper's dir is
    # the one Python's resolve_agent_dir() names — not merely "a lane".
    d="$WORK/s9"; build_project "$d" multi-user alice
    run_wrapper codex "$d"
    wrapper_lane="$(find "$d/.agent" -type d -name 'pid-*' | head -1)"
    wrapper_lane="$(dirname "$(dirname "$wrapper_lane")")"
    cli_lane="$(PYTHONPATH="$SCRIPTS/.." python3 -c '
import sys
from pathlib import Path
from tasks.core import resolve_agent_dir
print(resolve_agent_dir(Path(sys.argv[1])))
' "$d")"
    assert_eq "$wrapper_lane" "$cli_lane" "S9 wrapper lane == tasks.core.resolve_agent_dir"
}

echo "=== S10: monitor-nudge.sh delivers from the LANE, not the root ==="
{
    # A nudge written by the monitor into <lane>/monitor/nudge.md must be
    # consumed and logged by the delivery hook. Before task 022 the monitor
    # wrote the lane while this hook read the root: nudges vanished silently.
    NUDGE_HOOK="$HERE/../plugins/playbook/hooks/monitor-nudge.sh"
    payload='{"hook_event_name":"PostToolUse"}'

    d="$WORK/s10-mu"; build_project "$d" multi-user alice
    mkdir -p "$d/.agent/alice/monitor"
    printf 'look at the test output\n' > "$d/.agent/alice/monitor/nudge.md"
    : > "$d/.agent/alice/chat_log.md"
    set +e
    out="$(cd "$d" && printf '%s' "$payload" | bash "$NUDGE_HOOK" 2>&1)"; rc=$?
    set -e
    assert_eq "$rc" "0" "S10 hook exits 0 on a lane nudge"
    assert_contains "$out" "[MONITOR] look at the test output" "S10 emits the lane nudge as additionalContext"
    [ -f "$d/.agent/alice/monitor/nudge.md" ] \
        && fail "S10 nudge file not consumed" \
        || pass "S10 consumed the nudge (atomic claim)"
    assert_contains "$(cat "$d/.agent/alice/chat_log.md")" "MONITOR→" "S10 logged into the LANE chat_log"

    # Legacy layout must behave exactly as before.
    d="$WORK/s10-legacy"; build_project "$d" legacy
    mkdir -p "$d/.agent/monitor"
    printf 'legacy nudge\n' > "$d/.agent/monitor/nudge.md"
    set +e
    out="$(cd "$d" && printf '%s' "$payload" | bash "$NUDGE_HOOK" 2>&1)"; rc=$?
    set -e
    assert_eq "$rc" "0" "S10 legacy hook exits 0"
    assert_contains "$out" "[MONITOR] legacy nudge" "S10 legacy nudge still delivered"

    # A root-lane nudge must NOT be delivered in a marker'd repo — that is the
    # cross-user leak the lane model exists to prevent.
    d="$WORK/s10-cross"; build_project "$d" multi-user alice
    mkdir -p "$d/.agent/monitor" "$d/.agent/alice/monitor"
    printf 'someone elses nudge\n' > "$d/.agent/monitor/nudge.md"
    set +e
    out="$(cd "$d" && printf '%s' "$payload" | bash "$NUDGE_HOOK" 2>&1)"; rc=$?
    set -e
    assert_eq "$rc" "0" "S10 cross-lane: hook exits 0"
    case "$out" in
        *"someone elses nudge"*) fail "S10 delivered another lane's nudge" ;;
        *) pass "S10 did NOT deliver the root-lane nudge" ;;
    esac
    [ -f "$d/.agent/monitor/nudge.md" ] \
        && pass "S10 left the other lane's nudge untouched" \
        || fail "S10 consumed another lane's nudge"

    # Unusable marker: silent no-op, and the root nudge stays unread.
    d="$WORK/s10-bad"; build_project "$d" multi-user
    printf '../evil\n' > "$d/.agent/current_user"
    mkdir -p "$d/.agent/monitor"
    printf 'should not be read\n' > "$d/.agent/monitor/nudge.md"
    set +e
    out="$(cd "$d" && printf '%s' "$payload" | bash "$NUDGE_HOOK" 2>&1)"; rc=$?
    set -e
    assert_eq "$rc" "0" "S10 invalid marker exits 0 (never bricks a tool call)"
    assert_eq "$out" "" "S10 invalid marker emits nothing"
    [ -f "$d/.agent/monitor/nudge.md" ] \
        && pass "S10 invalid marker did not fall back to the root nudge" \
        || fail "S10 invalid marker consumed the root nudge"
}

echo "=== S11: monitor-nudge's inlined resolver agrees with the shared ones ==="
{
    # monitor-nudge.sh cannot source gate-echo-lib.sh (non-plugin hook, copied
    # into .claude/hooks/), so its resolver is a hand copy. Pin it against the
    # library and against tasks.core so the copies cannot drift apart.
    NUDGE_HOOK="$HERE/../plugins/playbook/hooks/monitor-nudge.sh"
    payload='{"hook_event_name":"PostToolUse"}'
    for name in alice bob-2 u_1 Team.Lead; do
        d="$WORK/s11-$name"; mkdir -p "$d/.agent/$name/tasks/x" "$d/.agent/$name/monitor"
        printf '%s\n' "$name" > "$d/.agent/current_user"
        printf 'nudge for %s\n' "$name" > "$d/.agent/$name/monitor/nudge.md"
        set +e
        out="$(cd "$d" && printf '%s' "$payload" | bash "$NUDGE_HOOK" 2>&1)"
        set -e
        assert_contains "$out" "nudge for $name" "S11[$name] hook resolved the same lane as the library"

        lib_lane="$(bash -c "source '$SCRIPTS/gate-echo-lib.sh'; resolve_agent_dir '$d'")"
        assert_eq "$lib_lane" "$d/.agent/$name" "S11[$name] gate-echo-lib agrees"
    done

    # And every reject: the hook must stay silent wherever the library errors.
    for bad in "" "." ".." "a/b" "-dash" "has space"; do
        d="$WORK/s11-bad$(echo "$bad" | tr -c 'a-z' '_')"
        mkdir -p "$d/.agent/alice/tasks/x" "$d/.agent/monitor"
        printf '%s\n' "$bad" > "$d/.agent/current_user"
        printf 'leak\n' > "$d/.agent/monitor/nudge.md"
        set +e
        out="$(cd "$d" && printf '%s' "$payload" | bash "$NUDGE_HOOK" 2>&1)"; rc=$?
        bash -c "source '$SCRIPTS/gate-echo-lib.sh'; resolve_agent_dir '$d'" >/dev/null 2>&1; lib_rc=$?
        set -e
        assert_eq "$rc" "0" "S11[$bad] hook still exits 0"
        assert_eq "$out" "" "S11[$bad] hook emitted nothing"
        [ "$lib_rc" -ne 0 ] \
            && pass "S11[$bad] library rejects it too (same verdict)" \
            || fail "S11[$bad] library ACCEPTED a marker the hook rejected — drift"
    done
}

echo "=== S12: monitor bootstrap briefing reads the LANE's tasks + chat log ==="
{
    # bootstrap.sh builds the monitor's whole briefing. Its two embedded Python
    # blocks used to glob '<root>/.agent/tasks/*/task.md', so on a multi-user
    # repo the monitor was briefed on an empty project — a shell-only lane fix
    # would have looked correct while leaving this broken.
    BOOTSTRAP="$HERE/../plugins/playbook/scripts/monitor-lib/bootstrap.sh"

    build_monitor_project() {   # $1=dir  $2=lane-subpath ("" for root)
        local d="$1" lane="$1/.agent${2:+/$2}"
        python3 - "$d" "$lane" "${2:-}" <<'PYEOF'
import sys, pathlib
d, lane, user = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
(lane / "tasks" / "003-lane-demo").mkdir(parents=True, exist_ok=True)
(lane / "tasks" / "003-lane-demo" / "task.md").write_text(
    "# 003 - Lane Demo\n\n## Status\nin_progress\n\n## Intent\nLANE_TASK_MARKER\n")
(lane / "chat_log.md").write_text("stop, that is not what i meant\n")
(d / "MIND_MAP.md").write_text("# map\n")
if user:
    (d / ".agent" / "current_user").write_text(user + "\n")
PYEOF
    }

    d="$WORK/s12-mu"; mkdir -p "$d"; build_monitor_project "$d" alice
    set +e
    out="$(cd "$d" && PLAYBOOK_PROJECT_DIR="$d" PLAYBOOK_SESSION_ID="pid-1" bash "$BOOTSTRAP" 2>&1)"; rc=$?
    set -e
    assert_eq "$rc" "0" "S12 bootstrap exits 0 on a multi-user repo"
    assert_contains "$out" "LANE_TASK_MARKER" "S12 briefing includes the LANE's task"
    assert_contains "$out" "that is not what i meant" "S12 briefing greps the LANE's chat_log"
    [ -d "$d/.agent/alice/monitor" ] \
        && pass "S12 MONITOR_DIR created in the lane" \
        || fail "S12 MONITOR_DIR not in the lane"
    [ -d "$d/.agent/monitor" ] \
        && fail "S12 also created a root-lane monitor dir" \
        || pass "S12 created NO root-lane monitor dir"

    # Legacy layout: identical behavior to before the change.
    d="$WORK/s12-legacy"; mkdir -p "$d"; build_monitor_project "$d" ""
    set +e
    out="$(cd "$d" && PLAYBOOK_PROJECT_DIR="$d" PLAYBOOK_SESSION_ID="pid-1" bash "$BOOTSTRAP" 2>&1)"; rc=$?
    set -e
    assert_eq "$rc" "0" "S12 legacy bootstrap exits 0"
    assert_contains "$out" "LANE_TASK_MARKER" "S12 legacy briefing still finds the task"
    [ -d "$d/.agent/monitor" ] \
        && pass "S12 legacy MONITOR_DIR still at the root" \
        || fail "S12 legacy MONITOR_DIR missing"

    # PLAYBOOK_AGENT_DIR (exported by launch-monitor) must be honored verbatim.
    d="$WORK/s12-export"; mkdir -p "$d"; build_monitor_project "$d" alice
    set +e
    out="$(cd "$d" && PLAYBOOK_PROJECT_DIR="$d" PLAYBOOK_AGENT_DIR="$d/.agent/alice" \
          PLAYBOOK_SESSION_ID="pid-1" bash "$BOOTSTRAP" 2>&1)"; rc=$?
    set -e
    assert_eq "$rc" "0" "S12 bootstrap honors an exported PLAYBOOK_AGENT_DIR"
    assert_contains "$out" "LANE_TASK_MARKER" "S12 exported agent dir briefs on the right lane"
}

echo "=== S13: launch-monitor resolves lane + root without hardcoding ==="
{
    # launch-monitor ends in `exec sandbox-exec … claude`, so drive it only as
    # far as its resolution logic: with no alive session it must fail in
    # find_main_session, and the error names the LANE's sessions dir.
    LAUNCH="$HERE/../plugins/playbook/scripts/monitor-lib/launch-monitor"

    d="$WORK/s13-mu"; build_project "$d" multi-user alice
    set +e
    out="$(cd "$d" && env -u PLAYBOOK_SESSION_ID PLAYBOOK_PROJECT_DIR="$d" bash "$LAUNCH" 2>&1)"; rc=$?
    set -e
    assert_eq "$rc" "1" "S13 exits 1 when no agent is running"
    assert_contains "$out" "$d/.agent/alice/sessions" "S13 looked in the LANE's sessions dir"

    d="$WORK/s13-legacy"; build_project "$d" legacy
    set +e
    out="$(cd "$d" && env -u PLAYBOOK_SESSION_ID PLAYBOOK_PROJECT_DIR="$d" bash "$LAUNCH" 2>&1)"; rc=$?
    set -e
    assert_eq "$rc" "1" "S13 legacy exits 1 when no agent is running"
    assert_contains "$out" "$d/.agent/sessions" "S13 legacy looked in the root sessions dir"

    # A9: launched from a subdirectory with no PLAYBOOK_PROJECT_DIR, the walk
    # must still find the project (a bare $PWD would resolve to the subdir).
    d="$WORK/s13-subdir"; build_project "$d" multi-user alice
    mkdir -p "$d/src/deep"
    set +e
    out="$(cd "$d/src/deep" && env -u PLAYBOOK_PROJECT_DIR -u PLAYBOOK_SESSION_ID bash "$LAUNCH" 2>&1)"; rc=$?
    set -e
    assert_eq "$rc" "1" "S13 subdir exits 1 when no agent is running"
    assert_contains "$out" "$d/.agent/alice/sessions" "S13 subdir walked up to the project lane"
}

echo "=== S14: init provisions the lane, never a phantom root lane ==="
{
    INIT="$SCRIPTS/init"
    # init writes ~/.claude/{bash-log.*,settings.json} and a shell rc line, so it
    # gets an isolated HOME. These runs used the developer's REAL home until task
    # 027 — every suite run mutated the machine it was testing on.
    S14_HOME="$WORK/s14-home"; mkdir -p "$S14_HOME"

    # (a) Brand-new project: everything at the root, exactly as before.
    d="$WORK/s14-new"; mkdir -p "$d"
    set +e
    out="$(cd "$d" && HOME="$S14_HOME" bash "$INIT" 2>&1)"; rc=$?
    set -e
    assert_eq "$rc" "0" "S14 new project: init exits 0"
    for sub in tasks playbooks monitor; do
        [ -d "$d/.agent/$sub" ] \
            && pass "S14 new project: .agent/$sub created" \
            || fail "S14 new project: .agent/$sub missing"
    done
    [ -f "$d/.agent/config.json" ] \
        && pass "S14 new project: config.json at the ROOT (shared policy)" \
        || fail "S14 new project: config.json missing"

    # (b) Multi-user: runtime state in the lane, shared policy still at root.
    d="$WORK/s14-mu"; build_project "$d" multi-user alice
    set +e
    out="$(cd "$d" && HOME="$S14_HOME" bash "$INIT" 2>&1)"; rc=$?
    set -e
    assert_eq "$rc" "0" "S14 multi-user: init exits 0"
    for sub in tasks playbooks monitor; do
        [ -d "$d/.agent/alice/$sub" ] \
            && pass "S14 multi-user: .agent/alice/$sub created" \
            || fail "S14 multi-user: .agent/alice/$sub missing"
        [ -d "$d/.agent/$sub" ] \
            && fail "S14 multi-user: also created a root .agent/$sub" \
            || pass "S14 multi-user: no root .agent/$sub"
    done
    # D4: config.json/models.json are root-by-design and must NOT move.
    [ -f "$d/.agent/config.json" ] \
        && pass "S14 multi-user: config.json stayed at the root" \
        || fail "S14 multi-user: config.json was moved into the lane"
    [ -f "$d/.agent/alice/config.json" ] \
        && fail "S14 multi-user: config.json duplicated into the lane" \
        || pass "S14 multi-user: no lane-local config.json"

    # (c) Fresh clone: refuse rather than create a phantom root lane.
    d="$WORK/s14-fresh"; build_project "$d" multi-user   # lanes, no marker
    set +e
    out="$(cd "$d" && HOME="$S14_HOME" bash "$INIT" 2>&1)"; rc=$?
    set -e
    assert_eq "$rc" "1" "S14 fresh clone: init exits 1"
    assert_contains "$out" "no .agent/current_user marker" "S14 fresh clone: explains why"
    [ -d "$d/.agent/tasks" ] \
        && fail "S14 fresh clone: created a phantom root lane" \
        || pass "S14 fresh clone: created NO phantom root lane"
    [ -f "$d/.agent/config.json" ] \
        && fail "S14 fresh clone: wrote config.json before bailing" \
        || pass "S14 fresh clone: wrote nothing at all"

    # (d) Idempotent re-run on the multi-user repo — still no root lane.
    d="$WORK/s14-mu"
    set +e
    out="$(cd "$d" && HOME="$S14_HOME" bash "$INIT" 2>&1)"; rc=$?
    set -e
    assert_eq "$rc" "0" "S14 re-run: init exits 0"
    assert_contains "$out" "exists" "S14 re-run: reports existing state as skipped"
    [ -d "$d/.agent/tasks" ] \
        && fail "S14 re-run: created a root lane on second init" \
        || pass "S14 re-run: still no root lane"
}

echo "=== S15: NO surface mints a root lane on a fresh clone (impl-panel I2) ==="
{
    # The impl panel showed the guard covered only `init` and the launchers.
    # A judge proved it was self-defeating: one `tasks new` created root
    # `.agent/tasks/`, after which the (correct, tested) mixed-layout exemption
    # treats the repo as legitimate and every surface writes root forever.
    # So this scenario drives EVERY state creator against one fresh clone and
    # then asserts the root lane is still untouched.
    d="$WORK/s15"; build_project "$d" multi-user   # lane 'alice', no marker
    printf '# x\n' > "$d/CLAUDE.md"; printf '# m\n' > "$d/MIND_MAP.md"

    run_in_repo() {   # $1=label, rest=command
        local label="$1"; shift
        set +e
        S15_OUT="$(cd "$d" && env -u PLAYBOOK_SESSION_ID "$@" 2>&1)"; S15_RC=$?
        set -e
    }

    run_in_repo "tasks new" "$SCRIPTS/tasks" new feature demo intent
    assert_eq "$S15_RC" "1" "S15 tasks new refuses"
    assert_contains "$S15_OUT" "no .agent/current_user marker" "S15 tasks new explains"

    run_in_repo "tasks init" "$SCRIPTS/tasks" init
    assert_eq "$S15_RC" "1" "S15 tasks init refuses"

    run_in_repo "bash init" bash "$SCRIPTS/init"
    assert_eq "$S15_RC" "1" "S15 bash init refuses"

    run_in_repo "launch-monitor" env PLAYBOOK_PROJECT_DIR="$d" \
        bash "$SCRIPTS/monitor-lib/launch-monitor"
    assert_eq "$S15_RC" "1" "S15 launch-monitor refuses"

    # Hooks must SKIP, never exit non-zero — aborting would brick the session.
    set +e
    out="$(cd "$d" && echo '{}' | env -u PLAYBOOK_SESSION_ID bash "$SCRIPTS/session-start-hook" 2>&1)"; rc=$?
    set -e
    assert_eq "$rc" "0" "S15 session-start-hook exits 0 (never bricks a session)"
    assert_contains "$out" "current_user is missing" "S15 session-start-hook warns the user"

    # The gate must still deny — with no knowable lane there is no active task.
    set +e
    (cd "$d" && printf '{"tool_name":"Edit","tool_input":{"file_path":"%s/a.py"}}' "$d" \
        | env -u PLAYBOOK_SESSION_ID bash "$SCRIPTS/task-gate-hook" >/dev/null 2>&1)
    rc=$?
    set -e
    assert_eq "$rc" "2" "S15 task-gate-hook still blocks (fail-closed)"

    set +e
    (cd "$d" && printf '{"hook_event_name":"UserPromptSubmit","prompt":"hi"}' \
        | env -u PLAYBOOK_SESSION_ID bash "$SCRIPTS/chat-log-hook" >/dev/null 2>&1)
    rc=$?
    set -e
    assert_eq "$rc" "0" "S15 chat-log-hook exits 0"

    # The whole point: after all of that, the root lane must be untouched.
    for stray in tasks sessions monitor playbooks chat_log.md chat_log_counter bash_history config.json; do
        [ -e "$d/.agent/$stray" ] \
            && fail "S15 root .agent/$stray was created on a fresh clone" \
            || pass "S15 no root .agent/$stray"
    done
    assert_eq "$(ls -1 "$d/.agent" | tr '\n' ' ')" "alice " "S15 .agent contains ONLY the pre-existing lane"
}

echo "=== S16: shell command loggers write the lane the CLI reads (impl-panel I6) ==="
{
    # These shipped with zero automated coverage while carrying the trickiest
    # semantics in the change (DEBUG-trap errexit, CRLF, multi-line markers).
    # Note `zsh -f`: without it zsh sources ~/.zshenv, which on a dogfooding
    # host loads the INSTALLED (old) logger and double-logs — that faked six
    # failures during development.
    BASH_LOG="$SCRIPTS/bash-log.sh"
    ZSH_LOG="$SCRIPTS/bash-log.zsh"

    cli_lane() {   # the file the tasks CLI will read
        PYTHONPATH="$SCRIPTS/.." python3 -c '
import sys
from pathlib import Path
from tasks.core import resolve_agent_dir
print(resolve_agent_dir(Path(sys.argv[1])))
' "$1" 2>/dev/null
    }

    write_marker() {   # $1=dir  $2=python-escaped bytes
        python3 -c '
import sys, pathlib
d = pathlib.Path(sys.argv[1]); (d/".agent").mkdir(parents=True, exist_ok=True)
with open(d/".agent"/"current_user", "w", newline="") as fh:
    fh.write(sys.argv[2])
' "$1" "$2"
    }

    # zsh is optional on the host: skip the zsh-logger half cleanly when it is
    # absent (otherwise `set -e` + a 127 from a missing zsh aborts the whole
    # fixture, losing the bash-logger, S15, and S18 session-GC coverage).
    if command -v zsh >/dev/null 2>&1; then _HAVE_ZSH=1; else
        _HAVE_ZSH=0
        echo "  (zsh not installed — S16 zsh-logger assertions skipped)"
    fi
    log_bash() { (cd "$1" && bash -c 'source "$1"; echo probe >/dev/null' _ "$BASH_LOG" >/dev/null 2>&1); }
    log_zsh()  { [ "$_HAVE_ZSH" = 1 ] || return 0; (cd "$1" && ZSH_EXECUTION_STRING="echo probe" zsh -f -c "source '$ZSH_LOG'" >/dev/null 2>&1); }

    # --- legacy: root, and it must be the file the CLI reads ---
    d="$WORK/s16-legacy"; build_project "$d" legacy
    log_bash "$d"; log_zsh "$d"
    assert_eq "$(ls "$d/.agent/bash_history" 2>/dev/null)" "$d/.agent/bash_history" \
        "S16 legacy: history at the root lane"
    assert_eq "$d/.agent" "$(cli_lane "$d")" "S16 legacy: writer lane == CLI reader lane"

    # --- multi-user: the lane, and NOT the root ---
    d="$WORK/s16-mu"; build_project "$d" multi-user alice
    log_bash "$d"; log_zsh "$d"
    [ -f "$d/.agent/alice/bash_history" ] \
        && pass "S16 multi-user: history in .agent/alice/" \
        || fail "S16 multi-user: no lane history file"
    [ -f "$d/.agent/bash_history" ] \
        && fail "S16 multi-user: also wrote the root history" \
        || pass "S16 multi-user: no root history"
    assert_eq "$d/.agent/alice/bash_history" "$(cli_lane "$d")/bash_history" \
        "S16 multi-user: writer file == the file tasks retro/context read"
    # Both shells logged into the same file (bash always; zsh only if present).
    _EXPECT_LOGS=$([ "$_HAVE_ZSH" = 1 ] && echo 2 || echo 1)
    assert_eq "$(grep -c 'AGENT' "$d/.agent/alice/bash_history")" "$_EXPECT_LOGS" \
        "S16 shells appended to the lane file (bash$([ "$_HAVE_ZSH" = 1 ] && echo '+zsh'))"

    # --- CRLF marker: must resolve, not silently disable logging ---
    d="$WORK/s16-crlf"; mkdir -p "$d/.agent/alice/tasks"
    write_marker "$d" 'alice
'
    python3 -c '
import pathlib,sys
p=pathlib.Path(sys.argv[1])/".agent"/"current_user"
open(p,"w",newline="").write("alice\r\n")' "$d"
    log_bash "$d"; log_zsh "$d"
    [ -f "$d/.agent/alice/bash_history" ] \
        && pass "S16 CRLF marker still logs to the lane" \
        || fail "S16 CRLF marker silently disabled logging"

    # --- smuggled second line: must NOT resolve to lane 'alice' ---
    d="$WORK/s16-smuggle"; mkdir -p "$d/.agent/alice/tasks"
    python3 -c '
import pathlib,sys
p=pathlib.Path(sys.argv[1])/".agent"/"current_user"
open(p,"w",newline="").write("alice\n../evil\n")' "$d"
    log_bash "$d"; log_zsh "$d"
    [ -f "$d/.agent/alice/bash_history" ] \
        && fail "S16 multi-line marker resolved to lane alice" \
        || pass "S16 multi-line marker rejected (no lane history)"
    [ -f "$d/.agent/bash_history" ] \
        && fail "S16 multi-line marker fell back to the root" \
        || pass "S16 multi-line marker wrote nothing at all"

    # --- invalid marker: nothing written anywhere, shell survives ---
    for bad in "../evil" "" "." "has space"; do
        d="$WORK/s16-bad$(echo "$bad" | tr -c 'a-z' '_')"
        mkdir -p "$d/.agent/alice/tasks"
        printf '%s\n' "$bad" > "$d/.agent/current_user"
        set +e
        out="$( (cd "$d" && bash -c 'source "$1"; echo probe >/dev/null; echo ALIVE' _ "$BASH_LOG" 2>&1) )"
        set -e
        assert_contains "$out" "ALIVE" "S16[$bad] shell survives"
        assert_eq "$(find "$d" -name bash_history | wc -l | tr -d ' ')" "0" \
            "S16[$bad] wrote no history anywhere"
    done

    # --- no trailing newline: read returns 1; errexit must not fire ---
    d="$WORK/s16-nonl"; mkdir -p "$d/.agent/alice/tasks"
    printf 'alice' > "$d/.agent/current_user"
    set +e
    out="$( (cd "$d" && bash -c 'set -e; source "$1"; echo probe >/dev/null; echo ALIVE' _ "$BASH_LOG" 2>&1) )"
    set -e
    assert_contains "$out" "ALIVE" "S16 no-trailing-newline marker: errexit did not fire"
    [ -f "$d/.agent/alice/bash_history" ] \
        && pass "S16 no-trailing-newline marker still logs to the lane" \
        || fail "S16 no-trailing-newline marker lost the log"
}

echo "=== S17: DEBUG trap must not kill set -e hooks (field report 2026-07-21) ==="
{
    # A bare `return` in a DEBUG-trap filter arm propagates the STALE $? of the
    # hook's previous command; a DEBUG trap returning non-zero kills a `set -e`
    # shell. Delivery path: BASH_ENV sources bash-log.sh into every
    # non-interactive bash, i.e. every PostToolUse hook. state-echo-hook died
    # at its first false `&&` conditional — silently, gate logging dead.
    #
    # S16's set -e probes could never catch this: their probe commands are
    # `echo …`, which no filter arm matches, so the bare-return path never ran
    # with a stale $?. The victim below is the failing shape: a false `&&`
    # short-circuit (errexit-exempt, leaves $?=1) followed by a command that
    # MATCHES a filter arm — one victim per arm family.
    BASH_LOG="$SCRIPTS/bash-log.sh"

    d="$WORK/s17"; build_project "$d" legacy
    cat > "$d/victim.sh" <<'VICTIM'
set -e
IS_FREEHAND=false
[ "$IS_FREEHAND" = true ] && echo "freehand branch"
ARM_CMD
echo "REACHED-THE-END"
VICTIM

    run_victim() {   # $1=bash-log file  $2=arm command; echoes output, RC in $?
        local log_file="$1" arm="$2"
        sed "s|ARM_CMD|$arm|" "$d/victim.sh" > "$d/victim-armed.sh"
        (cd "$d" && BASH_ENV="$log_file" bash victim-armed.sh 2>&1)
    }

    # One representative per filter-arm family (test/[[ /assignment/source).
    ARMS='[ -d /tmp ]
[[ -n ok ]]
HISTFILE=/dev/null
source /dev/null'
    while IFS= read -r arm; do
        set +e
        out="$(run_victim "$BASH_LOG" "$arm")"; rc=$?
        set -e
        assert_eq "$rc" "0" "S17[$arm] hook shell survives"
        assert_contains "$out" "REACHED-THE-END" "S17[$arm] hook ran to completion"
    done <<< "$ARMS"

    # The patched-trap row of the field report's table: logging still works.
    grep -q "REACHED-THE-END" "$d/.agent/bash_history" \
        && pass "S17 bash_history still logs after the fix" \
        || fail "S17 fix lost command logging"

    # Negative control 1: bare-return mutant kills the victim.
    # Loop over all 4 arms to ensure none are vacuous.
    #
    # The sed is range-scoped to the `case "$BASH_COMMAND"` block on purpose:
    # `) return 0 ;;` also matches the three marker-validation arms further
    # down, and an unscoped mutant would revert those too. They are unreachable
    # in this victim (legacy shape, no `current_user`), so it would still go
    # red — but for partly the wrong reason, and it would stop isolating the
    # filter arms the moment a marker enters the scenario.
    sed '/case "\$BASH_COMMAND" in/,/esac/ s|) return 0 ;;|) return ;;|' \
        "$BASH_LOG" > "$d/bash-log-mutant-arms.sh"
    assert_eq "$(grep -c ') return ;;' "$d/bash-log-mutant-arms.sh")" "4" \
        "S17 negative control (arms): mutant reverted exactly the 4 filter arms"
    while IFS= read -r arm; do
        set +e
        out="$(run_victim "$d/bash-log-mutant-arms.sh" "$arm")"; rc=$?
        set -e
        [ "$rc" -ne 0 ] \
            && pass "S17 negative control (arms): bare-return mutant kills victim on [$arm] (rc=$rc)" \
            || fail "S17 negative control (arms) VACUOUS: victim survived bare-return mutant on [$arm]"
        assert_eq "$(printf '%s' "$out" | grep -c 'REACHED-THE-END' || true)" "0" \
            "S17 negative control (arms): mutant died before the end on [$arm]"
    done <<< "$ARMS"

    # Now make bash_history a directory so append fails.
    rm -f "$d/.agent/bash_history"
    mkdir "$d/.agent/bash_history"

    # Test the append-failure path: when bash_history is unwritable (e.g. is a directory),
    # the fixed trap must return 0 and not kill the host shell.
    set +e
    out="$(run_victim "$BASH_LOG" "echo hello")"; rc=$?
    set -e
    assert_eq "$rc" "0" "S17 append-failure: hook shell survives unwritable history"
    assert_contains "$out" "REACHED-THE-END" "S17 append-failure: hook ran to completion"
    # …and silently. `echo … >> file 2>/dev/null` does not suppress a failure to
    # OPEN the file (bash reports it before applying the redirect), so the
    # unguarded form emits one "Is a directory" per command into hook output,
    # which the agent then reads. The append must be wrapped in a brace group.
    assert_eq "$(printf '%s' "$out" | grep -ci 'is a directory' || true)" "0" \
        "S17 append-failure: no per-command stderr noise leaks into hook output"

    # Negative control 2: no-append-failure-guard mutant kills the victim.
    # Revert only the guard on the append line, leaving the arms fixed.
    sed 's/^\( *\){ echo \(.*\); } 2>\/dev\/null || return 0$/\1echo \2/' \
        "$BASH_LOG" > "$d/bash-log-mutant-append.sh"
    assert_eq "$(grep -c 'return 0' "$d/bash-log-mutant-append.sh")" \
        "$(( $(grep -c 'return 0' "$BASH_LOG") - 1 ))" \
        "S17 negative control (append): mutant dropped exactly one guard"
    set +e
    out="$(run_victim "$d/bash-log-mutant-append.sh" "echo hello")"; rc=$?
    set -e
    [ "$rc" -ne 0 ] \
        && pass "S17 negative control (append): mutant without append guard kills victim on failure (rc=$rc)" \
        || fail "S17 negative control (append) VACUOUS: victim survived mutant without append guard on failure"
    assert_eq "$(printf '%s' "$out" | grep -c 'REACHED-THE-END' || true)" "0" \
        "S17 negative control (append): mutant without append guard died before the end"

    # Negative control 3: the brace group is load-bearing, not style. Revert it
    # to the inline `echo … >> file 2>/dev/null || return 0` form — which keeps
    # the shell ALIVE (the `|| return 0` is intact) but stops suppressing the
    # open error. Without this control, "simplifying" the braces away would
    # stay green while every hook shell regained per-command stderr noise.
    # Delimiter is '#', not '|': the pattern contains `||`, which would end a
    # '|'-delimited s/// early (BSD sed: "bad flag in substitute command").
    sed 's#{ echo \(.*\); } 2>/dev/null || return 0#echo \1 2>/dev/null || return 0#' \
        "$BASH_LOG" > "$d/bash-log-mutant-inline.sh"
    assert_eq "$(grep -c 'bash_history"; }' "$d/bash-log-mutant-inline.sh")" "0" \
        "S17 negative control (inline): mutant actually removed the brace group"
    set +e
    out="$(run_victim "$d/bash-log-mutant-inline.sh" "echo hello")"; rc=$?
    set -e
    assert_eq "$rc" "0" "S17 negative control (inline): shell still survives (guard intact)"
    [ "$(printf '%s' "$out" | grep -ci 'is a directory' || true)" -gt 0 ] \
        && pass "S17 negative control (inline): inline form leaks the open error (proves the braces work)" \
        || fail "S17 negative control (inline) VACUOUS: no leak from the inline form"
}

echo "=== S18: SessionStart GC must not delete a live session (field report 2026-07-29) ==="
{
    # Until v1.4.6 the sweep in session-start-hook keyed ONLY on `current_state`
    # mtime — no liveness check, no self-exclusion. But `current_state` is
    # written only by `tasks work <N>`, so its mtime means "when the task was
    # activated", never "when the session was last alive". Any task active >24h
    # therefore had its OWN pointer rm -rf'd at the next SessionStart, which
    # fires on `compact` too — and task-gate-hook then hard-blocks Edit/Write.
    #
    # The policy is now `tasks/shared.py::_gc_dead_sessions`' policy, and S18
    # asserts BOTH sweepers agree on the same tree (A2 below).
    #
    # Ages are deliberately far from the 24h boundary (now vs 2020) — `find
    # -mtime` buckets by whole days while Python compares epoch seconds, so
    # near-boundary parity is a documented tolerance, not a tested guarantee.
    HOOK="$SCRIPTS/session-start-hook"
    # Mutants must live BESIDE gate-echo-lib.sh: the hook sources it from
    # `dirname $0` under `set -e`, so a mutant dropped in $WORK dies at the
    # source line and sweeps nothing — every control would then "pass" by
    # never running. Copy the whole scripts dir once and mutate inside it.
    MUT="$WORK/s18-mutant-scripts"
    cp -R "$SCRIPTS" "$MUT"

    # S18 needs three pids: our own, a LIVE foreign one, and a genuinely dead
    # (and reaped) one. It deliberately uses NO background jobs to get them.
    #
    # Why: this fixture runs `trap 'rm -rf "$WORK"' EXIT`, and a backgrounded
    # simple command runs in a subshell that INHERITS that trap and fires it on
    # exit. So `sleep 300 & … kill` deletes the fixture's own scratch tree
    # mid-run — measured: $WORK vanished, later sections silently rebuilt only
    # the dirs they needed, and the mutant hooks were simply gone. A killed
    # background job is a booby trap in any fixture that cleans up via EXIT.
    #
    # Instead: our own pid, our PARENT's pid (alive for the whole run, foreign
    # to the sweep, nothing to clean up), and a pid harvested from a process
    # that has already exited and been reaped by the command substitution.
    OWN="pid-$$"                       # numeric and demonstrably alive
    OTHER="$PPID"                      # live, foreign, not a job we manage
    DEAD="$(bash -c 'echo $$')"        # exited + reaped before we look at it
    # Guard the pid-reuse window rather than trusting it: a recycled pid would
    # make the "dead session removed" assertions vacuous.
    if kill -0 "$DEAD" 2>/dev/null; then
        fail "S18 setup: harvested pid $DEAD is still alive (pid reuse) — dead-session assertions would be vacuous"
        DEAD=""
    fi

    # Populate $1 with one dir per policy case. Pointer ages are chosen so that
    # mtime and liveness DISAGREE wherever the bug lived:
    #   pid-own        live, STALE pointer  → the field case (kept: self-exclusion)
    #   pid-other-live live, STALE pointer  → kept by liveness alone
    #   pid-dead       dead, FRESH pointer  → removed despite a fresh pointer
    build_gc_tree() {
        local sdir="$1" n
        mkdir -p "$sdir"
        for n in "$OWN" "pid-$OTHER" "pid-$DEAD" pid-12ab pid-win-fallback uuid-stale uuid-fresh; do
            mkdir -p "$sdir/$n"
            printf '001\n' > "$sdir/$n/current_state"
        done
        for n in "$OWN" "pid-$OTHER" uuid-stale; do
            touch -t 202001010000 "$sdir/$n/current_state"
        done
        : > "$sdir/stray-file"       # not a dir — must survive untouched
    }
    survivors() {   # $1=sessions dir → sorted names, one per line
        find "$1" -mindepth 1 -maxdepth 1 2>/dev/null | sed 's|.*/||' | LC_ALL=C sort
    }

    # Project path contains a SPACE on purpose: the old sweep piped
    # `find -exec dirname` into `xargs rm -rf`, which word-splits — on any
    # iCloud/"Mobile Documents" checkout it both missed its target and aimed
    # rm -rf at path fragments.
    d="$WORK/s18 with space"; build_project "$d" legacy
    build_gc_tree "$d/.agent/sessions"
    set +e
    out="$(cd "$d" && PLAYBOOK_SESSION_ID="$OWN" bash "$HOOK" </dev/null 2>&1)"; rc=$?
    set -e
    assert_eq "$rc" "0" "S18 hook exits 0"
    got="$(survivors "$d/.agent/sessions")"
    want="$(printf '%s\n' "$OWN" "pid-$OTHER" stray-file uuid-fresh | LC_ALL=C sort)"
    assert_eq "$got" "$want" "S18 keeps exactly {own(stale), other-live(stale), uuid-fresh, stray file}"
    # Spelled out individually so a failure names the policy arm that broke.
    [ -d "$d/.agent/sessions/$OWN" ]           && pass "S18 own session survives a 48h-stale pointer (the field bug)" || fail "S18 own session deleted — the reported bug is back"
    [ -d "$d/.agent/sessions/pid-$OTHER" ]     && pass "S18 live foreign session survives a stale pointer" || fail "S18 deleted a live foreign session"
    [ ! -d "$d/.agent/sessions/pid-$DEAD" ]    && pass "S18 dead pid removed despite a fresh pointer" || fail "S18 kept a dead session"
    [ ! -d "$d/.agent/sessions/pid-12ab" ]     && pass "S18 non-numeric pid- name removed (matches Python's ValueError arm)" || fail "S18 kept pid-12ab"
    [ ! -d "$d/.agent/sessions/pid-win-fallback" ] && pass "S18 non-own pid-win-fallback removed" || fail "S18 kept a non-own pid-win-fallback"
    [ ! -d "$d/.agent/sessions/uuid-stale" ]   && pass "S18 legacy stale session removed (mtime fallback)" || fail "S18 kept a stale legacy session"
    [ -d "$d/.agent/sessions/uuid-fresh" ]     && pass "S18 legacy fresh session kept (mtime fallback)" || fail "S18 removed a fresh legacy session"
    [ -f "$d/.agent/sessions/stray-file" ]     && pass "S18 stray non-dir untouched" || fail "S18 clobbered a stray file"

    # ── A2: the two sweepers must agree ──────────────────────────────────────
    # cli.py::_gc_dead_sessions is the canonical policy and runs at every CLI
    # invocation; the hook's sweep runs first at SessionStart. Before this task
    # they implemented OPPOSITE policies over the same directory. Same tree,
    # same own-session id, same PIDs → the keep sets must be identical.
    d2="$WORK/s18 parity"; build_project "$d2" legacy
    build_gc_tree "$d2/.agent/sessions"
    set +e
    pyout="$(cd "$d2" && PYTHONPATH="$HERE/../plugins/playbook" PLAYBOOK_SESSION_ID="$OWN" \
        python3 -c 'import sys; from pathlib import Path
from tasks.shared import _gc_dead_sessions
_gc_dead_sessions(Path(sys.argv[1]))' "$d2" 2>&1)"; pyrc=$?
    set -e
    assert_eq "$pyrc" "0" "S18/A2 python sweeper runs clean${pyout:+ ($pyout)}"
    assert_eq "$(survivors "$d2/.agent/sessions")" "$got" \
        "S18/A2 bash and python sweepers keep the SAME set (one policy, not two)"

    # ── Negative control 1: self-exclusion ───────────────────────────────────
    # For a LIVE NUMERIC own pid, self-exclusion and liveness overlap — deleting
    # the guard changes nothing, so a numeric victim proves nothing. The only
    # case where self-exclusion is load-bearing is an own id that fails
    # `kill -0`: the Windows `pid-win-fallback` constant. That is the victim.
    d3="$WORK/s18 nc1"; build_project "$d3" legacy
    build_gc_tree "$d3/.agent/sessions"
    touch -t 202001010000 "$d3/.agent/sessions/pid-win-fallback/current_state"
    set +e
    (cd "$d3" && PLAYBOOK_SESSION_ID=pid-win-fallback bash "$HOOK" </dev/null >/dev/null 2>&1)
    set -e
    [ -d "$d3/.agent/sessions/pid-win-fallback" ] \
        && pass "S18 NC1 baseline: own pid-win-fallback survives a stale pointer" \
        || fail "S18 NC1 baseline: own pid-win-fallback deleted (Windows loses its session)"

    sed '/SESSION_ID" ] \&\& continue/d' "$HOOK" > "$MUT/session-start-hook"
    assert_eq "$(grep -c 'SESSION_ID" ] && continue' "$MUT/session-start-hook")" "0" \
        "S18 NC1: mutant actually removed the self-exclusion line"
    d4="$WORK/s18 nc1m"; build_project "$d4" legacy
    build_gc_tree "$d4/.agent/sessions"
    touch -t 202001010000 "$d4/.agent/sessions/pid-win-fallback/current_state"
    set +e
    (cd "$d4" && PLAYBOOK_SESSION_ID=pid-win-fallback bash "$MUT/session-start-hook" </dev/null >/dev/null 2>&1); rc=$?
    set -e
    # A mutant that dies BEFORE the sweep (e.g. a failed `source` under set -e)
    # deletes nothing, and every "mutant kills the victim" assertion below would
    # pass for the wrong reason. Demand it ran to completion first.
    assert_eq "$rc" "0" "S18 NC1: mutant hook actually ran to completion (not killed before the sweep)"
    [ ! -d "$d4/.agent/sessions/pid-win-fallback" ] \
        && pass "S18 NC1: mutant without self-exclusion DELETES the own session" \
        || fail "S18 NC1 VACUOUS: own session survived without the self-exclusion guard"

    # ── Negative control 2: liveness ─────────────────────────────────────────
    # Revert the pid- arm to the pre-1.4.7 mtime rule. This reproduces the
    # historical bug exactly, and flips TWO observables: a live session with an
    # old pointer dies (the field report), and a dead session with a fresh
    # pointer survives. Asserting "own dies" would be unreachable here — NC1's
    # self-exclusion still stands — which is why the arm needs its own victims.
    # Swap the liveness test for the pre-027 mtime rule, leaving everything else
    # intact. The EPERM arm below it then never matches (kill_err stays unset),
    # so the pid- branch becomes purely mtime-driven — exactly the old policy.
    sed 's|if kill_err="$(kill -0 "${name#pid-}" 2>&1)"; then|if [ -n "$(find "$d" -maxdepth 1 -name current_state -mtime -1 2>/dev/null)" ]; then|' \
        "$HOOK" > "$MUT/session-start-hook"
    # Count the CODE line, not the policy comment that also says "kill -0".
    assert_eq "$(grep -c 'kill -0 "${name#pid-}" 2>&1' "$MUT/session-start-hook")" "0" \
        "S18 NC2: mutant actually removed the liveness check"
    d5="$WORK/s18 nc2m"; build_project "$d5" legacy
    build_gc_tree "$d5/.agent/sessions"
    set +e
    (cd "$d5" && PLAYBOOK_SESSION_ID="$OWN" bash "$MUT/session-start-hook" </dev/null >/dev/null 2>&1); rc=$?
    set -e
    assert_eq "$rc" "0" "S18 NC2: mutant hook actually ran to completion (not killed before the sweep)"
    [ ! -d "$d5/.agent/sessions/pid-$OTHER" ] \
        && pass "S18 NC2: mtime-only mutant DELETES a live session with an old pointer (the field bug)" \
        || fail "S18 NC2 VACUOUS: live-stale session survived the mtime-only mutant"
    [ -d "$d5/.agent/sessions/pid-$DEAD" ] \
        && pass "S18 NC2: mtime-only mutant KEEPS a dead session with a fresh pointer" \
        || fail "S18 NC2 VACUOUS: dead-fresh session removed by the mtime-only mutant"

    # ── Negative control 3: fail-open removal ────────────────────────────────
    # The hook runs under `set -e`, so an undeletable session dir must not abort
    # session start — losing the wrapper provisioning and env export with it.
    if [ "$(id -u)" != 0 ]; then
        d6="$WORK/s18 ro"; build_project "$d6" legacy
        build_gc_tree "$d6/.agent/sessions"
        chmod 500 "$d6/.agent/sessions/uuid-stale"     # non-writable → rm -rf of its child fails
        set +e
        (cd "$d6" && PLAYBOOK_SESSION_ID="$OWN" bash "$HOOK" </dev/null >/dev/null 2>&1); rc=$?
        set -e
        chmod 700 "$d6/.agent/sessions/uuid-stale" 2>/dev/null || true
        assert_eq "$rc" "0" "S18 NC3: undeletable session dir does not abort SessionStart (set -e fail-open)"
    else
        pass "S18 NC3 skipped (running as root — chmod cannot block rm)"
    fi

    # ── Symlink safety (impl panel, Critical) ────────────────────────────────
    # `for d in "$SESSIONS_DIR"/*/` matched a symlink-to-dir WITH a trailing
    # slash, and `rm -rf "link/"` follows it: measured on macOS, the TARGET
    # directory was deleted and the symlink left behind. Python's rmtree refuses,
    # so this was also a bash/python parity hole the A2 parity test could not see.
    d8="$WORK/s18 symlink"; build_project "$d8" legacy
    mkdir -p "$d8/.agent/sessions" "$d8/precious"
    echo PRECIOUS > "$d8/precious/keepme.txt"
    ln -s "$d8/precious" "$d8/.agent/sessions/pid-12ab"   # named so policy calls it dead
    set +e
    (cd "$d8" && PLAYBOOK_SESSION_ID="$OWN" bash "$HOOK" </dev/null >/dev/null 2>&1); rc=$?
    set -e
    assert_eq "$rc" "0" "S18 symlink: hook exits 0"
    [ -f "$d8/precious/keepme.txt" ] \
        && pass "S18 symlink: rm -rf did NOT follow the link into the target" \
        || fail "S18 symlink: DESTROYED the symlink target — rm -rf followed the link"

    # Negative control: restore the trailing-slash glob and the target dies.
    sed 's|for d in "$SESSIONS_DIR"/\*; do|for d in "$SESSIONS_DIR"/*/; do|' \
        "$HOOK" > "$MUT/session-start-hook"
    assert_eq "$(grep -c 'SESSIONS_DIR"/\*/; do' "$MUT/session-start-hook")" "1" \
        "S18 symlink NC: mutant restored the trailing-slash glob"
    d9="$WORK/s18 symlink nc"; build_project "$d9" legacy
    mkdir -p "$d9/.agent/sessions" "$d9/precious"
    echo PRECIOUS > "$d9/precious/keepme.txt"
    ln -s "$d9/precious" "$d9/.agent/sessions/pid-12ab"
    set +e
    (cd "$d9" && PLAYBOOK_SESSION_ID="$OWN" bash "$MUT/session-start-hook" </dev/null >/dev/null 2>&1); rc=$?
    set -e
    assert_eq "$rc" "0" "S18 symlink NC: mutant hook ran to completion"
    [ ! -f "$d9/precious/keepme.txt" ] \
        && pass "S18 symlink NC: trailing-slash glob DOES destroy the target (control is live)" \
        || fail "S18 symlink NC VACUOUS: target survived the buggy glob"

    # ── EPERM means alive, not dead (impl panel) ─────────────────────────────
    # kill -0 on another user's live process fails with EPERM. Treating that as
    # dead would delete a live session — and the old mtime-only sweep kept it.
    if [ "$(id -u)" != 0 ]; then
        d10="$WORK/s18 eperm"; build_project "$d10" legacy
        mkdir -p "$d10/.agent/sessions/pid-1"          # launchd/init: alive, root-owned
        printf '001\n' > "$d10/.agent/sessions/pid-1/current_state"
        touch -t 202001010000 "$d10/.agent/sessions/pid-1/current_state"
        set +e
        (cd "$d10" && PLAYBOOK_SESSION_ID="$OWN" bash "$HOOK" </dev/null >/dev/null 2>&1)
        set -e
        [ -d "$d10/.agent/sessions/pid-1" ] \
            && pass "S18 EPERM: another user's LIVE session is kept (not cross-user deleted)" \
            || fail "S18 EPERM: deleted a live session owned by another OS user"
    else
        pass "S18 EPERM skipped (running as root — no EPERM)"
    fi

    # ── A2: SessionEnd must not deactivate a task on /clear ──────────────────
    # `clear` ends the *conversation*, not the process: the same pid continues
    # and SessionStart fires again immediately. Deleting the session dir there
    # drops the active-task pointer mid-session — the same observable failure as
    # the GC bug above, reached by a different path.
    END_HOOK="$SCRIPTS/session-end-hook"
    # Unknown/unparseable/case-variant reasons must KEEP: the delete path is an
    # explicit terminal allowlist, because payload drift landing on "delete"
    # costs a LIVE pointer, while a missed cleanup is free (the liveness GC
    # reclaims it). `resume` is here because SessionEnd reportedly fires on an
    # interactive /resume while the same process continues.
    for reason in clear resume Clear "" bogus_future_reason logout prompt_input_exit other; do
        d7="$WORK/s18 end-${reason:-empty}"; build_project "$d7" legacy
        mkdir -p "$d7/.agent/sessions/$OWN"
        printf '042\n' > "$d7/.agent/sessions/$OWN/current_state"
        set +e
        (cd "$d7" && printf '{"reason":"%s"}' "$reason" \
            | PLAYBOOK_SESSION_ID="$OWN" bash "$END_HOOK" >/dev/null 2>&1); rc=$?
        set -e
        assert_eq "$rc" "0" "S18/A2end[$reason] hook exits 0"
        case "$reason" in
            logout|prompt_input_exit|other) expect=delete ;;
            *)                              expect=keep ;;
        esac
        if [ "$expect" = keep ]; then
            [ -f "$d7/.agent/sessions/$OWN/current_state" ] \
                && pass "S18/A2end[${reason:-<empty>}] KEEPS the active-task pointer" \
                || fail "S18/A2end[${reason:-<empty>}] deleted a live pointer (fail-open is inverted)"
        else
            [ ! -d "$d7/.agent/sessions/$OWN" ] \
                && pass "S18/A2end[$reason] still cleans up the session dir (process is going away)" \
                || fail "S18/A2end[$reason] left a session dir behind"
        fi
    done

    # SessionEnd must use the SHARED session-id resolver. It used to compute
    # `${PLAYBOOK_SESSION_ID:-pid-$PPID}` under a comment claiming that matched
    # session-start-hook — it did not: the ancestor scan returns the ROOT agent
    # pid, and MSYS returns the constant pid-win-fallback, so with the env var
    # absent this hook could delete a directory that was never a session while
    # leaving the real one behind. Asserted structurally: reproducing an
    # env-less ancestor walk inside a fixture subshell resolves against the
    # FIXTURE's process tree, not a real agent's, so a behavioural assertion here
    # would pin the harness rather than the hook.
    grep -q 'SESSION_ID="$(resolve_session_id)"' "$END_HOOK" \
        && pass "S18/A2end session-end-hook uses the shared resolve_session_id" \
        || fail "S18/A2end session-end-hook computes its own session id again"
    # Match the ASSIGNMENT, not the word: the hook's comment names the old
    # `pid-$PPID` shape on purpose, to explain why it was wrong.
    assert_eq "$(grep -c 'SESSION_ID="${PLAYBOOK_SESSION_ID:-pid-\$PPID}"' "$END_HOOK")" "0" \
        "S18/A2end no ad-hoc pid-\$PPID fallback remains in code"

    # Nothing to tear down: S18 spawned no background jobs (see the note above).
}

echo
echo "============================================"
echo "wrapper multi-user fixture: $PASS passed, $FAIL failed"
echo "============================================"
[ "$FAIL" -eq 0 ] || exit 1
