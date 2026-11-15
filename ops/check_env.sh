#!/bin/sh
# Refuse to start the stack with weak or missing secrets.
#   sh ops/check_env.sh [ENV_FILE]      (default: .env)
# Every secret below must be: non-blank, not a placeholder (change-me*), at least 16 characters,
# letters and digits only (passwords are placed inside connection URLs), and unique.
# Values are never printed; only the variable name and the reason.
set -u

ENV_FILE="${1:-.env}"
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/env_key.sh"   # env_key FILE NAME: the one reader of .env keys (issue 212)
IMAGE_MODE_SOURCE_ONLY=1 . "$HERE/image_mode.sh"   # sha_pin_warning: the one wording of the sha- pin warning (issue 213)
MIN_LEN=16
SECRETS="POSTGRES_PASSWORD GRAFANA_RO_PASSWORD GRAFANA_ADMIN_PASSWORD WATCHDOG_DB_PASSWORD COLLECTOR_DB_PASSWORD BACKUP_DB_PASSWORD STATS_LOADER_DB_PASSWORD"

if [ ! -f "$ENV_FILE" ]; then
  echo "check_env: $ENV_FILE not found. Run: sh ops/gen_env.sh" >&2
  exit 1
fi

bad=0
seen=""
for name in $SECRETS; do
  val="$(env_key "$ENV_FILE" "$name")"
  if [ -z "$val" ]; then
    echo "check_env: $name is blank" >&2; bad=1; continue
  fi
  case "$val" in
    change-me*|changeme*|CHANGE-ME*) echo "check_env: $name is still a placeholder" >&2; bad=1; continue ;;
  esac
  if [ "${#val}" -lt "$MIN_LEN" ]; then
    echo "check_env: $name is shorter than $MIN_LEN characters" >&2; bad=1; continue
  fi
  case "$val" in
    *[!A-Za-z0-9]*) echo "check_env: $name must contain letters and digits only (it goes into URLs)" >&2; bad=1; continue ;;
  esac
  case " $seen " in
    *" $val "*) echo "check_env: $name repeats another secret; use a different value per role" >&2; bad=1 ;;
  esac
  seen="$seen $val"
done

# Registry mode is switched by VATPAC_IMAGE_PREFIX alone (issue 173); VATPAC_IMAGE_TAG is an optional override that pins an
# older build. A sha- pin gets a loud warning (values other than the pin's kind are never printed).
case "$(env_key "$ENV_FILE" VATPAC_IMAGE_TAG)" in
  sha-*) echo "check_env: $(sha_pin_warning)" >&2 ;;
esac

# Policy values (retention periods, alert thresholds) are literals in docker-compose.yml (ADR-034). A leftover line for one in
# the env file does nothing and misleads, so it is listed (names only, never values). Needs python3 or python; skipped without.
PY="$(command -v python3 || command -v python || true)"
if [ -n "$PY" ]; then
  for key in $("$PY" "$HERE/check_config_drift.py" --policy-keys 2>/dev/null | tr -d '\r'); do
    if grep -q "^[[:space:]]*\(export[[:space:]]\{1,\}\)\{0,1\}${key}[[:space:]]*=" "$ENV_FILE"; then
      echo "check_env: WARNING $key is in $ENV_FILE but is fixed in docker-compose.yml, so this line has no effect. Delete it (docs/CONFIG_MAP.md, 'Where each setting lives')." >&2
    fi
  done
fi

# An override file beats the compose literals and is invisible to git status (it is git-ignored).
if [ -f "$(dirname "$ENV_FILE")/docker-compose.override.yml" ]; then
  echo "check_env: NOTE docker-compose.override.yml is present next to $ENV_FILE and overrides the compose literals. Delete it once the fix it covers is deployed." >&2
fi

if [ "$bad" -ne 0 ]; then
  echo "check_env: FAILED. Generate strong values with: sh ops/gen_env.sh   (or edit $ENV_FILE)" >&2
  exit 1
fi
echo "check_env: OK ($ENV_FILE)"
