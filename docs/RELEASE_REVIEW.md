# Independent release review: branch `collector-release` (ff89b56)

Reviewer: an independent agent that did not write the code. Date: 08-10-2026 (all times UTC). VM clock at start 17:47, at end 18:30. Review branch: `release-review` (from ff89b56), nothing pushed, nothing deployed, the live stack was only read (SELECT-only, `docker compose ps`).

## Verdict

**GO for the code, conditional on two release conditions (C1, C2 below); NO-GO for tagging until the size-budget gate is green.** No defect found in the new rules would corrupt data or stop polling on the live feed as it is today. The merged tree passes everything except the size-budget gate (already filed by the owner). Review found 2 MEDIUM defects with a small fix each, 1 MEDIUM operational risk (clock skew), and a set of LOW items. Conditions:

- **C1.** `ops/check_size_budget.py` must pass (main.py is 310 lines, module limit 300). Already filed; proposed diff in H1.
- **C2.** Deploy with `docker compose up -d --build` (or after the new image is built), never `up -d` on the old image: the `/archive` ownership comes from the new Dockerfile (finding M4).
- Recommended, not blocking: fix M1 (the `OverflowError` poison pill), which is a two-line change, before or soon after the deploy.

## 1. Full suite on the merged tree

Method: throwaway `vp-rev-pg` (postgres:16.4-bookworm, 127.0.0.1, random port, own cluster), the CI steps mirrored (migrations, `db/roles.sql`, the `watchdog_ro` block cut from `ops/migrate/migrate.sh`), pytest inside an image built from `collector/Dockerfile` (`--network host`, one process, no xdist).

| Run | Result |
|---|---|
| Integration (`tests/integration -m integration`), ff89b56 | 237 passed, 1 skipped (`test_ingest.py:106`, "apply_snapshot did not write poll_log", a known assumption skip) in 84 s |
| Unit (`-m "not integration" tests collector/tests`) | 282 passed, 238 deselected |
| Everything together, ff89b56 | 519 passed, 1 skipped |
| Everything together, final review tree (adds my tests) | **586 passed, 1 skipped, 10 xfailed** in 106 s |
| ruff 0.8.4 (collector; ops) | clean |
| mypy 1.14.1 (collector `src`; ops `--strict`, 4 files) | clean |
| `ops/check_config_drift.py` | OK (30 settings, 23 collector env keys, 53 .env.example keys) |
| `ops/check_size_budget.py` | **FAILS, exit 1**: `main.py` 310 lines (limit 300); package 1611 of 1700 |
| `grafana/validate_dashboards.py` | OK (0 errors, 5 warnings, pre-existing) |
| shell syntax of `ops/*.sh`, `ops/migrate/*.sh` | OK |
| `docker compose config -q` (generated throwaway env) | OK |

Docs are stale on counts: `docs/RUNBOOKS/run-tests.md` says 252 unit, 231 integration, 483 total; the merged tree has 282, 238 (237 + 1 skipped), 520 (L9). The size-budget text says "1575 today"; it is 1611.

## 2. Findings

Severity: HIGH stops the release, MEDIUM should be fixed or consciously accepted, LOW is housekeeping. Each finding has a suggested GitHub issue title and a one-line body at the end of its block.

### H1 (known, filed by the owner): size-budget gate fails on the merge

Evidence: `python ops/check_size_budget.py` exits 1, `main.py  310  OVER 300`. Sizes of `main.py`: 288 on main, 306 on `collector-hardening` alone, 292 on `feed-archive-flap-alert` alone, 310 merged. `release.yml` only publishes if the CI workflow passes, so no tagged image can be built. ADR-022 raised only the package limit.
Fix (preferred, about 12 lines out of main.py, behaviour unchanged): move `_db_last_ts` and `load_last_ts` to a new `collector/src/vatpac/resume.py` and call it from `Collector`:

```diff
--- a/collector/src/vatpac/main.py
-    def _db_last_ts(self) -> datetime | None:
-        sql = "SELECT max(observed_at) FROM network_snapshot WHERE observed_at <= now() + make_interval(secs => %s)"
-        ...
-    async def load_last_ts(self) -> None:
-        ...
+    async def load_last_ts(self) -> None:
+        self.last_ts = await resume.newest_snapshot(self.pool, self.settings.feed_max_future_seconds)
```

Alternative: amend ADR-022 to raise the module limit as well. Note `tests/integration/test_review_extra_db.py::test_db_last_ts_ignores_a_future_row...` calls `Collector._db_last_ts`; update it if the function moves.
Issue: "Size budget: main.py is 310 lines, module limit 300". Body: the merge of the two branches takes main.py over the per-module limit, so CI size-budget fails and release.yml cannot publish.

### M1: one `1e999` number in the feed stops all ingest (`OverflowError` is not caught)

Evidence (`collector/src/vatpac/model.py:135-141`, `parse_snapshot` `except (KeyError, TypeError, ValueError)`): `json.loads` turns the valid JSON number `1e999` (or the non-standard `Infinity`) into `inf`; `int(inf)` raises `OverflowError`, which `_i` does not catch and `parse_all` does not count. It escapes `parse_snapshot`, `poll_once` records `OverflowError: ...` as a poll error, and **every poll fails for as long as that pilot is in the feed**. Reproduced locally for `altitude`, `groundspeed`, `heading` (and `pilot_rating`, `facility`, `rating`, `visual_range` take the same path). Pre-existing on main (same `_i`), but issues 36 and 37 were meant to harden exactly this area, and a single hostile or buggy client could trigger it. Live feed today: 0 rejected, 0 malformed on a 1.96 MB sample, so no current exposure.
Pinned by `tests/test_review_extra.py::test_infinite_integer_field_does_not_abort_the_snapshot` (strict xfail; remove the marker when fixed).
Fix:

```diff
--- a/collector/src/vatpac/model.py
     try:
         n = None if value is None else int(value)
-    except (TypeError, ValueError):
+    except (TypeError, ValueError, OverflowError):
         return None
```

Issue: "Parser: infinite number (1e999) in an integer field aborts every poll". Body: `int(inf)` raises `OverflowError`, which is not caught, so one pilot with `altitude: 1e999` fails all polls while present; catch `OverflowError` in `_i`.

### M2: NaN and infinite coordinates are accepted and silently treated as "outside the polygon"

Evidence: `_f("nan")`, `_f(float("inf"))`, `_f("1e999")` return a float; `parse_snapshot` reports `malformed 0`, `rejected 0`; `inside_polygon` is false for non-finite points, so the pilot disappears from the data uncounted. It does not reach jsonb (outside pilots are dropped first, so there is no insert failure). Issue 37 asked that unparsable coordinates be counted; non-finite ones are not. Pinned by `test_non_finite_coordinate_is_malformed` (strict xfail).
Fix:

```diff
 def _f(value: Any) -> float | None:
-    return None if value is None else float(value)
+    if value is None:
+        return None
+    x = float(value)
+    if not math.isfinite(x):
+        raise ValueError(f"non-finite coordinate {value!r}")
+    return x
```

Issue: "Parser: NaN or infinite latitude/longitude is not counted as malformed". Body: such a pilot is dropped as outside the polygon with no counter; treat non-finite coordinates like unparsable ones.

### M3: the future-timestamp bound turns a slow VM clock into a total outage, and a 119 s-ahead stamp costs up to 119 s of data

Evidence (`main.py:111-114`, fault test): clock 3 minutes slow (feed 180 s ahead): 3 of 3 polls `error`, nothing stored, until the clock is fixed; clock 3 minutes fast (feed 180 s behind): accepted. A stamp 119 s ahead is accepted and stored; the genuine snapshots that follow are `skipped_unchanged` until real time catches up (test: `['ok','error','ok','skipped_unchanged']`). Headroom on the live system: over the last 24 h the feed `update_timestamp` was always **behind** the poll time (7.9 s to 99.9 s, median 19.1 s), so the VM clock can run about 128 s slow before every poll fails; the VM reports "System clock synchronized: yes". The error text ("snapshot time ... is over 120 s ahead of the clock") is clear. The breaker opens after 5 failures and `/health` goes 503, so the watchdog may restart the collector uselessly (the restart cannot fix a clock).
Not a code defect; the design trade-off is stated here so the owner accepts it knowingly. Suggested mitigations: (1) a runbook line "snapshot time ... ahead of the clock means check NTP on the VM first" in `docs/RUNBOOKS/feed-outage.md`; (2) consider `FEED_MAX_FUTURE_SECONDS=60` to shorten the 119 s data-loss window (lower bound allowed is 15; the live feed never exceeds 0 ahead); (3) optional: a watchdog line when the last error text contains "ahead of the clock".
Startup behaviour (tested): empty database gives `last_ts=None`; a future-dated row (+1 day) in `network_snapshot` is ignored and the next poll is `ok`; a row at +119 s is honoured (it would then hide real updates for 119 s, same as above). Timezone and DST are not involved (all UTC, `datetime.now(UTC)`, aware timestamps).
Issue: "Document and soften the clock-skew failure mode of FEED_MAX_FUTURE_SECONDS". Body: a VM clock more than about 128 s slow fails every poll and a 119 s-ahead stamp hides real updates for 119 s; add a runbook line and consider a 60 s default.

### M4: first deploy on the old image leaves the archive volume root-owned (archive silently dead)

Evidence: the `/archive` directory is created and chowned to uid 10001 in the Dockerfile; Docker copies that ownership into a **new, empty named volume** on first mount. Tested with the new image, `--read-only`, a fresh volume: directory `drwxr-xr-x vatpac vatpac`, a gzip file written and renamed, root filesystem read-only (`touch /etc/x` refused). With the **old** image (no `/archive`), Docker creates the volume root-owned, every write fails with `PermissionError`, polling continues, and only the watchdog `feed_archive` WARN after about 10 minutes tells you. So the deploy must rebuild (the compose file builds `vatpac-stats-collector:local` from `collector/Dockerfile`, so `up -d --build` is right; plain `up -d` with a cached old image is not).
Issue: "Deploy runbook: build the collector image before first use of the archive volume". Body: the volume takes its owner from the image, so an old image yields a root-owned volume and a dead archive; say `up -d --build` and how to fix an existing root-owned volume.

### M5: hard cap deletes the oldest file first whatever its kind, so anomaly evidence is the first to go

Evidence (`archive.py:199-207`, runbook says "whatever their class"): the cap list is sorted by mtime only, so a 90-day `anomaly/` file is deleted before the newer compact and full files that explain it. Steady state is about 0.4 GB against a 1 GB cap, so this only bites after a flood (many anomalies, a big payload) or a lowered `ARCHIVE_MAX_MB`. Pinned by `test_hard_cap_keeps_anomaly_evidence_before_routine_files` (strict xfail). Test of the intended present behaviour (oldest first, stops at the cap): `test_hard_cap_removes_oldest_files_first_and_stops_at_the_cap` (passes).
Fix:

```diff
-        for _, size, path in sorted(files):
+        rank = {"compact": 0, "full": 0, "anomaly": 1}   # routine files go first
+        for _, size, path in sorted(files, key=lambda f: (rank[f[2].parent.parent.name], f[0])):
```

Issue: "Archive hard cap should delete routine files before anomaly files". Body: oldest-first across all kinds deletes the 90-day anomaly evidence first; sort routine kinds before anomalies.

### LOW items

- **L1: `ts_future` can never be the archive reason from the collector.** `poll_once` raises before `snap_ts` is set, so the archived file is `...-poll_error.json.gz` (payload kept; confirmed by the fault test). The reason exists only for direct callers. The runbook table promises `ts_future`. Also `FUTURE_S = 120` in `archive.py` ignores `FEED_MAX_FUTURE_SECONDS`. Fix: pass the parsed stamp to `submit` for the bound failure, or delete the reason and fix the runbook. Issue: "Archive: ts_future reason is unreachable from the collector".
- **L2: an error poll can lose its anomaly file when the worker is busy** (one job at a time, the next is dropped and only counted). Pinned by `test_error_poll_submitted_while_busy_is_dropped_and_counted`. Normal cadence: a job takes tens of milliseconds, polls are 15 s apart, so rare; on a slow disk (6 s per file test) 19 of 20 jobs were dropped, including the anomalies. Fix: keep a one-slot "pending anomaly" that replaces routine work. Issue: "Archive: never drop an error-poll anomaly because the worker is busy".
- **L3: a failed write leaves its `.tmp` and retries every poll.** `write_gz` does not unlink the partial `.name.tmp` when `write_bytes` fails (full disk); the leftover holds space until the next prune (about 10 minutes). The slot is not advanced on failure, so the compact and full writes (about 1.5 MB gzip each) are retried on every poll, which is CPU churn on a full volume (all 20 polls still `ok`, 1.65 s in total). Pinned by `test_failed_write_removes_its_temporary_file`. Fix: `try: ... except OSError: tmp.unlink(missing_ok=True); raise`, and advance the slot (or back off) after a failure. Issue: "Archive: remove the partial tmp file on a failed write and back off".
- **L4: SIGTERM waits for the in-flight archive job.** Real entrypoint, 6 s per write, signal 1 s in: exit code 0, but the process exited 11.2 s after the signal because the compact and the full write (6 s each) both ran to completion (the pool is joined at interpreter exit). No partial file remained. On a healthy disk this is milliseconds; on a hung volume it would exceed Docker's 10 s grace and end in SIGKILL (a stale `.tmp`, cleaned after 10 minutes). Optional fix: `Archive.close()` that sets a stop flag checked between the two writes. Issue: "Archive: stop between writes on shutdown".
- **L5: `ARCHIVE_GAP_S` (default 35) is independent of `POLL_INTERVAL_SECONDS` (valid up to 300).** With `POLL_INTERVAL_SECONDS` above 35 every poll is a `gap` anomaly (one pair of files per 5 minutes, 90 days, about 0.6 MB each, so up to about 14 GB over 90 days at the 1 GB cap, i.e. it would churn the cap). Fix: validate `archive_gap_s > poll_interval_seconds + 20` in `Settings`, or derive the default. Issue: "Settings: tie ARCHIVE_GAP_S to POLL_INTERVAL_SECONDS".
- **L6: rejected_values semantics changed; panel 9 is stale and a persistent condition counts every poll.** Malformed entries and cut text now count, but they count again in **every poll** while the pilot is online (a pilot with over-2000-character remarks adds 1 per 15 s, about 5,760 a day). Panel 9 "Rejected values (last 24 hours)" (`grafana/dashboards/monitoring/collector-health.json`, id 9) turns orange at 100 and its description still says "Groundspeed or altitude values outside the plausibility caps". No watchdog rule, alert or other panel reads `rejected_values` (grep of `ops/` and `grafana/` confirmed), so there is no false alert; the panel can go orange. Live baseline: 0 for the last 2 days; a live feed sample: 0. Fix: update the description to "Values dropped or cut at ingest (caps, unparsable, malformed entries, long text)" and treat the orange as informational, or count distinct sessions. Issue: "Collector-health panel 9: describe rejected_values correctly".
- **L7: late older snapshot cannot fill a NULL in the newer one.** After the issue-41 guard, if the newest snapshot has `remarks` NULL and a late older one has a value, the value is ignored (in order it would have survived via COALESCE). Harmless for a replay of the same feed; pinned in `test_late_older_snapshot_cannot_fill_a_null_in_the_newer_one`. No action.
- **L8: loop work in the poll thread.** Measured on a 1.53 MB feed over 200 polls: archive on p50 186 ms (p99 401), loop lag max 245 ms; archive off p50 180 ms (p99 412), loop lag max 329 ms. The archive adds nothing measurable; the up-to-330 ms loop stalls come from the in-loop parse and are pre-existing.
- **L9: documentation counts stale** (test counts, "1575 today" in the budget text, run-tests.md).

### Checks that found no defect

- **Pilot upsert (store.py `_FIELDS`, `_NEWER`, `CASE` guards)**: 40 random sequences (12 in-order snapshots each, 5 sessions, about 40 % NULL per field, random ratings, speeds, altitudes, headings) applied through the new statement and through the statement from main f41494d produce identical `pilot_session` and `pilot_position` rows (`test_new_upsert_equals_old_for_in_order_snapshots`, 40 cases). Equal `last_seen` behaves as before (`>=`), NULLs keep the old value, `plan_changed_at` moves only for a newer snapshot, `first_seen`/`last_seen`/maxima stay order-free, and a replay of the last three snapshots of 10 sequences writes zero rows (rowcount 0 and unchanged `xmin`).
- **Future bound**: both sides tested (clock 3 min slow and fast, 119 s accepted, 10 min rejected); error poll is archived as an anomaly with its payload (`anomaly/.../poll_error.json.gz`); polls recover and nothing stays stuck.
- **Restart replay**: fresh Collector with the same snapshot: `ok` then `skipped_unchanged`; `poll_log` shows `ok 1, skipped_unchanged 1`.
- **Archive**: atomic temp-then-rename (a failed rename leaves no final file, `test_a_write_that_cannot_be_renamed_never_leaves_a_final_file`); names are built only from timestamps and a fixed tag set, so there is no path traversal and no collision between kinds (separate folders) or reasons (the tag is in the name); pruning cannot loop (one pass, deletions only, `OSError` skipped) and does not touch files outside the three kind folders; a read-only directory, a file where the directory should be, and a full 20 KB tmpfs all leave 20 of 20 polls `ok` with errors counted (20, 20, 20) and a single log line. Memory over 500 polls of a 1.53 MB feed: RSS 109 MB at poll 50, 110 MB at poll 500; p50 197 ms, p99 384 ms, max 421 ms per poll; 153 files, 6.7 MB, 0 dropped, 0 errors. The 04:08 pattern (three snapshots with fractional `logon_time`, then three whole-second): 24 pilot and 4 controller sessions before and after (exactly one each), one `precision` anomaly archived with its `-prev`.
- **Watchdog**: on a throwaway copy with 300,000 pilot and 100,000 controller sessions the flap queries cost 58 ms total for a normal 24 h window (index scan on `last_seen`) and 230 ms in the worst case (window covering every row, hash join); cheap at the watchdog interval. Alert dedupe and RECOVERED are covered by the mutation check (M43, M44 killed). With the collector `/health` unreachable `check_archive` gets `body=None` and reports OK ("disabled or not reported"), and the existing `collector_health` check is the one that alerts, so there is no double alert. The `watchdog_ro` grants for `pilot_session` and `controller_session` are in `migrate.sh` and in the new CI step (applied in my run; the 48 role tests and `test_watchdog_flap.py` passed). Live check: 0 fractional `logon_time` and 0 session pairs in the last 24 h, so the flap WARN will not fire on deploy.
- **Settings**: ranges are sane (`TEXT_MAX_LENGTH` 100..100000, `FEED_MAX_FUTURE_SECONDS` 15..3600, archive intervals 1.., retention `gt 0`); drift check, `.env.example`, compose and CONFIG_MAP agree. Only L5 is missing as a cross-field rule.

## 3. Mutation check of the new rules

45 deliberate breaks, applied one at a time to a scratch copy (`tests/tools/release_review_mutants.py`, never committed to the tree under test), each run against the builders' unit tests, the watchdog tests and the relevant integration files. First pass on ff89b56 plus nothing of mine: **39 killed, 6 survived (87 %)**. After my tests: **45 of 45 killed**.

| # | Break | Result (first pass) | After review tests |
|---|---|---|---|
| M01 | future bound comparison flipped | KILLED (test_hardening) | |
| M02 | future bound check removed | KILLED | |
| M03 | future bound default 120 to 100000 | KILLED (via test_archive) | |
| M04 | future bound ignores the setting | KILLED | |
| M05 | startup `load_last_ts` call removed | **SURVIVED** | KILLED by `test_run_loads_the_last_snapshot_time_after_the_first_maintenance` |
| M06 | startup query counts future rows | KILLED | |
| M07 | `_SUP` no longer excluded from near-miss | KILLED | |
| M08 | `_OBS` no longer excluded | KILLED | |
| M09 | text cap `>` to `>=` | KILLED | |
| M10 | text cap keeps one extra character | KILLED | |
| M11 | text cut not counted | KILLED | |
| M12 | `parse_snapshot` ignores `max_text` | KILLED | |
| M13 | main does not pass `TEXT_MAX_LENGTH` | KILLED | |
| M14 | unparsable latitude swallowed again | KILLED | |
| M15 | nulled values not counted | KILLED | |
| M16 | malformed entries not counted | KILLED | |
| M17 | pilot parse rejections not added | KILLED | |
| M18 | controller parse rejections not added | KILLED | |
| M19 | upsert guard `>=` to `<=` | KILLED | |
| M20 | upsert guard `>=` to `>` (equal last_seen) | **SURVIVED** | KILLED by `test_equal_last_seen_still_updates_like_before` |
| M21 | `plan_changed_at` not guarded | KILLED | |
| M22 | CASE guard removed from all plan fields | KILLED | |
| M23 | `remarks` dropped from updated fields | KILLED | |
| M24 | archive future bound huge | KILLED | |
| M25 | archive older snapshot not flagged | KILLED | |
| M26 | archive gap never flagged | KILLED | |
| M27 | precision drop threshold 0.2 to 0.99 | **SURVIVED** | KILLED by `test_precision_anomaly_needs_a_drop_of_twenty_points[60-True]` |
| M28 | anomaly 5-minute throttle removed | **SURVIVED** | KILLED by `test_anomaly_with_the_same_reason_is_kept_once_per_five_minutes` |
| M29 | `poll_error` trigger removed | KILLED | |
| M30 | prune deletes newest first | KILLED | |
| M31 | hard cap ineffective (`cap * 1000`) | KILLED | |
| M32 | size trigger for the hard cap removed | **SURVIVED** | KILLED by `test_hard_cap_prunes_at_once_even_when_the_last_prune_was_recent` |
| M33 | drop-when-busy removed | KILLED | |
| M34 | stale tmp kept for retention days | KILLED | |
| M35 | compact retention ignored | KILLED | |
| M36 | atomic rename replaced by direct write | **SURVIVED** | KILLED by `test_a_write_that_cannot_be_renamed_never_leaves_a_final_file` |
| M37 | full file written every poll | KILLED | |
| M38 | `submit` no longer swallows errors | KILLED | |
| M39 | watchdog archive age `>` to `<` | KILLED | |
| M40 | watchdog archive check not skipped when collector down | KILLED | |
| M41 | watchdog flap ignores fractional count | KILLED | |
| M42 | watchdog flap window 24 h to 24000 h | KILLED | |
| M43 | watchdog alert dedupe removed | KILLED | |
| M44 | watchdog RECOVERED removed | KILLED | |
| M45 | watchdog flap pair window 2 s to 0 s | KILLED | |

Of the six survivors, M05 and M20 were the important ones: without the first, a refactor could silently drop the restart-replay protection of issue 41; without the second, the equal-`last_seen` behaviour is unprotected. I ran each of the six against the tree with my tests and confirmed each now fails (the mutant run lists the killing test).

## 4. Replay and fault tests (numbers)

Harness: `tests/tools/release_review_faults.py` (real Collector, archive and store; fake feed server; throwaway Postgres; every container limited to 512 MB, 1 CPU, with a timeout).

| Scenario | Result |
|---|---|
| 04:08 pattern (fraction then whole second) | PASS: sessions 24 pilots, 4 controllers before and after; precision anomaly archived (+ `-prev`) |
| Future-dated snapshot then genuine ones | PASS: `ok, error, ok, skipped_unchanged`; the error poll archived as `poll_error` with the payload; polls recover |
| Clock 3 min slow / 3 min fast | slow: 3 of 3 polls `error`; fast: `ok` |
| Startup: empty DB; future row (+1 day) in `network_snapshot` | `last_ts=None` in both; next poll `ok` |
| Restart mid-stream | `ok` then `skipped_unchanged` (poll_log 1 + 1) |
| Archive directory read-only / blocked by a file / 20 KB tmpfs full | 20 of 20 polls `ok` each; archive errors 20 / 20 / 20; 0.83 s, 0.83 s, 1.65 s for the 20 polls |
| Slow disk (6 s per file) | max poll 144 ms, median 40 ms, max loop lag 10 ms, 19 of 20 archive jobs dropped: the poll loop is never delayed |
| SIGTERM during an archive write (real entrypoint, 6 s writes) | exit code 0; exit 11.2 s after the signal (waits for both in-flight writes, L4); no partial file left |
| 500 polls of a 1.53 MB feed (1,800 pilots, 24 stored per poll plus clones inside the polygon) | RSS 109 MB (poll 50) to 110 MB (poll 500); poll p50 197 ms, p99 384 ms, max 421 ms; loop lag max 184 ms; 153 archive files, 6.7 MB, 0 dropped, 0 errors |
| Same, 200 polls, archive on vs off | p50 186 vs 180 ms; loop lag max 245 vs 329 ms: the archive adds no measurable stall |
| Docker named volume, read-only root filesystem, new image | volume owned by uid 10001, write and rename OK, root filesystem write refused |

An incident during review: my first "full directory" run hung and used about 6.8 GB of the VM's memory. **Cause: a defect in my harness, not in the collector** (`payload(scale>1)` appended to the pilot list while iterating it, so it grew without end). The collector's archive code has no retry loop and no unbounded queue (one worker, at most one pending job, the next submission is dropped). The live collector missed 6 polls (a 90 s gap, the single gap over 45 s in the 12 h soak report) while memory was short; it recovered by itself. Fixed in the harness and all later runs were capped.

## 5. Risk assessment for the live deploy

Order on `docker compose up -d --build` (from the compose file): the migrate job re-applies grants (adds `SELECT` on `pilot_session`, `controller_session` to `watchdog_ro`; idempotent `REVOKE ALL` then `GRANT`); the `archive` volume is created on first mount (ownership from the image, M4); `collector` and `watchdog` are recreated (the collector loses about 45 s of polling, as in the deploy runbook; `load_last_ts` then makes the first poll after start a logged `skipped_unchanged` if the feed has not moved).

What could make the first poll fail:

1. **Clock check**: not at risk today (NTP synchronised; feed stamps always 8 to 100 s behind the poll). Risk only if NTP drifts (M3).
2. **Empty archive volume permissions**: fine with the rebuilt image (tested); only a stale image breaks it, and then polls still succeed (M4).
3. **New env defaults**: all 11 new variables have defaults in compose and settings; `.env` needs no change; the drift check passes. `ARCHIVE_ENABLED` defaults to true, so the archive starts on this deploy.
4. **Watchdog**: if the watchdog starts before the migrate job re-applies grants, `check_identity_flap` returns "identity-flap query failed" as WARN for a cycle; the 2-cycle confirmation (`WATCHDOG_CONFIRM_CYCLES`) and `depends_on: migrate` make a false alert unlikely.
5. **Resource**: the archive adds about 20 MB a day (0.4 GB steady state against a 1 GB cap) and no measurable CPU or latency (L8). Collector memory limit is 512 MB; measured RSS 110 MB with a 1.5 MB feed.

Rollback: `git checkout` the previous tag and `docker compose up -d --build` (or retag the old image); the archive volume can stay (the old collector ignores it) or be removed with `docker volume rm vatpac_archive`. No database migration is part of this release (`git diff main...collector-release` shows none), so the schema needs no rollback; the issue-41 upsert is SQL text only. The extra `watchdog_ro` grants are harmless to leave in place.

## 6. What I could not verify

- A real SIGKILL or power loss during a write on the VM's real disk (only the stale-tmp logic is tested).
- Behaviour with a genuinely hung volume (NFS-like); the local named volume was used.
- The `release.yml` publish path and the GitHub CI itself (no `gh` workflow run from here); the CI steps were mirrored by hand.
- Whether main f41494d's T36 migration (logon_time truncation and twin merge) has already run on the live database; this release does not include migrations, and live shows no fractional or twin sessions.
- The 5 dashboard warnings are pre-existing and were not investigated.

## 7. Cleanup (proof)

Removed from the VM: container `vp-rev-pg` (with databases `vatpac_test`, `vatpac2_test`, `vatpac_fault`, `vatpac_explain`), image `vp-rev-collector:test`, volumes `vp-rev-vol` and `vp-rev-vol2`, all `/tmp/vp-rev*` and `/tmp/rev_*` files. Counts after cleanup on the VM: `vp-rev` containers 0, images 0, volumes 0, `/tmp` entries 0. Removed on the PC: `/tmp/rev_*` and the scratchpad scripts. Live stack at 18:30 UTC: five services `Up (healthy)`, restart counts all 0, collector started 17:10:30 (before this review began), `sh ops/soak_report.sh 12` verdict PASS (coverage 99.44 %, 1 gap over 45 s: the 90 s gap caused by my runaway test container, no new gap). Container `vp-t30a-pg` of the test auditor was not touched (it was no longer present at the end).

## 8. Files added on the `release-review` branch

`tests/tools/release_review_faults.py` (fault harness), `tests/tools/release_review_mutants.py` (mutation runner), `tests/test_review_extra.py` (13 passing unit tests and 10 strict xfails that pin M1, M2, M5, L3), `tests/integration/test_review_extra_db.py` (54 DB tests), and this report. The strict xfails keep the suite green now and flip to failures, prompting removal of the marker, when each defect is fixed.

## 9. Status

FINISHED. Nothing outstanding: all five requested items were completed (suite, review, 45-mutant check, fault tests, risk assessment). Issues to file are in docs/RELEASE_REVIEW_FINDINGS.md.
