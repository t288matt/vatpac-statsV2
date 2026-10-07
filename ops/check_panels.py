#!/usr/bin/env python3
"""Run every provisioned Grafana panel query through Grafana's own API (SPEC section 12, check 4).

Reads GRAFANA_PORT, GRAFANA_ADMIN_USER and GRAFANA_ADMIN_PASSWORD from .env (never printed).
For each dashboard JSON under grafana/dashboards it posts every panel's rawSql to /api/ds/query so
Grafana expands its macros, and reports OK (rows), EMPTY (no rows yet, acceptable while data is young)
or ERROR. Dashboard variables are substituted with their current value. Exit 1 on any ERROR.

Usage: python ops/check_panels.py [--env .env] [--range 24h]
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


class Grafana:
    def __init__(self, base: str, user: str, password: str) -> None:
        self.base = base.rstrip("/")
        self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any] | list[Any] | str]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", self.auth)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read().decode()
                return r.status, (json.loads(raw) if raw else "")
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw


def variable_values(dash: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for t in dash.get("templating", {}).get("list", []):
        cur = t.get("current", {}).get("value")
        if isinstance(cur, list):
            cur = cur[0] if cur else ""
        out[t["name"]] = "" if cur in (None, "$__all") else str(cur)
    return out


def substitute(sql: str, values: dict[str, str]) -> tuple[str, list[str]]:
    missing: list[str] = []

    def rep(m: re.Match[str]) -> str:
        name = m.group(1) or m.group(3)
        fmt = m.group(2)
        if name.startswith("__"):
            return m.group(0)  # a Grafana macro: expanded server side
        if name in values:
            val = values[name]
            if fmt == "sqlstring":  # Grafana quotes and escapes the value
                return "'" + val.replace("'", "''") + "'"
            return val
        missing.append(name)
        return m.group(0)

    return re.sub(r"\$\{(\w+)(?::(\w+))?\}|\$(\w+)", rep, sql), missing


def count_rows(result: dict[str, Any]) -> int:
    n = 0
    for frame in result.get("frames", []):
        vals = frame.get("data", {}).get("values", [])
        n += len(vals[0]) if vals else 0
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default=".env")
    ap.add_argument("--range", default="24h")
    ap.add_argument("--dashboards", default="grafana/dashboards")
    args = ap.parse_args()
    env = load_env(Path(args.env))
    g = Grafana(f"http://127.0.0.1:{env.get('GRAFANA_PORT', '3300')}",
                env.get("GRAFANA_ADMIN_USER", "admin"), env["GRAFANA_ADMIN_PASSWORD"])

    code, body = g.call("GET", "/api/health")
    print(f"grafana health: HTTP {code} {body if code != 200 else 'ok'}")
    code, body = g.call("GET", "/api/datasources/uid/vatpac/health")
    print(f"datasource vatpac: HTTP {code} {body.get('message') if isinstance(body, dict) else body}")
    if code != 200:
        return 1

    errors = empties = oks = 0
    for f in sorted(glob.glob(f"{args.dashboards}/*/*.json")):
        dash = json.loads(Path(f).read_text(encoding="utf-8"))
        values = variable_values(dash)
        for p in dash.get("panels", []):
            for t in p.get("targets", []):
                sql = t.get("rawSql")
                if not sql:
                    continue
                sql, missing = substitute(sql, values)
                title = p.get("title", "?")
                where = f"{Path(f).parent.name}/{Path(f).stem} :: {title}"
                if missing:
                    print(f"ERROR  {where}: unresolved variable(s) {sorted(set(missing))}")
                    errors += 1
                    continue
                q = {"refId": t.get("refId", "A"), "datasource": {"uid": "vatpac"}, "rawSql": sql,
                     "format": t.get("format", "table"), "rawQuery": True, "editorMode": "code"}
                code, body = g.call("POST", "/api/ds/query",
                                    {"queries": [q], "from": f"now-{args.range}", "to": "now"})
                res = (body.get("results", {}) if isinstance(body, dict) else {}).get(q["refId"], {})
                err = res.get("error") or (body.get("message") if isinstance(body, dict) and code != 200 else None)
                if code != 200 or err:
                    print(f"ERROR  {where}: {str(err)[:200]}")
                    errors += 1
                else:
                    n = count_rows(res)
                    if n:
                        oks += 1
                    else:
                        print(f"empty  {where}")
                        empties += 1
    print(f"panel queries: {oks} with rows, {empties} empty, {errors} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
