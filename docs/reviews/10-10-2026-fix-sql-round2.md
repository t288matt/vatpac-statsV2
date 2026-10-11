# Review record: fix-sql round 2 (issue #98, partition drop), 10-10-2026

Reviewer: independent agent (not the author), commit tested origin/fix-sql b9f0170 (scope: commits since c8951f9). Verdict: APPROVE WITH FIXES (none blocking; small follow-ups listed below). Deploy is NOT done and needs the owner's explicit go. Migration 20261009000170 is not on origin/main (latest there is 160) and cannot be applied live, so editing it is allowed.

Note: the round 1 findings were not written down in the repo or in issue #98. They were inferred from the migration header and the tests (NOWAIT probe, no sleeping while the parent is locked, cap per call plus collector loop, re-checks before the drop, lock_timeout restore, retention-edge test, a hung drop must fail a test, scale tool fixes). Each is confirmed fixed below.

## Verified (clean git archive of b9f0170)
- grafana/validate_dashboards.py: OK (0 errors, 4 warnings); check_columns.py: bad 0; check_config_drift.py: OK; check_size_budget.py: 1749 of 1800, no module over 300 (store.py 293); ruff (ops and collector): clean; mypy src: no issues in 14 files.
- Unit suite (Git Bash): 778 passed, 547 deselected.
- Integration on a throwaway Postgres 16.15 (own cluster, r2review-pg, 512m, 1 CPU, capped test container, VM): test_partition_drop_locks.py 17 passed; with test_controller_retention.py 27 passed; whole tests/integration except test_poison_records.py and test_review_extra_db.py (they errored on a shapely/numpy import clash in my container, environmental): 482 passed, 4 skipped, 0 failed. One UTC pass only (no Sydney pass, not strict).
- Lock probe: inside one transaction the function holds ACCESS EXCLUSIVE on pilot_position only; pilot_session gets only AccessShare/RowShare (the partition drop does not lock the table the collector upserts into).
- Mutation runs (function patched in the throwaway DB, partition tests rerun):
  - NOWAIT removed: test_a_busy_partition_after_the_first_drop_is_skipped_without_waiting fails
  - late-position check removed: test_a_late_position_for_a_finalised_session_keeps_the_partition fails
  - lock_timeout not restored: test_lock_timeout_is_restored fails
  - cap raised: test_at_most_ten_per_call_and_the_collector_loop_reaches_the_same_end_state fails
  - always 3 attempts: the busy-after-first-drop test fails
  - 1 s lock timeout raised to 20 s: test_an_open_writer_transaction_on_the_parent_... fails
  - pinning removed: both parametrisations of test_same_partitions_dropped_pinned_and_finalised... fail
  - phase-1 finalise check removed: test_a_partition_with_an_unsummarisable_session_is_kept fails (only via its NOTICE text)
  - phase-2 reactivation check removed: the reactivation test fails (only via NOTICE text; the partition is still kept by the late-position check)
  - cutoff one day early: not caught by this file, caught by test_audit_t30.py::test_retention_drops_exactly_the_days_older_than_ninety
  - SURVIVED: the in-lock re-check removed; cutoff one day late (safe direction)

## Findings (ranked)
1. LOW. Migration, the VP001 re-check under the partition lock: no test fails if it is deleted (the phase 2 re-check covers the same ground and the window between them cannot be hit in a test). It also does not re-check late positions or new sessions (ids is frozen in phase 1). Real exposure is nil (old partitions receive no live writes) but it is untested defence.
2. LOW. The reactivation and unsummarisable tests detect the mutants only through NOTICE wording, and the reactivation test also inserts a late position, so it cannot show that a bare last_seen update alone keeps the partition. Add a case with last_seen updated and no new position.
3. LOW (documentation). docs/RUNBOOKS/db-full.md says the flight-summary advisory lock is held "while a partition is being finalised". pg_advisory_xact_lock (migration 20261007000020) is held to the end of the transaction, i.e. the whole call (up to 10 partitions, about 8 minutes by the runbook's own estimate), so the 5 minute refresh_flights (20 s statement timeout) fails for that whole time. It recovers next cycle; no data loss. The runbook also says "the reviewer's 2 busy and long reader cases", which reads oddly in a runbook.
4. LOW. Loop: a permanently kept partition (busy, or re-check failed) is re-finalised in every call while other partitions are still dropping, and the loop stops at the first call that drops 0 even when later eligible partitions exist beyond the first 10 candidates; they wait for the next night. No spin (bounded by MAX_DROP_CALLS 60 and dropped == 0). No unit test for the bound.
5. LOW. A reader part-way through locking partitions for a parent query can deadlock with a drop that holds the partition lock and waits for the parent; the deadlock detector or the 1 s lock timeout may pick the reader as victim. Window is microseconds.
6. INFO. test_controller_retention.py was changed (adds an exactly-90-days case) although unrelated to the fix. It is deterministic (Python-side time is truncated, so the row is older than the SQL cutoff) and harmless.

## Race and lock analysis
- NOWAIT on the partition first means a reader of the partition never makes the drop queue on the parent. The DROP then needs the parent for at most 1 s (queued writers wait at most that, under the collector's 5 s lock_timeout). If a collector poll transaction outlasts the three attempts the drop is deferred (NOTICE, next run).
- After the first successful drop there is no sleeping or waiting: confirmed by test and by the NOWAIT and attempts mutations.
- Finalise versus drop: ids are re-checked with no lock (phase 2), then under the partition lock; a session reactivated meanwhile or a late position keeps the partition (tested). Row locks from finalisation block a concurrent un-finalise.
- Partitions that must not be dropped: pinned by last_seen >= cutoff, the cutoff edge, open flights (recent last_seen, so pinned), non-final sessions (kept with a notice). Verified by mutation.
- lock_timeout restore is transaction-local; checked inside the caller's own transaction.
- One call is at most 10 partitions, inside the collector's 15 minute statement_timeout; a timeout would roll back that call's finalisation (documented).
- Migration re-run safety: CREATE OR REPLACE FUNCTION plus REVOKE, idempotent; signature and return type unchanged.

## Round 1 findings status
All inferred items are fixed in code and covered by a test that fails when reverted, except the in-lock re-check (finding 1), which is redundant and untestable.

## Not verified
Full CI (Sydney pass, strict mode, migrate image, roles); behaviour at live scale or against Grafana readers (the author's 230-partition rehearsal numbers in db-full.md were not reproduced); test_poison_records.py and test_review_extra_db.py; the Sydney pass of the partition tests. Other agents' containers shared the VM (load average up to 24), so the timing-based tests ran under load and still passed.
