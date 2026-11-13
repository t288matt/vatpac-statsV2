# Runbook: VATSIM feed outage

Symptoms: Telegram `CRIT collector_health` or `poll_log_ok_age`; Grafana monitoring panels show a gap; `poll_log` rows with `status = 'error'`.

## Triage
```sh
docker compose logs --tail=100 collector
curl -sI https://data.vatsim.net/v3/vatsim-data.json | head -5     # from the VM
curl -s https://status.vatsim.net/status.json | head -c 300
docker compose exec db psql -U vatpac -d vatpac -c \
  "SELECT polled_at, status, http_status, error FROM poll_log ORDER BY id DESC LIMIT 10"
```
| What you see | Meaning | Action |
|---|---|---|
| `http_status` 5xx / timeouts, curl also fails | VATSIM is down | Wait. Do nothing. |
| 429 | We are rate limited | Check `POLL_INTERVAL_SECONDS` is not below 15; the collector honours `Retry-After`. |
| Only the VM cannot reach it | Local network/DNS | `dig data.vatsim.net`, check the VM firewall and DNS. |
| Collector `/health` 503 with `consecutive_failures` > 0 | Upstream failure; the circuit breaker (it opens at the fifth failure, about ten minutes into an outage) slows polling and never exits | Wait. Restarting does not help (and in this deployment the watchdog restarts nothing). |
| `/health` 503 with `consecutive_failures` 0, or unreachable | Collector is stuck, unless the outage began less than about two minutes ago: one poll makes four attempts 30 s apart before it counts as one failure | Wait two minutes and look again; if still 0, see `stuck-collector.md`. |

## Recovery
The collector resumes by itself and processes the first fresh snapshot; there is no backfill, so the outage shows as a gap (`snapshot_ts` jump in `poll_log`). Do not try to fill it. Confirm: `make smoke`, and the watchdog sends a `RECOVERED` message. Every alert name is explained in [alerts.md](alerts.md).

## Afterwards
Note start/end of the gap (UTC) in the ops log. If outages recur, check the status page URL still returns `data.v3`.
