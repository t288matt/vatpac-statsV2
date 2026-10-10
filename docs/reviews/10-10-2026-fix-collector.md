# Review record: fix-collector (issues #82, #85, #87, #88, #92, #99, #100)

Reviewed 10-10-2026 by an independent reviewer agent (not the author), using the code-review skill and a manual read, in two rounds. Owner approval to merge: 10-10-2026 (chat). Deploy: NOT done; needs the owner's yes (medium tier). A deploy restarts polling: a gap of about 45 seconds, normal image rebuild, no migration, rollback by restarting the previous image.

| Round | Commit reviewed | Verdict | Main findings |
|---|---|---|---|
| 1 | c5defc7 | APPROVE WITH FIXES BEFORE MERGE | #92 made the integrity invariant 'ok poll_log counts differ from network_snapshot' fire after any rejected pilot (reproduced); #82 treated a non-list pilots/controllers as empty (a changed feed shape would store empty snapshots as ok with no alert); a non-object flight_plan was invisible; AttributeError in the except tuple would hide future bugs. #88 was not fixed (size budget). |
| 2 | c5011b6 | APPROVE WITH FIXES | All round 1 findings fixed; #88 implemented and reviewed in full (serialised apply with a deadline). One CI blocker (a 141-character comment, E501 in ops/check_size_budget.py), already fixed on main in 646149b. Accepted trade-offs: a snapshot with about 800 or more poisoned records now fails at the 20 s deadline instead of being stored late; a pool wait up to 10 s lies outside the deadline. |

Evidence from the author, verified in part by the reviewer:
- Unit tests 725 passed. Integration at aabb333: 495 passed in the UTC pass and 495 in the Australia/Sydney pass, strict, zero skipped (the two lint-only commits after that were not re-run through the integration suite). The reviewer ran 70 integration tests itself (test_apply_deadline, test_poison_records, test_integrity_checks, test_ingest, test_hardening_ingest): 70 passed, 1 skipped, 0 failed.
- Replay: 283 snapshots (today's saved raw feed plus 4 fixture variants) through Collector.poll_once on two throwaway databases, origin/main code against the branch: per-table content hashes identical for pilot_session (493 rows), pilot_position (8993), controller_session (31), rating events (31), frequency events (33), network_snapshot (253), poll_log (283); per-poll stats, /metrics and /health identical apart from a time-based archive-age field. Differences in the data: 0.
- Fuzz (reviewer): 4000 mutated payloads, old against new parse_snapshot: new code raised 0 times except the intended FeedError (228, all for a non-list pilots/controllers); where the old code did not raise, results were identical after the falsy-flight_plan fix.
- Reverting each fix makes its test fail (mutation checks by the author and the reviewer).
- Existing tests: none deleted, skipped or loosened; none needed changing.

Intended behaviour changes: network_snapshot.pilots_in_region excludes rejected pilots (the invariant now compares coalesce(pilots_kept, pilots_inside)); a present non-list pilots/controllers raises a visible FeedError; a truthy non-object flight_plan keeps the pilot without a plan and is logged, and above 20 percent of pilots becomes an error; /health age limits scale with POLL_INTERVAL_SECONDS (60 s and 120 s at 30 s, unchanged); one apply at a time with a 20 s deadline; file_sha256 removed; reference files read once.

Not verified before merge: the live deploy and restart gap; a rare feed shape outside the two days of Australian-daytime data used for the replay; the effect of the extra set_config calls under real load (estimated about 1 ms each); the combination with fix-ops in one integration run (CI on main is that run).
