-- migrate:up transaction:false
-- Expression index on the normalised departure the flight view exposes (departure board and airport
-- charts filter on it). Built CONCURRENTLY, hence outside a transaction. dbmate sends a file as one
-- command, so each CONCURRENTLY index needs its own migration file.
CREATE INDEX CONCURRENTLY IF NOT EXISTS pilot_session_departure_norm_idx
    ON pilot_session ((NULLIF(upper(btrim(departure, E' \t\r\n')), '')));

-- migrate:down
-- Forward-only project: no automatic rollback.
