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
import base64
import json
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

WANTED = {"isEnabled": True, "timeSelectionEnabled": False, "annotationsEnabled": False, "share": "public"}


def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def plan(dashboards: list[dict[str, Any]], public: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any], dict[str, Any] | None]]:
    """What to do for each dashboard: ('create', d, None), ('update', d, existing), ('keep', d, existing)."""
    by_dash = {p["dashboardUid"]: p for p in public}
    steps: list[tuple[str, dict[str, Any], dict[str, Any] | None]] = []
    for d in sorted(dashboards, key=lambda x: x["title"]):
        existing = by_dash.get(d["uid"])
        if existing is None:
            steps.append(("create", d, None))
        elif any(existing.get(k) != v for k, v in WANTED.items()):
            steps.append(("update", d, existing))
        else:
            steps.append(("keep", d, existing))
    return steps


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """The login is sent as a header: never follow a redirect to another address with it."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def is_this_machine(host: str | None) -> bool:
    """True when the address belongs to this machine (a socket can be bound to it)."""
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as sock:
            sock.bind((host, 0))
        return True
    except OSError:
        return False


def check_url(url: str) -> None:
    """Basic auth in clear text only to this machine; any other address must be https."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "https" or (parsed.scheme == "http" and is_this_machine(parsed.hostname)):
        return
    raise ValueError(f"refusing to send the Grafana login to {url}: use https, or http to an address of this machine")


def default_url(env: dict[str, str]) -> str:
    """Where Grafana listens on this machine: GRAFANA_BIND and GRAFANA_PORT from .env (loopback when unset or 0.0.0.0)."""
    bind = env.get("GRAFANA_BIND") or "127.0.0.1"
    if bind == "0.0.0.0":
        bind = "127.0.0.1"
    return f"http://{bind}:{env.get('GRAFANA_PORT') or '3300'}"


class Grafana:
    def __init__(self, base: str, user: str, password: str) -> None:
        check_url(base)
        self.base = base.rstrip("/")
        self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self.opener = urllib.request.build_opener(NoRedirect)

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        req = urllib.request.Request(self.base + path, method=method)
        req.add_header("Authorization", self.auth)
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(req, data=data, timeout=60) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw)
            except ValueError:
                return exc.code, {"message": raw[:200].decode("utf-8", "replace")}


def link(token: str, public_url: str | None) -> str:
    path = f"/public-dashboards/{token}"
    return (public_url.rstrip("/") + path) if public_url else path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="print the links, change nothing")
    ap.add_argument("--remove", action="store_true", help="remove every public link")
    ap.add_argument("--url", default=None, help="Grafana address as seen from this machine (default: from .env)")
    ap.add_argument("--public-url", default=None, help="public address to print in the links, e.g. https://vatpac.mpengor.com")
    ap.add_argument("--env", default=".env", help="file with GRAFANA_ADMIN_USER and GRAFANA_ADMIN_PASSWORD")
    args = ap.parse_args(argv)

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
            print(f"{d['title']}: {link(token, args.public_url) if token else 'not public'}"
                  + ("" if action == "keep" or not existing else "  (settings differ)"))
            continue
        if action == "create":
            status, res = g.call("POST", f"/api/dashboards/uid/{d['uid']}/public-dashboards", dict(WANTED))
            if status == 200:
                token = res["accessToken"]
        elif action == "update" and existing is not None:
            status, res = g.call("PATCH", f"/api/dashboards/uid/{d['uid']}/public-dashboards/{existing['uid']}", dict(WANTED))
        else:
            status = 200
        ok = status == 200 and bool(token)
        failures += not ok
        verb = {"create": "created", "update": "fixed", "keep": "ok"}[action]
        shown = link(token, args.public_url) if token else ""
        print(f"{d['title']}: " + (f"{verb}  {shown}" if ok else f"FAILED ({status}) {json.dumps(res)[:120]}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
