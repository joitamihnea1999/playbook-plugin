#!/bin/bash
# Install the arena git hooks for this repo. Idempotent. Run once from anywhere
# inside the repo:  bash arena/install-hooks.sh
set -e
ROOT="$(git rev-parse --show-toplevel)"
HOOKS_DIR="$(git rev-parse --git-path hooks)"
mkdir -p "$HOOKS_DIR"
SHIM="$HOOKS_DIR/pre-push"
# A tiny shim that execs the version-controlled hook (so edits to it take effect
# without reinstalling), passing git's pre-push stdin/args straight through.
cat > "$SHIM" <<'SHIMBODY'
#!/bin/bash
exec "$(git rev-parse --show-toplevel)/arena/githooks/pre-push" "$@"
SHIMBODY
chmod +x "$SHIM"
echo "arena: installed pre-push hook → $SHIM"
