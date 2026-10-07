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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import psycopg

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
        self.restart_cap = int(env_num("WATCHDOG_RESTART_CAP_PER_HOUR", 3))
        self.restart_after = max(1, int(env_num("WATCHDOG_RESTART_AFTER_CYCLES", 3)))
        self.restartable = env_set("WATCHDOG_RESTARTABLE", "db,collector,grafana,backup")
        self.expected = env_set("WATCHDOG_EXPECTED", "db,collector,grafana,backup,docker-socket-proxy")
        # Empty (the default) means no Docker access: container state checks and
        # restarts are skipped and the watchdog only checks health and the database.
        self.docker = env("DOCKER_HOST_URL", "")
        self.project = env("COMPOSE_PROJECT", "vatpac")
        self.collector_url = env("COLLECTOR_HEALTH_URL", "http://collector:8080/health")
        self.grafana_url = env("GRAFANA_HEALTH_URL", "http://grafana:3000/api/health")
        self.db_dsn = (
            f"host={env('DB_HOST', 'db')} dbname={env('DB_NAME', 'vatpac')} "
            f"user={env('DB_USER', 'watchdog_ro')} password={env('DB_PASSWORD')} connect_timeout=5"
        )
        self.backup_dir = Path(env("BACKUP_DIR", "/backups"))
        self.status_file = Path(env("STATUS_FILE", "/var/watchdog/watchdog_status.json"))
        self.tg_token = env("TELEGRAM_BOT_TOKEN")
        self.tg_chat = env("TELEGRAM_CHAT_ID")
        self.kuma_url = env("UPTIME_KUMA_PUSH_URL")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
        out["database"] = (OK, "reachable")
    except psycopg.Error as exc:
        out["database"] = (CRIT, f"query/connect failed ({type(exc).__name__})")
    return out


def check_disk(cfg: Config) -> Result:
    usage = shutil.disk_usage("/")
    pct = usage.used / usage.total * 100
    level = CRIT if pct >= cfg.disk_pct + 10 else WARN if pct >= cfg.disk_pct else OK
    return level, f"{pct:.0f}% used"


def check_backup(cfg: Config, first_run: float) -> Result:
    files = list(cfg.backup_dir.glob("*.dump")) if cfg.backup_dir.is_dir() else []
    newest = max((f.stat().st_mtime for f in files), default=first_run)
    age_h = (time.time() - newest) / 3600
    detail = f"newest backup {age_h:.1f} h old" if files else f"no backup yet ({age_h:.1f} h since watchdog first ran)"
    return (CRIT if age_h > cfg.backup_max_h else OK), detail


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

class Alerter:
    def __init__(self, cfg: Config, client: httpx.Client, state: dict[str, Any]) -> None:
        self.cfg, self.client = cfg, client
        self.confirmed: dict[str, str] = state.setdefault("alerts", {})
        self.pending: dict[str, tuple[str, int]] = {}

    def send(self, text: str) -> None:
        log.info("ALERT %s", text)
        if not (self.cfg.tg_token and self.cfg.tg_chat):
            return
        try:
            r = self.client.post(
                f"https://api.telegram.org/bot{self.cfg.tg_token}/sendMessage",
                json={"chat_id": self.cfg.tg_chat, "text": f"vatpac-stats: {text}"},
                timeout=10,
            )
            if r.status_code != 200:
                log.warning("telegram returned HTTP %s", r.status_code)
        except httpx.HTTPError as exc:  # never log the URL: it contains the bot token
            log.warning("telegram send failed (%s)", type(exc).__name__)

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
    results: dict[str, Result] = {}
    streak: dict[str, int] = state.setdefault("streak", {})

    coll, coll_body = check_http(client, cfg.collector_url)
    results["collector_health"] = coll
    graf, _ = check_http(client, cfg.grafana_url)
    results["grafana_health"] = graf

    db = check_db(cfg)
    near_miss = db.pop("_near_miss", (OK, ""))[1]
    results.update(db)
    results["disk"] = check_disk(cfg)
    results["backup_age"] = check_backup(cfg, state.setdefault("first_run", time.time()))

    containers: dict[str, dict[str, Any]] = {}
    if cfg.docker:
        try:
            containers = docker.containers()
            results.update(check_containers(cfg, containers, state))
        except (httpx.HTTPError, ValueError) as exc:
            results["docker_proxy"] = (CRIT, f"unreachable ({type(exc).__name__})")

    for name, (level, detail) in results.items():
        alerter.update(name, level, detail)

    # New near-miss controller callsigns: alert once each, ever (persisted).
    known = set(state.setdefault("known_near_miss", []))
    fresh = [c for c in near_miss.split(",") if c and c not in known]
    if fresh:
        alerter.send(f"NEAR-MISS controller callsign(s) not on whitelist: {', '.join(fresh[:10])}")
        state["known_near_miss"] = sorted(known | set(fresh))

    # Restart decisions. A target must be bad for restart_after consecutive cycles.
    bad: dict[str, str] = {}
    for svc, ins in containers.items():
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
