"""VATPAC stats watchdog (SPEC section 8).

Every WATCHDOG_INTERVAL_SECONDS: run checks, alert on confirmed state changes only (Telegram,
plain text), and write a status JSON to a volume. It never changes data or configuration.
By default it has no Docker access (DOCKER_HOST_URL is empty): it checks HTTP health and the
database only and does not restart anything. If DOCKER_HOST_URL is set (for example to a
restricted Docker API endpoint provided outside this repo) it also checks container state and
restarts clearly stuck containers (capped per container per hour).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import integrity
import messages
import psycopg
from psycopg.conninfo import make_conninfo

OK, WARN, CRIT = "ok", "warn", "crit"
RANK = {OK: 0, WARN: 1, CRIT: 2}
Result = tuple[str, str]  # (level, detail)

log = logging.getLogger("watchdog")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def env_num(name: str, default: float) -> float:
    try:
        return float(env(name, str(default)))
    except ValueError:
        return default


def env_set(name: str, default: str) -> set[str]:
    return {s.strip() for s in env(name, default).split(",") if s.strip()}


class Config:
    def __init__(self) -> None:
        self.interval = env_num("WATCHDOG_INTERVAL_SECONDS", 60)
        self.warn_s = env_num("WATCHDOG_WARN_SECONDS", 60)
        self.page_s = env_num("WATCHDOG_PAGE_SECONDS", 300)
        self.confirm = max(1, int(env_num("WATCHDOG_CONFIRM_CYCLES", 2)))
        self.disk_pct = env_num("WATCHDOG_DISK_PCT", 80)
        self.db_max_gb = env_num("WATCHDOG_DB_MAX_GB", 20)
        self.backup_max_h = env_num("WATCHDOG_BACKUP_MAX_HOURS", 26)
        self.restore_test_max_days = env_num("WATCHDOG_RESTORE_TEST_MAX_DAYS", 9)
        # On/off flag only (compose sets it to 1 when BACKUP_REMOTE is non-empty); the command itself never reaches the watchdog.
        self.backup_remote = env("BACKUP_REMOTE_ENABLED").strip().lower() in ("1", "true", "yes", "on")
        # Filesystems to check. The pgdata and backups named volumes both live under the Docker data
        # root on the host; the watchdog sees /backups (and / for the container layer), which is
        # that same host filesystem. Add a path here if a volume is moved to its own mount.
        self.disk_paths = [p.strip() for p in env("WATCHDOG_DISK_PATHS", "/backups,/").split(",") if p.strip()]
        self.restart_cap = int(env_num("WATCHDOG_RESTART_CAP_PER_HOUR", 3))
        self.restart_after = max(1, int(env_num("WATCHDOG_RESTART_AFTER_CYCLES", 3)))
        self.restartable = env_set("WATCHDOG_RESTARTABLE", "db,collector,grafana,backup")
        self.expected = env_set("WATCHDOG_EXPECTED", "db,collector,grafana,backup")
        # Empty (the default) means no Docker access: container state checks and
        # restarts are skipped and the watchdog only checks health and the database.
        self.docker = env("DOCKER_HOST_URL", "")
        self.project = env("COMPOSE_PROJECT", "vatpac")
        self.collector_url = env("COLLECTOR_HEALTH_URL", "http://collector:8080/health")
        self.grafana_url = env("GRAFANA_HEALTH_URL", "http://grafana:3000/api/health")
        # make_conninfo quotes/escapes values; a raw space-separated string breaks on odd passwords
        self.db_dsn = make_conninfo(
            host=env("DB_HOST", "db"), dbname=env("DB_NAME", "vatpac"),
            user=env("DB_USER", "watchdog_ro"), password=env("DB_PASSWORD"), connect_timeout=5,
        )
        self.backup_dir = Path(env("BACKUP_DIR", "/backups"))
        self.status_file = Path(env("STATUS_FILE", "/var/watchdog/watchdog_status.json"))
        self.tg_token = env("TELEGRAM_BOT_TOKEN")
        self.tg_chat = env("TELEGRAM_CHAT_ID")
        self.kuma_url = env("UPTIME_KUMA_PUSH_URL")
        # Scheduled integrity checks (issue 20): the invariant sweep and the archive cross-check. 0 disables either one.
        self.integrity = integrity.Plan(
            dsn=self.db_dsn,
            sql_path=Path(env("INVARIANTS_SQL", "/app/invariants.sql")),
            archive_dir=Path(env("ARCHIVE_DIR", "/archive")),
            config_dir=Path(env("REFERENCE_CONFIG_DIR", "/config")),
            invariants_interval_s=max(0.0, env_num("WATCHDOG_INTEGRITY_INTERVAL_HOURS", 72)) * 3600,
            crosscheck_interval_s=max(0.0, env_num("WATCHDOG_CROSSCHECK_INTERVAL_DAYS", 14)) * 86400,
            samples=max(1, int(env_num("WATCHDOG_CROSSCHECK_SAMPLES", 24))),
        )


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def age_level(age: float | None, cfg: Config, what: str) -> Result:
    if age is None:
        return WARN, f"no {what} yet"
    if age >= cfg.page_s:
        return CRIT, f"{what} is {age:.0f}s old"
    if age >= cfg.warn_s:
        return WARN, f"{what} is {age:.0f}s old"
    return OK, f"{what} {age:.0f}s old"


# ---------------------------------------------------------------- checks

def check_http(client: httpx.Client, url: str) -> tuple[Result, dict[str, Any] | None]:
    try:
        r = client.get(url, timeout=5)
    except httpx.HTTPError as exc:
        return (CRIT, f"unreachable ({type(exc).__name__})"), None
    body: dict[str, Any] | None
    try:
        parsed = r.json()
        body = parsed if isinstance(parsed, dict) else None
    except ValueError:
        body = None
    if r.status_code == 200:
        return (OK, "healthy"), body
    return (CRIT, f"HTTP {r.status_code}"), body


def collector_is_stuck(res: Result, body: dict[str, Any] | None) -> bool:
    """Stuck = not answering, or 503 with no failures counted (no polls and no errors).
    A responsive collector with consecutive_failures > 0 is an upstream outage: do not restart."""
    if res[0] == OK:
        return False
    if body is None:
        return True
    return int(body.get("consecutive_failures", 0) or 0) == 0


def check_db(cfg: Config) -> dict[str, Result]:
    out: dict[str, Result] = {}
    try:
        with psycopg.connect(cfg.db_dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT EXTRACT(EPOCH FROM now() - max(polled_at)) FROM poll_log WHERE status = 'ok'")
            row = cur.fetchone()
            out["poll_log_ok_age"] = age_level(float(row[0]) if row and row[0] is not None else None, cfg, "newest ok poll")
            cur.execute("SELECT EXTRACT(EPOCH FROM now() - max(observed_at)) FROM network_snapshot")
            row = cur.fetchone()
            out["network_snapshot_age"] = age_level(float(row[0]) if row and row[0] is not None else None, cfg, "newest snapshot")
            cur.execute("SELECT pg_database_size(current_database())")
            row = cur.fetchone()
            gb = (int(row[0]) if row else 0) / 1024**3
            out["db_size"] = (WARN if gb > cfg.db_max_gb else OK, f"{gb:.2f} GB (limit {cfg.db_max_gb:g})")
            cur.execute("SELECT callsign FROM controller_unlisted_sighting ORDER BY first_seen")
            out["_near_miss"] = (OK, ",".join(r[0] for r in cur.fetchall()))
            out["identity_flap"] = check_identity_flap(cur)  # last, so a failure here cannot hide the checks above
        out["database"] = (OK, "reachable")
    except psycopg.Error as exc:
        out["database"] = (CRIT, f"query/connect failed ({type(exc).__name__})")
    return out


# Same logic as ops/check_identity_flap.sql (session pairs with one cid and callsign and logon_time within 2 s, and
# fractional logon_time), limited to sessions seen in the last FLAP_WINDOW_HOURS so that the alert clears once they age
# out. It walks the last_seen index and the (cid, callsign, logon_time) unique index, so it is cheap every cycle.
FLAP_WINDOW_HOURS = 24
_PAIRS_SQL = (
    "SELECT count(*), min(b.first_seen) FROM {t} b JOIN {t} a ON a.cid = b.cid AND a.callsign = b.callsign AND a.id < b.id "
    "AND a.logon_time > b.logon_time - interval '2 seconds' AND a.logon_time < b.logon_time + interval '2 seconds' "
    "WHERE b.last_seen > now() - make_interval(hours => %s)"
)
_FRACTION_SQL = (
    "SELECT count(*) FROM (SELECT logon_time FROM pilot_session WHERE last_seen > now() - make_interval(hours => %s) "
    "UNION ALL SELECT logon_time FROM controller_session WHERE last_seen > now() - make_interval(hours => %s)) s "
    "WHERE logon_time <> date_trunc('second', logon_time)"
)


def flap_level(pilot_pairs: int, controller_pairs: int, fractional: int, first: datetime | None) -> Result:
    """WARN while any recent session pair or fractional logon_time exists; OK once none is left in the window."""
    if not (pilot_pairs or controller_pairs or fractional):
        return OK, f"no identity flap in the last {FLAP_WINDOW_HOURS} h"
    when = first.astimezone(UTC).strftime("%d-%m-%Y %H:%M:%S UTC") if first else "unknown time"
    return WARN, (f"{pilot_pairs} pilot and {controller_pairs} controller session pairs (same cid and callsign, logon_time "
                  f"within 2 s), {fractional} fractional logon_time, first seen {when}; run ops/check_identity_flap.sql")


def check_identity_flap(cur: Any) -> Result:
    """Run the identity-flap queries on an open cursor (role watchdog_ro needs SELECT on both session tables)."""
    try:
        counts: list[int] = []
        firsts: list[datetime] = []
        for table in ("pilot_session", "controller_session"):
            cur.execute(_PAIRS_SQL.format(t=table), (FLAP_WINDOW_HOURS,))
            n, first = cur.fetchone()
            counts.append(int(n))
            if first is not None:
                firsts.append(first)
        cur.execute(_FRACTION_SQL, (FLAP_WINDOW_HOURS, FLAP_WINDOW_HOURS))
        return flap_level(counts[0], counts[1], int(cur.fetchone()[0]), min(firsts) if firsts else None)
    except psycopg.Error as exc:
        return WARN, f"identity-flap query failed ({type(exc).__name__})"


def check_archive(body: dict[str, Any] | None, collector_ok: bool) -> Result:
    """The collector's raw-feed archive must have written something within twice its compact interval. Skipped while the
    collector itself is unhealthy (no fresh feed means no writes, and that is already alerted)."""
    arc = body.get("archive") if body else None
    if not isinstance(arc, dict) or not arc.get("enabled"):
        return OK, "archive disabled or not reported"
    if not collector_ok:
        return OK, "not checked while the collector is unhealthy"
    age, limit = float(arc.get("last_write_age_seconds") or 0), 2 * float(arc.get("compact_every_s") or 300)
    errors, mb = int(arc.get("errors") or 0), int(arc.get("size_bytes") or 0) / 1048576
    detail = f"last write {age:.0f}s ago (limit {limit:.0f}s), {errors} write errors, {mb:.0f} MB"
    return (WARN if age > limit else OK), detail


def check_disk(cfg: Config) -> Result:
    """Worst level across WATCHDOG_DISK_PATHS (paths on the same device are checked once)."""
    worst: Result = (OK, "no path to check")
    parts: list[str] = []
    seen_dev: set[int] = set()
    for path in cfg.disk_paths:
        try:
            dev = os.stat(path).st_dev
            if dev in seen_dev:
                continue
            seen_dev.add(dev)
            usage = shutil.disk_usage(path)
        except OSError:
            parts.append(f"{path} unreadable")
            if RANK[WARN] > RANK[worst[0]]:
                worst = (WARN, "")
            continue
        pct = usage.used / usage.total * 100
        level = CRIT if pct >= cfg.disk_pct + 10 else WARN if pct >= cfg.disk_pct else OK
        parts.append(f"{path} {pct:.0f}% used")
        if RANK[level] > RANK[worst[0]]:
            worst = (level, "")
    return worst[0], ", ".join(parts) if parts else worst[1]


def check_backup(cfg: Config, first_run: float) -> Result:
    mtimes: list[float] = []
    try:
        candidates = list(cfg.backup_dir.glob("*.dump")) if cfg.backup_dir.is_dir() else []
    except OSError:
        candidates = []
    for f in candidates:
        try:
            mtimes.append(f.stat().st_mtime)
        except OSError:  # rotation or a restore deleted it between the listing and the stat: skip it
            continue
    newest = max(mtimes, default=first_run)
    age_h = (time.time() - newest) / 3600
    detail = f"newest backup {age_h:.1f} h old" if mtimes else f"no backup yet ({age_h:.1f} h since watchdog first ran)"
    return (CRIT if age_h > cfg.backup_max_h else OK), detail


def check_backup_remote(cfg: Config, first_run: float) -> Result:
    """The off-box copy (BACKUP_REMOTE) must have succeeded within WATCHDOG_BACKUP_MAX_HOURS: the backup container writes
    /backups/.last_remote_success after every successful hook run. Only active when BACKUP_REMOTE_ENABLED is set (issue 91)."""
    if not cfg.backup_remote:
        return OK, "off-box copy not configured (BACKUP_REMOTE is empty)"
    path = cfg.backup_dir / ".last_remote_success"
    try:
        text = path.read_text()
        mtime = path.stat().st_mtime
    except OSError:
        age_h = (time.time() - first_run) / 3600
        if age_h > cfg.backup_max_h:
            return WARN, f"no off-box copy recorded after {age_h:.1f} h since watchdog first ran"
        return OK, "no off-box copy yet"
    try:
        ts = datetime.strptime(text.strip().split()[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()
    except (ValueError, IndexError):
        ts = mtime
    age_h = (time.time() - ts) / 3600
    if age_h > cfg.backup_max_h:
        return WARN, f"last off-box copy {age_h:.1f} h ago (limit {cfg.backup_max_h:g})"
    return OK, f"last off-box copy {age_h:.1f} h ago"


def check_telegram_outbox(cfg: Config, state: dict[str, Any], now: float | None = None) -> Result:
    """WARN while undelivered Telegram messages are older than OUTBOX_WARN_AGE_S: the alerts are not reaching the owner.
    The status file (and Uptime Kuma, if configured) still show it. OK when Telegram is not configured."""
    if not (cfg.tg_token and cfg.tg_chat):
        return OK, "telegram not configured"
    now = time.time() if now is None else now
    waiting = [m for m in state.get("outbox", []) if isinstance(m, dict) and not m.get("notice")]
    if not waiting:
        return OK, "no undelivered telegram messages"
    oldest = min(float(m.get("at", now)) for m in waiting)
    age = max(0.0, now - oldest)
    detail = f"{len(waiting)} undelivered telegram message(s), oldest {age / 60:.0f} min old"
    refused = state.get("outbox_refused")
    if isinstance(refused, dict):  # the last failure was a configuration fault: not transient, so warn at once
        return WARN, (f"Telegram refuses the messages (HTTP {refused.get('status')}): check TELEGRAM_CHAT_ID and the bot token; "
                      f"{detail}")
    return (WARN if age >= OUTBOX_WARN_AGE_S else OK), detail


def parse_restore_result(text: str) -> tuple[datetime | None, str]:
    """Parse the .last_restore_test line: '<UTC timestamp> OK|FAIL|WARN ...'. Returns (time, OK|FAIL|WARN|?)."""
    tokens = text.strip().split()
    if len(tokens) < 2:
        return None, "?"
    try:
        when: datetime | None = datetime.strptime(tokens[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        when = None
    return when, tokens[1] if tokens[1] in ("OK", "FAIL", "WARN") else "?"


def check_restore_test(cfg: Config, first_run: float) -> Result:
    """The weekly restore test must have passed recently. FAIL is critical; a stale or missing
    result is a warning (BACKUP_RESTORE_TEST_DAY=0 disables the test: set the max days to 0 too)."""
    path = cfg.backup_dir / ".last_restore_test"
    if cfg.restore_test_max_days <= 0:
        return OK, "restore-test age check disabled"
    try:
        text = path.read_text()
        mtime = path.stat().st_mtime
    except OSError:
        age_d = (time.time() - first_run) / 86400
        if age_d > cfg.restore_test_max_days:
            return WARN, f"no restore test result after {age_d:.1f} days"
        return OK, "no restore test yet"
    when, verdict = parse_restore_result(text)
    ts = when.timestamp() if when else mtime
    age_d = (time.time() - ts) / 86400
    if verdict == "FAIL":
        return CRIT, f"last restore test FAILED {age_d:.1f} days ago"
    if verdict == "WARN":
        return WARN, f"last restore test restored fine but the data in the dump was stale ({age_d:.1f} days ago)"
    if verdict != "OK":
        return WARN, "last restore test result unreadable"
    if age_d > cfg.restore_test_max_days:
        return WARN, f"last successful restore test {age_d:.1f} days ago (limit {cfg.restore_test_max_days:g})"
    return OK, f"last restore test OK {age_d:.1f} days ago"


class Docker:
    def __init__(self, cfg: Config, client: httpx.Client) -> None:
        self.cfg, self.client = cfg, client

    def containers(self) -> dict[str, dict[str, Any]]:
        flt = json.dumps({"label": [f"com.docker.compose.project={self.cfg.project}"]})
        r = self.client.get(f"{self.cfg.docker}/containers/json", params={"all": "1", "filters": flt}, timeout=10)
        r.raise_for_status()
        out: dict[str, dict[str, Any]] = {}
        for c in r.json():
            svc = c.get("Labels", {}).get("com.docker.compose.service", "")
            ins = self.client.get(f"{self.cfg.docker}/containers/{c['Id']}/json", timeout=10)
            ins.raise_for_status()
            out[svc] = ins.json()
        return out

    def restart(self, container_id: str) -> bool:
        r = self.client.post(f"{self.cfg.docker}/containers/{container_id}/restart", params={"t": "20"}, timeout=60)
        return r.status_code == 204


def check_containers(cfg: Config, containers: dict[str, dict[str, Any]], state: dict[str, Any]) -> dict[str, Result]:
    out: dict[str, Result] = {}
    seen_rc: dict[str, Any] = state.setdefault("restart_counts", {})
    for svc in sorted(cfg.expected | {"migrate"}):
        ins = containers.get(svc)
        name = f"container:{svc}"
        if ins is None:
            out[name] = (CRIT, "missing")
            continue
        st = ins.get("State", {})
        status = st.get("Status", "unknown")
        health = st.get("Health", {}).get("Status", "none")
        rc = int(ins.get("RestartCount", 0))
        if svc == "migrate":
            code = st.get("ExitCode", 0)
            ok = status == "exited" and code == 0
            out[name] = (OK if ok or status == "running" else CRIT, f"{status} exit={code}")
            continue
        prev = seen_rc.get(svc)
        seen_rc[svc] = rc
        recent = state.setdefault("recent_restart_until", {})
        if prev is not None and rc > prev:
            recent[svc] = time.time() + 600
        if status != "running":
            out[name] = (CRIT, f"state {status}")
        elif health == "unhealthy":
            out[name] = (CRIT, "unhealthy")
        elif recent.get(svc, 0) > time.time():
            out[name] = (WARN, f"restarted by Docker (count {rc})")
        else:
            out[name] = (OK, f"{status}/{health}")
    return out


# ---------------------------------------------------------------- alerting and restarts

OUTBOX_MAX = 20  # undelivered Telegram messages kept; the oldest is dropped (and logged) beyond this
OUTBOX_BACKOFF_BASE_S = 30.0  # wait after the first failed delivery, doubling each time up to the maximum
OUTBOX_BACKOFF_MAX_S = 900.0
OUTBOX_MAX_REJECTIONS = 5  # HTTP 400 or 413 this many times: Telegram will never take this message, so drop it
OUTBOX_POISON_STATUSES = (400, 413)  # bad request / too large: the message may be at fault, unless the description says otherwise
# Telegram answers a wrong chat id, a blocked bot and similar with HTTP 400 and a description: those are configuration faults, never
# poison. Matched case-insensitively against the response's "description" text (be generous: a wrongly dropped CRIT is worse).
OUTBOX_CONFIG_HINTS = ("chat not found", "chat_id", "bot was blocked", "bot was kicked", "user is deactivated", "unauthorized",
                       "not enough rights", "have no rights", "forbidden", "peer_id_invalid", "bot token", "bot is not a member",
                       "group chat was upgraded", "not found")


def _telegram_description(r: httpx.Response) -> str:
    """The 'description' text of a Telegram error answer (nothing else from the body), clipped, for the log and for the
    configuration-fault test. Empty when there is none."""
    try:
        d = r.json().get("description", "")
    except (ValueError, AttributeError, TypeError):
        return ""
    return " ".join(str(d).split())[:120]


def _is_config_fault(status: int, description: str) -> bool:
    """401, 403 and 404 are always configuration faults; a 400 is one when its description says so (e.g. 'chat not found')."""
    if status in (401, 403, 404):
        return True
    return status == 400 and any(h in description.lower() for h in OUTBOX_CONFIG_HINTS)
OUTBOX_MAX_SENDS_PER_CYCLE = 5  # sends per watchdog CYCLE (shared by every flush in it): a slow Telegram must not hold a cycle for minutes
OUTBOX_WARN_AGE_S = 900.0  # the oldest undelivered message is this old: raise the telegram_outbox check


def _retry_after(r: httpx.Response) -> float | None:
    """Seconds Telegram asked us to wait (HTTP 429), capped, else None."""
    try:
        v = r.json().get("parameters", {}).get("retry_after") if r.status_code == 429 else None
        return min(float(v), OUTBOX_BACKOFF_MAX_S) if v is not None else None
    except (ValueError, AttributeError, TypeError):
        return None


class Alerter:
    def __init__(self, cfg: Config, client: httpx.Client, state: dict[str, Any]) -> None:
        self.cfg, self.client = cfg, client
        self.state = state
        # Undelivered Telegram messages (oldest first), persisted in the status file so a restart keeps them (issue 83).
        self.outbox: list[dict[str, Any]] = state.setdefault("outbox", [])
        self._budget: int | None = None  # sends left in the current cycle (None = no cycle running: each flush gets the full cap)
        self.confirmed: dict[str, str] = state.setdefault("alerts", {})
        self.pending: dict[str, tuple[str, int]] = {}
        # When each check went bad (epoch seconds), persisted in the status file so a restart keeps it. Used only to say how
        # long the alert was active in the RECOVERED message; absent (an older status file) means the duration is left out.
        self.bad_since: dict[str, float] = state.setdefault("bad_since", {})
        self._context: tuple[str | None, float | None] = (None, None)  # (previous level, seconds bad) for the next send

    def send(self, text: str) -> None:
        """Log the technical text; queue the plain-language version for Telegram (messages.py) and try to deliver the queue.

        A message that cannot be delivered stays in a small outbox in the watchdog state (the status file) and is retried on
        later cycles, oldest first, until Telegram answers HTTP 200 (issue 83)."""
        log.info("ALERT %s", text)
        previous, down_for = self._context
        self._context = (None, None)
        if not (self.cfg.tg_token and self.cfg.tg_chat):
            return
        body = messages.telegram_text(text, down_for, previous)  # the time shown in it is the time of the event
        self._enqueue({"body": body, "at": time.time(), "tries": 0, "ok": text.startswith("RECOVERED")})
        self.flush()

    def begin_cycle(self) -> None:
        """Start a watchdog cycle: every flush() in it shares one budget of OUTBOX_MAX_SENDS_PER_CYCLE sends."""
        self._budget = OUTBOX_MAX_SENDS_PER_CYCLE

    def end_cycle(self) -> None:
        self._budget = None

    def _enqueue(self, item: dict[str, Any]) -> None:
        """Append to the outbox. At the bound, drop the oldest all-clear message first (a lost RECOVERED matters least), then the
        oldest message; one notice at the front counts what was dropped, so the owner knows the story has gaps."""
        while len(self.outbox) >= OUTBOX_MAX:
            victims = [i for i, m in enumerate(self.outbox) if not m.get("notice")]
            if not victims:
                break
            idx = next((i for i in victims if self.outbox[i].get("ok")), victims[0])
            self.outbox.pop(idx)
            log.warning("telegram outbox full (%d); dropped the oldest undelivered message", OUTBOX_MAX)
            if self.outbox and self.outbox[0].get("notice"):
                notice = self.outbox[0]
            else:
                notice = {"notice": True, "dropped": 0, "at": time.time(), "tries": 0, "ok": False}
                self.outbox.insert(0, notice)
            notice["dropped"] = int(notice.get("dropped", 0)) + 1
            notice["body"] = messages.dropped_notice(int(notice["dropped"]), float(notice["at"]))
        self.outbox.append(item)

    def flush(self) -> None:
        """Deliver the outbox in order, at most OUTBOX_MAX_SENDS_PER_CYCLE per call. Stops at the first failure (so order is kept
        and a RECOVERED never overtakes its problem message) and waits an increasing time before the next try. Never raises.

        HTTP 400 and 413 mean this message itself is bad: after OUTBOX_MAX_REJECTIONS of them it is dropped and the next one is
        tried at once. Anything else (401, 403, 404: a rotated token, a blocked bot, a wrong chat id; 5xx; network errors) is a
        fault elsewhere, so the message is KEPT; 4xx faults retry at the maximum back-off."""
        if not (self.cfg.tg_token and self.cfg.tg_chat):
            return
        budget = self._budget if self._budget is not None else OUTBOX_MAX_SENDS_PER_CYCLE
        while self.outbox and budget > 0:
            now = time.time()
            due = float(self.state.get("outbox_next_try", 0) or 0)
            if now < due <= now + OUTBOX_BACKOFF_MAX_S:  # a time further ahead than any back-off means the clock moved: ignore it
                return
            item = self.outbox[0]
            body = messages.mark_delayed(str(item.get("body", "")), float(item.get("at", now)), now)
            wait: float | None = None
            try:
                r = self.client.post(
                    f"https://api.telegram.org/bot{self.cfg.tg_token}/sendMessage",
                    json={"chat_id": self.cfg.tg_chat, "text": body},
                    timeout=10,
                )
                if r.status_code == 200:
                    self.outbox.pop(0)
                    self.state.pop("outbox_next_try", None)
                    self.state.pop("outbox_refused", None)
                    budget -= 1
                    if self._budget is not None:
                        self._budget = budget
                    continue
                desc = _telegram_description(r).replace(self.cfg.tg_token, "***")
                log.warning("telegram returned HTTP %s%s", r.status_code, f" ({desc})" if desc else "")
                wait = _retry_after(r)
                if _is_config_fault(r.status_code, desc):
                    # wrong chat id, revoked token, blocked bot: never the message's fault. Keep it, retry slowly, and say so.
                    wait = OUTBOX_BACKOFF_MAX_S
                    self.state["outbox_refused"] = {"status": r.status_code, "at": time.time()}
                elif r.status_code in OUTBOX_POISON_STATUSES:
                    item["rejected"] = int(item.get("rejected", 0)) + 1
                    if item["rejected"] >= OUTBOX_MAX_REJECTIONS:
                        self.outbox.pop(0)
                        self.state.pop("outbox_next_try", None)
                        log.warning("telegram refused a message %d times; dropped it", OUTBOX_MAX_REJECTIONS)
                        continue  # the next message is unrelated: no back-off for the one that was dropped
                elif 400 <= r.status_code < 500 and r.status_code != 429:
                    wait = OUTBOX_BACKOFF_MAX_S  # some other client error: keep the message, retry slowly
            except httpx.HTTPError as exc:  # never log the URL: it contains the bot token
                log.warning("telegram send failed (%s)", type(exc).__name__)
            item["tries"] = int(item.get("tries", 0)) + 1
            delay = min(OUTBOX_BACKOFF_BASE_S * 2 ** (int(item["tries"]) - 1), OUTBOX_BACKOFF_MAX_S)
            self.state["outbox_next_try"] = time.time() + max(delay, wait or 0.0)
            return

    def _track(self, name: str, level: str, current: str) -> None:
        """Keep the time a check went bad (best effort; never raises) and hand the context to the next send."""
        try:
            since = self.bad_since
            now = time.time()
            down_for: float | None = None
            if level == OK:
                started = since.pop(name, None)
                if isinstance(started, (int, float)) and now >= started:
                    down_for = now - started
            elif current == OK:
                since[name] = now
            self._context = (current if current != OK else None, down_for)
        except Exception:  # noqa: BLE001 - timing is a nicety; the alert matters more
            self._context = (None, None)

    def update(self, name: str, level: str, detail: str) -> None:
        current = self.confirmed.get(name, OK)
        if level == current:
            self.pending.pop(name, None)
            return
        want, count = self.pending.get(name, (level, 0))
        count = count + 1 if want == level else 1
        if count < self.cfg.confirm:
            self.pending[name] = (level, count)
            return
        self.pending.pop(name, None)
        self.confirmed[name] = level
        self._track(name, level, current)
        if level == OK:
            self.send(f"RECOVERED {name} (was {current}): {detail}")
        else:
            self.send(f"{level.upper()} {name}: {detail}")


def try_restart(cfg: Config, docker: Docker, alerter: Alerter, state: dict[str, Any], svc: str, ins: dict[str, Any], why: str) -> None:
    if svc not in cfg.restartable:
        return
    stamps = [t for t in state.setdefault("restarts", {}).get(svc, []) if time.time() - t < 3600]
    state["restarts"][svc] = stamps
    if len(stamps) >= cfg.restart_cap:
        alerter.update(f"restart-cap:{svc}", CRIT, f"restart cap {cfg.restart_cap}/hour reached; not restarting ({why})")
        return
    alerter.update(f"restart-cap:{svc}", OK, "below restart cap")
    try:
        done = docker.restart(ins["Id"])
    except httpx.HTTPError as exc:
        log.warning("restart %s failed (%s)", svc, type(exc).__name__)
        return
    stamps.append(time.time())
    alerter.send(f"RESTARTED {svc} ({len(stamps)}/{cfg.restart_cap} this hour, ok={done}): {why}")


# ---------------------------------------------------------------- main loop

def load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
        return data.get("state", {}) if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_status(cfg: Config, results: dict[str, Result], state: dict[str, Any]) -> str:
    overall = max((lv for lv, _ in results.values()), key=lambda lv: RANK[lv], default=OK)
    payload = {
        "updated": now_iso(),
        "overall": overall,
        "checks": {k: {"level": v[0], "detail": v[1]} for k, v in sorted(results.items())},
        "state": state,
    }
    try:
        cfg.status_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = cfg.status_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, cfg.status_file)
    except OSError as exc:
        log.warning("could not write status file (%s)", type(exc).__name__)
    return overall


def cycle(cfg: Config, client: httpx.Client, docker: Docker, alerter: Alerter, state: dict[str, Any]) -> str:
    alerter.begin_cycle()  # one send budget for every flush in this cycle (issue 83)
    try:
        return _cycle(cfg, client, docker, alerter, state)
    finally:
        alerter.end_cycle()


def _cycle(cfg: Config, client: httpx.Client, docker: Docker, alerter: Alerter, state: dict[str, Any]) -> str:
    results: dict[str, Result] = {}
    streak: dict[str, int] = state.setdefault("streak", {})

    coll, coll_body = check_http(client, cfg.collector_url)
    results["collector_health"] = coll
    graf, _ = check_http(client, cfg.grafana_url)
    results["grafana_health"] = graf
    results["feed_archive"] = check_archive(coll_body, coll[0] == OK)

    db = check_db(cfg)
    near_miss = db.pop("_near_miss", (OK, ""))[1]
    results.update(db)
    sched, notices = integrity.run_due(cfg.integrity, state)  # slow checks, persisted schedule, see integrity.py
    results.update(sched)
    results["disk"] = check_disk(cfg)
    first_run = state.setdefault("first_run", time.time())
    results["backup_age"] = check_backup(cfg, first_run)
    results["restore_test"] = check_restore_test(cfg, first_run)
    results["backup_remote"] = check_backup_remote(cfg, first_run)
    results["telegram_outbox"] = check_telegram_outbox(cfg, state)

    containers: dict[str, dict[str, Any]] = {}
    if cfg.docker:
        try:
            containers = docker.containers()
            results.update(check_containers(cfg, containers, state))
        except (httpx.HTTPError, ValueError) as exc:
            results["docker_proxy"] = (CRIT, f"unreachable ({type(exc).__name__})")

    for name, (level, detail) in results.items():
        alerter.update(name, level, detail)

    for text in notices:  # violations that are new since the previous sweep, while the integrity alert is already active
        alerter.send(text)

    # New near-miss controller callsigns: alert once each, ever (persisted).
    known = set(state.setdefault("known_near_miss", []))
    fresh = [c for c in near_miss.split(",") if c and c not in known]
    if fresh:
        alerter.send(f"NEAR-MISS controller callsign(s) not on whitelist: {', '.join(fresh[:10])}")
        state["known_near_miss"] = sorted(known | set(fresh))

    # Restart decisions. A target must be bad for restart_after consecutive cycles.
    bad: dict[str, str] = {}
    for svc in containers:
        lv, detail = results.get(f"container:{svc}", (OK, ""))
        if lv == CRIT and svc not in ("migrate", "watchdog"):
            bad[svc] = detail
    coll_health = containers.get("collector", {}).get("State", {}).get("Health", {}).get("Status")
    if collector_is_stuck(coll, coll_body) and "collector" in containers and coll_health != "starting":
        bad.setdefault("collector", f"collector {coll[1]} and no failures counted")
    if graf[0] == CRIT and "grafana" in containers:
        bad.setdefault("grafana", f"grafana {graf[1]}")
    for svc in list(streak):
        if svc not in bad:
            streak.pop(svc)
    for svc, why in bad.items():
        streak[svc] = streak.get(svc, 0) + 1
        if streak[svc] >= cfg.restart_after:
            streak[svc] = 0
            try_restart(cfg, docker, alerter, state, svc, containers[svc], why)

    alerter.flush()  # retry anything undelivered, just before the state (with the outbox) is saved
    overall = write_status(cfg, results, state)
    if cfg.kuma_url and overall != CRIT:
        try:
            client.get(cfg.kuma_url, timeout=5)
        except httpx.HTTPError:
            log.warning("uptime-kuma push failed")
    return overall


def main() -> None:
    logging.basicConfig(level=env("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx logs full URLs at INFO
    cfg = Config()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    state = load_state(cfg.status_file)
    log.info("watchdog started: interval=%ss expected=%s", cfg.interval, sorted(cfg.expected))
    with httpx.Client() as client:
        docker, alerter = Docker(cfg, client), Alerter(cfg, client, state)
        while not stop.is_set():
            try:
                cycle(cfg, client, docker, alerter, state)
            except Exception:  # a watchdog must not die; log and carry on
                log.exception("cycle failed")
            stop.wait(cfg.interval)


if __name__ == "__main__":
    main()
