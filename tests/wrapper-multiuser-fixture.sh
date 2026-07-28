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

    # (a) Brand-new project: everything at the root, exactly as before.
    d="$WORK/s14-new"; mkdir -p "$d"
    set +e
    out="$(cd "$d" && bash "$INIT" 2>&1)"; rc=$?
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
    out="$(cd "$d" && bash "$INIT" 2>&1)"; rc=$?
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
    out="$(cd "$d" && bash "$INIT" 2>&1)"; rc=$?
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
    out="$(cd "$d" && bash "$INIT" 2>&1)"; rc=$?
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

    log_bash() { (cd "$1" && bash -c 'source "$1"; echo probe >/dev/null' _ "$BASH_LOG" >/dev/null 2>&1); }
    log_zsh()  { (cd "$1" && ZSH_EXECUTION_STRING="echo probe" zsh -f -c "source '$ZSH_LOG'" >/dev/null 2>&1); }

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
    # Both shells logged into the same file.
    assert_eq "$(grep -c 'AGENT' "$d/.agent/alice/bash_history")" "2" \
        "S16 both shells appended to the lane file"

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

echo
echo "============================================"
echo "wrapper multi-user fixture: $PASS passed, $FAIL failed"
echo "============================================"
[ "$FAIL" -eq 0 ] || exit 1
