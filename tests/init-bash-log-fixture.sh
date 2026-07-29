#!/usr/bin/env bash
# init's bash-log deployment (task 027, closing task 023's two parked items).
#
# Two defects, both in `scripts/init`'s BEGIN:bash-log block:
#
#  1. DANGLING BASH_ENV ON ZSH HOSTS. The $SHELL branch deployed only
#     bash-log.zsh when the login shell was zsh, but the settings.json injection
#     ALWAYS sets BASH_ENV=~/.claude/bash-log.sh ("CC's Bash tool is /bin/bash
#     regardless"). On every macOS/zsh install that variable therefore pointed at
#     a file that was never deployed, and CC-side command logging was silently
#     dead. Task 023 found it and deferred it on purpose: fixing it ARMS
#     bash-log's DEBUG trap on every zsh host, which was unsafe until the trap's
#     `set -e` kill path was fixed (shipped v1.4.6).
#
#  2. NO CRLF NORMALIZATION. These files are sourced into every hook shell, so a
#     CRLF copy from a Windows checkout breaks them — and byte-comparing a CRLF
#     source against an LF destination never matches, so init re-copied forever
#     and never reported "unchanged".
#
# EVERY arm runs init under its own temporary HOME. init writes ~/.claude/,
# ~/.claude/settings.json and shell rc files, so running it against the real HOME
# would mutate the developer's machine on every test run.
#
# Run from anywhere: `bash playbook-plugin/tests/init-bash-log-fixture.sh`.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PLUGIN="$HERE/../plugins/playbook"
SCRIPTS="$PLUGIN/scripts"
INIT="$SCRIPTS/init"

# Variant runs (CRLF source, mutants) need a WHOLE plugin copy, not just
# scripts/: init also reaches siblings like hooks/monitor-nudge.sh, and a
# scripts-only copy fails late with "monitor-nudge.sh source not found" — which
# has nothing to do with bash-log but would mark those arms red.
copy_plugin() {   # $1 = destination; echoes the copy's scripts dir
    cp -R "$PLUGIN" "$1"
    echo "$1/scripts"
}

PASS=0
FAIL=0
pass() { echo "  PASS  $*"; PASS=$((PASS+1)); }
fail() { echo "  FAIL  $*"; FAIL=$((FAIL+1)); }

assert_eq() {
    local got="$1" want="$2" label="$3"
    if [ "$got" = "$want" ]; then pass "$label"; else fail "$label — expected [$want], got [$got]"; fi
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Run init in a fresh project with a fresh HOME.
#   $1 = scenario name   $2 = login shell to advertise   $3 = scripts dir to use
# Sets: RUN_HOME, RUN_RC, RUN_OUT, RUN_RC_FILE
run_init() {
    local name="$1" shell_path="$2" scripts_dir="${3:-$SCRIPTS}"
    RUN_HOME="$WORK/$name/home"
    local proj="$WORK/$name/proj"
    mkdir -p "$RUN_HOME" "$proj"
    set +e
    RUN_OUT="$(cd "$proj" && HOME="$RUN_HOME" SHELL="$shell_path" \
        bash "$scripts_dir/init" 2>&1)"
    RUN_RC=$?
    set -e
    case "$shell_path" in
        *zsh) RUN_RC_FILE="$RUN_HOME/.zshenv" ;;
        *)    RUN_RC_FILE="$RUN_HOME/.bash_profile" ;;
    esac
}

bash_env_of() {   # echo settings.json's env.BASH_ENV, or empty
    python3 -c "
import json, sys, os
p = sys.argv[1]
try:
    print(json.load(open(p)).get('env', {}).get('BASH_ENV', ''))
except Exception:
    print('')
" "$1/.claude/settings.json"
}

echo "=== I1: zsh host gets BOTH variants, and BASH_ENV points at a file that EXISTS ==="
{
    run_init zsh /bin/zsh
    assert_eq "$RUN_RC" "0" "I1 init exits 0 on a zsh host"
    [ -f "$RUN_HOME/.claude/bash-log.sh" ] \
        && pass "I1 bash-log.sh IS deployed on a zsh host (the dangling-BASH_ENV bug)" \
        || fail "I1 bash-log.sh missing — BASH_ENV will dangle and CC logging stays dead"
    [ -f "$RUN_HOME/.claude/bash-log.zsh" ] \
        && pass "I1 bash-log.zsh still deployed for the interactive zsh" \
        || fail "I1 bash-log.zsh missing on a zsh host"

    target="$(bash_env_of "$RUN_HOME")"
    assert_eq "$target" "$RUN_HOME/.claude/bash-log.sh" "I1 settings.json BASH_ENV points at bash-log.sh"
    [ -n "$target" ] && [ -f "$target" ] \
        && pass "I1 BASH_ENV target exists on disk (no dangling reference)" \
        || fail "I1 BASH_ENV names a nonexistent file: $target"

    # The zsh rc line is what a zsh LOGIN shell needs (zsh ignores BASH_ENV).
    assert_eq "$(grep -c 'claude/bash-log' "$RUN_RC_FILE" 2>/dev/null || true)" "1" \
        "I1 .zshenv has exactly one source line"
}

echo "=== I2: bash host keeps today's behaviour ==="
{
    run_init bash /bin/bash
    assert_eq "$RUN_RC" "0" "I2 init exits 0 on a bash host"
    [ -f "$RUN_HOME/.claude/bash-log.sh" ] \
        && pass "I2 bash-log.sh deployed" || fail "I2 bash-log.sh missing"
    [ ! -f "$RUN_HOME/.claude/bash-log.zsh" ] \
        && pass "I2 bash-log.zsh NOT deployed (no interactive zsh to serve)" \
        || fail "I2 deployed the zsh variant on a bash host"
    assert_eq "$(bash_env_of "$RUN_HOME")" "$RUN_HOME/.claude/bash-log.sh" \
        "I2 settings.json BASH_ENV set"
    assert_eq "$(grep -c 'claude/bash-log' "$RUN_RC_FILE" 2>/dev/null || true)" "1" \
        "I2 .bash_profile has exactly one BASH_ENV line"
}

echo "=== I3: re-running init is idempotent (no duplicate rc lines, reports unchanged) ==="
{
    for sh in /bin/zsh /bin/bash; do
        label="$(basename "$sh")"
        run_init "idem-$label" "$sh"
        first_home="$RUN_HOME"
        # Second run against the SAME home + project.
        set +e
        out2="$(cd "$WORK/idem-$label/proj" && HOME="$first_home" SHELL="$sh" \
            bash "$INIT" 2>&1)"; rc2=$?
        set -e
        assert_eq "$rc2" "0" "I3/$label second init exits 0"
        assert_eq "$(grep -c 'claude/bash-log' "$RUN_RC_FILE" 2>/dev/null || true)" "1" \
            "I3/$label rc file still has exactly one source line after two runs"
        printf '%s' "$out2" | grep -q 'bash-log.sh (unchanged)' \
            && pass "I3/$label second run reports bash-log.sh unchanged" \
            || fail "I3/$label second run did not report unchanged (re-copy loop?)"
    done
}

echo "=== I4: CRLF-tainted source deploys as LF, and stays 'unchanged' on re-run ==="
{
    # A Windows checkout's working copy: CRLF line endings in the source.
    CRLF_SCRIPTS="$(copy_plugin "$WORK/crlf-plugin")"
    for f in bash-log.sh bash-log.zsh; do
        python3 -c "
import sys
p = sys.argv[1]
b = open(p, 'rb').read().replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
open(p, 'wb').write(b)
" "$CRLF_SCRIPTS/$f"
    done
    # Confirm the fixture actually built a CRLF victim — else I4 proves nothing.
    assert_eq "$(python3 -c "
import sys; print(1 if b'\r\n' in open(sys.argv[1],'rb').read() else 0)" "$CRLF_SCRIPTS/bash-log.sh")" "1" \
        "I4 fixture source really is CRLF"

    run_init crlf /bin/zsh "$CRLF_SCRIPTS"
    assert_eq "$RUN_RC" "0" "I4 init exits 0 with a CRLF source"
    for f in bash-log.sh bash-log.zsh; do
        assert_eq "$(python3 -c "
import sys; print(open(sys.argv[1],'rb').read().count(b'\r'))" "$RUN_HOME/.claude/$f")" "0" \
            "I4 deployed $f is LF-only"
    done
    # The comparison must see through the CRLF difference, or init re-copies forever.
    set +e
    out2="$(cd "$WORK/crlf/proj" && HOME="$RUN_HOME" SHELL=/bin/zsh \
        bash "$CRLF_SCRIPTS/init" 2>&1)"; rc2=$?
    set -e
    assert_eq "$rc2" "0" "I4 second init exits 0"
    printf '%s' "$out2" | grep -q 'bash-log.sh (unchanged)' \
        && pass "I4 CRLF source vs LF dest compares equal (no endless re-copy)" \
        || fail "I4 re-copied a CRLF source over an LF destination"
    # No temp files left behind by either path.
    assert_eq "$(find "$RUN_HOME/.claude" -maxdepth 1 -name 'bash-log*.tmp.*' | wc -l | tr -d ' ')" "0" \
        "I4 no .tmp.* litter left in ~/.claude"
}

echo "=== I5: negative control — the pre-027 zsh branch dangles BASH_ENV ==="
{
    # Deploying ONLY the zsh variant on a zsh host is exactly the shipped bug.
    # Without this control, I1 could pass for reasons unrelated to the fix.
    MUT_SCRIPTS="$(copy_plugin "$WORK/mut-plugin")"
    python3 - "$MUT_SCRIPTS/init" <<'PY'
import sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
old = '''    deploy_bash_log "bash-log.sh"
    # zsh hosts additionally get the zsh variant for their interactive shells.
    if [ "$SHELL_NAME" = "zsh" ]; then
        deploy_bash_log "bash-log.zsh"
    fi'''
new = '''    if [ "$SHELL_NAME" = "zsh" ]; then
        deploy_bash_log "bash-log.zsh"
    else
        deploy_bash_log "bash-log.sh"
    fi'''
assert old in s, "mutant anchor not found — I5 would be vacuous"
open(p, "w", encoding="utf-8").write(s.replace(old, new, 1))
PY
    pass "I5 mutant built (pre-027 either/or deployment)"
    run_init mut /bin/zsh "$MUT_SCRIPTS"
    assert_eq "$RUN_RC" "0" "I5 mutant init still exits 0 (the bug is silent — that was the problem)"
    target="$(bash_env_of "$RUN_HOME")"
    [ -n "$target" ] && [ ! -f "$target" ] \
        && pass "I5 mutant leaves BASH_ENV dangling (control is live, fix is load-bearing)" \
        || fail "I5 VACUOUS: mutant did not reproduce the dangling BASH_ENV"
}

echo
echo "============================================"
echo "init bash-log fixture: $PASS passed, $FAIL failed"
echo "============================================"
[ "$FAIL" -eq 0 ] || exit 1
