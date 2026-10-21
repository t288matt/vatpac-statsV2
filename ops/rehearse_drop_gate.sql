-- Issue #89 phase 4 (#140): rehearsal of the raw-position drop gate on a RESTORED COPY of a live dump. The gate is deployed (migrations 20261013000210 and 20261013000300 are applied); this script is only for a scratch copy and is never run on the live database.
-- Steps for the owner: docs/RUNBOOKS/drop-gate-rehearsal.md. Usage, against the scratch database only:
--   psql -X -v ON_ERROR_STOP=1 -v retention=2 -v today=2026-10-13 [-v as_role=collector] -f ops/rehearse_drop_gate.sql -d vatpac_gate_test
--   as_role: run the drop calls as that role (the collector's), which needs GRANT EXECUTE on the drop function in the scratch copy (the runbook gives the line).
-- What it does, in this order:
--   A. predicts, for every daily partition, whether the gate should drop it (due, not pinned, nothing unreconciled) and says why not;
--   B. runs the drop function the way the collector does (ONE partition per call, one call more than needed, each its own transaction). THIS DROPS PARTITIONS OF THE COPY;
--   C. checks the outcome against the prediction: exactly the predicted partitions are gone, no written flight changed, and the
--      positions left are exactly the positions before minus those of the dropped partitions. Last line: REHEARSAL RESULT: OK or FAIL.
-- It refuses to run unless the database name ends in _test, so it cannot be pointed at the live database by mistake.
\set ON_ERROR_STOP on
SELECT current_database() ~ '_test$' AS name_ok \gset
\if :name_ok
\else
  \echo 'REHEARSAL REFUSED: the database name must end in _test (a restored scratch copy). Nothing was done.'
  \quit
\endif
\if :{?retention}
\else
  \echo 'REHEARSAL REFUSED: pass -v retention=N (days) and -v today=YYYY-MM-DD. Nothing was done.'
  \quit
\endif
\if :{?today}
\else
  \echo 'REHEARSAL REFUSED: pass -v retention=N (days) and -v today=YYYY-MM-DD. Nothing was done.'
  \quit
\endif

\echo '== A. prediction (read-only) =='
CREATE TEMP TABLE gate_plan AS
SELECT c.relname::text AS part, t.d AS p_start, t.d + interval '24 hours' AS p_end
  FROM pg_inherits i
  JOIN pg_class c ON c.oid = i.inhrelid
 CROSS JOIN LATERAL (SELECT to_date(right(c.relname, 8), 'YYYYMMDD')::timestamp AT TIME ZONE 'UTC' AS d) t
 WHERE i.inhparent = 'public.pilot_position'::regclass AND c.relname ~ '^pilot_position_[0-9]{8}$';
ALTER TABLE gate_plan ADD COLUMN due boolean, ADD COLUMN pinned boolean, ADD COLUMN unreconciled bigint,
                      ADD COLUMN positions bigint, ADD COLUMN independent_uncovered bigint, ADD COLUMN expect_drop boolean;
UPDATE gate_plan g SET
    due    = g.p_end <= (:'today'::date::timestamp AT TIME ZONE 'UTC') - make_interval(hours => :retention * 24),
    positions = (SELECT count(*) FROM pilot_position p WHERE p.observed_at >= g.p_start AND p.observed_at < g.p_end);
UPDATE gate_plan g SET
    pinned = EXISTS (SELECT 1 FROM pilot_position p JOIN pilot_session s ON s.id = p.session_id
                      WHERE p.observed_at >= g.p_start AND p.observed_at < g.p_end
                        AND s.last_seen >= (:'today'::date::timestamp AT TIME ZONE 'UTC') - make_interval(hours => :retention * 24))
 WHERE g.due;
UPDATE gate_plan g SET unreconciled = (SELECT count(*) FROM unreconciled_partition_sessions(g.p_start, g.p_end))
 WHERE g.due AND NOT g.pinned;
-- An INDEPENDENT count, written differently from unreconciled_partition_sessions(): per position, no aggregate, no range join. A position
-- counts as uncovered when its session was not processed up to it, or the session has a flight plan and no written flight of that
-- session has the position inside its time range. The gate must agree: if it says "reconciled" while this count is not 0, that is a FAIL.
UPDATE gate_plan g SET independent_uncovered = (
    SELECT count(*) FROM pilot_position p LEFT JOIN pilot_session s ON s.id = p.session_id
     WHERE p.observed_at >= g.p_start AND p.observed_at < g.p_end
       AND (s.id IS NULL OR s.flown_upto IS NULL OR p.observed_at > s.flown_upto
            OR (NULLIF(upper(btrim(s.departure, E' \t\r\n')), '') IS NOT NULL AND NULLIF(upper(btrim(s.arrival, E' \t\r\n')), '') IS NOT NULL
                AND NOT EXISTS (SELECT 1 FROM flight_summary f
                                 WHERE p.session_id = ANY (f.session_ids) AND p.observed_at BETWEEN f.start_time AND f.end_time))))
 WHERE g.due AND NOT g.pinned;
UPDATE gate_plan g SET expect_drop = COALESCE(g.due AND NOT g.pinned AND g.unreconciled = 0 AND g.independent_uncovered = 0, false);

SELECT count(*) AS partitions, count(*) FILTER (WHERE due) AS due, count(*) FILTER (WHERE due AND pinned) AS pinned,
       count(*) FILTER (WHERE due AND NOT pinned AND unreconciled > 0) AS refused_by_gate,
       count(*) FILTER (WHERE due AND NOT pinned AND unreconciled = 0 AND independent_uncovered > 0) AS gate_too_lax,
       count(*) FILTER (WHERE expect_drop) AS expected_to_drop,
       coalesce(sum(positions) FILTER (WHERE expect_drop), 0) AS positions_expected_gone
  FROM gate_plan;
\echo 'Partitions that are due but the gate should keep (reason per session):'
SELECT g.part, u.reason, count(*) AS sessions, min(u.session_id) AS example_session
  FROM gate_plan g CROSS JOIN LATERAL unreconciled_partition_sessions(g.p_start, g.p_end) u
 WHERE g.due AND NOT g.pinned AND g.unreconciled > 0
 GROUP BY g.part, u.reason ORDER BY g.part, u.reason;

CREATE TEMP TABLE gate_before AS
SELECT (SELECT count(*) FROM flight_summary) AS flights,
       (SELECT coalesce(sum(sample_count), 0) FROM flight_summary) AS samples,
       (SELECT count(*) FROM pilot_position) AS positions,
       (SELECT coalesce(sum(positions) FILTER (WHERE expect_drop), 0) FROM gate_plan) AS positions_expected_gone;

\echo '== B. the drop loop, as the collector runs it (each call its own transaction; THIS DROPS PARTITIONS OF THE COPY) =='
CREATE TEMP TABLE gate_calls (dropped int);
GRANT ALL ON gate_calls TO PUBLIC;
SELECT least(700, count(*) FILTER (WHERE expect_drop)::int + 1) AS n_calls FROM gate_plan \gset
\if :{?as_role}
  \echo 'running the drop calls as role' :as_role '(as the collector does)'
  SET ROLE :"as_role";
\endif
SELECT format('INSERT INTO gate_calls SELECT drop_old_pilot_position_partitions(%s, %L::date)', :retention, :'today')
  FROM generate_series(1, :n_calls) \gexec
RESET ROLE;
SELECT count(*) AS calls, coalesce(sum(dropped), 0) AS partitions_dropped FROM gate_calls;

\echo '== C. outcome against prediction =='
CREATE TEMP TABLE gate_after AS
SELECT c.relname::text AS part FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
 WHERE i.inhparent = 'public.pilot_position'::regclass;
SELECT (SELECT count(*) FROM gate_plan g WHERE g.expect_drop AND EXISTS (SELECT 1 FROM gate_after a WHERE a.part = g.part)) AS predicted_gone_but_still_there,
       (SELECT count(*) FROM gate_plan g WHERE NOT g.expect_drop AND NOT EXISTS (SELECT 1 FROM gate_after a WHERE a.part = g.part)) AS dropped_but_not_predicted,
       (SELECT count(*) FROM flight_summary) - b.flights AS flights_changed,
       (SELECT coalesce(sum(sample_count), 0) FROM flight_summary) - b.samples AS samples_changed,
       (SELECT count(*) FROM pilot_position) - (b.positions - b.positions_expected_gone) AS positions_unaccounted,
       (SELECT count(*) FROM gate_plan g WHERE g.due AND NOT g.pinned AND g.unreconciled = 0 AND g.independent_uncovered > 0) AS gate_too_lax
  FROM gate_before b \gset
SELECT :predicted_gone_but_still_there AS predicted_gone_but_still_there, :dropped_but_not_predicted AS dropped_but_not_predicted,
       :flights_changed AS flights_changed, :samples_changed AS samples_changed, :positions_unaccounted AS positions_unaccounted, :gate_too_lax AS gate_too_lax;
SELECT (:predicted_gone_but_still_there = 0 AND :dropped_but_not_predicted = 0 AND :flights_changed = 0 AND :samples_changed = 0
        AND :positions_unaccounted = 0 AND :gate_too_lax = 0) AS all_ok \gset
\if :all_ok
  \echo 'REHEARSAL RESULT: OK (the gate dropped exactly what it should; no written flight changed)'
\else
  \echo 'REHEARSAL RESULT: FAIL (read the numbers above; do not deploy the gate)'
\endif
