# ops/audit: independent data-integrity audit scripts (T30, 08-10-2026)

Scratch tooling written by an independent auditor for `docs/DATA_INTEGRITY_AUDIT.md`. Nothing here is imported by the
collector or run by CI. Every script that touches the live database is **read only**: one SELECT-only session over ssh to
`psql` inside the `db` container, with `default_transaction_read_only = on` (and `BEGIN ... READ ONLY` where a consistent
snapshot matters). The scripts in `throwaway/` WRITE (TRUNCATE, INSERT) and are for a scratch Postgres container only: each refuses to run unless
the database name ends in `t30b` or `_test`. They were only ever run against a throwaway container, never the live database.

| Script | Purpose | Touches |
|---|---|---|
| `independent.py` | USED BY THE WATCHDOG (14-day archive cross-check). Own point-in-polygon (exact rational arithmetic, no shapely), whitelist, near-miss, frequency, caps; `analyse(payload)` recomputes everything from one raw feed payload. Imports nothing from `collector/`. | nothing |
| `feed_sampler.py` | Fetches the public feed a modest number of times (default every 75 s, never faster than 15 s), stores gzip payloads and the independent analysis. | public feed (GET) |
| `compare_samples.py` | Compares each sampled snapshot with the stored rows for the same `update_timestamp` (counts, exact pilot set, positions, plan fields, controller set, frequency and rating events, near misses). | live DB, read only |
| `polygon_edge_test.py` | 700k synthetic points (vertices, edges, one-ulp neighbours, grid, random) through the exact implementation and shapely. | nothing |
| `capture.py` | One repeatable-read, read-only transaction: raw tables plus the result of each audited panel query (panel SQL with Grafana macros replaced). | live DB, read only |
| `recompute_flights.py` | Finished flights (v1 join rule, airborne minutes) from raw positions and sessions in plain Python, compared with `flight_summary` row by row. | capture files |
| `poll_phase.py` | Explains the missing 15 s ticks: store lag against snapshot gaps (poll-phase wrap). | capture files |
| `invariants.sql` | USED BY THE WATCHDOG (3-day sweep). Read-only SQL pack (one repeatable-read transaction, 0.7 s): 18 integrity invariants including the identity-flap detector. Each one is proved to fire by `tests/integration/test_integrity_checks.py`; the gap threshold, first-snapshot exclusion and speed were fixed under issue #77. | live DB, read only |
| `throwaway/replay_check.py` | Replays real payloads through the collector's own parse and apply code into a throwaway database, then poison records and stale / future timestamps through `poll_once`. Run inside the collector image. | throwaway DB only |

Typical run (from the repository root, with the `vatpac-v3` ssh alias):

```
python ops/audit/feed_sampler.py samples --count 44 --interval 75
python ops/audit/compare_samples.py samples --markdown samples/table.md
python ops/audit/capture.py cap && python ops/audit/recompute_flights.py cap raw_
```

Lint: `ruff check --config ops/ruff.toml ops/audit`.

Scratch-database scripts (`throwaway/`): `replay_check.py` (see above). (`synthetic_flights.sql`, `retention_drill.sql`, `panel_recompute.py` and `logon_precision_impact.py` described the old pieces table and were removed in issue 89 phase 5; they stay in git history. The drop gate is rehearsed with `ops/rehearse_drop_gate.sql`.)
Start the scratch database with `docker run -d --name vp-t30b-pg -e POSTGRES_PASSWORD=... -e POSTGRES_DB=t30b -p 127.0.0.1::5432 postgres:16.4-bookworm`,
apply every migration with `sed '/^-- migrate:down/,$d'`, and remove it with `docker rm -f -v vp-t30b-pg` afterwards.
