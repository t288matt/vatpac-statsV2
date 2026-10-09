#!/usr/bin/env python3
"""Outside heartbeat (issue #17, T19): checks Grafana's health endpoint from a machine other than the VM and
messages the owner on Telegram.

FAILED is sent after FAIL_AFTER consecutive failed checks, RECOVERED when the first check succeeds afterwards.
Run it every 5 minutes (Windows Task Scheduler, see docs/RUNBOOKS/outside-heartbeat.md).

Usage: outside_heartbeat.py --url http://<vm>:3300/api/health --settings <settings.local.json> [--state file] [--test-prefix TEST]
The Telegram token and chat id are read from the "env" block of the settings file (TELEGRAM_BOT_TOKEN,
TELEGRAM_MATT_CHAT_ID) and are never printed. Plain ASCII messages only.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

FAIL_AFTER = 2
TIMEOUT_S = 10


def check(url: str) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_S) as r:  # noqa: S310 (operator-supplied URL)
            body = r.read().decode("utf-8", "replace")
            if r.status != 200:
                return False, f"HTTP {r.status}"
            if '"database"' in body and '"ok"' not in body.replace(" ", ""):
                return False, "Grafana is up but reports its database is not ok"
            return True, "ok"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {str(e)[:120]}"


def telegram(settings: Path, text: str) -> None:
    env = json.loads(settings.read_text(encoding="utf-8")).get("env", {})
    data = urllib.parse.urlencode({"chat_id": env["TELEGRAM_MATT_CHAT_ID"], "text": text}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{env['TELEGRAM_BOT_TOKEN']}/sendMessage", data=data)
    urllib.request.urlopen(req, timeout=15).read()  # noqa: S310


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--settings", required=True, type=Path)
    ap.add_argument("--state", type=Path, default=Path(__file__).with_name("heartbeat_state.json"))
    ap.add_argument("--test-prefix", default="")
    a = ap.parse_args()
    state = {"fails": 0, "alerted": False}
    try:
        state.update(json.loads(a.state.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    ok, why = check(a.url)
    now = datetime.now(timezone.utc).strftime("%d-%m-%Y %H:%M UTC")
    pre = f"{a.test_prefix} " if a.test_prefix else ""
    if ok:
        if state["alerted"]:
            telegram(a.settings, f"{pre}VATPAC v3 RECOVERED at {now}: Grafana health is ok again.")
        state = {"fails": 0, "alerted": False}
    else:
        state["fails"] += 1
        if state["fails"] >= FAIL_AFTER and not state["alerted"]:
            telegram(a.settings, f"{pre}VATPAC v3 FAILED at {now}: {FAIL_AFTER} checks in a row failed ({why}). "
                                 "The VM, its network or Grafana may be down. Checked from the PC, outside the VM.")
            state["alerted"] = True
    a.state.write_text(json.dumps(state), encoding="utf-8")
    print(f"{now} ok={ok} {why} fails={state['fails']} alerted={state['alerted']}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
