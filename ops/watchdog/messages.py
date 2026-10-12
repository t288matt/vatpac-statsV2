"""Plain-language wording of the watchdog's Telegram messages (GitHub issue 79).

Pure functions, no network and no database. Each message is a plain sentence or two (what happened, whether data is
affected, what to do) followed by ONE last line starting "Technical:" that carries the check name and the key numbers.
The alert logic (levels, thresholds, confirmation, de-duplication) lives in watchdog.py and is not touched here; the
technical text with all its detail still goes to the watchdog log and to watchdog_status.json unchanged.

Plain ASCII only (emoji and curly quotes caused UTF-8 errors before). Formatting must never stop an alert: telegram_text()
catches every error and falls back to the old technical wording.
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

log = logging.getLogger("watchdog.messages")

PREFIX = "VATPAC Stats:"
LABELS = {"warn": "heads up", "crit": "problem", "ok": "all clear"}
MAX_TECH = 170  # characters of detail kept on the Technical line, so a whole message stays near 400
MAX_TECH_INTEGRITY = 120  # the integrity line lists several long check names; the plain part above it is already long

# One plain wording for every invariant in ops/audit/invariants.sql, keyed by the exact invariant name.
# tests/test_humane_messages.py fails if the pack gains, loses or renames an invariant without this table following.
INVARIANT_WORDS: dict[str, str] = {
    "snapshot rows without an ok poll_log row": "a saved snapshot of the network has no matching record of a successful poll",
    "ok poll_log counts differ from network_snapshot": "the poll records and the saved snapshots disagree on how many pilots or controllers there were",
    "in-region count above global count": "a snapshot counts more pilots or controllers in our region than on the whole network",
    "positions per snapshot differ from pilots_in_region": "the number of stored aircraft positions does not match the pilot count of a snapshot",
    "position rows at a timestamp with no network_snapshot": "aircraft positions are stored for a moment that has no snapshot",
    "snapshot gaps over 5 minutes (last 3 days)": "no data was recorded for more than 5 minutes at some point in the last 3 days",
    "newest snapshot in the future or older than 5 minutes": "the newest snapshot is more than 5 minutes old or has a time in the future",
    "pilot session first/last seen differ from first/last stored position": "a flight's first or last seen time does not match its stored positions",
    "pilot sessions with no position (active in last 3 days)": "a recent flight was recorded without any positions",
    "identity duplicates (cid, callsign, logon_time)": "the same flight appears to be stored twice",
    "positions outside plausibility caps": "an aircraft position has an impossible speed or altitude",
    "pilot session pairs, same cid+callsign, logon_time within 2 s (identity flap)": "one pilot connection may have been stored as two flights",
    "controller session pairs, same cid+callsign, logon_time within 2 s (identity flap)": "one controller connection may have been stored as two sessions",
    "snapshots that created sessions for more than 50% of in-region pilots": "a snapshot started new flights for over half the pilots in our region, as if everyone had reconnected",
    "flight_summary rows missing for sessions older than 12 h": "a finished flight has no summary row",
    "flight_summary sample_count differs from stored positions (final or settled sessions, last 3 days)": "a flight summary disagrees with the positions stored for that flight",
    "flights quiet longer than the quiet period plus 1 hour with unprocessed positions": "some flights ended over nine hours ago and are not yet in the finished-flights table",
    "flights written in the last 3 days whose sample_count differs from the stored positions of their sessions": "a finished flight counts a different number of positions from those stored for it",
    "sessions ended more than 2 days ago with unprocessed positions": "some flights that ended over two days ago are not yet in the finished-flights table",
    "daily partitions missing for today..today+3": "the storage tables for today or the next three days have not been created",
    "reference file hashes in poll_log differ from reference_data_change": "the airspace or callsign list used for polling does not match the recorded change history",
}

_IG = "docs/RUNBOOKS/integrity-checks.md"
_ENGINEER = f"Not urgent, but an engineer should look (see {_IG} on the VM)."
_LOGON = "VATSIM may have changed how it stamps logon times; an engineer should look (docs/RUNBOOKS/alerts.md on the VM, identity_flap)."
_MAINTENANCE = f"Check the collector and maintenance log today (see {_IG} on the VM)."
_WRITER = f"Not urgent, no chart reads the new table yet. It clears as the backfill runs after a deploy; if not, see {_IG} on the VM."

# What to do about each invariant, same keys as INVARIANT_WORDS (a test checks both). Wording only: the alert stays one level (WARN).
# Based on the "what a violation means" table in docs/RUNBOOKS/integrity-checks.md.
INVARIANT_ACTIONS: dict[str, str] = {
    "snapshot rows without an ok poll_log row": _ENGINEER,
    "ok poll_log counts differ from network_snapshot": _ENGINEER,
    "in-region count above global count": f"The live dashboard counts may be wrong. An engineer should look today (see {_IG} on the VM).",
    "positions per snapshot differ from pilots_in_region": f"The live dashboard counts may be wrong. An engineer should look today (see {_IG} on the VM).",
    "position rows at a timestamp with no network_snapshot": _ENGINEER,
    "snapshot gaps over 5 minutes (last 3 days)": f"Nothing to do if the time matches an outage you already know about; otherwise see {_IG} on the VM.",
    "newest snapshot in the future or older than 5 minutes": "Data may have stopped: check the collector and the VATSIM feed now (docs/RUNBOOKS/stuck-collector.md on the VM).",
    "pilot session first/last seen differ from first/last stored position": _ENGINEER,
    "pilot sessions with no position (active in last 3 days)": _ENGINEER,
    "identity duplicates (cid, callsign, logon_time)": _LOGON,
    "positions outside plausibility caps": f"Bad values may be getting into the data; an engineer should look today (see {_IG} on the VM).",
    "pilot session pairs, same cid+callsign, logon_time within 2 s (identity flap)": _LOGON,
    "controller session pairs, same cid+callsign, logon_time within 2 s (identity flap)": _LOGON,
    "snapshots that created sessions for more than 50% of in-region pilots": _LOGON,
    "flight_summary rows missing for sessions older than 12 h": _MAINTENANCE,
    "flight_summary sample_count differs from stored positions (final or settled sessions, last 3 days)": _MAINTENANCE,
    "flights quiet longer than the quiet period plus 1 hour with unprocessed positions": _WRITER,
    "flights written in the last 3 days whose sample_count differs from the stored positions of their sessions": _WRITER,
    "sessions ended more than 2 days ago with unprocessed positions": _WRITER,
    "daily partitions missing for today..today+3": ("Writes will start to fail if today's table is missing. Check the collector and maintenance log today "
                                                    "(docs/RUNBOOKS/integrity-checks.md and db-full.md on the VM)."),
    "reference file hashes in poll_log differ from reference_data_change": "An engineer should look (docs/RUNBOOKS/update-reference-files.md on the VM).",
}


def _clip(text: str, n: int = MAX_TECH) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 3].rstrip() + "..."


def _ascii(text: str) -> str:
    return text.encode("ascii", "replace").decode("ascii")


def duration_words(seconds: float) -> str:
    """'under a minute', 'about 4 minutes', 'about 3 hours', 'about 2 days'. Rounded; never claims precision."""
    s = max(0.0, float(seconds))
    if s < 60:
        return "under a minute"
    if s < 90 * 60:
        n = max(1, round(s / 60))
        return f"about {n} minute{'s' if n != 1 else ''}"
    if s < 48 * 3600:
        n = round(s / 3600)
        return f"about {n} hour{'s' if n != 1 else ''}"
    n = round(s / 86400)
    return f"about {n} days"


def _num(pattern: str, detail: str) -> float | None:
    m = re.search(pattern, detail)
    return float(m.group(1)) if m else None


def _age_words(seconds: float | None) -> str:
    return duration_words(seconds) if seconds is not None else "a while"


# ---------------------------------------------------------------- per-check wording
# Each builder returns (plain text, needs-action sentence is part of the text). Level is "warn" or "crit".

def _collector_health(level: str, detail: str) -> str:
    return ("The data collector is not answering properly, so new VATSIM data may not be arriving. "
            "It keeps trying; if this lasts, check whether VATSIM is down or the collector is stuck (see docs/RUNBOOKS/stuck-collector.md on the VM).")


def _poll_age(level: str, detail: str) -> str:
    if "no newest ok poll" in detail:
        return "No successful VATSIM data fetch has been recorded yet. If this is not a fresh install, see docs/RUNBOOKS/stuck-collector.md on the VM."
    age = _age_words(_num(r"(\d+)s old", detail))
    tail = ("The system keeps retrying; if this lasts, the collector may be stuck (see docs/RUNBOOKS/stuck-collector.md on the VM)."
            if level == "crit" else "Usually a brief hiccup; no action unless it grows.")
    return f"No fresh VATSIM data for {age}. {tail}"


def _snapshot_age(level: str, detail: str) -> str:
    if "no newest snapshot" in detail:
        return "No snapshot of the network has been stored yet. If this is not a fresh install, see docs/RUNBOOKS/stuck-collector.md on the VM."
    age = _age_words(_num(r"(\d+)s old", detail))
    tail = ("Data is not being recorded; if the poll alert is also active, see docs/RUNBOOKS/stuck-collector.md on the VM, otherwise the VATSIM feed may have frozen (see docs/RUNBOOKS/feed-outage.md on the VM)."
            if level == "crit" else "Usually a brief hiccup; no action unless it grows.")
    return f"The newest stored picture of the network is {age} old. {tail}"


def _grafana(level: str, detail: str) -> str:
    return ("The dashboards (Grafana) are down or unwell. Data collection is not affected. "
            "Restart Grafana when convenient (docker compose restart grafana, from ~/vatpac-statsV2 on the VM).")


def _database(level: str, detail: str) -> str:
    return ("The watchdog cannot read the database, so the checks that need it are blind. Collection may still be running. "
            "Check the database container and its log (see docs/RUNBOOKS/db-full.md on the VM if the disk is full).")


def _db_size(level: str, detail: str) -> str:
    gb = _num(r"([\d.]+) GB", detail)
    size = f"{gb:.1f} GB" if gb is not None else "larger than expected"
    return (f"The database has grown to {size}, more than expected (normally 2 to 3 GB). Nothing is broken yet, "
            "but old data may not be being cleared away; see docs/RUNBOOKS/db-full.md on the VM when you can.")


def _disk(level: str, detail: str) -> str:
    found = [(path, float(p)) for path, p in re.findall(r"(\S+) (\d+)% used", detail)]
    if not found:
        return "A disk the watchdog watches could not be read. Check the VM when convenient (see docs/RUNBOOKS/db-full.md on the VM)."
    path, top = max(found, key=lambda f: f[1])
    pct = f"{top:.0f}%"
    which = f" ({path})" if path else ""
    if level == "crit":
        return f"A disk on the VM{which} is {pct} full. Writes could start to fail soon, so clear space today (see docs/RUNBOOKS/db-full.md on the VM)."
    return f"A disk on the VM{which} is {pct} full. Nothing breaks yet, but it needs clearing within days (see docs/RUNBOOKS/db-full.md on the VM)."


def _backup_age(level: str, detail: str) -> str:
    if "no backup yet" in detail:
        return "No database backup has been found. Take one by hand (run make backup from ~/vatpac-statsV2 on the VM) and check the backup log."
    h = _num(r"backup ([\d.]+) h old", detail)
    age = f"{h:.0f} hours" if h is not None else "over a day"
    return f"The newest database backup is {age} old; one should be made every night. Take one by hand (run make backup from ~/vatpac-statsV2 on the VM) and check the backup log."


def _backup_remote(level: str, detail: str) -> str:
    return ("The nightly backup is being made, but the copy to the second location (off the VM) has not succeeded recently, so losing "
            "the VM disk could lose the backups too. Check the backup log (docker compose logs --tail=50 backup, from ~/vatpac-statsV2 "
            "on the VM) and the copy destination (see docs/RUNBOOKS/restore.md on the VM, Off-box copies).")


def _telegram_outbox(level: str, detail: str) -> str:
    if "refuses" in detail:
        return ("Telegram is refusing the alert messages, which usually means a wrong chat id or a wrong, revoked or blocked bot. "
                "Nothing is lost yet: they are kept and retried. Check TELEGRAM_CHAT_ID and TELEGRAM_BOT_TOKEN in .env on the VM "
                "(see docs/RUNBOOKS/alerts.md on the VM, telegram_outbox).")
    return ("Alert messages are not reaching Telegram: the oldest has been waiting for a while and the watchdog keeps retrying. "
            "Check the bot token and chat id in .env and whether Telegram is reachable from the VM (docker compose logs --tail=50 watchdog, "
            "from ~/vatpac-statsV2 on the VM); problems are still visible in the status file.")


def _restore_test(level: str, detail: str) -> str:
    if "FAILED" in detail:
        return ("The weekly test that proves a backup can really be restored has failed, so a recent backup may be unusable. "
                "Run make backup-test from ~/vatpac-statsV2 on the VM and read the result (see docs/RUNBOOKS/restore.md on the VM).")
    if "stale" in detail:
        return ("The weekly restore test worked, but the newest data in the backup was old (a long outage or a quiet period before the dump). "
                "Backups are probably fine; check the collector was running, then run make backup-test from ~/vatpac-statsV2 on the VM "
                "(see docs/RUNBOOKS/restore.md on the VM).")
    if "unreadable" in detail:
        return "The result of the weekly backup restore test cannot be read. Run make backup-test from ~/vatpac-statsV2 on the VM to check (see docs/RUNBOOKS/restore.md on the VM)."
    return ("The weekly test that proves a backup can really be restored is overdue. Backups are probably fine, "
            "but run make backup-test from ~/vatpac-statsV2 on the VM when convenient (see docs/RUNBOOKS/restore.md on the VM).")


def _identity_flap(level: str, detail: str) -> str:
    if "query failed" in detail:
        return "The check for duplicated flight sessions could not run. An engineer should look at it; no action needed today."
    return ("VATSIM may have changed how it stamps logon times again, so one connection could be stored as two and flight counts "
            "inflated. Not urgent today, but an engineer should look (the exact command is in docs/RUNBOOKS/alerts.md on the VM, under identity_flap).")


def _feed_archive(level: str, detail: str) -> str:
    return ("The safety copy of the raw VATSIM data (the archive) has stopped being written. Live collection is not affected. "
            "Check the disk space and the collector log (see docs/RUNBOOKS/feed-archive.md on the VM).")


def _integrity_failing_names(detail: str) -> list[str]:
    """The invariant names in the detail, in the order the detail lists them."""
    return sorted((n for n in INVARIANT_WORDS if n in detail), key=detail.index)


# Invariants whose failure needs action TODAY (data may stop or be wrong now), most urgent first. When several invariants fail
# the message is built from the most urgent one so a same-day problem is never hidden behind a failure that can wait.
SAME_DAY_INVARIANTS: tuple[str, ...] = (
    "daily partitions missing for today..today+3",
    "newest snapshot in the future or older than 5 minutes",
    "flight_summary rows missing for sessions older than 12 h",
    "positions outside plausibility caps",
    "in-region count above global count",
    "positions per snapshot differ from pilots_in_region",
)


def _by_urgency(names: list[str]) -> list[str]:
    """Same-day invariants first (in SAME_DAY_INVARIANTS order), then the rest in their original order."""
    rank = {n: i for i, n in enumerate(SAME_DAY_INVARIANTS)}
    return sorted(names, key=lambda n: rank.get(n, len(rank)))  # sorted() is stable: the rest keep their order


def _several_todo(names: list[str]) -> str:
    """The action sentence when more than one invariant failed: the most urgent one's action, plus where to read the rest."""
    todo = INVARIANT_ACTIONS[_by_urgency(names)[0]]
    today = " Some need action today." if any(n in SAME_DAY_INVARIANTS for n in names) else ""
    return f"{todo}{today} The rest: watchdog log."


def _failing_count(detail: str, names: list[str]) -> int:
    """How many invariants failed: the 'N of 18' figure in the detail (the detail lists at most six names), else the names seen."""
    m_ = re.search(r"(\d+) of \d+ invariants", detail)
    return max(int(m_.group(1)), len(names)) if m_ else len(names)


def _data_integrity(level: str, detail: str) -> str:
    if detail.startswith("new violations since the last sweep"):
        names = _by_urgency(_integrity_failing_names(detail))
        what = INVARIANT_WORDS[names[0]] if names else "another rule is now broken"
        more = f" (and {len(names) - 1} more)" if len(names) > 1 else ""
        todo = _several_todo(names) if len(names) > 1 else INVARIANT_ACTIONS[names[0]] if names else f"See {_IG} on the VM."
        return f"The routine data check found a new problem on top of the earlier one: {what}{more}. {todo}"
    if "could not run" in detail or "did not report" in detail or "unexpected row" in detail or "no invariants found" in detail:
        return ("The routine data check could not run properly (it retries in an hour). Data collection is not affected. "
                f"If it keeps failing, an engineer should look (see {_IG} on the VM).")
    names = _integrity_failing_names(detail)
    if not names:
        return f"A routine data check found a problem. See {_IG} on the VM for what to do."
    first = INVARIANT_WORDS[_by_urgency(names)[0]]
    others = _failing_count(detail, names) - 1
    more = f" (plus {others} other{'s' if others > 1 else ''})" if others > 0 else ""
    # more than the six names the detail lists can fail: then at least two did, so the several-failed wording applies
    todo = _several_todo(names) if others > 0 else INVARIANT_ACTIONS[names[0]]
    return f"A routine data check found a problem: {first}{more}. {todo}"


def _feed_crosscheck(level: str, detail: str) -> str:
    if "nothing to compare" in detail:
        return ("The fortnightly check that compares saved raw VATSIM data with what the system stored had nothing to compare. "
                "Check that the raw-data archive is switched on and writing (see docs/RUNBOOKS/feed-archive.md on the VM).")
    if "could not run" in detail:
        return "The fortnightly cross-check of saved raw data against stored data could not run; it retries in an hour. No action unless it keeps failing."
    n = _num(r"(\d+) difference", detail)
    diff = f"{n:.0f} difference{'s' if n != 1 else ''}" if n is not None else "differences"
    return (f"The fortnightly check against saved raw VATSIM data found {diff}: the system may have kept or dropped some pilots "
            "or controllers wrongly. Not urgent; an engineer should look (see docs/RUNBOOKS/integrity-checks.md on the VM).")


def _container(level: str, detail: str, svc: str) -> str:
    if svc == "migrate":
        return ("The migrate container (a one-off job that updates the database layout when the system starts) is reported as not "
                "healthy. It is not meant to keep running; read its log (docker compose logs migrate, from ~/vatpac-statsV2 on the VM).")
    if "missing" in detail:
        return f"The {svc} container is missing. Run docker compose up -d from ~/vatpac-statsV2 on the VM and check it comes back."
    if level == "warn":
        return f"Docker restarted the {svc} container by itself. It should be fine; check it stays up."
    return f"The {svc} container is not running properly. Check docker compose ps and its log (from ~/vatpac-statsV2 on the VM), then restart it if needed."


def _docker_proxy(level: str, detail: str) -> str:
    return "The watchdog cannot reach Docker to check the containers. Container checks are blind until it is back; check the Docker proxy."


def _restart_cap(level: str, detail: str, svc: str) -> str:
    return (f"The {svc} container keeps failing and has hit the hourly limit on automatic restarts, so the watchdog has stopped restarting it. "
            "Look at its log and restart it by hand.")


def _generic(name: str, level: str, detail: str) -> str:
    what = "has a problem" if level == "crit" else "has a warning"
    return f"A monitoring check ({name}) {what}. See docs/RUNBOOKS/alerts.md on the VM for what it means."


_FIXED = {
    "collector_health": _collector_health,
    "poll_log_ok_age": _poll_age,
    "network_snapshot_age": _snapshot_age,
    "grafana_health": _grafana,
    "database": _database,
    "db_size": _db_size,
    "disk": _disk,
    "backup_age": _backup_age,
    "backup_remote": _backup_remote,
    "telegram_outbox": _telegram_outbox,
    "restore_test": _restore_test,
    "identity_flap": _identity_flap,
    "feed_archive": _feed_archive,
    "data_integrity": _data_integrity,
    "feed_crosscheck": _feed_crosscheck,
    "docker_proxy": _docker_proxy,
}

# What a RECOVERED message says in plain words (the "all clear" sentence).
_RECOVERED = {
    "collector_health": "The data collector is answering again.",
    "poll_log_ok_age": "Data is flowing again.",
    "network_snapshot_age": "Fresh network snapshots are being stored again.",
    "grafana_health": "The dashboards (Grafana) are back.",
    "database": "The watchdog can read the database again.",
    "db_size": "The database size is back within the expected range.",
    "disk": "The disk has enough free space again.",
    "backup_age": "A fresh database backup now exists.",
    "backup_remote": "The off-box backup copy is working again.",
    "telegram_outbox": "Alert messages are reaching Telegram again.",
    "restore_test": "The weekly backup restore test is in order again.",
    "identity_flap": "No duplicated sessions are left in the last 24 hours.",
    "feed_archive": "The raw-data archive is being written again.",
    "data_integrity": "The routine data check is clean again.",
    "feed_crosscheck": "The cross-check against saved raw data is clean again.",
    "docker_proxy": "The watchdog can reach Docker again.",
}


def known_checks() -> list[str]:
    """Check names with their own wording (the names the watchdog's cycle produces; container and restart-cap are prefixes)."""
    return sorted(_FIXED)


def _plain(name: str, level: str, detail: str) -> str:
    if name in _FIXED:
        return _FIXED[name](level, detail)
    if name.startswith("container:"):
        return _container(level, detail, name.split(":", 1)[1])
    if name.startswith("restart-cap:"):
        return _restart_cap(level, detail, name.split(":", 1)[1])
    return _generic(name, level, detail)


def _recovered_plain(name: str, detail: str) -> str:
    if name in _RECOVERED:
        return _RECOVERED[name]
    if name.startswith("container:"):
        return f"The {name.split(':', 1)[1]} container is running normally again."
    if name.startswith("restart-cap:"):
        return f"The {name.split(':', 1)[1]} container is below the restart limit again."
    return f"A monitoring check ({name}) is back to normal."


def humanise(name: str, level: str, detail: str, previous_level: str | None = None, down_for: float | None = None,
             now: datetime | None = None) -> str:
    """The Telegram text for one alert: 'VATPAC Stats: <label> (HH:MM UTC). <plain text>' then 'Technical: <check>, <detail>'.

    level is warn, crit or ok (ok means RECOVERED; previous_level is what it was). down_for is how long the alert was active
    (counted from its confirmation, not the true start of the problem) in seconds, or None when unknown (then no duration is claimed). Never raises for an unknown check name."""
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%H:%M UTC")
    label = LABELS.get(level, "heads up")
    if name == "near-miss":
        plain = (f"a controller logged on with a callsign that is not on the whitelist, so they are not being recorded: {detail}. "
                 "It looks like a new position; add it to the whitelist if it is real.")
        return _ascii(f"{PREFIX} {label} ({stamp}). {plain[0].upper() + plain[1:]}\nTechnical: near-miss {_clip(detail)}")
    if name.startswith("restarted:"):
        svc = name.split(":", 1)[1]
        plain = f"The watchdog restarted the {svc} container because it looked stuck. Check that it stays up; no other action."
        return _ascii(f"{PREFIX} heads up ({stamp}). {plain}\nTechnical: restarted {svc}, {_clip(detail)}")
    if level == "ok":
        plain = _recovered_plain(name, detail)
        if down_for is not None and down_for >= 0:
            plain += f" The alert was active for {duration_words(down_for)}."
        label = LABELS["ok"]
        tech = f"{name} recovered" + (f" (was {previous_level})" if previous_level else "") + f", {_clip(detail)}"
    else:
        plain = _plain(name, level, detail)
        if level == "warn" and previous_level == "crit":
            plain = "This has eased from a problem to a warning. " + plain
        tech = f"{name}, {_tech_detail(name, detail)}"
    return _ascii(f"{PREFIX} {label} ({stamp}). {plain}\nTechnical: {tech}")


def _tech_detail(name: str, detail: str) -> str:
    """The detail for the Technical line. The integrity sweep lists up to six long invariant names: keep the count and the first."""
    if name == "data_integrity":
        names = _integrity_failing_names(detail)
        counts = re.search(r"(\d+) of (\d+) invariants", detail)
        if names and counts:
            return _integrity_tech(names, _failing_count(detail, names), counts.group(1), counts.group(2))
    if name == "data_integrity":
        short = detail.replace("new violations since the last sweep:", "new violations:", 1)
        return _clip(_strip_log_pointer(short), MAX_TECH_INTEGRITY + 20)
    return _clip(_strip_log_pointer(detail))


def _integrity_tech(names: list[str], total: int, failing: str, of: str) -> str:
    """'N of 18 checks failing: a; b; c (+K more)': every name the detail gave, most urgent first, as many as fit in MAX_TECH;
    K counts the failing checks not named (including those the detail itself did not list)."""
    head = f"{failing} of {of} checks failing: "
    ordered = _by_urgency(names)
    shown: list[str] = []
    for n in ordered:
        left = total - len(shown) - 1
        reserve = len(f" (+{left} more)") if left > 0 else 0
        if shown and len(head) + len("; ".join(shown + [n])) + reserve > MAX_TECH_INTEGRITY:
            break
        shown.append(n)
    left = total - len(shown)
    return _clip(head + "; ".join(shown) + (f" (+{left} more)" if left > 0 else ""), MAX_TECH_INTEGRITY + 20)


def _strip_log_pointer(detail: str) -> str:
    """Drop the trailing 'Details are in the watchdog log; ...' pointer: the Technical line has little room."""
    return re.split(r"\.\s*Details are in the watchdog log", detail)[0]


_ALERT_RE = re.compile(r"^(WARN|CRIT) (\S+): (.*)$", re.S)
_RECOVERED_RE = re.compile(r"^RECOVERED (\S+) \(was (\w+)\): (.*)$", re.S)
_RESTARTED_RE = re.compile(r"^RESTARTED (\S+) \((.*?)\): (.*)$", re.S)


def from_alert_text(text: str, down_for: float | None = None, previous_level: str | None = None,
                    now: datetime | None = None) -> str:
    """Parse the technical alert text the watchdog logs ('WARN x: d', 'CRIT x: d', 'RECOVERED x (was warn): d',
    'NEAR-MISS ...', 'RESTARTED svc (...): why') and humanise it. Unrecognised text gets a generic wrapper that keeps it."""
    if (m := _ALERT_RE.match(text)):
        return humanise(m.group(2), m.group(1).lower(), m.group(3), previous_level, None, now)
    if (m := _RECOVERED_RE.match(text)):
        return humanise(m.group(1), "ok", m.group(3), m.group(2), down_for, now)
    if text.startswith("NEAR-MISS"):
        callsigns = text.split(":", 1)[1].strip() if ":" in text else text
        return humanise("near-miss", "warn", callsigns, None, None, now)
    if (m := _RESTARTED_RE.match(text)):
        return humanise(f"restarted:{m.group(1)}", "warn", f"{m.group(2)}: {m.group(3)}", None, None, now)
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%H:%M UTC")
    return _ascii(f"{PREFIX} heads up ({stamp}). The monitoring system sent a message; see docs/RUNBOOKS/alerts.md on the VM.\nTechnical: {_clip(text, 300)}")


DELAY_NOTE_AFTER_S = 120  # a message delivered this long after it was raised says so


def mark_delayed(body: str, raised_at: float, now: float) -> str:
    """Say that a message is late and when it was really raised (UTC, DD-MM-YYYY). The body already carries the time of the
    event ('VATPAC Stats: heads up (14:03 UTC). ...'); a retried message more than DELAY_NOTE_AFTER_S old gets the note
    after that bracket: 'VATPAC Stats: heads up (14:03 UTC), delivered late (first raised 09-10-2026 14:03 UTC). ...'."""
    if now - raised_at < DELAY_NOTE_AFTER_S or not body.startswith(PREFIX):
        return body
    when = datetime.fromtimestamp(raised_at, UTC).strftime("%d-%m-%Y %H:%M UTC")
    head, sep, rest = body.partition("). ")
    if not sep:
        return body
    return f"{head}), delivered late (first raised {when}). {rest}"


def dropped_notice(n: int, first_at: float) -> str:
    """The message that tells the owner earlier alerts were lost because Telegram stayed unreachable."""
    stamp = datetime.fromtimestamp(first_at, UTC).strftime("%H:%M UTC")
    return _ascii(f"{PREFIX} heads up ({stamp}). {n} earlier alert message{'s were' if n != 1 else ' was'} dropped while Telegram could not be "
                  "reached, so the story may have gaps. Check the dashboards and docs/RUNBOOKS/alerts.md on the VM.\n"
                  f"Technical: telegram_outbox, {n} dropped")


def telegram_text(text: str, down_for: float | None = None, previous_level: str | None = None) -> str:
    """The text to send to Telegram for a technical alert text. A formatting bug must never stop an alert, so on any
    error the old technical wording is sent and the error is logged."""
    try:
        return from_alert_text(text, down_for, previous_level)
    except Exception as exc:  # noqa: BLE001 - deliberately broad: never drop an alert
        log.error("could not format the alert in plain words (%s); sending the technical text", type(exc).__name__)
        return f"vatpac-stats: {text}"
