#!/usr/bin/env bash
# setup-dev.sh — one-time developer environment setup.
#
# Run once after cloning:
#   chmod +x setup-dev.sh && ./setup-dev.sh
#
# What it does:
#   1. Configures git to use .githooks/ (activates pre-commit / pre-push).
#   2. Detects a mismatched global git identity and sets a repo-local one.
#   3. Registers the global email as blocked in .git/config so the hooks
#      catch it — no email is hardcoded in the repo itself.
#   4. Creates a Python venv and installs test dependencies.

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_ROOT"

echo "=== Print Bridge dev setup ==="

# ── 1. Activate hooks ─────────────────────────────────────────────────────────
echo "→ Setting core.hooksPath = .githooks"
git config core.hooksPath .githooks
chmod +x .githooks/pre-commit .githooks/pre-push
echo "  Hooks installed."

# ── 2. Identity check ─────────────────────────────────────────────────────────
CANONICAL_EMAIL="rubeecube@users.noreply.github.com"
CANONICAL_NAME="rubeecube"
GLOBAL_EMAIL=$(git config --global user.email 2>/dev/null || echo "")
REPO_EMAIL=$(git config user.email 2>/dev/null || echo "")

if [[ "$GLOBAL_EMAIL" != "$CANONICAL_EMAIL" && "$REPO_EMAIL" != "$CANONICAL_EMAIL" ]]; then
    echo ""
    echo "  ⚠ Global git email differs from the canonical repo identity."
    echo "    Setting repo-local identity to: $CANONICAL_EMAIL"
    git config user.name  "$CANONICAL_NAME"
    git config user.email "$CANONICAL_EMAIL"

    # Register the detected global email as blocked so the hooks catch it.
    # This lives only in .git/config — never committed or pushed.
    if [[ -n "$GLOBAL_EMAIL" ]]; then
        EXISTING=$(git config hooks.blocked-author-emails 2>/dev/null || echo "")
        if [[ -z "$EXISTING" ]]; then
            git config hooks.blocked-author-emails "$GLOBAL_EMAIL"
        elif [[ "$EXISTING" != *"$GLOBAL_EMAIL"* ]]; then
            git config hooks.blocked-author-emails "$EXISTING,$GLOBAL_EMAIL"
        fi
        echo "  Global email registered in hooks.blocked-author-emails (.git/config)."
    fi
    echo ""
fi

echo "  Git identity: $(git config user.name) <$(git config user.email)>"

# ── 3. Python venv ────────────────────────────────────────────────────────────
if [[ ! -d venv ]]; then
    echo "→ Creating Python venv"
    python3 -m venv venv
fi
echo "→ Installing test dependencies"
venv/bin/pip install --quiet -r requirements-test.txt

echo ""
echo "=== Setup complete. Run tests with: ==="
echo "    ./venv/bin/pytest tests/ -v"
