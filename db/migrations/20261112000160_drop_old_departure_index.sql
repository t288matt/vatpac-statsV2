-- migrate:up transaction:false
-- Issue #207: drop the departure index written with the long expression (migration 20261007000022); pilot_session_departure_code_idx
-- (migration 20261112000110) replaces it. DROP INDEX CONCURRENTLY does not block reads or writes, and cannot run inside a transaction
-- or with a second statement, hence one file per index.
DROP INDEX CONCURRENTLY IF EXISTS pilot_session_departure_norm_idx;

-- migrate:down
-- Forward-only project: no automatic rollback.
