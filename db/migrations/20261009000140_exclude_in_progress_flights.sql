-- migrate:up
-- Owner decision 09-10-2026 (issue #38, ADR-025, tracker T46): flights that are still connected are left out of the
-- flight counts. v1 only summarised a flight 8 hours after it ended, so unfinished flights were never counted there;
-- about 5% of flights (18 of 382) were in progress when counted. flight_ext is the base of every counting panel except
-- the ones that read `flight` directly (those carry NOT is_in_progress in their own SQL), so filtering here lets
-- movements and cohorts inherit it: an in-progress flight loses BOTH its departure and its arrival movement, as the
-- whole flight is out. Columns, order and types of flight_ext are unchanged (CREATE OR REPLACE). `flight` itself still
-- lists in-progress flights, and the live views (pilot_latest, pilot_online_now, online_now, network_history) are not
-- touched. If the collector is stopped, online_now is empty, nothing reads as in progress and every flight is counted.
CREATE OR REPLACE VIEW flight_ext AS
SELECT f.session_id, f.cid, f.callsign, f.name, f.home_icao, f.server,
       f.departure, f.arrival,
       f.departure || '-' || f.arrival AS route_label,
       upper(btrim(f.aircraft_icao)) AS aircraft_icao,
       f.flight_rules, f.remarks,
       f.start_time, f.end_time,
       extract(epoch FROM (f.end_time - f.start_time)) / 60.0 AS duration_minutes,
       f.airborne_minutes, f.max_gs_kt, f.max_alt_ft,
       f.is_circuit, f.is_zzzz,
       icao_is_home(f.departure) AS dep_is_home,
       icao_is_home(f.arrival) AS arr_is_home,
       icao_is_home(f.home_icao) AS pilot_is_local,
       icao_region(f.home_icao) AS pilot_region,
       (f.cid / 100000)::int AS cid_group,
       CASE WHEN f.callsign ~ '^[A-Z]{3}[0-9]' THEN left(f.callsign, 3) END AS airline_prefix,
       a.name AS airline_name,
       t.category AS aircraft_category,
       COALESCE(t.is_military, false) AS is_military,
       (f.remarks ~* 'SEL/') AS has_selcal,
       (f.remarks IS NOT NULL AND btrim(f.remarks) <> '') AS has_remarks
FROM flight f
LEFT JOIN airline a
       ON a.prefix = CASE WHEN f.callsign ~ '^[A-Z]{3}[0-9]' THEN left(f.callsign, 3) END
LEFT JOIN aircraft_type t
       ON t.icao = upper(btrim(f.aircraft_icao))
WHERE NOT f.is_in_progress;
COMMENT ON VIEW flight_ext IS
    'Completed flights only: flights still connected (flight.is_in_progress) are excluded, as v1 never counted an unfinished flight (ADR-025). Wide view over flight with the derived fields the charts need.';
COMMENT ON VIEW movements IS
    'Departures (at flight start) and arrivals (at flight end) at home-region airports, of completed flights only: a flight still connected contributes neither (ADR-025). Counts every flight-gate session, including sessions that never became airborne (airborne_minutes = 0).';
COMMENT ON VIEW cohorts IS
    'Joining cohorts (groups of 100,000 CIDs) over completed flights only (ADR-025).';

-- migrate:down
-- Back to the unfiltered view; the column list is identical.
CREATE OR REPLACE VIEW flight_ext AS
SELECT f.session_id, f.cid, f.callsign, f.name, f.home_icao, f.server,
       f.departure, f.arrival,
       f.departure || '-' || f.arrival AS route_label,
       upper(btrim(f.aircraft_icao)) AS aircraft_icao,
       f.flight_rules, f.remarks,
       f.start_time, f.end_time,
       extract(epoch FROM (f.end_time - f.start_time)) / 60.0 AS duration_minutes,
       f.airborne_minutes, f.max_gs_kt, f.max_alt_ft,
       f.is_circuit, f.is_zzzz,
       icao_is_home(f.departure) AS dep_is_home,
       icao_is_home(f.arrival) AS arr_is_home,
       icao_is_home(f.home_icao) AS pilot_is_local,
       icao_region(f.home_icao) AS pilot_region,
       (f.cid / 100000)::int AS cid_group,
       CASE WHEN f.callsign ~ '^[A-Z]{3}[0-9]' THEN left(f.callsign, 3) END AS airline_prefix,
       a.name AS airline_name,
       t.category AS aircraft_category,
       COALESCE(t.is_military, false) AS is_military,
       (f.remarks ~* 'SEL/') AS has_selcal,
       (f.remarks IS NOT NULL AND btrim(f.remarks) <> '') AS has_remarks
FROM flight f
LEFT JOIN airline a
       ON a.prefix = CASE WHEN f.callsign ~ '^[A-Z]{3}[0-9]' THEN left(f.callsign, 3) END
LEFT JOIN aircraft_type t
       ON t.icao = upper(btrim(f.aircraft_icao));
