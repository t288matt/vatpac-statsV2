# Pending document changes (prepared for deploys)

**13-10-2026: issue 89 phases 1, 1b, 3, 4 and 5 are live (markers c70e72f, 3d4b5b6, 38e3958 and 7a71d30) and their text has been switched into the documents (see the section "Issue 89" at the end of this page). Nothing for issue 89 is pending.**

**12-10-2026: the deploys listed below all went live on 11-10-2026 (CHANGELOG.md). The restore.md, CONFIG_MAP.md, stuck-collector.md and db-full.md switch-ins were not re-checked one by one; treat this page as history and check each named document against the code before relying on it.**

Created 10-10-2026. The documents describe what is deployed and running now (commit 3eb81a3 plus migration 160). Work that is merged to `main` but not yet deployed is described in [../CHANGELOG.md](../CHANGELOG.md) and [RESUME.md](RESUME.md) only. This page lists, per deploy, which document changes are ready to switch in once that deploy is VERIFIED LIVE (smoke, panel check, restart counts, watch period), so the update afterwards is quick. Rule: documents are updated after each deploy is verified live, not after each merge.

The prepared text exists in git. For each file below, `git show c89e544:<path>` is the version written for the merged code (deploy 1 text is also correct for later deploys). Take that version, then re-apply any unrelated edit made to the file since. After switching it in, delete the row here.

Three documents are NOT in this list because they describe main and are kept equal to it (tests check them): RUNBOOKS/alerts.md, DATA_DICTIONARY.md and CHART_DEFINITIONS.md carry a line saying so. On the VM today the collector role still holds DELETE on ten tables and loses nine at deploy 1; the data dictionary already describes the result (DELETE on `poll_log` only).

## Deploy 1: fix-ops and the Telegram wording (exact commit 520dc3e)
| Document | What to switch in |
|---|---|
| RUNBOOKS/restore.md | `BACKUP_DUMP_TIMEOUT_S` and `BACKUP_VALIDATE_TIMEOUT_S`; the stricter restore sanity check including `RESTORE_MAX_POLL_AGE_HOURS` (default 48, 0 turns it off); a failed validation no longer keeps the dump. |
| CONFIG_MAP.md | Rows `BACKUP_DUMP_TIMEOUT_S`, `BACKUP_VALIDATE_TIMEOUT_S`, `BACKUP_REMOTE_ENABLED`, `RESTORE_MAX_POLL_AGE_HOURS` (this one is not yet a row at c89e544: add it, default 48, read by `ops/backup/restore-lib.sh`). |
| RUNBOOKS/deploy-to-vm.md | Section 4f (the watchdog-only rebuild for the new wording; it is the only new section) as in `git show c89e544:docs/RUNBOOKS/deploy-to-vm.md`; keep the rollback steps. |
| IF_CLAUDE_IS_UNAVAILABLE.md | Line 24 describes today's message form (`Today's messages begin vatpac-stats:`); switch to the new form. |
| OWNER_BACKUP_COPY.md | It already describes the `backup_remote` check; confirm after the deploy that the wording matches what the watchdog prints. |

## Deploy 2: fix-collector (exact commit 520dc3e, about 24 hours after deploy 1)
| Document | What to switch in |
|---|---|
| CONFIG_MAP.md | `POLL_INTERVAL_SECONDS` row: collector `/health` ages derive from the interval (last good poll under max(60, 2 x interval) s, newest snapshot under max(120, 4 x interval) s). |
| RUNBOOKS/stuck-collector.md | The paragraph on a snapshot with many rejected records and `apply timed out` (one apply at a time, 25 s deadline, issue #88). |

## fix-sql deploy (tip of main, after deploys 1 and 2; high tier)
| Document | What to switch in |
|---|---|
| RUNBOOKS/db-full.md | Section 5 "The first live partition drop" with the rehearsal results, and the changed step 3. |
| DEFINITIONS.md | If it defines reactivated controllers, add the earlier-session rule. |

## fix-111-114 deploy (after fix-sql)
Callsign, departure and arrival byte caps and infinite or huge controller frequencies: add a sentence under the ingest caps in DEFINITIONS.md or SPEC.md section 5 if the text names the caps. No other document changes.

## Scripted checks, `ops/deploy.sh` (merged, read-only; no deploy needed to use it, but the owner decides when it enters the procedure)
RUNBOOKS/deploy-to-vm.md: the section "Scripted checks (`ops/deploy.sh`)" (`--preflight`, `--verify`, `--status`, `--mark`) from `git show c89e544:docs/RUNBOOKS/deploy-to-vm.md`. README.md and RESUME.md may then mention it.

## Issue 89 (updated 13-10-2026, after the phase 4 and 5 deploy)
Done on 13-10-2026 after reading the VM read-only: phases 1, 1b and 3 (earlier on 13-10-2026) and phases 4 and 5 (release marker 7a71d30, 10:05:56 UTC) switched to live in CHANGELOG.md, DATA_DICTIONARY.md, DEFINITIONS.md, CONFIG_MAP.md, CHART_DEFINITIONS.md, DECISIONS.md (ADR-030 and the ADR-026 amendment), TRACKER.md (T73 to T77), ARCHITECTURE.md, README.md, the conformance tables of phases 4 and 5 and the runbooks deploy-to-vm, alerts, integrity-checks, daily-checks, drop-gate-rehearsal and restore. Nothing for issue 89 is pending in this file any more. The exit-test record (#138) is in CHANGELOG.md.

What is still open after the flip (not document switches): the 24 hour watch of the phase 4 and 5 deploy (from 13-10-2026 10:05 UTC; plan in `DEPLOY_PLAN_PHASE_4_5.md`, section 5, in the `vatpac-claude-setup` repository); the first nightly backup and restore check on the new table; the measurement of the lock-hold time of one partition drop before the first real drop, about 05-04-2027; the owner's closing of issues #89 and #137 to #142.
