#!/usr/bin/env python3
"""Share every provisioned Grafana dashboard as a read-only public dashboard (fixed time range), or list or remove the links.

A public dashboard runs only the queries saved on the dashboard: visitors cannot send their own SQL, change the
time range or edit anything (docs/RUNBOOKS/public-access.md). Run on the VM, from ~/vatpac-statsV2:

    python3 ops/grafana_public.py                 create or repair the public links and print them
    python3 ops/grafana_public.py --list          print the links only (changes nothing)
    python3 ops/grafana_public.py --remove        remove every public link (stops public access at once)
    python3 ops/grafana_public.py --public-url https://vatpac.mpengor.com    print full URLs

Safe to run again: it only creates what is missing and fixes a link whose settings differ. The admin login is read from
.env (GRAFANA_ADMIN_USER, GRAFANA_ADMIN_PASSWORD) and is never printed. Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from env_file import read_env  # the one .env reader (ops/env_file.py); python puts this script's folder first on sys.path
from grafana_api import Grafana, default_url  # the one Grafana client with its login-protection rules (ops/grafana_api.py)

WANTED = {"isEnabled": True, "timeSelectionEnabled": False, "annotationsEnabled": False, "share": "public"}

# Short, readable addresses (https://<host>/live) that redirect to a dashboard's public link. Grafana only accepts a
# 32-character hex token in its own address, so words can only be a redirect, done by Traefik labels (--traefik-labels).
SHORT_NAMES = {
    "vatpac-pilots-now": "live",
    "vatpac-pilots-traffic": "traffic",
    "vatpac-pilot-onboarding": "new-pilots",
    "vatpac-demographics": "origins",
    "vatpac-atc-overview": "atc",
    "vatpac-atc-retention": "atc-retention",
    "vatpac-atc-position-hours": "atc-hours",
    "vatpac-pilots-records": "pilots",
    "vatpac-monday-events": "monday",
    "vatpac-military": "military",
    "vatpac-management": "monthly",
    "vatpac-monitoring": "health",
}


def stable_token(uid: str) -> str:
    """The same dashboard always gets the same public token (32 hex characters), so the short addresses never go stale."""
    return hashlib.sha256(f"vatpac-public:{uid}".encode()).hexdigest()[:32]


def traefik_labels(host: str) -> str:
    """Traefik label lines (for the helper container's compose file) that redirect each short address to its public link."""
    lines = []
    for uid, name in sorted(SHORT_NAMES.items(), key=lambda kv: kv[1]):
        r = f"vatpac-{name}"
        lines += [
            f"      # /{name}  ->  {uid}",
            f'      - "traefik.http.routers.{r}.entrypoints=https"',
            f'      - "traefik.http.routers.{r}.rule=Host(`{host}`) && Path(`/{name}`)"',
            f'      - "traefik.http.routers.{r}.tls=true"',
            f'      - "traefik.http.routers.{r}.service=vatpac"',
            f'      - "traefik.http.routers.{r}.middlewares={r}-go"',
            f'      - "traefik.http.middlewares.{r}-go.redirectregex.regex=^https://[^/]+/{name}/?$$"',
            f'      - "traefik.http.middlewares.{r}-go.redirectregex.replacement=https://{host}/public-dashboards/{stable_token(uid)}"',
        ]
    return "\n".join(lines)


def plan(dashboards: list[dict[str, Any]], public: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any], dict[str, Any] | None]]:
    """What to do for each dashboard: create, recreate (wrong token), update (wrong settings) or keep."""
    by_dash = {p["dashboardUid"]: p for p in public}
    steps: list[tuple[str, dict[str, Any], dict[str, Any] | None]] = []
    for d in sorted(dashboards, key=lambda x: x["title"]):
        existing = by_dash.get(d["uid"])
        if existing is None:
            steps.append(("create", d, None))
        elif existing.get("accessToken") != stable_token(d["uid"]):
            steps.append(("recreate", d, existing))
        elif any(existing.get(k) != v for k, v in WANTED.items()):
            steps.append(("update", d, existing))
        else:
            steps.append(("keep", d, existing))
    return steps


def link(token: str, public_url: str | None) -> str:
    path = f"/public-dashboards/{token}"
    return (public_url.rstrip("/") + path) if public_url else path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="print the links, change nothing")
    ap.add_argument("--remove", action="store_true", help="remove every public link")
    ap.add_argument("--traefik-labels", action="store_true", help="print the Traefik labels for the short addresses (needs --host)")
    ap.add_argument("--host", default=None, help="public host name for --traefik-labels, e.g. vpac.mpengor.com")
    ap.add_argument("--url", default=None, help="Grafana address as seen from this machine (default: from .env)")
    ap.add_argument("--public-url", default=None, help="public address to print in the links, e.g. https://vatpac.mpengor.com")
    ap.add_argument("--env", default=".env", help="file with GRAFANA_ADMIN_USER and GRAFANA_ADMIN_PASSWORD")
    args = ap.parse_args(argv)

    if args.traefik_labels:
        if not args.host:
            print("--traefik-labels needs --host", file=sys.stderr)
            return 2
        print(traefik_labels(args.host))
        return 0

    try:
        env = read_env(Path(args.env))
    except OSError as exc:
        print(f"cannot read {args.env}: {exc.strerror}", file=sys.stderr)
        return 2
    password = env.get("GRAFANA_ADMIN_PASSWORD")
    if not password:
        print("GRAFANA_ADMIN_PASSWORD is not set in", args.env, file=sys.stderr)
        return 2
    args.url = args.url or default_url(env)
    try:
        g = Grafana(args.url, env.get("GRAFANA_ADMIN_USER", "admin"), password)
        return run(g, args)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    except OSError as exc:  # Grafana down, refused, timed out (urllib's URLError is an OSError)
        print(f"cannot reach Grafana at {args.url}: {exc}", file=sys.stderr)
        return 1


def run(g: Grafana, args: argparse.Namespace) -> int:

    status, dashboards = g.call("GET", "/api/search?type=dash-db&limit=200")
    if status != 200:
        print(f"cannot list dashboards: HTTP {status} {json.dumps(dashboards)[:120]} (wrong password, or Grafana not up?)", file=sys.stderr)
        return 1
    status, listing = g.call("GET", "/api/dashboards/public-dashboards")
    if status != 200:
        print(f"cannot list public dashboards: HTTP {status} {json.dumps(listing)[:120]}", file=sys.stderr)
        return 1
    public = listing.get("publicDashboards", []) if isinstance(listing, dict) else []
    for k, item in enumerate(public):  # the list has no settings; the per-dashboard call does
        status, full = g.call("GET", f"/api/dashboards/uid/{item['dashboardUid']}/public-dashboards")
        if status == 200 and isinstance(full, dict):
            public[k] = {**item, **full}

    failures = 0
    if args.remove:
        for p in public:
            status, res = g.call("DELETE", f"/api/dashboards/uid/{p['dashboardUid']}/public-dashboards/{p['uid']}")
            ok = status == 200
            failures += not ok
            print(("removed " if ok else f"FAILED ({status}) ") + p.get("title", p["dashboardUid"]))
        return 1 if failures else 0

    if args.list:
        known = {d["uid"] for d in dashboards}
        for p in public:
            if p["dashboardUid"] not in known:
                name, url = p.get("title", p["dashboardUid"]), link(p["accessToken"], args.public_url)
                print(f"{name}: {url}  (not a provisioned dashboard any more; --remove deletes it)")
    for action, d, existing in plan(dashboards, public):
        token: str | None = existing["accessToken"] if existing else None
        res = None
        if args.list:
            short = f"  (short: /{SHORT_NAMES[d['uid']]})" if d["uid"] in SHORT_NAMES else ""
            note = "" if action == "keep" or not existing else "  (token or settings differ: run without --list)"
            print(f"{d['title']}: {link(token, args.public_url) if token else 'not public'}{short}{note}")
            continue
        if action == "recreate" and existing is not None:  # a link with another token: replace it (its address changes once)
            status, res = g.call("DELETE", f"/api/dashboards/uid/{d['uid']}/public-dashboards/{existing['uid']}")
            action = "create" if status == 200 else "recreate"
        if action == "create":
            status, res = g.call("POST", f"/api/dashboards/uid/{d['uid']}/public-dashboards",
                                 {**WANTED, "accessToken": stable_token(d["uid"])})
            if status == 200:
                token = res["accessToken"]
        elif action == "update" and existing is not None:
            status, res = g.call("PATCH", f"/api/dashboards/uid/{d['uid']}/public-dashboards/{existing['uid']}", dict(WANTED))
        else:
            status = 200
        ok = status == 200 and bool(token)
        failures += not ok
        verb = {"create": "created", "update": "fixed", "keep": "ok", "recreate": "FAILED to replace"}[action]
        shown = link(token, args.public_url) if token else ""
        print(f"{d['title']}: " + (f"{verb}  {shown}" if ok else f"FAILED ({status}) {json.dumps(res)[:120]}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
