-- Soak report (SPEC section 12, checks 2 and 7). Run through ops/soak_report.sh, which sets :hours.
-- Evidence comes only from poll_log: one row per poll attempt.
WITH win AS (
    SELECT now() - make_interval(hours => :hours) AS since
), snaps AS (
    SELECT DISTINCT p.snapshot_ts
    FROM poll_log p, win
    WHERE p.status = 'ok' AND p.polled_at >= win.since AND p.snapshot_ts IS NOT NULL
), gaps AS (
    SELECT snapshot_ts, snapshot_ts - lag(snapshot_ts) OVER (ORDER BY snapshot_ts) AS gap FROM snaps
), span AS (
    SELECT min(snapshot_ts) AS first_ts, max(snapshot_ts) AS last_ts, count(*) AS stored FROM snaps
), att AS (
    SELECT count(*) FILTER (WHERE status = 'error') AS errors,
           count(*) FILTER (WHERE status = 'skipped_unchanged') AS skipped,
           count(*) AS attempts
    FROM poll_log p, win WHERE p.polled_at >= win.since
), calc AS (
    SELECT s.first_ts, s.last_ts, s.stored,
           extract(epoch FROM (s.last_ts - s.first_ts)) / 3600.0 AS covered_hours,
           floor(extract(epoch FROM (s.last_ts - s.first_ts)) / 30.0) + 1 AS expected,
           (SELECT count(*) FROM gaps WHERE gap > interval '75 seconds') AS gaps_over_75s,
           (SELECT max(gap) FROM gaps) AS longest_gap,
           (SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM gap)) FROM gaps) AS p95_gap_s
    FROM span s
)
SELECT to_char(c.first_ts, 'DD-MM-YYYY HH24:MI:SS') || ' UTC' AS first_snapshot,
       to_char(c.last_ts, 'DD-MM-YYYY HH24:MI:SS') || ' UTC' AS last_snapshot,
       round(c.covered_hours::numeric, 2) AS hours_covered,
       :hours AS hours_requested,
       c.stored AS snapshots_stored,
       c.expected::bigint AS snapshots_expected,
       round(100.0 * c.stored / NULLIF(c.expected, 0), 2) AS coverage_pct,
       c.gaps_over_75s,
       c.longest_gap,
       round(c.p95_gap_s::numeric, 1) AS p95_cadence_s,
       a.errors AS error_polls,
       a.skipped AS skipped_polls,
       CASE
           WHEN c.stored IS NULL OR c.stored = 0 THEN 'FAIL: no snapshots in the window'
           WHEN c.covered_hours < :hours * 0.98 THEN 'INCOMPLETE: only ' || round(c.covered_hours::numeric, 2) || ' h covered of ' || :hours || ' h requested'
           WHEN 100.0 * c.stored / NULLIF(c.expected, 0) < 99.0 THEN 'FAIL: coverage below 99%'
           WHEN c.p95_gap_s > 32 THEN 'FAIL: p95 cadence above 32 s'
           ELSE 'PASS'
       END AS verdict
FROM calc c, att a;
