# V1 against v3, second comparison (11-10-2026)

Written 11-10-2026 about 10:30 UTC, BEFORE any figure for this window was pulled (predictions first). Method, rules for touching v1 (read-only) and expected differences E1 to E13 are unchanged from V1_COMPARISON.md.

## Window
**10-10-2026 02:00 to 08:00 UTC (6 whole hours).** Chosen from per-hour poll counts of both systems (v1 `flights.last_updated` distinct values, v3 `poll_log` ok rows): from 09-10-2026 00:00 to 11-10-2026 02:59 UTC every hour has 36 to 40 v1 polls and a full v3 set (239 or 240 per hour at 15 s until 09-10 11:xx, 120 per hour at 30 s after the 09-10-2026 change; 15:00 on 10-10 had 111 because of the maintenance and apply timeouts, outside the window). It is a busy Australian night/morning (Saturday), more than 24 h old so v1 summaries are complete (E3). v3 polled every 30 s here, v1 about every 92 s.

## Predictions
1. Mean pilots online per poll: v3 (plan-gated) within 2 % of v1 for every hour; v3 all-pilots up to about 3 % higher (E2).
2. Distinct pilot CIDs per hour: v3 equal or up to about 5 % higher than v1 (E1, E2); whole-window distinct CIDs within 5 %.
3. Controllers: identical distinct callsigns and CIDs per hour; controller-hours within 3 % (v3 lower, because v1 double counts overlapping reconnect rows, section 6.4 of the first comparison).
4. Controller sessions: v1 summaries at or above v3 merged (v1 duplicates), by a handful.
5. Completed flights per hour: v3 at or above v1; v1 missing none that v3 holds with 3 or more samples (section 12 of the first comparison).
6. A v1 session last row sits 10 to 30 s after v3's last sighting (v1 stamp lag).
7. v3 at 30 s cadence should detect short controller and pilot visits better than at 15 s only in v1's direction: v1 (92 s) can only miss visits, never add them.
Anything outside these ranges is unexplained and becomes an issue.

## Results (queries run 11-10-2026 about 10:25 to 10:45 UTC, SELECT only, read-only session, 30 s timeout, bounded by time and id; about 11,000 v1 rows)
"v3 plan" = pilots whose session has a departure and arrival (what v1 stores); "v3 all" = every in-region pilot. Means are rows per poll (v1, about 40 polls an hour) or per snapshot (v3, 120 an hour).

| Hour (UTC, 10-10) | v1 mean | v3 plan mean | v3 all mean | v1 CIDs | v3 plan CIDs | v3 all CIDs | v1 flights ended | v3 flights ended |
|---|---|---|---|---|---|---|---|---|
| 02 | 42.6 | 42.9 | 44.2 | 66 | 66 | 69 | 20 | 20 |
| 03 | 51.4 | 51.9 | 52.4 | 75 | 75 | 77 | 26 | 26 |
| 04 | 46.6 | 46.7 | 46.8 | 73 | 74 | 75 | 32 | 32 |
| 05 | 47.8 | 48.0 | 48.8 | 76 | 76 | 77 | 27 | 27 |
| 06 | 48.9 | 50.0 | 52.1 | 75 | 76 | 80 | 25 | 24 |
| 07 | 54.8 | 55.5 | 57.8 | 77 | 77 | 81 | 23 | 23 |
| **Sum / total** | **292.0** | **295.0 (+1.0 %)** | **302.1 (+3.5 %)** | **442** | **444 (+0.5 %)** | **459 (+3.8 %)** | **153** | **152** |

Whole window distinct pilot CIDs: v1 179, v3 plan 179, v3 all 183 (+2.2 %). Controllers: distinct CIDs per hour 6, 6, 5, 7, 7, 4 and distinct callsigns 5, 6, 5, 6, 6, 4 in BOTH systems, hour by hour. Controller sessions (by start): v1 17, v3 merged 17; hours online v1 24.9, v3 25.2 (+1.2 %); 16 distinct (CID, callsign) in both.

## Scorecard
1. Pilot means: v3 plan within 2 % in 5 of 6 hours (hour 06 is +2.1 %); v3 all is 0.4 % to 6.6 % higher, over my 3 % guess in hours 02, 06 and 07. The extra is the pilots v1 never stores (no plan yet, or none filed): explained, E2. Partly missed.
2. Distinct CIDs: confirmed (plan +0.5 %, all +3.8 %, window equal on plan).
3. Controllers identical per hour: confirmed. Hours online: v3 is 1.2 % HIGHER, not lower as predicted. No overlapping v1 duplicate rows in this window; the difference is E1: v3 sees each session start and end about 40 s sooner and later than v1 (17 sessions x about 80 s is 0.4 h against the 0.3 h seen). Prediction wrong, explained.
4. Sessions: tied at 17 (prediction "v1 at or above" holds).
5. Flights: v1 has ONE more than v3 (hour 06), not fewer. It is SIA7449 (CID 1622916): one v3 flight YSSY to WSSS (a single session 05:00 to 10:59 UTC, 721 samples) against v1 raw rows for one logon, and a v1 summary for KLAX to YSSY completing 06:54 UTC, an earlier plan that the pilot amended mid-session. The v1 summary key includes departure and arrival, so an amended plan makes an extra v1 summary: E5, a v1 duplicate, not a missing v3 flight. Prediction missed E5; no v3 defect.
6. v1 stamp lag of 10 to 30 s: not tested in this run.
7. Not testable on its own (no v1 only observation found at the totals level).

## Conclusion
No unexplained difference. Every gap sits in E1, E2 or E5. Nothing for v3 to fix. With the poll interval now 30 s in v3 and about 92 s in v1 the pilot and controller sets still agree hour by hour. Note that no later window can be compared yet: VATSIM's feed has been frozen since 11-10-2026 03:08 UTC, so neither system has stored new data since (v1 shows no polls for 03:00 to 08:59 UTC; the Proxmox crash restarted both VMs at about 08:49 UTC).
