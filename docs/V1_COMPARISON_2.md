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
