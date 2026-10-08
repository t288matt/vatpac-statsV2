"""Mutation check of the NEW rules in collector-release (release review, 08-10-2026). Never committed to a tree under test.

Copies the repository to a scratch directory, applies one deliberate break at a time, runs the relevant tests and prints
KILLED (some test failed) or SURVIVED. Run inside the collector image:
  python tests/tools/release_review_mutants.py /work /tmp/mut [name-substring]
Needs VATPAC_TEST_DSN for the integration subset.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

SRC, SCRATCH = Path(sys.argv[1]), Path(sys.argv[2])
ONLY = sys.argv[3] if len(sys.argv) > 3 else ""
V = "collector/src/vatpac/"
W = "ops/watchdog/watchdog.py"
UNIT = ["collector/tests", "tests/test_watchdog_checks.py", "tests/test_review_extra.py"]
INTEG = ["tests/integration/test_hardening_ingest.py", "tests/integration/test_upserts_and_events.py",
         "tests/integration/test_watchdog_flap.py", "tests/integration/test_review_extra_db.py"]
NEWER = '_NEWER = "EXCLUDED.last_seen >= ps.last_seen"'
BOUND = "if ts - datetime.now(UTC) > timedelta(seconds=bound):"

# (name, file, old, new)
M: list[tuple[str, str, str, str]] = [
    ("M01 future bound comparison flipped", V + "main.py", BOUND, "if ts - datetime.now(UTC) < timedelta(seconds=bound):"),
    ("M02 future bound check removed", V + "main.py", BOUND, "if False:"),
    ("M03 future bound default 120 s -> 100000 s", V + "settings.py", "feed_max_future_seconds: int = Field(120,", "feed_max_future_seconds: int = Field(100000,"),
    ("M04 future bound ignores the setting (fixed 3600 s)", V + "main.py", "bound = update_timestamp(payload), self.settings.feed_max_future_seconds", "bound = update_timestamp(payload), 3600"),
    ("M05 startup load_last_ts call removed", V + "main.py", "        await collector.load_last_ts()\n", ""),
    ("M06 startup query counts future rows", V + "main.py", " WHERE observed_at <= now() + make_interval(secs => %s)\"", "\" ; _ = %s\""),
    ("M07 _SUP no longer excluded from near-miss", V + "filters.py", 'callsign.endswith(("_OBS", "_SUP"))', 'callsign.endswith(("_OBS",))'),
    ("M08 _OBS no longer excluded from near-miss", V + "filters.py", 'callsign.endswith(("_OBS", "_SUP"))', 'callsign.endswith(("_SUP",))'),
    ("M09 text cap boundary > -> >=", V + "model.py", "len(v) > limit", "len(v) >= limit"),
    ("M10 text cap keeps one extra character", V + "model.py", "v[:limit]", "v[:limit + 1]"),
    ("M11 text cut not counted as rejected", V + "model.py", "rec.rejected + len(cut)", "rec.rejected"),
    ("M12 parse_snapshot ignores max_text argument", V + "model.py", "cap_text(build(raw), max_text)", "cap_text(build(raw), MAX_TEXT)"),
    ("M13 main does not pass TEXT_MAX_LENGTH", V + "main.py", "parse_snapshot(payload, self.settings.text_max_length)", "parse_snapshot(payload)"),
    ("M14 unparsable latitude swallowed again (issue 37)", V + "model.py", "    return None if value is None else float(value)\n", "    try:\n        return None if value is None else float(value)\n    except (TypeError, ValueError):\n        return None\n"),
    ("M15 nulled values not counted", V + "model.py", "return sum(v is None and raw.get(k) is not None for k, v in parsed.items())", "return 0"),
    ("M16 malformed entries not counted as rejected", V + "store.py", "rejected_values=snapshot.malformed)", "rejected_values=0)"),
    ("M17 pilot parse rejections not added", V + "store.py", "res.rejected_values += rejected + p.rejected", "res.rejected_values += rejected"),
    ("M18 controller parse rejections not added", V + "store.py", "            res.rejected_values += c.rejected\n", ""),
    ("M19 upsert guard >= -> <= (older overwrites newer)", V + "store.py", NEWER, '_NEWER = "EXCLUDED.last_seen <= ps.last_seen"'),
    ("M20 upsert guard >= -> > (equal last_seen)", V + "store.py", NEWER, '_NEWER = "EXCLUDED.last_seen > ps.last_seen"'),
    ("M21 plan_changed_at not guarded by newer", V + "store.py", "WHEN {_NEWER} AND ((EXCLUDED.departure", "WHEN ((EXCLUDED.departure"),
    ("M22 CASE guard removed from every plan field", V + "store.py", '_NEW = {c: f"CASE WHEN {_NEWER} THEN COALESCE(EXCLUDED.{c}, ps.{c}) ELSE ps.{c} END" for c in _FIELDS}', '_NEW = {c: f"COALESCE(EXCLUDED.{c}, ps.{c})" for c in _FIELDS}'),
    ("M23 remarks dropped from the updated fields", V + "store.py", '"route", "remarks", "planned_alt"', '"route", "planned_alt"'),
    ("M24 archive future bound 120 s -> huge", V + "archive.py", "FUTURE_S, ANOMALY_REPEAT_S, STALE_TMP_S = 120.0,", "FUTURE_S, ANOMALY_REPEAT_S, STALE_TMP_S = 1e12,"),
    ("M25 archive older snapshot not flagged", V + "archive.py", "if gap < 0:", "if gap < -1e12:"),
    ("M26 archive gap never flagged", V + "archive.py", "if gap > self.s.archive_gap_s:", "if gap > 1e12:"),
    ("M27 precision drop threshold 0.2 -> 0.99", V + "archive.py", "PRECISION_DROP, PRECISION_MIN_VALUES = 0.2,", "PRECISION_DROP, PRECISION_MIN_VALUES = 0.99,"),
    ("M28 anomaly 5-minute throttle removed", V + "archive.py", "< ANOMALY_REPEAT_S:", "< -1:"),
    ("M29 poll_error trigger removed", V + "archive.py", 'out = ["poll_error"] if status == "error" else []', "out = []"),
    ("M30 prune deletes newest first", V + "archive.py", "for _, size, path in sorted(files):", "for _, size, path in sorted(files, reverse=True):"),
    ("M31 hard cap ineffective", V + "archive.py", "if total <= cap:", "if total <= cap * 1000:"),
    ("M32 size trigger for the hard cap removed", V + "archive.py", " or self.size > s.archive_max_mb * 1_048_576:", ":"),
    ("M33 drop-when-busy removed", V + "archive.py", "            if self._busy:\n                self.dropped += 1\n                return\n", ""),
    ("M34 stale tmp files kept for the retention days", V + "archive.py", 'limit = STALE_TMP_S if path.name.startswith(".") else days * 86400', "limit = days * 86400"),
    ("M35 compact retention ignored", V + "archive.py", '"compact": self.s.archive_compact_keep_days,', '"compact": 10**6,'),
    ("M36 atomic rename replaced by direct write", V + "archive.py", "    tmp.write_bytes(blob)\n    os.replace(tmp, path)\n", "    path.write_bytes(blob)\n"),
    ("M37 full file written every poll", V + "archive.py", 'if self._slot.get("full") != int(epoch // s.archive_full_every_s):', "if True:"),
    ("M38 submit no longer swallows errors", V + "archive.py", "        except Exception:\n            self._busy = False\n            self.errors += 1\n", "        except KeyError:\n            self._busy = False\n            self.errors += 1\n"),
    ("M39 watchdog archive age compare > -> <", W, "return (WARN if age > limit else OK), detail", "return (WARN if age < limit else OK), detail"),
    ("M40 watchdog archive check not skipped when collector down", W, '    if not collector_ok:\n        return OK, "not checked while the collector is unhealthy"\n', ""),
    ("M41 watchdog flap ignores fractional count", W, "if not (pilot_pairs or controller_pairs or fractional):", "if not (pilot_pairs or controller_pairs):"),
    ("M42 watchdog flap window 24 h -> 24000 h", W, "FLAP_WINDOW_HOURS = 24", "FLAP_WINDOW_HOURS = 24000"),
    ("M43 watchdog alert dedupe removed", W, "        if level == current:\n            self.pending.pop(name, None)\n            return\n", ""),
    ("M44 watchdog RECOVERED message removed", W, '        if level == OK:\n            self.send(f"RECOVERED', '        if False:\n            self.send(f"RECOVERED'),
    ("M45 watchdog flap pair window 2 s -> 0 s", W, "interval '2 seconds' AND a.logon_time < b.logon_time + interval '2 seconds'", "interval '0 seconds' AND a.logon_time < b.logon_time + interval '0 seconds'"),
]


def run(f: str, old: str, new: str) -> str:
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    shutil.copytree(SRC, SCRATCH, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))
    p = SCRATCH / f
    text = p.read_text()
    if old not in text:
        return "NOT APPLIED (pattern missing)"
    p.write_text(text.replace(old, new, 1))
    env = dict(os.environ, PYTHONPATH=f"{SCRATCH}/collector/src:/tmp/pt", PYTHONDONTWRITEBYTECODE="1")
    targets = [t for t in UNIT + INTEG if (SCRATCH / t).exists()]
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *targets],
                       cwd=SCRATCH, env=env, capture_output=True, text=True, timeout=600)
    tail = (r.stdout.strip().splitlines() or ["?"])[-1]
    if r.returncode == 0:
        return f"SURVIVED ({tail})"
    first = next((ln for ln in r.stdout.splitlines() if ln.startswith(("FAILED", "ERROR"))), tail)
    return f"KILLED by {first[:110]}"


if __name__ == "__main__":
    for name, f, old, new in M:
        if ONLY in name:
            print(f"{name} | {run(f, old, new)}", flush=True)
