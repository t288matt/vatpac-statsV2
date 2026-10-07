# Runbook: stuck collector

The collector should never exit and never stop polling. "Stuck" means the container is up but `poll_log` has no new rows, or `/health` does not answer or returns 503 with `consecutive_failures` of 0.

The watchdog restarts the collector itself after `WATCHDOG_RESTART_AFTER_CYCLES` (default 3) bad minutes, at most 3 times per hour, and sends `RESTARTED collector ...`. If you receive `restart cap ... reached`, it needs a human.

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
1. Restart: `docker compose restart collector` (the watchdog does this too).
2. If it fails to start with a reference-data error, the polygon or whitelist is missing or invalid: the collector fails closed by design. See `update-reference-files.md`.
3. If a blocked query is the cause, find the blocker in `pg_stat_activity` and `SELECT pg_terminate_backend(<pid>)` for that session only.
4. Verify: `make smoke`; a `RECOVERED` message arrives.

## Deliberately stopping the collector
Stopping it by hand makes the watchdog restart it. Stop the watchdog first: `docker compose stop watchdog collector`, and start both when done.
