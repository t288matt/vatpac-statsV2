# Owner steps for the #138 exit gate (comparisons A and B)

Revised 12-10-2026. Everything here is read only. No step writes to v3 or v1, no step touches `flights_archive` or `transceivers`, and no password is typed or printed (the database container reads its own environment). You run the commands; nothing is run for you. Files are in `docs/qa_89/`. Run every command in Git Bash (not PowerShell: the quoting differs). Run them from that folder on your PC; `python` needs only the standard library.

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

Replace `<TO_TS>` as in section 0. Prefer a restore of a live dump (`docs/RUNBOOKS/restore.md`) for `a_independent`, which scans `pilot_position`; on the live VM keep the window short or run it at a quiet hour. `a_differences` runs flight_part look-ups per difference: run A at a quiet hour with a statement timeout of 180 s (change `statement_timeout=120s` to `180s` in the command above; on 12-10-2026 it ran in under a minute at load 0.3). Sizes: `a_summary` is 4 rows, `a_duplicates` should be empty, `a_differences` should be a few lines per day.

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

### 2e. Stronger check against v1's raw positions (added 13-10-2026)

v1's summary table is an incomplete reference: on 13-10-2026 v1 held raw positions for about a dozen flights (11 checked by hand, 12 in the table below) that its summary job never summarised, although they were more than 30 hours old. So also compare against v1's raw table, rebuilt with v1's own rule (`compare_raw.py`). All commands are read only and run in Git Bash from a scratch folder.

1. Exports (6 hour chunks keep each v1 query under its 30 second limit; edit the dates for another window):
   - v1 raw rows: `SELECT cid, callsign, departure, arrival, last_updated FROM flights WHERE last_updated >= '<A>+00' AND last_updated < '<B>+00' AND COALESCE(departure,'')<>'' AND COALESCE(arrival,'')<>'' ORDER BY cid, callsign, last_updated;` with `SET default_transaction_read_only = on; SET statement_timeout='30s';` first, through `ssh vatpac 'docker exec -i vatsim_postgres sh -c "psql -X -q -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" --csv -f -"' < query.sql > v1raw_<n>.csv`. Never query `flights_archive` or `transceivers`. Index note (checked 13-10-2026): `flights` has no index on `last_updated` alone (only `(callsign, last_updated)`), so each 6 hour query scans the whole 1.0 M row table; every one finished inside the 30 second limit. This is the one place the rule "bounded by an indexed column" is not met; keep the chunks at 6 hours, run them one after another at a quiet moment, and stop if any hits the timeout.
   - v3 flights: `SELECT cid, callsign, departure, arrival, date_trunc('second', logon_time) AS logon_time, start_time, end_time, session_id, cardinality(session_ids) AS session_count, sample_count FROM flight_record WHERE end_time >= '<W0 - 2 h>' AND start_time < '<W1 + 2 h>' ORDER BY cid, callsign, start_time;` through `ssh vatpac-v3 'cd ~/vatpac-statsV2 && docker compose exec -T db sh -c "psql -X -q -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" --csv -f -"' < query.sql > b_v3_wide.csv`, with `SET default_transaction_read_only = on; SET statement_timeout='30s';` as the first line of query.sql. Do not filter by logon time or the power cut here: `compare_raw.py` applies the exclusions to both sides alike.
2. `python compare_raw.py` in that folder.

### 2f. What the numbers were on 13-10-2026 (so the second run before phase 5 is read correctly)

Window 08-10-2026 08:00 to 11-10-2026 03:00 UTC (both systems collecting; v1 was off 11-10 03:00 to 12-10 about 15:30), power cut 09-10 15:30 to 15:50 and 30 minutes at each edge excluded, every flight ended at least 30 hours before the run.

| comparison | result |
|---|---|
| flight_record against v1 summaries (v1 1008, v3 996) | 966 exact, 18 more match with a logon a few minutes apart (984 of 1008), 0 split, 0 merged, v1-only 24 (22 amended plans and 2 of the same kind), v3-only 12 |
| flight_record against v1 raw positions (v1 998 flights after exclusions, v3 977) | 974 matched one to one, 0 split, v1-only 24 (23 amended plans, 1 flight that started before the window), v3-only 3 (1 amendment mirror, 2 flights of 4 and 1 positions that v1 never recorded) |

The 12 v3-only against the summaries are all flights v1's raw table holds; v1 never wrote their summary. Expected differences that are not defects: a pilot who files a new plan inside one connection is ONE flight in v3 under the final plan (rule: no split inside a connection) but one v1 flight per plan; v3 keeps positions from before a plan was filed and v1 drops them at ingest, so v3 starts earlier; v1 polls about every 92 seconds and misses very short flights and short leading connections.
