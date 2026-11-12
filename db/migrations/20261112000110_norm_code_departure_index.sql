-- migrate:up transaction:false
-- Issue #207: the departure index again, now written with norm_code(). Built CONCURRENTLY, so the collector keeps writing to pilot_session
-- while it builds (a CONCURRENTLY build takes a lock that does not block inserts or updates; it scans the table twice). Outside a
-- transaction, hence its own file (dbmate sends one file as one command). The old index stays until migration 20261112000160 drops it, so
-- a query always has an index. No IF NOT EXISTS on purpose: if a build is interrupted it leaves an INVALID index, and IF NOT EXISTS
-- would then skip it silently on the next run; without it the next run stops with "already exists" and the invalid index must be dropped
-- (docs/RUNBOOKS/deploy-to-vm.md).
CREATE INDEX CONCURRENTLY pilot_session_departure_code_idx
    ON pilot_session ((norm_code(departure)));

-- migrate:down
-- Forward-only project: no automatic rollback.
