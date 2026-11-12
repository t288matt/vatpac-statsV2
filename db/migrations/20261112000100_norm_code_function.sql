-- migrate:up
-- Issue #207, OWNER DECISION 12-11-2026: one rule for normalising an ICAO text code, written once. The strong rule: strip spaces, tabs,
-- carriage returns and line feeds from both ends, upper-case, and read an empty result as NULL. Before this the rule was copied about 20
-- times in two spellings (the strong one in the flight writer, the drop gate and two indexes; a spaces-only one, with '' instead of NULL,
-- in pilot_latest, flight_ext, home_prefix, icao_is_home and icao_region). Migration 20261112000150 points every view and function at it;
-- the scripts, tests and dashboard queries call it too.
--
-- IMMUTABLE and PARALLEL SAFE, so it can sit in an index. It is a plain SQL function with one expression, so the planner inlines it: a
-- query on norm_code(departure) and an index on norm_code(departure) both become the same expression and the index is used.
-- Not STRICT on purpose: a STRICT SQL function is inlined only when the planner can prove the body is strict, and nothing is gained.
-- Execute rights: left with PUBLIC, like icao_is_home, icao_region and rank_group_of (it only reads its argument), so grafana_ro,
-- collector and watchdog_ro (through the ops_* views) can all call it, and this migration needs no role to exist yet.
-- WARNING: an index does not notice when a function body changes. Never change this body without rebuilding every index on it.
CREATE OR REPLACE FUNCTION norm_code(x text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT NULLIF(upper(btrim(x, E' \t\r\n')), '')
$$;
COMMENT ON FUNCTION norm_code(text) IS
    'The one rule for an ICAO text code (airport, aircraft type, prefix): strip spaces, tabs, CR and LF from both ends, upper-case, empty becomes NULL. Used by every view, function, index and script that normalises such a code (issue 207). Never change the body without rebuilding the indexes built on it.';

-- migrate:down
-- Forward-only project: no automatic rollback (the copies this replaces are in the migrations before this one).
