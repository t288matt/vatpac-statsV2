#!/bin/sh
# Smoke test for a running stack. Run after `docker compose up -d` from the repo root:
#   sh ops/smoke.sh
# Prints PASS/FAIL per check; exits non-zero if any check fails. Needs only docker compose
# and a .env file (reads GRAFANA_RO_PASSWORD from it). POSIX sh.

WAIT_HEALTH="${SMOKE_WAIT_HEALTH:-240}"   # seconds to wait for containers to be healthy
WAIT_POLL="${SMOKE_WAIT_POLL:-90}"        # seconds to wait for an ok poll_log row
FAILS=0

pass() { printf 'PASS  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1"; FAILS=$((FAILS + 1)); }

dc() { docker compose "$@"; }

health_of() {
  cid="$(dc ps -q "$1" 2>/dev/null)"
  [ -n "$cid" ] || { echo missing; return; }
  docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || echo unknown
}

wait_healthy() {
  svc="$1"; waited=0
  while [ "$waited" -lt "$WAIT_HEALTH" ]; do
    [ "$(health_of "$svc")" = "healthy" ] && return 0
    sleep 5; waited=$((waited + 5))
  done
  return 1
}

psql_db() { dc exec -T db sh -c 'psql -X -tA -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -c "$1"' sh "$1"; }

# 1. containers healthy
for svc in db collector grafana backup watchdog; do
  if wait_healthy "$svc"; then pass "container healthy: $svc"; else fail "container healthy: $svc (status: $(health_of "$svc"))"; fi
done
mig="$(docker inspect -f '{{.State.ExitCode}}' "$(dc ps -aq migrate 2>/dev/null)" 2>/dev/null)"
[ "$mig" = "0" ] && pass "migrate exited 0" || fail "migrate exit code: ${mig:-unknown}"

# 2. collector /health
if dc exec -T collector python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('HEALTH_PORT','8080'), timeout=5)" >/dev/null 2>&1; then
  pass "collector /health returns 200"
else
  fail "collector /health returns 200"
fi

# 3. poll_log has an ok row within WAIT_POLL seconds
waited=0; n=0
while [ "$waited" -lt "$WAIT_POLL" ]; do
  n="$(psql_db "SELECT count(*) FROM poll_log WHERE status = 'ok' AND polled_at > now() - interval '90 seconds'" 2>/dev/null)"
  [ "${n:-0}" -gt 0 ] 2>/dev/null && break
  sleep 5; waited=$((waited + 5))
done
if [ "${n:-0}" -gt 0 ] 2>/dev/null; then pass "poll_log has an ok row within 90 s"; else fail "poll_log has an ok row within 90 s"; fi

# 4. Grafana /api/health
if dc exec -T grafana wget -q -O - http://127.0.0.1:3000/api/health 2>/dev/null | grep -q '"database"[: ]*"ok"'; then
  pass "grafana /api/health reports database ok"
else
  fail "grafana /api/health reports database ok"
fi

# 5. grafana_ro cannot write (the role is read-only by default, but a session can switch that off, so
#    switch it off here and prove the privileges themselves deny the write)
RO_PW="$(sed -n 's/^GRAFANA_RO_PASSWORD=//p' .env 2>/dev/null | head -n1)"
ro_try() {
  dc exec -T -e PGPASSWORD="$RO_PW" db sh -c 'psql -X -tA -h 127.0.0.1 -U grafana_ro -d "$POSTGRES_DB" -c "SET default_transaction_read_only = off" -c "$1" 2>&1' sh "$1"
}
denied=0
for stmt in "CREATE TABLE smoke_should_fail (i int)" "INSERT INTO poll_log (status) VALUES ('ok')" "DELETE FROM poll_log"; do
  if ro_try "$stmt" | grep -qi 'permission denied'; then denied=$((denied + 1)); fi
done
if [ "$denied" -eq 3 ]; then pass "grafana_ro cannot create, insert or delete"; else fail "grafana_ro write attempts denied: $denied of 3"; fi

if [ "$FAILS" -eq 0 ]; then echo "SMOKE PASS"; exit 0; fi
echo "SMOKE FAIL ($FAILS failed)"; exit 1
