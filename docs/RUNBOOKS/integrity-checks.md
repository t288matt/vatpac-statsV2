# Runbook: the scheduled data-integrity checks

Last reviewed: 09-10-2026, against `ops/watchdog/integrity.py` and `ops/audit/invariants.sql`. Times are UTC. GitHub issue 20, tracker T16 (slim version; invariant pack defects: issue 77, tracker T54). Alert meanings are in [alerts.md](alerts.md); this page says what the checks do, how to run them by hand and what is deliberately not automated.

## What runs, where and how often

Both checks run inside the `watchdog` container, in its normal cycle, as the read-only database role `watchdog_ro`. They are slow checks: the watchdog remembers when each last ran in its status file (volume `watchdog_status`, key `state.integrity`), so a restart of the watchdog or of the VM neither re-runs them at once nor makes them wait another whole interval. A check that has never run (a fresh install) runs on the first cycle. A run that could not complete (database down, grant missing) is retried after 1 hour.

| Alert name | What it does | Default schedule | Setting |
|---|---|---|---|
| `data_integrity` | Runs the 18 read-only invariants in `ops/audit/invariants.sql` in one repeatable-read read-only transaction (about 0.7 s on the live database). Duplicates, impossible durations and caps, orphans, in-region not above global, snapshot counts against positions and `poll_log`, snapshot gaps over 5 minutes, identity flap (sessions of one cid and callsign less than 2 s apart), a snapshot that created sessions for most of the in-region pilots, missing flight summaries, missing daily partitions, reference-file hashes. | Every 72 hours (3 days). A sweep that found violations is repeated every 6 hours so the RECOVERED message does not wait 3 days. | `WATCHDOG_INTEGRITY_INTERVAL_HOURS` (0 disables) |
| `feed_crosscheck` | Takes up to 24 compact files of the collector's raw-feed archive, evenly spread over the last 14 days, recomputes for each the in-region pilot count and controller count (and the exact set of in-region pilots) with the independent rules in `ops/audit/independent.py` (exact-arithmetic polygon test, case-sensitive whitelist; imports nothing from `collector/`), and compares them with `network_snapshot` and the stored positions for the same `update_timestamp`. About 1 second of CPU per sample, so about 25 seconds per run, during which the watchdog's other checks wait. | Every 14 days | `WATCHDOG_CROSSCHECK_INTERVAL_DAYS` (0 disables), `WATCHDOG_CROSSCHECK_SAMPLES` (24) |

**Alert latency.** The sweep runs every 3 days, so a break that does not trip one of the real-time checks (`collector_health`, `network_snapshot_age`, `identity_flap`, `feed_archive`, `db_size` and so on) can take up to 3 days to be noticed, and a break in how the collector filters the feed (the cross-check) up to 14 days. The three-day windows inside the invariants (positions, gaps, sessions) are exactly as long as the interval, so a restart or a skipped cycle that delays a sweep can let a short-lived violation slip out of the window unseen; do not set `WATCHDOG_INTEGRITY_INTERVAL_HOURS` above 72 without widening the windows in the SQL. To look sooner, run a check by hand (below).

One alert level only: both checks report `WARN`. The usual watchdog rules apply (one message when the state is confirmed bad, one `RECOVERED` when it is clean again, nothing in between). One addition: when the sweep is already failing and a later sweep finds an invariant that was not failing before, the watchdog sends one follow-up, `WARN data_integrity: new violations since the last sweep: ...`. A violation ageing out of its window is not announced.

## Why the watchdog, and why the archive

- **Watchdog, not a new container or the collector.** It already has the database role pattern (`watchdog_ro`: read-only, 10 s statement timeout, `SELECT` on a short list of tables), the Telegram alert path with de-duplication, the status file and the persisted state, and it is independent of the collector so it also catches collector faults. A new sidecar would need its own image, credentials, health check and alert path for a job that runs for a second every three days.
- **Archive, not the live feed.** The watchdog could reach the internet (it posts to Telegram), but fetching the feed would race the collector (the stored row may not exist yet, the collector may legitimately have skipped that tick), would only ever test the one snapshot fetched, and would need the audit's timing allowances. The archive route is deterministic: a compact file is written only after the snapshot was stored, it is named by the same `update_timestamp`, there is no network call and no new secret, and samples can be spread over two weeks, including the Australian evening peak. Trade-off: the archive is written by the collector, so the check proves the collector's filtering (polygon, whitelist, gate, caps, dedupe) against the raw feed values it received, but it cannot see a mistake in how the collector first read the feed before archiving. A feed format change that breaks reading shows up as `poll_error` anomalies and failing polls, which the real-time checks already alert on.
- **What the cross-check allows.** Nothing is tolerated: counts and the pilot set must match exactly (the audit of 08-10-2026 found 0 unexplained differences in 44 snapshots; see [../DATA_INTEGRITY_AUDIT.md](../DATA_INTEGRITY_AUDIT.md) section 3.1). The legitimate differences the audit lists (a plan amended after the snapshot, the flight gate, timing between polls) do not apply because only counts and pilot identity are compared, at the same timestamp. One real difference is handled: samples older than the last change of the polygon or whitelist (`reference_data_change`) are skipped, because the stored rows were produced with the old rules. If every sample is that new, or none exists, the check warns that it had nothing to compare (it never passes silently) and retries in an hour.

## Run them by hand

The watchdog container has everything needed and uses the same role:

```sh
# the invariant pack, as the watchdog runs it (prints the verdict; violations are logged as WARNING lines)
docker compose exec -T watchdog python -c "import logging,time,watchdog as w,integrity as i; logging.basicConfig(level='INFO'); print(i.sweep(w.Config().integrity)[0])"
# the archive cross-check (about 25 s)
docker compose exec -T watchdog python -c "import logging,time,watchdog as w,integrity as i; logging.basicConfig(level='INFO'); print(i.crosscheck(w.Config().integrity, time.time())[0])"
```

These do not change the schedule or send an alert. To see every invariant with its count and detail (what the log line shows), run the file itself, read-only, as the database owner:

```sh
( echo "SET default_transaction_read_only = on;"; cat ops/audit/invariants.sql ) | \
  docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -X'
```

To force the watchdog to re-run a check at its next cycle, delete its entry from the state and restart the watchdog: not needed in normal use; the schedule is `state.integrity.<name>.last_run` in `docker compose exec -T watchdog cat /var/watchdog/watchdog_status.json`.

## What a violation means and what to do

Each line of the message is an invariant name and its count. The watchdog log has the detail (`docker compose logs --since 24h watchdog | grep "integrity violation"`).

| Invariant | Likely cause | Next step |
|---|---|---|
| snapshot rows without an ok poll_log row; ok poll_log counts differ from network_snapshot | A write path changed, or a manual edit | Compare the two tables for the timestamp; read the collector log around it. |
| in-region count above global count; positions per snapshot differ from pilots_in_region; position rows at a timestamp with no network_snapshot | Ingest bug, partial transaction, manual deletion | Same; the counts feed every live panel. |
| snapshot gaps over 5 minutes | An outage: collector stopped, VM down, feed down. Real, and already alerted at the time by `collector_health`; this one stays for 3 days. The first run after the deploy will show the 6 minute power cut of 09-10-2026 (T51) until 12-10-2026 15:42 UTC | Nothing to repair; check the time against [feed-outage.md](feed-outage.md) and [stuck-collector.md](stuck-collector.md). Shorter gaps (a missed tick, a restart) are the soak report's job. |
| newest snapshot in the future or older than 5 minutes | Feed or collector stalled at the moment of the sweep (also covered live by `network_snapshot_age`), or a future-dated feed timestamp | [feed-outage.md](feed-outage.md). |
| pilot session first/last seen differ from stored positions; pilot sessions with no position | Session bookkeeping broke | Read the collector log; run the audit scripts under `ops/audit/` by hand. |
| identity duplicates; identity-flap pairs (pilot, controller); snapshot that created sessions for most of the in-region pilots | The feed changed how it writes `logon_time` (issue 15, ADR-021) | [alerts.md](alerts.md), `identity_flap`; fetch the payloads from the archive ([feed-archive.md](feed-archive.md)). |
| positions outside plausibility caps | A CHECK constraint was dropped, or a bad feed value passed the caps | Find the rows (`gs_kt`, `alt_ft`), read the collector `rejected_values` counter. |
| flight_summary rows missing or sample_count differs | `refresh_flight_summary` is not running or failed | Collector log; `SELECT refresh_flight_summary(NULL)` by the owner role (writes: do it deliberately). |
| daily partitions missing for today..today+3 | Maintenance did not run | Collector log; [db-full.md](db-full.md) for partition housekeeping. |
| reference file hashes in poll_log differ from reference_data_change | A polygon or whitelist was loaded without being recorded | [update-reference-files.md](update-reference-files.md). |

`feed_crosscheck` messages name the snapshot time (`dd-mm-yyyy hh:mm:ss UTC`), the count that differs (`independent` is the audit rule, `stored` is the database) or the pilots that differ. A difference means the collector kept or dropped something the independent rules would not: a polygon or whitelist regression, a changed gate, a cap, or a feed field that changed meaning. Replay the file offline through the collector's own parser ([feed-archive.md](feed-archive.md), "Replay one through the collector's own parser") and compare. "Nothing to compare" means the archive is empty or disabled, or every sample predates a reference-file change: check [feed-archive.md](feed-archive.md).

If a message says `could not run (InsufficientPrivilege)`, the `watchdog_ro` grants are missing: the migrate job must run (`docker compose up -d --build migrate`, see [deploy-to-vm.md](deploy-to-vm.md), section 4e). Never run `db/roles.sql` by hand against the live database.

## Deliberately not automated (owner decision 09-10-2026)

The heavy deep recomputations of the audit stay manual tools under `ops/audit/`: `recompute_flights.py` (every flight rebuilt from raw positions), `panel_recompute.py` (26 panels recomputed from raw tables) and `capture.py` (the read-only capture they read). Run them by hand after a large change, as described in [../../ops/audit/README.md](../../ops/audit/README.md). There are no tuned per-check severities; every finding is one `WARN`.

## Tests that prove the checks fire

`tests/integration/test_integrity_checks.py` (on a throwaway `_test` database, as `watchdog_ro` when the role exists): a clean database reports 0 for every invariant; each of the 18 invariants has a deliberately broken database and must report exactly that invariant (plus the knock-on invariants listed for that case); a guard test fails if the pack and the cases ever list different invariants; the cross-check is run against stored rows and archived payloads damaged one count, one pilot or one file at a time. `tests/test_integrity_schedule.py` covers the judging of rows (a missing invariant is a failure), the persisted schedule across a restart, the alert text, de-duplication and recovery, and the settings. To see that a test really guards a check, make an invariant always return 0 in `ops/audit/invariants.sql` (or delete one) and run the file: it fails.
