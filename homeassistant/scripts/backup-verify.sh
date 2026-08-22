#!/usr/bin/env bash
# backup-verify.sh — an unverified backup is not a backup.
#
# Checks that the most recent backup (a) exists, (b) is not trivially small,
# and (c) is newer than the threshold. Alerts through the notification router
# if any of those fail — including the case where the backup directory itself
# has vanished, which is the failure people discover at the worst moment.
#
# Run from cron or from an HA shell_command on a schedule.
set -uo pipefail

BACKUP_DIR="${BACKUP_DIR:-/backup}"
MIN_MB="${MIN_MB:-10}"
MAX_AGE_HOURS="${MAX_AGE_HOURS:-26}"   # 26 not 24: a daily backup that slips an hour is fine
HA_URL="${HA_URL:-http://localhost:8123}"
HA_TOKEN="${HA_TOKEN:-}"

notify() {
  local priority="$1" title="$2" message="$3"
  echo "[$priority] $title — $message"
  [ -z "$HA_TOKEN" ] && return 0
  curl -sf -X POST "$HA_URL/api/services/script/notify_person" \
    -H "Authorization: Bearer $HA_TOKEN" -H "Content-Type: application/json" \
    -d "$(printf '{"target":"all","priority":"%s","title":"%s","message":"%s"}' \
          "$priority" "$title" "$message")" >/dev/null 2>&1 || true
}

if [ ! -d "$BACKUP_DIR" ]; then
  notify critical "Backup directory missing" "$BACKUP_DIR does not exist. There are no backups at all."
  exit 1
fi

LATEST=$(find "$BACKUP_DIR" -maxdepth 1 -type f \( -name '*.tar' -o -name '*.tar.gz' \) \
         -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)

if [ -z "$LATEST" ]; then
  notify critical "No backups found" "No .tar backups in $BACKUP_DIR."
  exit 1
fi

SIZE_MB=$(( $(stat -c %s "$LATEST") / 1024 / 1024 ))
AGE_HOURS=$(( ( $(date +%s) - $(stat -c %Y "$LATEST") ) / 3600 ))
NAME=$(basename "$LATEST")
FAILED=0

if [ "$SIZE_MB" -lt "$MIN_MB" ]; then
  notify critical "Backup is suspiciously small" "$NAME is ${SIZE_MB}MB (expected at least ${MIN_MB}MB). A truncated backup restores to nothing."
  FAILED=1
fi

if [ "$AGE_HOURS" -gt "$MAX_AGE_HOURS" ]; then
  notify critical "Backup is stale" "$NAME is ${AGE_HOURS}h old (threshold ${MAX_AGE_HOURS}h). Backups have silently stopped."
  FAILED=1
fi

# Integrity: a tar that cannot be listed cannot be restored.
if ! tar -tf "$LATEST" >/dev/null 2>&1; then
  notify critical "Backup is corrupt" "$NAME cannot be read by tar. It will not restore."
  FAILED=1
fi

if [ "$FAILED" -eq 0 ]; then
  echo "ok: $NAME, ${SIZE_MB}MB, ${AGE_HOURS}h old, archive readable"
fi
exit $FAILED
