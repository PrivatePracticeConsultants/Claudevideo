#!/usr/bin/env bash
# Installs the pre-commit hook. Run once after cloning.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK="$REPO_ROOT/.git/hooks/pre-commit"
[ -d "$REPO_ROOT/.git" ] || { echo "not a git repo: $REPO_ROOT"; exit 1; }
cp "$REPO_ROOT/scripts/pre-commit" "$HOOK"
chmod +x "$HOOK"
echo "installed $HOOK"
