"""The one Grafana API client for the ops scripts (issue 210): ops/check_panels.py and ops/grafana_public.py both use it.

Basic auth over urllib, standard library only. The admin login goes out as a header, so two rules protect it:
  - it is sent in clear text (http) only to an address of this machine; any other address must be https (check_url);
  - a redirect is never followed, because it could lead to another address (NoRedirect).
Scripts that import this run as `python3 ops/<name>.py`; Python then puts ops/ first on sys.path, so `import grafana_api` works.
"""
from __future__ import annotations

import base64
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


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
        """(HTTP status, parsed JSON). An empty reply gives None; a reply that is not JSON, on an error status, gives {"message": text}."""
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
