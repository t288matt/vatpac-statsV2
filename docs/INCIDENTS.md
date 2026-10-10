# Incidents

DRAFT for the owner's approval, written 10-10-2026. An incident is anything that loses or risks data, stops polling for more than a few minutes, or makes the dashboards wrong or unavailable. Examples: 08-10-2026 (a test container used the VM's memory, T39), 10-10-2026 (several test containers pushed the load to about 24 and two snapshots timed out, issue #113).

## Who acts first
The manager (Emma). If the manager is not available, the owner uses [IF_CLAUDE_IS_UNAVAILABLE.md](IF_CLAUDE_IS_UNAVAILABLE.md). DevOps is called in when the manager needs a fix to a script, the stack or a backup.

## First steps (stop the bleeding)
1. Look before touching: `docker compose ps`, `uptime`, `free -m`, the collector log, the watchdog status file (read-only).
2. Stop whatever is causing it if that is a test container or an agent run (`docker rm -f` of a non-`vatpac-` container). Do not restart live services without need; a plain `docker compose restart collector` is allowed for a hung collector with no code change (stuck-collector.md).
3. Take a fresh backup before any repair that changes data.
4. Follow the matching runbook: [stuck-collector](RUNBOOKS/stuck-collector.md), [feed-outage](RUNBOOKS/feed-outage.md), [db-full](RUNBOOKS/db-full.md), [restore](RUNBOOKS/restore.md).

## When to tell the owner
Tell him on Telegram at once if there is a risk of data loss, polling has stopped for more than 10 minutes, or the dashboards are down. Otherwise tell him in the next daily digest. Always tell him before any action that deletes data, restores a backup or restarts the database.

## Write-up (ten lines, blameless, within 24 hours)
Add it to the issue for the incident, and a row in docs/TRACKER.md:
1. What happened (times in UTC).
2. How it was noticed.
3. Impact (data lost or late, for how long).
4. Cause.
5. What was done to fix it.
6. How it was verified fixed.
7. What went well.
8. What did not.
9. ONE prevention action, filed as its own GitHub issue (or a rule edit in the working agreement).
10. Who owns that action and when it is due.

Blameless means the write-up names causes and missing guards, not people or agents.
