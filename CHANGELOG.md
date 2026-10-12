# Changelog

What changed in VATPAC Stats v3, newest first, in plain words. Dates are DD-MM-YYYY and times are UTC. Written 10-10-2026 from the git history, [docs/TRACKER.md](docs/TRACKER.md), [docs/DECISIONS.md](docs/DECISIONS.md) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) section 10; updated in the evening of 10-10-2026 (docs batch 2) for the state of `origin/main` at commit 27cb266 (CI green at 27cb266; main then moved to c89e544).

How to read it. "Deployed" means running on the production VM, with the date and time and commit taken from the deploy records in the tracker and the runbooks. Where a record gives only part of that, the entry says "not recorded". The code history is much finer than this page; the full list is `git log`. Decision numbers (ADR-nnn) are in docs/DECISIONS.md and issue numbers (#nn) are on GitHub.

Updated 12-10-2026 (docs batch 3, issue #107). The production VM runs commit b3189ad, marked live on 11-10-2026 at 20:35:53 UTC in the VM's release record (`.deploy/release.log`, read on 12-10-2026). Everything merged to main up to c89e544 on 10-10-2026, plus the work of 11-10-2026 described below, is now deployed. Main has since moved on by documentation and tooling commits only (the last, 86a5172, adds no code to the running system; the VM marker stays b3189ad).

No deploy happened on 10-10-2026. The VM stayed on commit 3eb81a3 (deployed 09-10-2026) all that day; the work merged on 10-10-2026 went live in the deploys of 11-10-2026.

## Merged 09-10 to 10-10-2026 (deployed 11-10-2026)

The items below were written while they were still undeployed. They are now live: they shipped in the deploy marked at 12:38:39 UTC on 11-10-2026 (marked commit 34e5194), which contains the branch tips named below. The original plan to ship them in separate steps (deploys 1 and 2 at commit 520dc3e, then fix-sql) was replaced by that single deploy; the repository does not record the owner's reason or the step-by-step checks of that deploy (listed under "Things this page could not confirm").

### fix-ops (merged about 08:42 UTC on 10-10-2026, branch tip d0ceaaa; independently reviewed in two rounds)
Safer backups, restores, roles and alerts. Fixes issues #81, #83, #84, #86, #90, #91, #95, #96, #97, #101 and #102.
- A backup that fails its own check is no longer kept as good, and `pg_dump` and its check have time limits.
- The restore test is stricter: it refuses a dump that is empty or stale, not only one that is missing tables.
- The database roles are applied in one go, and the collector keeps the right to delete rows from one table only (`poll_log`) instead of nine.
- Telegram messages that cannot be sent are kept and retried in order instead of being lost; a new check warns when the copy of the backup to a second place has not worked recently, and when the Telegram queue is stuck.
- The smoke test can no longer change data. The test database in CI is built the same way as production. The release workflow only moves the `latest` tag for the highest version.
- Dates in integrity alerts read DD-MM-YYYY.
- Review record: [docs/reviews/10-10-2026-fix-ops.md](docs/reviews/10-10-2026-fix-ops.md).

### fix-collector (merged about 08:51 UTC on 10-10-2026, branch tip acc55db; independently reviewed in two rounds)
A sturdier collector. Fixes issues #82, #85, #87, #88, #92, #99 and #100.
- Odd or changed shapes in the VATSIM feed are counted as errors instead of stopping a poll.
- Health thresholds follow the polling interval instead of fixed numbers.
- Only one snapshot is written at a time, with a time limit, so a slow write cannot pile up behind the next poll.
- The pilots-in-region count leaves out pilots whose record was rejected.
- Expect a polling gap of about 45 seconds when it is deployed.
- Review record: [docs/reviews/10-10-2026-fix-collector.md](docs/reviews/10-10-2026-fix-collector.md).

### Plain-language Telegram messages (issue #79; commits 3c8df0e, aacd655, bed4dfa, cdfdc1b; on main 09-10-2026 evening)
Alerts become a short sentence in everyday words with a last line starting `Technical:` for the detail, labelled "heads up", "problem" or "all clear". When an alert clears, the message says how long it lasted. Independently reviewed. Details in [docs/RUNBOOKS/alerts.md](docs/RUNBOOKS/alerts.md).

### fix-sql (on main by the evening of 10-10-2026; branch tip when reviewed b9f0170; the merge time is not recorded)
Three database and dashboard fixes and one measurement. Fixes issues #93, #94 and #98; #89 is measured and documented only. Independently reviewed in two rounds (round-2 record is on branch `review-fix-sql-r2`; its low follow-ups are issue #112).
- Controllers who log back in no longer count as "reactivated" unless they have an earlier session (ADR-028, confirmed by the owner; the live count goes from 53 to 0 until 90 days of history exist) (issue #93).
- The Monday event panel shows this Monday, not last Monday, before 07:00 UTC (issue #94).
- Dropping old position partitions is rewritten as migration 20261009000170: it takes locks without waiting, re-checks each partition before dropping and drops at most 10 per call; `maintenance.py` repeats the call. The first real drop is not due until about 06-01-2027 (issue #98).
- The flight view's growth with history is measured (docs/FLIGHT_VIEW_SCALING.md); the fix is future work (issue #108).
- The first real partition drop is still not due until about 06-01-2027.

### Replay and fuzz tests (issue #103; on main 10-10-2026, last commit 27cb266 at 20:39 UTC)
Tests only; nothing to deploy. Two real raw-feed snapshots are kept as fixtures, with a replay tool, seeded fuzz generators, parser fuzz tests and ingest tests against Postgres. They found two real defects, now issues #111 (oversized multi-byte callsign) and #114 (infinite controller frequency), pinned as expected failures until fixed. The full fuzz grids run in CI.

### Documentation (10-10-2026)
- Batch 1: [docs/IF_CLAUDE_IS_UNAVAILABLE.md](docs/IF_CLAUDE_IS_UNAVAILABLE.md), the documentation index [docs/README.md](docs/README.md), this changelog, and fixes to ten stale statements (issue #110).
- Owner card for the off-VM backup copy on the Proxmox host ([docs/OWNER_BACKUP_COPY.md](docs/OWNER_BACKUP_COPY.md), 10-10-2026 16:45 UTC; ADR-026 item 7; not yet run).
- Deploy plan pins the commit to ship (520dc3e) and the owner decision that a hung collector with no code change may be restarted plainly (issue #110).
- Batch 2 (RESUME.md rewritten short, tracker rows T66 to T71, working agreement additions, this section): branch `docs-batch-2`.

### Also on main, no effect on the running system
- Collector size budget raised from 1700 to 1800 lines (ADR-027, 10-10-2026).
- Retention rules decided (ADR-026, 09-10-2026): raw positions kept 180 days, sessions, flights and controller data kept forever, and more. Decided but not built yet (issue #21).
- Working agreement and team briefs ([docs/WORKING_AGREEMENT.md](docs/WORKING_AGREEMENT.md), [docs/TEAM_BRIEFS.md](docs/TEAM_BRIEFS.md)), the deploy plan and the cold-start prompt.
- CI and release workflow fixes (a line-length error had kept CI red for four commits on 10-10-2026).

### Collector fixes for oversized and odd feed values (10-10-2026, c89e544, PR 117; issues #111 and #114)
A callsign, departure or arrival is capped in bytes and an index-size error is bisected out like a bad row; an infinite or huge controller frequency is treated as malformed. Before, either could lose the whole snapshot on every poll. Found by the replay and fuzz tests; the expected-failure pins are removed. Deployed in the 11-10-2026 12:38 deploy.

### Read-only deploy checks (10-10-2026, 991ddc0, PR 116; issue #104 trimmed)
`ops/deploy.sh` with `--preflight`, `--verify`, `--status` and `--mark` (a release marker on the VM). Reading checks only; every deploy still needs the owner's go. The runner and automatic rollback are parked.

### Second review of deploy 1 (10-10-2026)
An Opus review of privileges and backup and restore says go with two extra checks, now in docs/DEPLOY_PLAN_REVIEW_FIXES.md (record on branch `review-fix-ops-opus`). It also filed issue #118 (backup validation accepts a dump of an empty or wrong database).

### Not on main
- Incident 10-10-2026 (issue #113): two snapshots were stored late (apply timeouts at 15:47 and 15:52 UTC) because several test containers ran on the live VM at once. No data was lost; new rule in the working agreement.

## Deployed

### 11-10-2026 16:01 (marked): faster "online now" and a stricter backup check (commit aeaf72f; issues #109 and #118)
- `online_now`, `pilot_latest` and `pilot_online_now` look up the newest position of each session with a `LATERAL ... LIMIT 1` lookup instead of probing every position partition, and the read-only Grafana role has JIT off (migration 20261011000180, commit 60d848c; issue #109). Results are meant to be identical; the speed-up matters once ADR-026 retention creates about 190 partitions.
- A backup is only kept if it holds real data: at least 50 percent of the size of the newest good dump (or at least 1 MiB when there is none), data entries for the key tables, and rotation never leaves fewer than 3 good dumps (settings `BACKUP_MIN_SIZE_PERCENT`, `BACKUP_MIN_FIRST_BYTES`, `BACKUP_MIN_KEEP`; issue #118, commits 9c3d866 and 3caf4bd). The backup container was recreated at 15:56 UTC (container start time).
- Rollback copy on the VM: `~/vatpac-statsV2.rollback-11-10-grafana` (15:55).

### 11-10-2026 12:38 (marked) and 12:47 (marked, documents only): the combined deploy of everything merged on 09-10 and 10-10-2026 (commits 34e5194, then b2a4cd6)
- Contains fix-ops, fix-collector, the plain-language Telegram wording, fix-sql (migration 20261009000170), fix-111-114 and the read-only deploy checks, all described in the section above, plus the collector changes of the morning of 11-10-2026:
- **A hard floor of 30 seconds between any two requests to VATSIM**, retries and discovery included (`MIN_REQUEST_GAP_S` in `feed.py`, commit 4d5b16e). Owner rule: never overpoll the feed. Once the feed has stalled for a full interval the collector polls again at the normal interval instead of faster (commit 0c720de), and waits for the feed end at once on shutdown (commit f780b20).
- The collector and watchdog containers were recreated about 12:35 UTC (container start times, restart counts 0). The 12:47 mark covers a documents-only update (reviews kept in docs/reviews).
- Rollback copies on the VM: `~/vatpac-statsV2.rollback-11-10-all2` (dated 11-10 12:46, fits this deploy). `-all` is dated 09-10 19:13, so its contents were not verified and it may be an older copy.
- Context, not caused by the deploy: the Proxmox host restarted or crashed at about 08:49 UTC on 11-10-2026 (database container start time, verified), and the owner reported the VATSIM feed frozen from about 03:08 to about 12:00 UTC (not verified here); both left a data gap. The invariant checks that count snapshot gaps are expected to clear about 14-10-2026.

### 11-10-2026 20:35: stable public links and short addresses (commit b3189ad)
The ten public dashboard links now have fixed tokens (the first 32 hex characters of sha256 of `vatpac-public:` plus the dashboard uid), so they no longer change when recreated, and ten short addresses (`/live`, `/traffic`, `/new-pilots`, `/origins`, `/atc`, `/atc-retention`, `/monday`, `/military`, `/monthly`, `/health`) redirect to them. The fixed `ops/grafana_public.py` (finds Grafana through `GRAFANA_BIND` and `GRAFANA_PORT`; `--traefik-labels --host` prints the Traefik labels) is on the VM tree; the 10 links were replaced once by the stable ones. The owner pasted the label block into `~/vatpac-proxy/docker-compose.yml` on the Traefik machine and recreated the container. Checked from outside through Cloudflare: each short address answers 302, the dashboard loads (200), anonymous API calls answer 401. The hostname `vpac.mpengor.com` is confirmed. See [docs/RUNBOOKS/public-access.md](docs/RUNBOOKS/public-access.md).

### 11-10-2026 about 19:40: Grafana 12.4.12 and public read-only dashboards (commit 848526c)
Grafana was upgraded from 11.6.5 to 12.4.12 (image `grafana/grafana`, the OSS edition, because the 11.6 line ended with 11.6.15 on 03-06-2026 and `grafana-oss` stops at 11.6.5); it was tested first on a copy of the live volume. All ten dashboards are now published read-only on the internet through Grafana public dashboards (one link each, fixed time range, anonymous access off; owner decision, ADR-029) via Cloudflare, a Cloudflare Tunnel, Traefik and a small helper container on the owner's other machine. The Departure board is fixed to YSSY. Links are managed by `ops/grafana_public.py`. Rollback copies are on the VM (`~/grafana_data_pre_12.tgz`, `~/vatpac-statsV2.rollback-11-10-grafana`). Setup: [docs/RUNBOOKS/public-access.md](docs/RUNBOOKS/public-access.md). 

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
Specification and scaffold (commits e5616a5 and 450b0fb, about 16:10 UTC), the full first build of collector, schema, dashboards, compose stack, ops scripts and tests (d6d4398, about 19:32 UTC), an architecture and docs pass (5aff4f3), and a round of review fixes: flights as an incrementally maintained table, least-privilege database roles, collector hardening, the soak report (1587ba9, 7c361d4). Decisions ADR-001 to ADR-019 date from this day.

## Things this page could not confirm
- The checks run at each of the four deploys marked on 11-10-2026 (smoke, panel check, restart counts, watch period): the VM's release record holds only the commit and time, and no deploy report for 11-10-2026 is in the repository. Which of the changes in the 12:38 deploy were applied at that mark, and which at later ones, is inferred from the branch contents of the marked commits and the VM's file and container times.
- Whether the plan of separate deploys 1 and 2 was dropped by an owner decision (not recorded in the repository).
- The result of the 12-10-2026 pending checks (collector maintenance at about 12:35, the 02:00 backup under the new content check, first message in the new wording): not read for this page.
- A Grafana 12.x first-start note: it downloads default plugins from grafana.com (reported in the session handover of 11-10-2026, not checked here); the owner has not yet decided whether to switch that off.
- The commit and time of the first deploy on 07-10-2026 (see above).
- The deployed commits of the two small database-only changes on 09-10-2026 (about 15:57 and about 16:30); the tracker gives only the times.
- Whether any deploy happened between 07-10-2026 and 08-10-2026 12:37 other than the first: the tracker and ARCHITECTURE.md record none.
- Merge times are git commit times converted to UTC (git records them in British Summer Time, one hour ahead of UTC); deploy times come from the tracker rows and were not re-read from the VM, except that the VM's running containers (database, collector, grafana and backup up for about 24 hours, watchdog for about 20 hours at 15:24 on 10-10-2026) are consistent with the deploys above.
