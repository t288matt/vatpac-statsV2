-- migrate:up
-- Comment only (issue #32). The comment set by 20261007000024 said "SPEC decision pending"; 20261009000140 already
-- removed that phrase, but the comment still did not state the decided behaviour. ADR-018 / issue #74: every
-- flight-gate session counts, including parked sessions with no airborne time and circuits, as in v1. ADR-025:
-- flights still connected are excluded (movements reads flight_ext). The view definition is unchanged.
COMMENT ON VIEW movements IS
    'Departures (at start_time) and arrivals (at end_time) at home-region airports, ZZZZ excluded. Counts every completed flight-gate session, as v1 does (ADR-018, issue 74): sessions that never became airborne (airborne_minutes = 0, parked connections) and circuits count as one departure and one arrival. A flight still connected contributes neither, because the view reads flight_ext (ADR-025).';

-- migrate:down
COMMENT ON VIEW movements IS
    'Departures (at flight start) and arrivals (at flight end) at home-region airports, of completed flights only: a flight still connected contributes neither (ADR-025). Counts every flight-gate session, including sessions that never became airborne (airborne_minutes = 0).';
