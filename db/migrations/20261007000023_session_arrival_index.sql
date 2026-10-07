-- migrate:up transaction:false
-- Expression index on the normalised arrival (see 20261007000022: one CONCURRENTLY index per file).
CREATE INDEX CONCURRENTLY IF NOT EXISTS pilot_session_arrival_norm_idx
    ON pilot_session ((NULLIF(upper(btrim(arrival, E' \t\r\n')), '')));

-- migrate:down
-- Forward-only project: no automatic rollback.
