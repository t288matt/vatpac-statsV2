-- migrate:up transaction:false
-- Issue #207: the arrival index again, now written with norm_code(). Same method and same reasons as migration 20261112000110.
CREATE INDEX CONCURRENTLY pilot_session_arrival_code_idx
    ON pilot_session ((norm_code(arrival)));

-- migrate:down
-- Forward-only project: no automatic rollback.
