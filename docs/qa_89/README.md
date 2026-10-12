# Owner steps for the #138 exit gate (comparisons A and B)

Revised 12-10-2026. Everything here is read only. No step writes to v3 or v1, no step touches `flights_archive` or `transceivers`, and no password is typed or printed (the database container reads its own environment). You run the commands; nothing is run for you. Files are in `docs/qa_89/`. Run them from that folder on your PC; `python` needs only the standard library.

Check the tooling first: `python run_comparisons.py selftest` must print `selftest: OK`.

## 0. Choosing the window (both comparisons)

- Write `REF` = the newest snapshot, read only:
  `ssh vatpac-v3 "cd ~/vatpac-statsV2 && docker compose exec -T db sh -c 'psql -X -At -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -c \"SELECT max(observed_at) FROM network_snapshot\"'"`
- `TO_TS` = `REF` minus 9 hours (8 h quiet period plus a margin). Flights ending after it may not be written yet, so comparing them proves nothing. This also answers the live run of 12-10-2026: under the old per-pilot rule 141 pilots were held back; compare only flights that are already written, or wait until the quiet period has passed.
- `FROM_TS` = go-live `2026-10-07 21:38+00` for A; for B the design window starts `2026-10-08 07:52+00` (v1 was polling from then).

## 1. Comparison A: old `flight` view against `flight_record`

One command per query. It sends a linted SELECT through ssh into the database container, in a read-only session with a statement timeout, and saves a CSV:

```bash
Q=a_differences   # then a_summary, a_independent, a_duplicates
python run_comparisons.py extract --file comparison_a.sql --name $Q > $Q.sql
ssh vatpac-v3 "cd ~/vatpac-statsV2 && docker compose exec -T db sh -c 'PGOPTIONS=\"-c default_transaction_read_only=on -c statement_timeout=120s\" psql -X -q --csv -v ON_ERROR_STOP=1 -v from_ts=\"2026-10-07 21:38+00\" -v to_ts=\"<TO_TS>\" -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -f -'" < $Q.sql > $Q.csv
```

Replace `<TO_TS>` as in section 0. Prefer a restore of a live dump (`docs/RUNBOOKS/restore.md`) for `a_independent`, which scans `pilot_position`; on the live VM keep the window short or run it at a quiet hour. Sizes: `a_summary` is 5 rows, `a_duplicates` should be empty, `a_differences` should be a few lines per day.

Reading the output (`a_differences.csv`): one row per difference, `reason` explains it:

| reason | meaning | acceptable? |
|---|---|---|
| `E1` 8-hour parts | old view cut one connection into parts, new has one row | yes (about 9 in 5.5 days) |
| `E2` 120-minute silence | old cut a connection at a silence | yes |
| `E3` grouping | join decided by `logon_time`, not first position | yes, expect none |
| `E4` plan fields | frozen at write; route latest, deptime maximum | yes |
| `E5` airborne rounding | at most 0.01 per extra old piece | yes |
| `E6` returning session | same session, changed plan | yes |
| `E7` stale old pieces | old sample count below stored positions | yes |
| `UNEXPECTED` | anything else | NO: stop and report on #138 |

Pass = no `UNEXPECTED` row, `a_independent`: both numbers equal, `a_duplicates` empty. Two rule changes of 12-10-2026 do not alter A: the join rule is v1's and the quiet rule only decides WHEN a flight is written, so compare only pilots with no unprocessed session (the SQL scope already does this).

## 2. Comparison B: `flight_record` against v1 (the mandatory independent check)

### 2a. v3 export (plain SELECT, same method as the live run)

```bash
python run_comparisons.py extract --file comparison_b.sql --name b_v3 > b_v3.sql
ssh vatpac-v3 "cd ~/vatpac-statsV2 && docker compose exec -T db sh -c 'PGOPTIONS=\"-c default_transaction_read_only=on -c statement_timeout=30s\" psql -X -q --csv -v ON_ERROR_STOP=1 -v from_ts=\"2026-10-08 07:52+00\" -v to_ts=\"<TO_TS>\" -v gap_from=\"2026-10-09 15:30+00\" -v gap_to=\"2026-10-09 15:50+00\" -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -f -'" < b_v3.sql > b_v3.csv
```

### 2b. v1 export (read only, see `docs/V1_COMPARISON.md` section 1)

The v1 box is the ssh alias `vatpac` and its database container is `vatsim_postgres` (v1 stays up for reference; read-only rules in docs/V1_COMPARISON.md section 1). Use v1's own container environment so no credential is printed:

```bash
python run_comparisons.py extract --file comparison_b.sql --name b_v1 > b_v1.sql
ssh vatpac "docker exec -i vatsim_postgres sh -c 'PGOPTIONS=\"-c default_transaction_read_only=on -c statement_timeout=30s\" psql -X -q --csv -v ON_ERROR_STOP=1 -v from_ts=\"2026-10-08 07:52+00\" -v to_ts=\"<TO_TS>\" -v gap_from=\"2026-10-09 15:30+00\" -v gap_to=\"2026-10-09 15:50+00\" -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -f -'" < b_v1.sql > b_v1.csv
```

The query reads only `flight_summaries`, bounded by `logon_time` and `completion_time`. If v1's variable names differ, use its own; never copy a password out. The query assumes the columns `cid, callsign, departure, arrival, logon_time, completion_time, id`; if v1 names differ, edit your local copy of `b_v1.sql` (a plain SELECT) and keep the output column names `cid, callsign, departure, arrival, logon_time, completion_time`. Any CSV with those headers works (extra columns, header case and fractional seconds are tolerated; a v3 export without `start_time` falls back to `logon_time`).

### 2c. Match

```bash
python run_comparisons.py b --v3-csv b_v3.csv --v1-csv b_v1.csv --cutoff "<TO_TS>"
# optional: --exclude-cids held_back.txt   (one CID per line, pilots the old rule still held back)
# optional: --strict                        (also compare with the design's counts 864 / 887 / 867 / 23 / 3)
# optional: --known-v3-only list.csv        (cid,callsign,departure,arrival,logon_time of flights you already judged)
```

### 2d. Reading the output

| line | meaning | expected |
|---|---|---|
| exact 1-to-1 matches | same pilot, callsign, airports, whole-second logon | nearly all (design window: 864 of 887) |
| measurement differences | v3 logon earlier than v1 logon but the flights overlap: v1 polls about every 92 s and can miss a short leading session | a few; not a defect |
| v1 flights split by v3 / v3 merging v1 rows | the join rule differs from v1 | 0 / 0, else a bug |
| v1-only, amended plan | v1 kept the earlier plan as its own summary | all of the v1-only rows (design window: 23) |
| v1-only, UNEXPLAINED | v1 has a flight and v3 has nothing for that pilot and callsign | 0, else a bug (or v3 not yet written: check `TO_TS`) |
| v3-only | listed in full for you to judge; the design lists 3 for its window but not which | judge each: pre-plan time, v1 missed it entirely, or a defect |

`RESULT: PASS` only means no split, no merge and no unexplained v1-only flight (and, with `--strict`, the design counts). The v3-only list always needs your eyes; save it as `list.csv` for later runs.

Report both results as a comment on #138.
