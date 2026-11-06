#!/bin/sh
# Write a .env from .env.example with a fresh random hex password for every blank secret.
#   sh ops/gen_env.sh [OUT_FILE]        (default: .env; refuses to overwrite unless FORCE=1)
# Passwords are written to the file only (mode 600) and are never printed.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/../.env.example"
OUT="${1:-.env}"

[ -f "$SRC" ] || { echo "gen_env: $SRC not found" >&2; exit 1; }
if [ -e "$OUT" ] && [ "${FORCE:-0}" != "1" ]; then
  echo "gen_env: $OUT already exists; refusing to overwrite (FORCE=1 to replace)" >&2
  exit 1
fi

rand() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24
  else
    head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n'
  fi
}

umask 077
tmp="$OUT.tmp.$$"
trap 'rm -f "$tmp"' EXIT
: > "$tmp"
filled=""
while IFS= read -r line || [ -n "$line" ]; do
  line="${line%"$(printf '\r')"}"
  case "$line" in
    POSTGRES_PASSWORD=|GRAFANA_RO_PASSWORD=|GRAFANA_ADMIN_PASSWORD=|WATCHDOG_DB_PASSWORD=|COLLECTOR_DB_PASSWORD=|BACKUP_DB_PASSWORD=|METABASE_RO_PASSWORD=|METABASE_DB_PASSWORD=|METABASE_ENCRYPTION_KEY=)
      printf '%s%s\n' "$line" "$(rand)" >> "$tmp"
      filled="$filled ${line%=}"
      ;;
    *) printf '%s\n' "$line" >> "$tmp" ;;
  esac
done < "$SRC"
mv "$tmp" "$OUT"
chmod 600 "$OUT" 2>/dev/null || true
trap - EXIT
echo "gen_env: wrote $OUT; generated:$filled"
