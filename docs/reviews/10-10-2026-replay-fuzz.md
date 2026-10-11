# Review record: replay-fuzz (issue 103), tip 5cd51b1, 10-10-2026

Reviewer: independent agent (not the author). Notes committed progressively; final verdict at the end.

## Verified (clean git archive of origin/replay-fuzz)
- validate_dashboards OK (0 errors), check_columns bad 0, check_config_drift OK, check_size_budget 1742 lines, ruff (ops and collector) clean, mypy src clean.
- Unit: `PYTHONPATH=collector/src python -m pytest -q -m "not integration" tests collector/tests`: 848 passed in 32 s (PC, Python 3.11.9). Without the two new unit files: 778 passed in 13 s.
- Integration on VM throwaway Postgres 16.15 (rvw103-pg, 512m/512m/1 CPU), strict mode, Australia/Sydney session pass, whole tests/integration: 545 passed, 4 skipped (no psql binary, no grafana_ro DSN: my environment, not the branch), 2 xfailed, 314 s. New integration files alone, UTC: 22 passed, 2 xfailed in 26 s.

## Cost
- tests/test_fuzz_parser.py: 19.4 s, of which one grid test 18.7 s (60 percent of the whole unit job's runtime). Integration additions about 18 s per pass (fuzz grid 9.7 s, CLI tests 6 s), two passes in CI.

## Mutation results (new tests only: test_fuzz_parser, test_replay_tool, test_replay_real_feed, test_fuzz_ingest)
Killed: no-op malformed count; bisect re-raises; no-position pilot outside; lat/lon swapped in inside_polygon; logon fraction kept (pilot and controller); OverflowError not caught; non-object entries not skipped; rejected count dropped for DB-refused rows; pilots_with_plan or; gs/alt swapped (incidentally, by CHECK constraint); whitelist ignored; clamp off; text cap off; polygon test skipped; _write narrowed to IntegrityError; load_polygon x/y flipped (killed only because the import fails); the fix for #111 (add ProgramLimitExceeded to the except) makes the strict xfail fail as designed.
SURVIVED: callsign `.strip()` removed in model._callsign; heading stored as 0; altitude stored +1; polygon shifted 0.3 degrees in load_polygon.

## New fault cases (scratch tests on a throwaway DB, not committed)
- FAIL, real defect: a whitelisted controller with `frequency` "inf", "Infinity" or "-Infinity" makes `model.parse_frequency_hz` raise OverflowError (only InvalidOperation and ValueError are caught; `int(Decimal('Infinity'))`), raised from `store.apply_snapshot` (store.py:225) before any write: the whole snapshot is lost. The branch's fuzz does not catch it: the parser grid never calls apply, and the ingest grid samples 160 of thousands of cases (tests/tools/fuzz_payloads.py sample_cases), so `frequency=inf` on a listed controller is not drawn.
- Pass: "1e30", "1e999", "NaN", huge digit string, "0x10", Arabic-Indic digits for frequency; empty feed (0 pilots, 0 controllers) stores a zero network_snapshot; logon_time in six formats (Z, fraction, +10:00 offset, naive) gives ONE session; same cid as pilot and controller; NaN/-inf/-99999/1e9/361 in altitude, groundspeed, heading.
- Observation: padded callsign " X " collapses into "X" (one session); lower case and zero-width-space variants are distinct sessions (acceptable). A snapshot dated 2019 raises CheckViolation (no partition): a poll error, by design.

## Xfail and strict mode
Strict, raises=ProgramLimitExceeded; with the #111 fix applied (ProgramLimitExceeded added to the except in store._write) both xfails fail as XPASS(strict), so the pin releases only for that reason. Strict mode with a genuine `pytest.skip()` still turns the run red (listed by name); a `pytest.skip()` inside an xfail-marked test is also still listed. Residual risk: the filter ignores every xfail report, including `xfail(run=False)` and non-strict xfails, which would hide not-run tests.

## Replay tool
Refuses a non-_test database before any write ("refusing to empty database 'postgres'", exit 2, shown); the check precedes the partition drops and TRUNCATE; it needs only the DSN it is given (live DB never needed). Fixtures: no IPs, emails, tokens; only public VATSIM fields (names, CIDs, ATIS text with public URLs), real shape (full and compact). Weakness: no fixture has a logon_time without a fractional part, or a pilot without a position.

## Docs (run-tests.md section 6)
Commands work as typed (replay run and diff shown). Inaccuracies: section 1 says the unit run shows "xfailed", but the two xfails are integration tests; `git archive ... | tar -x -C /tmp/old` needs mkdir first; "Last reviewed: 09-10-2026" not updated. The no-test-counts test passes.

## Findings, ranked
1. (real defect, new) model.py:85 OverflowError on frequency "inf"/"Infinity": snapshot lost. Fuzz gap: sample_cases samples, apply never run on the full grid. Recommend: file an issue, add frequency to the named ingest cases, and make the ingest grid cover every key for one listed controller and one inside pilot.
2. Value-level correctness is untested by the new tests: they check keys and counts only (replay.py check, test_fuzz_ingest._check_snapshot). Mutations heading=0 and altitude+1 survive. The existing suite may cover these; the new tests do not.
3. No fixture or fuzz case for a padded callsign; removing `.strip()` in `_callsign` survives. Oracle `_key` strips, so a case would be cheap.
4. Oracle shares `ref.polygon` and the whitelist with the code under test (a shifted polygon in load_polygon survives); lat/lon rule and "no position is inside" are copied from filters. Independent in logic of model.py, not in reference data.
5. Cost: one parser grid test is 18.7 s (CI unit job grows by about 2.4x in test time, 13 s to 32 s). Trim to 1 index per section in the unit grid, or keep the full grid in a nightly/marked run.
6. Loose bounds: test_many_bad_entries_at_once (`malformed <= 12`, `len(kept) >= len(survivors)`) and `res.rejected_values >= snap.malformed`; they still caught every mutation tried.
7. Docs inaccuracies above; conftest ignores all xfail reports.

## Verdict: APPROVE WITH FIXES
Merge is safe (tests and tooling only, no product code changed, all checks green, 13 of 17 product mutations killed). Fix before or soon after: findings 1 (file issue, add case), 2 and 3 (add a few assertions/cases), 5 (trim), 7 (docs).

## Re-check of tip 387c58a (diff 5cd51b1..387c58a), 10-10-2026
- No product code changed (only tests/, docs/RUNBOOKS/run-tests.md).
- Quick unit suite (`-m "not integration and not slow_fuzz"`, Git Bash, clean archive): 851 passed in 20 s (was 848 in 32 s).
- Scratch parser-level mutations: padded-callsign strip removed killed by test_a_padded_callsign_collapses_to_the_plain_one; heading forced to 0 and altitude +1 killed by test_the_independent_values_agree_with_the_parser_on_real_data.
- conftest narrowing correct: only a strict xfail that runs is exempt. Strict mode with a scratch file: a genuine skip, an `xfail(run=False)` and a skip inside a strict xfail all turn the run red and are listed; a strict xfail that raises as pinned is not listed.
- Store-level value checks (replay.value_problems, exact bounds, slow_fuzz ingest grid) need Postgres: read in the diff, NOT run (no VM container, as instructed). CI after merge runs them; I see no safety reason against that.
- Remaining nit: tests/conftest.py registers the slow_fuzz marker without type hints or blank-line spacing before the function (style only; ruff is not run on tests).
Verdict: APPROVE.
