# Runbook: outside heartbeat (issue #17, T19)

Last reviewed: 09-10-2026. The watchdog runs on the VM, so a dead VM or network sends no alert. This check runs on the owner's PC, outside the VM, and messages the owner on Telegram.

**What it does:** every 5 minutes it fetches `http://192.168.1.222:3300/api/health` (Grafana on the VM's LAN address). After 2 failed checks in a row (about 10 minutes) it sends `VATPAC v3 FAILED ...`; when the first check succeeds afterwards it sends `VATPAC v3 RECOVERED ...`. One message per outage, not one per check.

**Where it lives:** Windows Task Scheduler task `VatpacV3Heartbeat` on the PC. The script is `ops/heartbeat/outside_heartbeat.py`; the running copy and its state file are in `C:\Users\mdora\Claude\vatpac-heartbeat\` (outside any git worktree, so deleting a worktree does not stop it). The Telegram token and chat id are read from the `env` block of `C:\Users\mdora\Claude\.claude\settings.local.json` and are never printed or committed.

**Limits:** it only runs while the PC is on and logged in. If the PC is off there is no outside check. It checks Grafana's health, not data freshness (the watchdog covers that on the VM).

**Tested 09-10-2026:** a real check passed; a simulated outage (wrong port) sent one FAILED message after the second failure and none after the third; the next good check sent one RECOVERED message and the one after sent nothing. Test messages carried the prefix TEST.

**Operate**
- Run it by hand: `python C:\Users\mdora\Claude\vatpac-heartbeat\outside_heartbeat.py --url http://192.168.1.222:3300/api/health --settings C:\Users\mdora\Claude\.claude\settings.local.json --state C:\Users\mdora\Claude\vatpac-heartbeat\heartbeat_state.json`
- Pause it: `Disable-ScheduledTask -TaskName VatpacV3Heartbeat` (and `Enable-ScheduledTask` to resume). Pause it before a planned VM restart if you do not want an alert.
- If the VM address changes, re-register the task with the new URL.
