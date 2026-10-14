#!/bin/sh
# Refuse to start the stack with weak or missing secrets.
#   sh ops/check_env.sh [ENV_FILE]      (default: .env)
# Every secret below must be: non-blank, not a placeholder (change-me*), at least 16 characters,
# letters and digits only (passwords are placed inside connection URLs), and unique.
# Values are never printed; only the variable name and the reason.
set -u

ENV_FILE="${1:-.env}"
MIN_LEN=16
SECRETS="POSTGRES_PASSWORD GRAFANA_RO_PASSWORD GRAFANA_ADMIN_PASSWORD WATCHDOG_DB_PASSWORD COLLECTOR_DB_PASSWORD BACKUP_DB_PASSWORD"

if [ ! -f "$ENV_FILE" ]; then
  echo "check_env: $ENV_FILE not found. Run: sh ops/gen_env.sh" >&2
  exit 1
fi

bad=0
seen=""
for name in $SECRETS; do
  # last assignment wins, as in compose; strip a trailing CR (files edited on Windows)
  val="$(sed -n "s/^${name}=//p" "$ENV_FILE" | tail -n 1 | tr -d '\r')"
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

# Registry image variables (issue 173): both or neither. One alone is a trap (a tag alone builds locally and tags it
# sha-...; a prefix alone pulls a :local image that does not exist). Empty counts as unset; values are never printed.
img_prefix="$(sed -n "s/^VATPAC_IMAGE_PREFIX=//p" "$ENV_FILE" | tail -n 1 | tr -d '')"
img_tag="$(sed -n "s/^VATPAC_IMAGE_TAG=//p" "$ENV_FILE" | tail -n 1 | tr -d '')"
if [ -n "$img_prefix" ] && [ -z "$img_tag" ]; then
  echo "check_env: VATPAC_IMAGE_PREFIX is set but VATPAC_IMAGE_TAG is not; set both or neither" >&2; bad=1
elif [ -z "$img_prefix" ] && [ -n "$img_tag" ]; then
  echo "check_env: VATPAC_IMAGE_TAG is set but VATPAC_IMAGE_PREFIX is not; set both or neither" >&2; bad=1
fi

if [ "$bad" -ne 0 ]; then
  echo "check_env: FAILED. Generate strong values with: sh ops/gen_env.sh   (or edit $ENV_FILE)" >&2
  exit 1
fi
echo "check_env: OK ($ENV_FILE)"
