# Runbook: stuck collector

Last reviewed 12-10-2026 against commit b3189ad (the commit running in production since 11-10-2026), checked against `collector/src/vatpac/feed.py` and `main.py`. The collector never sends two requests to VATSIM less than 30 seconds apart (`MIN_REQUEST_GAP_S`), so a restart or a stalled feed does not make it poll faster; do not expect a quick burst of polls after a restart.

The collector should never exit and never stop polling. "Stuck" means the container is up but `poll_log` has no new rows, or `/health` does not answer or returns 503 with `consecutive_failures` of 0.

In this deployment the watchdog is alert-only (ADR-012): it has no Docker access, so it never restarts anything. You will receive `CRIT collector_health` (and later `poll_log_ok_age` and `network_snapshot_age`) and the restart is yours to do. Docker itself restarts the collector only if the process exits. (The restart logic in `watchdog.py`, including `RESTARTED collector ...` and `restart cap ... reached` messages, only activates if `DOCKER_HOST_URL` points at an external Docker endpoint.)

## Diagnose
```sh
docker compose ps collector
docker compose logs --tail=200 collector
docker compose exec collector python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/health').read())"
docker compose exec db psql -U vatpac -d vatpac -c "SELECT now(), max(polled_at) FILTER (WHERE status='ok') AS last_ok FROM poll_log"
docker compose exec db psql -U vatpac -d vatpac -c \
  "SELECT pid, state, wait_event_type, now()-xact_start AS age, left(query,80) FROM pg_stat_activity WHERE datname='vatpac' ORDER BY xact_start NULLS LAST"
docker stats --no-stream
```
Common causes: a long lock held by a maintenance task or a migration; the database is down or full (see `db-full.md`); memory limit hit (`docker inspect -f '{{.State.OOMKilled}}' $(docker compose ps -q collector)`).

## Fix
1. Restart: `docker compose restart collector`.
2. If it fails to start with a reference-data error, the polygon or whitelist is missing or invalid: the collector fails closed by design. See `update-reference-files.md`.
3. If a blocked query is the cause, find the blocker in `pg_stat_activity` and `SELECT pg_terminate_backend(<pid>)` for that session only.
4. Verify: `make smoke`; a `RECOVERED` message arrives.

## Deliberately stopping the collector
Stopping it by hand raises the alerts above after about two minutes (the watchdog confirms a state over `WATCHDOG_CONFIRM_CYCLES` cycles, 2 by default) and a `RECOVERED` message when it is back. To avoid the noise stop the watchdog first, `docker compose stop watchdog collector`, and start both when done (`docker compose start collector watchdog`). A stop also leaves a gap in `poll_log`, which a running soak counts.
