-- migrate:up
-- Card 156 (airborne aircraft during the Monday event) needs raw altitude samples, but dashboards may read
-- views only (grafana_ro has no base-table access). This is a plain column-subset view over pilot_position,
-- with no filter and no aggregation, so time predicates on observed_at still prune the daily partitions.
-- It carries no sector or frequency logic (ADR-002). Retention is that of pilot_position (90 days).
CREATE OR REPLACE VIEW pilot_sample AS
SELECT observed_at, session_id, alt_ft, gs_kt
FROM pilot_position;

-- migrate:down
-- Forward-only project: no automatic rollback.
