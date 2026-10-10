# Pending document changes (prepared for deploys)

Created 10-10-2026. The documents describe what is deployed and running now (commit 3eb81a3 plus migration 160). Work that is merged to `main` but not yet deployed is described in [../CHANGELOG.md](../CHANGELOG.md) and [RESUME.md](RESUME.md) only. This page lists, per deploy, which document changes are ready to switch in once that deploy is VERIFIED LIVE (smoke, panel check, restart counts, watch period), so the update afterwards is quick. Rule: documents are updated after each deploy is verified live, not after each merge.

The prepared text exists in git. For each file below, `git show c89e544:<path>` is the version written for the merged code (deploy 1 text is also correct for later deploys). Take that version, then re-apply any unrelated edit made to the file since. After switching it in, delete the row here.

## Deploy 1: fix-ops and the Telegram wording (exact commit 520dc3e)
| Document | What to switch in |
|---|---|
| RUNBOOKS/alerts.md | The "Message formats" section and the plain-language examples; the `telegram_outbox` and `backup_remote` checks; the Telegram retry behaviour. Until then the VM sends `vatpac-stats: WARN <check>: <detail>`. |
| RUNBOOKS/restore.md | `BACKUP_DUMP_TIMEOUT_S` and `BACKUP_VALIDATE_TIMEOUT_S`; the stricter restore sanity check including `RESTORE_MAX_POLL_AGE_HOURS` (default 48, 0 turns it off); a failed validation no longer keeps the dump. |
| CONFIG_MAP.md | Rows `BACKUP_DUMP_TIMEOUT_S`, `BACKUP_VALIDATE_TIMEOUT_S`, `BACKUP_REMOTE_ENABLED`, `RESTORE_MAX_POLL_AGE_HOURS` (this one is not yet a row at c89e544: add it, default 48, read by `ops/backup/restore-lib.sh`). |
| DATA_DICTIONARY.md | `collector` role: DELETE on `poll_log` only (it holds DELETE on nine tables today). |
| RUNBOOKS/deploy-to-vm.md | Section 4f (the watchdog-only rebuild for the new wording) and section 4e/4f ordering as in `git show c89e544:docs/RUNBOOKS/deploy-to-vm.md`; keep the rollback steps. |
| RUNBOOKS/daily-checks.md, IF_CLAUDE_IS_UNAVAILABLE.md | Describe the new message form (line starting `Today's messages begin vatpac-stats:`). |
| OWNER_BACKUP_COPY.md | It already describes the `backup_remote` check; confirm after the deploy that the wording matches what the watchdog prints. |

## Deploy 2: fix-collector (exact commit 520dc3e, about 24 hours after deploy 1)
| Document | What to switch in |
|---|---|
| CONFIG_MAP.md | `POLL_INTERVAL_SECONDS` row: collector `/health` ages derive from the interval (last good poll under max(60, 2 x interval) s, newest snapshot under max(120, 4 x interval) s). |
| DATA_DICTIONARY.md | `network_snapshot.pilots_in_region` and `poll_log.pilots_inside` wording (rejected pilots are excluded from the first). |
| RUNBOOKS/stuck-collector.md | The paragraph on a snapshot with many rejected records and `apply timed out` (one apply at a time, 25 s deadline, issue #88). |

## fix-sql deploy (tip of main, after deploys 1 and 2; high tier)
| Document | What to switch in |
|---|---|
| DATA_DICTIONARY.md | Checked-against line (migrations up to 20261009000170, 23 migrations); the `drop_old_pilot_position_partitions` function row and the partition-drop paragraph (finalise at most 10 partitions per call, re-checks, no waiting for locks, the collector repeats the call). |
| RUNBOOKS/db-full.md | Section 5 "The first live partition drop" with the rehearsal results, and the changed step 3. |
| CHART_DEFINITIONS.md | Cards 171, 172, 177: "reactivated" needs an earlier session (ADR-028; figure is zero until more than 90 days of history); card 156 and the Monday panel before 07:00 UTC show the previous Monday; the retention dashboard note. |
| DEFINITIONS.md | If it defines reactivated controllers, add the earlier-session rule. |

## fix-111-114 deploy (after fix-sql)
Callsign, departure and arrival byte caps and infinite or huge controller frequencies: add a sentence under the ingest caps in DEFINITIONS.md or SPEC.md section 5 if the text names the caps. No other document changes.

## Scripted checks, `ops/deploy.sh` (merged, read-only; no deploy needed to use it, but the owner decides when it enters the procedure)
RUNBOOKS/deploy-to-vm.md: the section "Scripted checks (`ops/deploy.sh`)" (`--preflight`, `--verify`, `--status`, `--mark`) from `git show c89e544:docs/RUNBOOKS/deploy-to-vm.md`. README.md and RESUME.md may then mention it.
