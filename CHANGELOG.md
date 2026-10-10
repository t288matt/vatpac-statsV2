# Changelog

What changed in VATPAC Stats v3, newest first, in plain words. Dates are DD-MM-YYYY and times are UTC. Written 10-10-2026 from the git history, [docs/TRACKER.md](docs/TRACKER.md), [docs/DECISIONS.md](docs/DECISIONS.md) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) section 10; written for the state of `origin/main` at commit f893585.

How to read it. "Deployed" means running on the production VM, with the date and time and commit taken from the deploy records in the tracker and the runbooks. Where a record gives only part of that, the entry says "not recorded". The code history is much finer than this page; the full list is `git log`. Decision numbers (ADR-nnn) are in docs/DECISIONS.md and issue numbers (#nn) are on GitHub.

The production VM runs the tree shipped from commit 3eb81a3 on 09-10-2026 at 19:13 (the last deploy listed below).

## Merged to main but NOT yet deployed (as of 10-10-2026)

Nothing in this section is running on the VM. Each deploy needs the owner's explicit go ([docs/DEPLOY_PLAN_REVIEW_FIXES.md](docs/DEPLOY_PLAN_REVIEW_FIXES.md)): deploy 1 (fix-ops and the Telegram wording) is planned for Sunday 11-10-2026 after the 02:00 backup and restore test have been read; deploy 2 (fix-collector) about 24 hours later.

### fix-ops (merged 10-10-2026 09:42, merge commit d0ceaaa; independently reviewed in two rounds)
Safer backups, restores, roles and alerts. Fixes issues #81, #83, #84, #86, #90, #91, #95, #96, #97, #101 and #102.
- A backup that fails its own check is no longer kept as good, and `pg_dump` and its check have time limits.
- The restore test is stricter: it refuses a dump that is empty or stale, not only one that is missing tables.
- The database roles are applied in one go, and the collector keeps the right to delete rows from one table only (`poll_log`) instead of nine.
- Telegram messages that cannot be sent are kept and retried in order instead of being lost; a new check warns when the copy of the backup to a second place has not worked recently, and when the Telegram queue is stuck.
- The smoke test can no longer change data. The test database in CI is built the same way as production. The release workflow only moves the `latest` tag for the highest version.
- Dates in integrity alerts read DD-MM-YYYY.
- Review record: [docs/reviews/10-10-2026-fix-ops.md](docs/reviews/10-10-2026-fix-ops.md).

### fix-collector (merged 10-10-2026 09:51, merge commit acc55db; independently reviewed in two rounds)
A sturdier collector. Fixes issues #82, #85, #87, #88, #92, #99 and #100.
- Odd or changed shapes in the VATSIM feed are counted as errors instead of stopping a poll.
- Health thresholds follow the polling interval instead of fixed numbers.
- Only one snapshot is written at a time, with a time limit, so a slow write cannot pile up behind the next poll.
- The pilots-in-region count leaves out pilots whose record was rejected.
- Expect a polling gap of about 45 seconds when it is deployed.
- Review record: [docs/reviews/10-10-2026-fix-collector.md](docs/reviews/10-10-2026-fix-collector.md).

### Plain-language Telegram messages (issue #79; commits 3c8df0e, aacd655, bed4dfa, cdfdc1b; on main 09-10-2026 evening)
Alerts become a short sentence in everyday words with a last line starting `Technical:` for the detail, labelled "heads up", "problem" or "all clear". Today's messages still read `vatpac-stats: WARN ...`. When an alert clears, the message says how long it lasted. Independently reviewed. Details in [docs/RUNBOOKS/alerts.md](docs/RUNBOOKS/alerts.md).

### Also on main, no effect on the running system
- Collector size budget raised from 1700 to 1800 lines (ADR-027, 10-10-2026).
- Retention rules decided (ADR-026, 09-10-2026): raw positions kept 180 days, sessions, flights and controller data kept forever, and more. The rules are decided but not built yet (issue #21).
- Working agreement and team briefs ([docs/WORKING_AGREEMENT.md](docs/WORKING_AGREEMENT.md), [docs/TEAM_BRIEFS.md](docs/TEAM_BRIEFS.md)), the deploy plan, the cold-start prompt, the owner card [docs/IF_CLAUDE_IS_UNAVAILABLE.md](docs/IF_CLAUDE_IS_UNAVAILABLE.md) and this changelog.
- CI and release workflow fixes (a line-length error had kept CI red for four commits on 10-10-2026).

### Not on main at all yet
A branch `fix-sql` (reconnected controllers need an earlier session, ADR-028; the Monday event panel before 07:00; a safer way to drop old position partitions as a new migration; a measurement of flight-view speed). It is built and is waiting for a short second review. Do not rely on anything in it.

## Deployed

### 09-10-2026 19:13: scheduled data-integrity checks (commit 3eb81a3)
The watchdog now runs a set of 18 data checks every 3 days and compares the live data with the saved raw feed every 14 days, and sends a Telegram message if either finds something (issue #20, closed). Only the watchdog was rebuilt; the collector was not restarted. The first run found one thing, the 6-minute gap from the power cut below. It clears from the 3-day window on 12-10-2026 at about 15:42. The owner confirmed the Telegram message arrived.

### 09-10-2026 about 16:30: military aircraft card 65 rebuilt from v1's list (migration 20261009000160)
The "Most popular military aircraft" panel now uses the same list of aircraft types as v1 (issue #16, closed). Database change and dashboard file only; no container restarted. Deployed commit not recorded in the tracker (built in e208565 and the dashboard changes around it).

### 09-10-2026 about 15:57: movements comment (migration 20261009000150)
A comment-only database change that states the decided behaviour of airport movements (ADR-018) (issue #32, closed). No container restarted. Deployed commit not recorded in the tracker (built in 0e98b26 and 0d2c24e).

### 09-10-2026 15:36 to 15:42: unplanned power cut (not a release)
The Proxmox host lost power; the owner restarted the VM at about 15:41. Everything came back unaided, the database recovered cleanly and there was one snapshot gap of about 6 minutes (15:36 to 15:42). This counts as a second reboot test.

### 09-10-2026 13:46 to 13:47: image updates (commit 8905d85, carrying aecec21)
Postgres 16.4 to 16.15 (database and backup images), Grafana 11.2.2 to 11.6.5 and dbmate 2.21.0 to 2.36.0 (issue #31, closed). Postgres 17 and 18, Grafana 13 and numpy 2.x were deliberately skipped. The database and Grafana containers were recreated.

### 09-10-2026 12:24: fixes bundle (commit a600905)
- Flights that are still connected are left out of the flight counts, as they cannot yet be judged complete (ADR-025, issue #38).
- The pilot-origins panel names rating 11 "Supervisor" and 12 "Administrator", as in the feed (issue #39); the collector-health panel describes rejected values correctly (issue #71).
- Raw-feed archive fixes: the size cap removes routine files before anomaly files, a failed write cleans up after itself and backs off, snapshots rejected for being from the future are kept as anomalies, and the archive stops cleanly on shutdown (issues #65 to #69).

### 09-10-2026 11:58: collector release, flight merging and 30-second polling (commit da864f9)
- **Polling every 30 seconds instead of 15** (ADR-024, issue #42). Everything that depends on the interval (the gap limit for airborne time, the "online now" windows, the soak report, the missed-snapshots panel) is derived from one setting, `POLL_INTERVAL_SECONDS`. For 24 hours after this the 24-hour coverage figures read wrong.
- **Reconnects of one flight are merged into one flight**, as v1 did (ADR-023, issue #35): the same pilot, callsign and airports within 120 minutes. Flights on the dashboards went from 234 to 177 in a simulation on 08-10-2026 data (v1: 173).
- **Collector hardening and the rolling raw-feed archive**: a compact, hourly and anomaly archive of the raw feed so that odd events can be replayed later, plus watchdog alerts for an identity flap and a stalled archive, and several defence fixes (infinite or not-a-number coordinates, snapshots dated in the future, free-text length caps) (issues #34, #36, #37, #40, #41, #43, #61 to #64).
- Deployed after an independent release review (GO with two conditions: the size-budget check green and a stop-then-build deploy).
- The v1 against v3 comparison was completed the same day: no v3 defect found (docs/V1_COMPARISON.md).

### 08-10-2026 17:10: duplicate-session fix (commit c071955)
At about 04:08 on 08-10-2026 the VATSIM feed changed the precision of logon times and 31 pilot and 3 controller sessions were stored twice. Session identity now uses whole-second logon times (ADR-021, issue #15, closed); a migration merged the twins (31 pilot and 3 controller pairs, no positions lost) and a check was added (`ops/check_identity_flap.sql`).

### 08-10-2026 12:37: dashboards rebuilt from v1 (commit fec82a4)
- The controller retention dashboard rebuilt from v1's real SQL (13 cards) and 16 more cards from v1 added (the list is in docs/CHART_DEFINITIONS.md), cards 94 and 95 matched to v1's counting, card 90 aligned with v1.
- New database views for these, the unused `controller_activity_summary` view removed, and the Grafana read-only role pinned to UTC.
- Pre-deploy dump taken; only the collector was recreated; checks passed (84 panel queries, 0 errors).
- Goal checks recorded the same day: a 12-hour soak PASS at the owner's standard (08-10-2026), and a full 24-hour soak PASS on 09-10-2026 at 07:23.

### 07-10-2026: first live deployment
The first working stack (collector, Postgres, Grafana, backup, watchdog) was built and deployed to the VM on 07-10-2026, and live data started at 21:38 UTC (no history was copied from v1: ADR-011). The same evening the reboot test passed at 21:48 and a Telegram test alert reached the owner. The exact commit and time of this first deploy are not recorded in the repository. v1 stays running on its own VM as a read-only reference.

## Earlier work, same day as the first deployment (07-10-2026, before the first live data)
Specification and scaffold (commits e5616a5 and 450b0fb, 17:09 to 17:10), the full first build of collector, schema, dashboards, compose stack, ops scripts and tests (d6d4398, 20:32), an architecture and docs pass (5aff4f3), and a round of review fixes: flights as an incrementally maintained table, least-privilege database roles, collector hardening, the soak report (1587ba9, 7c361d4). Decisions ADR-001 to ADR-019 date from this day.

## Things this page could not confirm
- The commit and time of the first deploy on 07-10-2026 (see above).
- The deployed commits of the two small database-only changes on 09-10-2026 (about 15:57 and about 16:30); the tracker gives only the times.
- Whether any deploy happened between 07-10-2026 and 08-10-2026 12:37 other than the first: the tracker and ARCHITECTURE.md record none.
- The times of merges on 09-10-2026 are git commit times; deploy times come from the tracker rows and were not re-read from the VM, except that the VM's running containers (database, collector, grafana and backup up for about 24 hours, watchdog for about 20 hours at 15:24 on 10-10-2026) are consistent with the deploys above.
