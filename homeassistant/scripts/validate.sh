#!/usr/bin/env bash
# validate.sh — the gate. yamllint + Home Assistant check_config, plus the two
# checks HA itself does not do: a secrets scan, and a scan for placeholder
# entity_ids that were never filled in.
#
# HA_PYTHON must point at a python with `homeassistant` installed, pinned to the
# SAME version as production. Validating against a different version is worse
# than not validating: it will accept syntax production has removed.
set -uo pipefail

CONFIG_DIR="${CONFIG_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
HA_PYTHON="${HA_PYTHON:-}"
FAILED=0

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
fail() { printf '\033[31mFAIL\033[0m %s\n' "$1"; FAILED=1; }
ok()   { printf '\033[32mok\033[0m   %s\n' "$1"; }

# ---------------------------------------------------------------------------
step "yamllint"
if command -v yamllint >/dev/null 2>&1; then
  if yamllint -c "$CONFIG_DIR/.yamllint" "$CONFIG_DIR"; then ok "yamllint clean"; else fail "yamllint reported problems"; fi
else
  printf 'skipped: yamllint not installed (pip install yamllint)\n'
fi

# ---------------------------------------------------------------------------
step "secrets scan"
# Anything that looks like a credential in a tracked file. secrets.yaml is
# gitignored and deliberately excluded from this scan.
# NOTE: inside a POSIX bracket expression, [^!\s] means "not !, \, or s" -- it
# does NOT mean non-whitespace. Use [:space:] explicitly. Getting this wrong made
# every `password: !secret ...` line look like a hardcoded credential.
PATTERN='(eyJ[A-Za-z0-9_-]{20,}|[a-f0-9]{64}|api[_-]?key["'"'"' :=]+[A-Za-z0-9]{16,}|token["'"'"' :=]+[A-Za-z0-9]{20,}|password["'"'"' :=]+[^![:space:]][^[:space:]]{5,})'
HITS=$(cd "$CONFIG_DIR" && git ls-files -z 2>/dev/null \
       | tr '\0' '\n' \
       | grep -vE '^(secrets\.yaml|.*\.example|docs/|scripts/validate\.sh)' \
       | while read -r f; do
           # A line referencing !secret is correct by construction.
           [ -f "$f" ] && grep -HnEi "$PATTERN" "$f" 2>/dev/null | grep -v '!secret'
         done)
if [ -n "$HITS" ]; then fail "possible secret in a tracked file:"; echo "$HITS"; else ok "no secrets in tracked files"; fi

# ---------------------------------------------------------------------------
step "placeholder scan"
# Commented-out placeholders are documentation (a worked example the user
# uncomments). optional/ is not-yet-live config by definition. Only flag a
# placeholder that is LIVE config -- which is exactly what makes moving a file
# from optional/ into packages/ with placeholders still in it fail this gate.
PH=$(cd "$CONFIG_DIR" && grep -rniE 'REPLACE_ME|TODO_ENTITY|CHANGEME|<your' \
      --include='*.yaml' --exclude='*.example' . 2>/dev/null \
      | grep -vE '^\./docs/' \
      | grep -vE '^\./optional/' \
      | grep -vE '^[^:]+:[0-9]+: *#' || true)
if [ -n "$PH" ]; then fail "unfilled placeholders remain:"; echo "$PH"; else ok "no unfilled placeholders"; fi

# ---------------------------------------------------------------------------
step "home assistant check_config"
if [ -z "$HA_PYTHON" ]; then
  for c in "$CONFIG_DIR/.venv/bin/python" "$(command -v hass 2>/dev/null)"; do
    [ -x "$c" ] && HA_PYTHON="$c" && break
  done
fi
if [ -n "$HA_PYTHON" ] && [ -x "$HA_PYTHON" ]; then
  VER=$("$HA_PYTHON" -c 'from homeassistant.const import __version__; print(__version__)' 2>/dev/null || echo unknown)
  printf 'validating against Home Assistant %s\n' "$VER"
  OUT=$("$HA_PYTHON" -m homeassistant --script check_config -c "$CONFIG_DIR" 2>&1)
  RC=$?
  # check_config exits 0 even on some failures; the banner is authoritative.
  if [ $RC -ne 0 ] || echo "$OUT" | grep -q 'Failed config'; then
    fail "check_config rejected the configuration"
    echo "$OUT" | grep -vE 'Attempting install|util\.package' | tail -60
  else
    ok "check_config accepted the configuration (HA $VER)"
  fi
else
  fail "no HA python found — set HA_PYTHON. Validation is NOT complete without this."
fi

# ---------------------------------------------------------------------------
printf '\n'
if [ $FAILED -eq 0 ]; then printf '\033[32mALL CHECKS PASSED\033[0m\n'; else printf '\033[31mVALIDATION FAILED\033[0m\n'; fi
exit $FAILED
