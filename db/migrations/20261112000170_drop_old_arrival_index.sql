-- migrate:up transaction:false
-- Issue #207: drop the arrival index written with the long expression (migration 20261007000023); pilot_session_arrival_code_idx
-- (migration 20261112000120) replaces it. Same method as migration 20261112000160.
DROP INDEX CONCURRENTLY IF EXISTS pilot_session_arrival_norm_idx;

-- migrate:down
-- Forward-only project: no automatic rollback.
