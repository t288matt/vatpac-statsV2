# T14 ATC card re-check (08-10-2026)

> Working note from 08-10-2026, kept as a record of the T14 re-check. Statements such as "not run" or "owner question open" describe that moment; the current position is in [../TRACKER.md](../TRACKER.md) and [../CHART_DEFINITIONS.md](../CHART_DEFINITIONS.md), which win where they differ.

Input: the current v1 Metabase SQL for nine ATC cards (owner's live Metabase; cards last updated 30-09-2025 to 18-10-2025). Dashboard owned: `grafana/dashboards/atc/atc-overview.json`. Tests: `tests/integration/test_atc_t14.py` (written, NOT run: no throwaway `_test` Postgres on this PC).

| Card | Name | Repo said | v1 says now | Class | Action |
|---|---|---|---|---|---|
| 41 | % of flights with at least one ATC interaction | DROPPED: needs frequency matching (ADR-002) | Monthly share of flights with `controller_time_percentage > 0`, read from v1's `flight_summaries` | STAYS-DROPPED | None. `controller_time_percentage` is the output of v1's frequency/ATC-interaction matching, which ADR-002 and SPEC section 13 exclude. v3 has no such column. |
| 43 | % of time pilots spent on an ATC frequency | DROPPED: needs frequency matching | Minutes-weighted monthly average of `controller_time_percentage` | STAYS-DROPPED | None. Same reason as card 41. |
| 63 | CTR/FSS controllers active (30 days) | KEPT: weekly, rolling 30 days to the end of each week, session over 60 min | Monthly: for each calendar month, distinct CTR/FSS controllers with a session over 60 min that started in the 30 days before the month end; months with no controllers hidden | CHANGED | Panel 7 rewritten as monthly (UTC), title "CTR and FSS controllers active (30 days to month end, monthly)". Zero months drawn (v1 hides them). Status in CHART_DEFINITIONS should move KEPT to CHANGED. |
| 94 | When are people online on weekdays | CHANGED (v3 uses average concurrent users from snapshots, Mon-Fri, 90 days) | Title and window now "last 30 days" (was 90); counts distinct flight callsigns and ATC callsigns touching each UTC hour; ATC sessions of 20 min or more; flights with enroute time over 0; day filter is Sun, Mon, Sat (comment: "where your data is") | CHANGED (v1 side), no panel edit by me | Panel lives in `pilots/pilots-now.json`, which I do not own. See owner questions 1 and 2. |
| 95 | What time are pilots/ATC online weekend | As card 94, weekends, 90 days | 30-day window; ATC sessions of 30 min or more; Sat and Sun | CHANGED (v1 side), no panel edit by me | As card 94. |
| 98 | % of time airborne pilots spent on a frequency with ATC | DROPPED: needs frequency matching | Minutes-weighted monthly average of `airborne_controller_time_percentage` | STAYS-DROPPED | None. Same reason as card 41. |
| 108 | Top 50 service providers (30 days) | CHANGED: hours online only; aircraft handled dropped (needs frequency matching) | Still `5 x hours + SUM(total_aircraft_handled)`, ordered by that value; sessions over 0 min, 30 days, grouped by CID, limit 50 | SAME | None. Hours, the 30-day window, the >0 filter and the limit match. The aircraft-handled column stays out (ADR-002), so the ranking is by hours, not by v1's combined score. |
| 116 | How many controllers logged each month (was: last 3 months) | KEPT as "Controllers logged on (last 3 months)": one bar chart over 3 months | Month by month, pivoted S1, S2, S3, C1, C3, I1+: distinct controllers per calendar month whose sessions at that rating total over 60 min; grouped by (CID, rating), so a rating change inside a month is judged per rating | CHANGED | Panel 6 replaced by a monthly timeseries "Controllers logged on each month, by rating group" (same id and position). Panel 5 (card 115, 30 days) untouched. |
| 137 | Controllers active in the previous 30 days | KEPT: weekly, session over 60 min | Monthly (same shape as card 63), any position, session over 30 min (was 60) | CHANGED | Panel 8 rewritten as monthly, minimum 30 min, title "Controllers active (30 days to month end, monthly)". |

## What changed in the dashboard
- Panels 6, 7, 8: SQL, titles, descriptions rewritten; `timeFrom` set to 12M; all read `controller_session_ranked` (existing view), UTC explicit; no new migration.
- Months run from the first to the last month holding any session (as v1), clipped to the dashboard range. The latest month is partial. Because there is no backfill, panels start at go-live.
- v1 quirk kept: a session counts for the month it started in. For cards 63 and 137 the window is the last 30 days of the month, so on a 31-day month the first day of the month is in no window (v1 behaviour, not corrected).
- Difference from v1: months with zero controllers are drawn as 0 (v1 `WHERE ... > 0` omits them). Merged sessions (reconnect within 5 minutes) as already documented for `controller_summaries`.
- v1 filtered CTR/FSS with `LIKE '%CTR' OR '%FSS'`; v3 uses `is_ctr_fss` (callsign ends `_CTR` or `_FSS`). Identical for whitelisted callsigns.

## Needs owner input
1. Card 94: the v1 SQL filters weekdays to Sun, Mon, Sat with the comment "where your data is". That looks like a workaround for gaps in v1's data, not an intended definition. Should v3 keep Monday to Friday (UTC), as the title says?
2. Cards 94 and 95: v1 now uses a 30-day window (v3 panel uses 90 days) and counts distinct callsigns active in each hour, with minimum ATC session lengths of 20 minutes (94) and 30 minutes (95). The v3 panel averages concurrent users from snapshots. Keep the snapshot average and only change the window to 30 days, or rebuild to the v1 counting method? (The panels are in `pilots/pilots-now.json`; the coordinator or its owner needs to apply the answer.)
3. Cards 41, 43, 98 stay dropped. If you want an "ATC present" substitute that needs no frequency matching (for example: a flight overlapping any controller session in the home region), that is a new definition and would need a new ADR; say so if wanted.

## Coordinator follow-ups (files I was told not to edit)
- `docs/CHART_DEFINITIONS.md`: cards 63, 116, 137 become CHANGED with the monthly definitions above and the new panel titles; card 108 stays CHANGED as written; cards 41, 43, 98 stay DROPPED (the "needs frequency matching" reason still holds; v1 now reads precomputed percentage columns that come from that matching); the summary counts shift by 3 from KEPT to CHANGED.
- Test column for cards 63/137 ("weekly value <=") now reads: monthly value of 63 <= monthly value of 137 (covered by `test_ctr_fss_never_exceeds_all_positions`).
