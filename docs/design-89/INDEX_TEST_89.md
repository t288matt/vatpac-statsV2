# Would plain indexes fix #89? Test result (11-10-2026)

Question from the owner: could indexes stop the `flight` view growing with history, making the stored `flight_merged` table (docs/DESIGN_89.md) unnecessary? Local test only, nothing run on the live VM.

## Answer

**No.** Indexes do not get 5 years under 3 s and do not flatten the growth. The best case (indexes plus the planner forced to use them) is about 20 percent faster and still grows in a straight line. The stored table is the only candidate tested that flattens it.

## Method

- Same generator and sizes as `docs/FLIGHT_VIEW_SCALING.md` (`tests/scale/gen_synth.sql`): 1 year (185,018 merged flights, identical counts to the earlier test), 3 years (555,000 generated flights) and 5 years (978,357 merged flights). Migrations applied, timed as `grafana_ro`, work_mem 16 MB, parallel workers off, statement timeout lifted to 180 s so slow panels finish instead of being cancelled, best of two runs, `EXPLAIN (ANALYZE, BUFFERS)`.
- Docker Desktop would not start on this PC, so the throwaway database was a private PostgreSQL 17.5 cluster created with `initdb` in a temp folder on port 55432 (not the PC's own Postgres service), removed afterwards. It is not capped to 1 CPU / 1 GB like the earlier container; this machine is about 2 times faster, so absolute numbers are lower than in FLIGHT_VIEW_SCALING.md. Compare the columns here with each other, not with the old document.
- The four slowest panels: pilots-traffic busiest and quietest home-region airports, most popular domestic and international routes.
- Candidate indexes, all created together: `flight_summary (cid, start_time, session_id, gap_segment, span_part)`; covering `flight_summary (session_id, gap_segment, span_part) INCLUDE (...)`; `pilot_session (cid, callsign, dep_n, arr_n, id)` on the view's normalising expressions, partial on the view's NOT NULL filter; covering `pilot_session (id) INCLUDE (...)`. Total size 107 MB at 1 year, 337 MB at 3, 562 MB at 5.

## Results (ms)

| Panel | 1 y | 3 y | 5 y |
|---|---|---|---|
| Busiest airports, no index | 5,235 | 17,026 | 29,659 |
| Busiest airports, indexes (planner free) | 5,286 | 17,047 | 30,518 |
| Busiest airports, indexes (forced) | 4,634 | 14,784 | 24,515 |
| Quietest airports, no index | 5,240 | 17,076 | 28,339 |
| Quietest airports, indexes (planner free) | 5,278 | 17,088 | 30,043 |
| Quietest airports, indexes (forced) | 4,609 | 14,779 | 24,242 |
| Domestic routes, no index | 3,368 | 10,741 | 19,110 |
| Domestic routes, indexes (planner free) | 3,337 | 10,712 | 18,880 |
| Domestic routes, indexes (forced) | 2,727 | 8,981 | 15,492 |
| International routes, no index | 2,978 | 9,552 | 17,052 |
| International routes, indexes (planner free) | 2,944 | 9,593 | 16,631 |
| International routes, indexes (forced) | 2,395 | 7,628 | 13,380 |
| `SELECT count(*) FROM flight`, no index | 873 | 3,032 | 5,427 |
| same, indexes | 888 | 3,017 | 5,568 |
| **Stored-table floor** (`flight_mat`, 267 MB, 5 y only) | | | 229 to 296 |

"Forced" = `enable_hashjoin`, `enable_mergejoin`, `enable_seqscan` and `enable_sort` off for the role, the most index-friendly setting the planner allows.

Slope per year of history (busiest airports): no index about 6 s a year; best indexed case about 5 s a year. The routes panels grow about 3.5 s a year unindexed and 2.8 s forced. The stored table is 0.3 s at 5 years, against 29.7 s unindexed.

## Why indexes cannot work here

1. The planner ignored every index on its own (timings equal to the baseline), because the plan is a hash join of two full tables, a sort, three window passes and a hash aggregate, all linear in history.
2. Forcing index use removed none of the real cost. The sort key is (cid, callsign, departure, arrival, start_time, ...): the first four come from `pilot_session` (as expressions) and `start_time` from `flight_summary`, so no single btree can deliver that order across a join and the Sort stays. The saving came only from the join and scan stage.
3. The window functions and the ordered `array_agg` calls still read every row of history. A panel's time filter cannot be pushed below the running-sum chain id (as already found in candidate 2 of FLIGHT_VIEW_SCALING.md).
4. Indexes cost 562 MB at 5 years (more than the 267 MB stored table), plus write cost on the 5-minute refresh, for about a 20 percent gain.

## Conclusion

Keep the DESIGN_89 recommendation (stored `flight_merged`). On the slower VM-like box the 5-year indexed figures would be roughly double those above, so well over 3 s either way.
