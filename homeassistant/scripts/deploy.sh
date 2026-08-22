#!/usr/bin/env bash
# deploy.sh — pull, validate ON THE BOX, reload or restart, roll back if the
# check fails, and tag the commit that worked.
#
# The rollback is the point. A config check that runs only in CI cannot see the
# custom components, the secrets, or the .storage on this machine, so the last
# possible moment to catch a bad deploy is here, after the pull and before the
# restart.
#
# Usage: ./scripts/deploy.sh [--restart]
set -uo pipefail

CONFIG_DIR="${CONFIG_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
HA_PYTHON="${HA_PYTHON:-}"
HA_URL="${HA_URL:-http://localhost:8123}"
HA_TOKEN="${HA_TOKEN:-}"
FORCE_RESTART=0
[ "${1:-}" = "--restart" ] && FORCE_RESTART=1

cd "$CONFIG_DIR" || { echo "cannot cd to $CONFIG_DIR"; exit 1; }

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
die()  { printf '\033[31m%s\033[0m\n' "$*"; exit 1; }

# Notify through the router, not through notify.* directly — same rule as the
# automations. Best-effort: a failed notification must never fail a deploy.
notify() {
  local priority="$1" title="$2" message="$3"
  [ -z "$HA_TOKEN" ] && return 0
  curl -sf -X POST "$HA_URL/api/services/script/notify_person" \
    -H "Authorization: Bearer $HA_TOKEN" \
    -H "Content-Type: application/json" \
    -d "$(printf '{"target":"all","priority":"%s","title":"%s","message":"%s"}' \
          "$priority" "$title" "$message")" >/dev/null 2>&1 || true
}

# ---------------------------------------------------------------------------
say "== recording last known good =="
LAST_GOOD=$(git rev-parse HEAD) || die "not a git checkout"
echo "current HEAD: $LAST_GOOD"

say "== pulling =="
BRANCH=$(git rev-parse --abbrev-ref HEAD)
for attempt in 1 2 3 4; do
  if git pull --ff-only origin "$BRANCH"; then break; fi
  [ "$attempt" = 4 ] && die "git pull failed after 4 attempts"
  sleep $((2 ** attempt))
done
NEW_HEAD=$(git rev-parse HEAD)

if [ "$LAST_GOOD" = "$NEW_HEAD" ]; then
  say "already up to date — nothing to deploy"
  exit 0
fi

# ---------------------------------------------------------------------------
say "== validating on this machine =="
if HA_PYTHON="$HA_PYTHON" CONFIG_DIR="$CONFIG_DIR" ./scripts/validate.sh; then
  say "validation passed"
else
  say "VALIDATION FAILED — rolling back to $LAST_GOOD"
  git reset --hard "$LAST_GOOD" || die "rollback failed; the config dir needs manual attention NOW"
  notify critical "Deploy rolled back" \
    "Config check failed on $(git rev-parse --short "$NEW_HEAD"); reverted to $(git rev-parse --short "$LAST_GOOD"). The house is running the previous config."
  die "rolled back. Fix the config and try again."
fi

# ---------------------------------------------------------------------------
# Reload beats restart: a restart drops every timer, re-runs startup
# automations, and blanks the dashboards for ~30 seconds. Only the things that
# genuinely cannot be reloaded justify one.
say "== deciding reload vs restart =="
CHANGED=$(git diff --name-only "$LAST_GOOD" "$NEW_HEAD")
echo "$CHANGED" | sed 's/^/  /'

NEEDS_RESTART=0
echo "$CHANGED" | grep -qE '^configuration\.yaml$|^packages/.*(recorder|command_line)|^secrets\.yaml$' && NEEDS_RESTART=1
[ "$FORCE_RESTART" = 1 ] && NEEDS_RESTART=1

api() {
  [ -z "$HA_TOKEN" ] && { echo "  (no HA_TOKEN — do this by hand: $1)"; return 0; }
  curl -sf -X POST "$HA_URL/api/services/$1" \
    -H "Authorization: Bearer $HA_TOKEN" -H "Content-Type: application/json" \
    -d '{}' >/dev/null && echo "  $1 ok" || echo "  $1 FAILED"
}

if [ "$NEEDS_RESTART" = 1 ]; then
  say "restart required"
  notify info "Deploying $(git rev-parse --short "$NEW_HEAD")" "Restarting Home Assistant."
  api homeassistant/restart
else
  say "reload is enough"
  for svc in automation/reload script/reload scene/reload \
             template/reload input_boolean/reload input_number/reload \
             input_select/reload input_datetime/reload timer/reload; do
    api "$svc"
  done
fi

# ---------------------------------------------------------------------------
say "== tagging last known good =="
TAG="deployed-$(date -u +%Y%m%d-%H%M%S)"
git tag -a "$TAG" -m "Deployed $(git rev-parse --short "$NEW_HEAD")" && echo "tagged $TAG"
for attempt in 1 2 3 4; do
  if git push -u origin "$TAG"; then break; fi
  [ "$attempt" = 4 ] && echo "warning: could not push tag (deploy itself succeeded)"
  sleep $((2 ** attempt))
done

say "deployed $(git rev-parse --short "$NEW_HEAD")"
