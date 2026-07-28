#!/usr/bin/env bash
# Synthetic git fixture for the merge skill's Step 7 verification (task 021).
#
# Step 7 is prose an agent executes, so the two commands it hands over are only
# as good as their behavior on real repos. This fixture runs BOTH against real
# scratch repositories, across the project shapes the field report called out:
#
#   (d) code identity  — `git diff "$target_before" -- . ':(exclude)…'`
#                        must see code wherever it lives (including root-level
#                        files), must ignore the paths the semantic steps own,
#                        and must be empty ONLY when the merge really added no
#                        code. The literal `backend/` it replaced diffed a
#                        directory that most repos don't have — reporting green
#                        for having looked at nothing.
#   (e) project verify — merge-verify.py's four-way verdict on repos that
#                        declare a command, declare a broken one, or declare
#                        nothing at all.
#
# Shapes covered (the report's F3 table): backend+frontend, frontend-only,
# polyglot with root-level code, and docs/infra-only with no code at all.
#
# Run from anywhere: `bash playbook-plugin/tests/merge-verify-fixture.sh`.
# Exits 0 if every scenario passes, non-zero on the first failure.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$HERE/../plugins/playbook/skills/merge"
MERGE_VERIFY="$SKILL_DIR/merge-verify.py"

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

assert_not_contains() {
    local haystack="$1" needle="$2" label="$3"
    if printf '%s' "$haystack" | grep -qF "$needle"; then
        fail "$label — did NOT expect '$needle'"
        echo "----- output start -----"; printf '%s\n' "$haystack"; echo "----- output end -----"
    else
        pass "$label"
    fi
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

git_q() { git -c user.name=t -c user.email=t@t -c commit.gpgsign=false "$@"; }

# ── The identity-diff recipe, exactly as Step 7(d) documents it ──────────────
# Kept in one place so a drift between fixture and skill is a one-line fix.
# Root-anchored on purpose (`:/`, `,top`): the plain `-- .` form is cwd-relative
# and silently narrows to a subtree. test_merge_config.py pins this string against
# SKILL.md so the fixture can't drift from the documented recipe.
identity_diff() {
    local base="$1"
    git diff --name-only "$base" -- \
        ':/' ':(exclude,top)MIND_MAP.md' ':(exclude,top)MIND_MAP_OVERFLOW.md' ':(exclude,top).agent'
}

# Build a repo of a given shape with a real divergent merge, and echo the
# pre-merge target tip ($target_before) on stdout.
#   $1 = repo dir, $2 = shape
build_repo() {
    local dir="$1" shape="$2"
    mkdir -p "$dir"; cd "$dir"
    git_q init -qb main .
    mkdir -p .agent/u1
    echo '[1] **Node** - x' > MIND_MAP.md
    echo '[1] **Node** - x' > MIND_MAP_OVERFLOW.md
    echo 'log' > .agent/u1/chat_log.md
    case "$shape" in
        backend-frontend) mkdir -p backend frontend; echo 'b' > backend/app.py; echo 'f' > frontend/app.ts ;;
        frontend-only)    mkdir -p frontend/src; echo 'f' > frontend/src/app.ts ;;
        polyglot-root)    mkdir -p services; echo 's' > services/svc.go; echo '{}' > package.json; echo 'm' > main.py ;;
        docs-only)        mkdir -p docs; echo 'd' > docs/readme.md ;;
    esac
    git_q add -A; git_q commit -qm base
    # source branch changes code (whatever "code" means for this shape)
    git_q checkout -qb feature
    case "$shape" in
        backend-frontend) echo 'b2' >> backend/app.py; echo 'f2' >> frontend/app.ts ;;
        frontend-only)    echo 'f2' >> frontend/src/app.ts ;;
        polyglot-root)    echo 's2' >> services/svc.go; echo '{"dep":1}' > package.json; echo 'm2' >> main.py ;;
        docs-only)        echo 'd2' >> docs/readme.md ;;
    esac
    git_q commit -qam feat
    # target branch moves independently, touching only owned paths
    git_q checkout -q main
    echo '[2] **Two** - y' >> MIND_MAP.md
    echo 'more log' >> .agent/u1/chat_log.md
    git_q commit -qam main-side
    git rev-parse HEAD
}

echo "=== S1: backend+frontend — code identity sees BOTH lanes ==="
{
    tb=$(build_repo "$WORK/s1" backend-frontend)
    cd "$WORK/s1"; git_q merge --no-edit -q feature
    out="$(identity_diff "$tb")"
    assert_contains "$out" "backend/app.py"  "S1 identity diff includes the backend lane"
    assert_contains "$out" "frontend/app.ts" "S1 identity diff includes the frontend lane"
    assert_not_contains "$out" "MIND_MAP"    "S1 identity diff excludes MIND_MAP.md"
    assert_not_contains "$out" ".agent"      "S1 identity diff excludes .agent/"
}

echo "=== S2: frontend-only — the shape where the old backend/ check went vacuous ==="
{
    tb=$(build_repo "$WORK/s2" frontend-only)
    cd "$WORK/s2"; git_q merge --no-edit -q feature
    out="$(identity_diff "$tb")"
    assert_contains "$out" "frontend/src/app.ts" "S2 identity diff sees code in a repo with no backend/"
    # The regression being fenced: scoping to a literal `backend/` here returns
    # nothing, which reads as "clean" while the code went unexamined.
    old="$(git diff --name-only "$tb" -- backend/ || true)"
    assert_eq "$old" "" "S2 the OLD backend/-scoped diff is empty here (the vacuous-green bug)"
}

echo "=== S3: polyglot — root-level files are covered (merge_code_roots would miss them) ==="
{
    tb=$(build_repo "$WORK/s3" polyglot-root)
    cd "$WORK/s3"; git_q merge --no-edit -q feature
    out="$(identity_diff "$tb")"
    assert_contains "$out" "services/svc.go" "S3 identity diff includes a nested code dir"
    assert_contains "$out" "package.json"    "S3 identity diff includes a ROOT-level manifest"
    assert_contains "$out" "main.py"         "S3 identity diff includes a ROOT-level source file"
}

echo "=== S4: docs-only — empty diff must mean 'no code introduced', not 'not checked' ==="
{
    tb=$(build_repo "$WORK/s4" docs-only)
    cd "$WORK/s4"; git_q merge --no-edit -q feature
    out="$(identity_diff "$tb")"
    assert_contains "$out" "docs/readme.md" "S4 identity diff sees the docs change (nothing is invisible)"
}

echo "=== S5: a merge that touches ONLY owned paths diffs to empty ==="
{
    tb=$(build_repo "$WORK/s5" backend-frontend)
    cd "$WORK/s5"
    git_q merge --no-edit -q feature
    # revert every code hunk the merge brought in; only owned paths differ now
    git_q checkout "$tb" -- backend frontend
    out="$(identity_diff "$tb")"
    assert_eq "$out" "" "S5 empty identity diff when the merge introduced no code"
}

echo "=== S6: deletions are attributable too (source may legitimately remove code) ==="
{
    tb=$(build_repo "$WORK/s6" backend-frontend)
    cd "$WORK/s6"; git_q merge --no-edit -q feature
    git_q rm -q -r frontend
    out="$(identity_diff "$tb")"
    assert_contains "$out" "frontend/app.ts" "S6 identity diff surfaces a deleted path"
}

echo "=== S7: merge-verify verdicts on real repos ==="
{
    tb=$(build_repo "$WORK/s7" backend-frontend)
    cd "$WORK/s7"; git_q merge --no-edit -q feature

    # nothing declared → SKIPPED (3), and it must say so out loud
    set +e; out="$(python3 "$MERGE_VERIFY" 2>&1)"; rc=$?; set -e
    assert_eq "$rc" "3" "S7 unconfigured repo exits SKIPPED(3)"
    assert_contains "$out" "SKIPPED" "S7 unconfigured repo says SKIPPED"

    # declared and green → 0
    printf '{"merge_verify":{"command":"echo suite ok"}}' > .agent/config.json
    set +e; out="$(python3 "$MERGE_VERIFY" 2>&1)"; rc=$?; set -e
    assert_eq "$rc" "0" "S7 declared+passing command exits GREEN(0)"
    assert_contains "$out" "suite ok" "S7 the declared command actually ran"

    # declared and red → 1 (this is what must block --push)
    printf '{"merge_verify":{"command":"echo suite red; exit 5"}}' > .agent/config.json
    set +e; out="$(python3 "$MERGE_VERIFY" 2>&1)"; rc=$?; set -e
    assert_eq "$rc" "1" "S7 declared+failing command exits FAILED(1)"
    assert_contains "$out" "exited 5" "S7 failure reports the command's own exit code"

    # broken declaration → 2, never silently skipped
    printf '{"merge_verify":{"commnd":"echo typo"}}' > .agent/config.json
    set +e; out="$(python3 "$MERGE_VERIFY" 2>&1)"; rc=$?; set -e
    assert_eq "$rc" "2" "S7 misspelled key exits BLOCKED(2)"
    assert_not_contains "$out" "SKIPPED" "S7 a broken declaration is not reported as SKIPPED"
}

echo "=== S8: a tree-mutating verify command is invisible to an identity diff run BEFORE it ==="
{
    tb=$(build_repo "$WORK/s8" backend-frontend)
    cd "$WORK/s8"; git_q merge --no-edit -q feature
    # Production order is 7(d) THEN 7(e), so a formatter-style command that writes
    # during verification lands after the identity check has already concluded.
    # This is why 7(e) tells you to re-run (d) when the command writes.
    # (a) a TRACKED file rewritten by the command: invisible before, visible after.
    printf '{"merge_verify":{"command":"echo mutated >> frontend/app.ts"}}' > .agent/config.json
    before="$(identity_diff "$tb" | grep -c 'frontend/app.ts' || true)"
    python3 "$MERGE_VERIFY" >/dev/null 2>&1
    after_hunks="$(git diff "$tb" -- ':/frontend/app.ts' | grep -c '^+mutated' || true)"
    assert_eq "$before" "1" "S8a the pre-verify diff saw the merge's own change to that file"
    assert_eq "$after_hunks" "1" "S8a re-running the diff AFTER verification surfaces the command's write"

    # (b) an UNTRACKED artifact: git diff cannot see it at all, at any point —
    # which is why 7(e) also tells you to check `git status --porcelain`, not
    # just to re-run the diff.
    printf '{"merge_verify":{"command":"echo junk > build-artifact.txt"}}' > .agent/config.json
    python3 "$MERGE_VERIFY" >/dev/null 2>&1
    assert_not_contains "$(identity_diff "$tb")" "build-artifact.txt" \
        "S8b an untracked artifact is invisible to git diff (a known blind spot)"
    assert_contains "$(git status --porcelain)" "build-artifact.txt" \
        "S8b ...but git status --porcelain catches it, as 7(e) instructs"
}

echo "=== S9: the identity diff is root-anchored (same scope from any cwd) ==="
{
    tb=$(build_repo "$WORK/s9" backend-frontend)
    cd "$WORK/s9"; git_q merge --no-edit -q feature
    from_root="$(identity_diff "$tb")"
    cd backend
    from_subdir="$(identity_diff "$tb")"
    assert_eq "$from_subdir" "$from_root" "S9 scope identical from a subdirectory (cwd-relative form would narrow)"
    assert_contains "$from_subdir" "frontend/app.ts" "S9 sibling lane still seen from inside backend/"
    cd ..
}

echo "=== S10: an early failing step cannot report GREEN ==="
{
    tb=$(build_repo "$WORK/s10" backend-frontend)
    cd "$WORK/s10"; git_q merge --no-edit -q feature
    # bash reports the LAST command's status; without set -e this is a green
    # stamp on a red run — the defect class this whole task exists to remove.
    printf '{"merge_verify":{"command":"echo step1; false; echo step3"}}' > .agent/config.json
    set +e; python3 "$MERGE_VERIFY" >/dev/null 2>&1; rc=$?; set -e
    assert_eq "$rc" "1" "S10 failing middle step exits FAILED(1), not GREEN"
    printf '{"merge_verify":{"command":"false | true"}}' > .agent/config.json
    set +e; python3 "$MERGE_VERIFY" >/dev/null 2>&1; rc=$?; set -e
    assert_eq "$rc" "1" "S10 failing pipe head exits FAILED(1) (pipefail)"
}

echo
echo "============================================"
echo "merge-verify fixture: $PASS passed, $FAIL failed"
echo "============================================"
[ "$FAIL" -eq 0 ] || exit 1
