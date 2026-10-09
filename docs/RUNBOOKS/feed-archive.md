# Runbook: the raw-feed archive

Last reviewed: 08-10-2026, against `collector/src/vatpac/archive.py`. Times are UTC. Added for GitHub issue 20 (T16) after the T30 audit: the 04:08 event behind issue 15 could not be replayed because no feed payload had been kept.

## What is kept

The collector writes a rolling archive to the named volume `archive` (mounted at `/archive`, owned by the collector's non-root user, `ARCHIVE_DIR`). Every file is gzipped JSON, written once to a temporary name and renamed into place. Nothing is ever appended to, so the disk sees a few sequential writes per hour. The archive is written after the snapshot is stored, by a single background thread; if it fails the poll is not affected (the failure is counted and logged once).

| Folder | Content | When | Kept | Size |
|---|---|---|---|---|
| `compact/YYYYMMDD/HHMMSS.json.gz` | Every pilot (cid, callsign, logon_time, latitude, longitude, groundspeed, altitude) and controller (cid, callsign, logon_time, frequency, rating, facility) and the feed timestamp, in the feed's own key names | The first good snapshot of every 5th minute (`ARCHIVE_COMPACT_EVERY_S`) | 30 days (`ARCHIVE_COMPACT_KEEP_DAYS`) | about 42 KB (audit measurement on a live payload) |
| `full/YYYYMMDD/HHMMSS.json.gz` | The whole payload exactly as received | The first good snapshot of every hour (`ARCHIVE_FULL_EVERY_S`) | 7 days (`ARCHIVE_FULL_KEEP_DAYS`) | about 310 KB |
| `anomaly/YYYYMMDD/HHMMSS-<reasons>.json.gz` and `...-prev.json.gz` | The payload that triggered the anomaly and the good one before it (for a poll error with no body: a small JSON note) | At most one file per reason every 5 minutes | 90 days (`ARCHIVE_ANOMALY_KEEP_DAYS`) | about 310 KB each |

The file name is the feed's own `update_timestamp` (the time of the snapshot, not of the write). The compact record holds every pilot and controller worldwide, not only those inside the region, on purpose: it lets you check the region and whitelist filters themselves. Names and CIDs are public (ADR-019); the volume is private to the VM and is not exposed anywhere.

Anomaly reasons (joined with `+` when several apply):

| Reason | Trigger |
|---|---|
| `poll_error` | The poll ended in `error` (HTTP failure, bad JSON, empty feed, too many malformed entries, database timeout) |
| `gap` | The feed timestamp moved forward by more than `ARCHIVE_GAP_S` (35 s). A single missed tick (30 s) is normal (audit F10) and is not kept |
| `ts_older` | The feed timestamp is older than the newest one seen |
| `ts_future` | The feed timestamp is more than `FEED_MAX_FUTURE_SECONDS` (120 s) ahead of the VM clock. The poll itself is an error, so the file is named `...-poll_error+ts_future` (issue 66) |
| `precision` | The share of `logon_time` values that have a fractional second dropped by 20 points or more between two polls (at least 50 values). This is the signature of issue 15 / ADR-021 |

Limits: a hard cap `ARCHIVE_MAX_MB` (1024) deletes the oldest compact and full files first and touches anomaly files only when nothing else is left (issue 65). Files past their retention are removed every 10 minutes (twice the compact interval). Stale `.tmp` files from a crash are removed after 10 minutes.

Expected writes at the defaults: 12 compact files and 1 full file per hour, about 0.8 MB an hour and about 20 MB a day, plus about 0.6 MB per anomaly. Steady-state size is about 0.4 GB (30 days of compact files, 7 days of full ones), far below the cap.

## Where to see it

- `docker compose exec collector python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/health').read().decode())"` shows the `archive` block: `enabled`, `size_bytes`, `files_written`, `errors`, `dropped`, `anomalies`, `last_write`, `last_write_age_seconds`.
- `/metrics` has `vatpac_archive_size_bytes`, `vatpac_archive_last_write_age_seconds` and the counters `vatpac_archive_{files_written,errors,dropped,anomalies}_total`.
- The watchdog warns (`feed_archive`, see [alerts.md](alerts.md)) when the archive is enabled and nothing was written for twice the compact interval (10 minutes).
- A new anomaly is logged by the collector: `archive anomaly kept`, with the reasons.

## Read a file

The archive volume is not mounted on the host. List it and read a file from a short-lived container:

```sh
docker run --rm -v vatpac_archive:/a:ro alpine find /a -type f -newermt '-1 day' | sort | tail
docker run --rm -v vatpac_archive:/a:ro alpine sh -c 'zcat /a/anomaly/20261008/040827-gap.json.gz' | head -c 600
docker run --rm -v vatpac_archive:/a:ro alpine sh -c 'zcat /a/compact/20261008/040500.json.gz' | python3 -m json.tool | head -40
```

(The compose project is called `vatpac`, so the volume is `vatpac_archive`; check with `docker volume ls`.) To copy a file out for analysis: `docker run --rm -v vatpac_archive:/a:ro -v "$PWD":/out alpine cp /a/anomaly/20261008/040827-gap.json.gz /out/`.

## Replay one through the collector's own parser, offline

No database and no network are needed. From the repository root, with the collector requirements installed (or inside a container built from the collector image):

```sh
PYTHONPATH=collector/src python - <<'PY'
import gzip, json
from vatpac.filters import load_reference, inside_polygon
from vatpac.model import parse_snapshot

payload = json.loads(gzip.open("040827-gap.json.gz").read())      # a full, anomaly or compact file
snap = parse_snapshot(payload)                                    # the exact parser the collector uses
ref = load_reference("config/australian_airspace_polygon.json", "config/controller_callsigns_list.txt")
inside = [p for p in snap.pilots if inside_polygon(ref.polygon, p.latitude, p.longitude)]
kept = [c for c in snap.controllers if c.callsign in ref.whitelist]
print(snap.observed_at, "pilots", snap.pilots_global, "in region", len(inside), "controllers kept", len(kept),
      "malformed", snap.malformed)
PY
```

To replay a sequence into a scratch database, apply `store.apply_snapshot` to each parsed snapshot in time order on a throwaway database (see `tests/integration/helpers.py` for the call and [run-tests.md](run-tests.md), section 3, for a throwaway Postgres). Never replay into the live database. For the precision question, compare the `logon_time` strings of one `cid` in a `-prev` file and its trigger file; `archive.fraction_share(raw_bytes)` returns the count and the fraction share of any payload.

## Settings and switching it off

All settings are in `.env` (see [../CONFIG_MAP.md](../CONFIG_MAP.md)): `ARCHIVE_ENABLED`, `ARCHIVE_DIR`, `ARCHIVE_COMPACT_EVERY_S`, `ARCHIVE_COMPACT_KEEP_DAYS`, `ARCHIVE_FULL_EVERY_S`, `ARCHIVE_FULL_KEEP_DAYS`, `ARCHIVE_ANOMALY_KEEP_DAYS`, `ARCHIVE_GAP_S`, `ARCHIVE_MAX_MB`. Changing one needs `docker compose up -d collector` (about 45 s of polling lost, see [deploy-to-vm.md](deploy-to-vm.md)). `ARCHIVE_ENABLED=false` switches it off and the watchdog stops checking it. To free the space: `docker compose stop collector && docker volume rm vatpac_archive && docker compose up -d collector` (this deletes the whole archive).

## If it misbehaves

- `errors` above 0 and `last_write_age_seconds` growing: the volume is full or unwritable. `df -h /` on the VM, `docker compose logs collector | grep "archive failed"` for the first error. Polling is unaffected.
- `dropped` above 0: a write took longer than the poll interval and one snapshot was skipped by the archive (harmless; a slow disk). Error polls are the exception: while the worker is busy up to 3 of them wait in a queue and are written afterwards, so a poll_error anomaly is only dropped when that queue is full (issue 67). After a failed write (disk full) the archive also pauses for 60 seconds, counting each skipped poll as dropped, and the partial temporary file is removed at once (issue 68).
- The size sits at the cap: lower the retention days or raise `ARCHIVE_MAX_MB`; many `poll_error` or `gap` files point at a feed problem, see [feed-outage.md](feed-outage.md).
- Disk wear (issue 14): the archive adds about 13 file writes an hour (about 0.8 MB). It writes each file once with an atomic rename and never appends.
