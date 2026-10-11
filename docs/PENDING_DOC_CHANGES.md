# Pending document changes (prepared for deploys)

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

## Grafana stable links (PR 131, branch `stable-links`; NOT deployed)
Switch in after it is deployed and verified live:
- RUNBOOKS/public-access.md: public tokens become stable (the sha256 of the dashboard uid, 32 hex characters, the only form Grafana accepts), so the links survive a wiped volume. Add 10 short addresses that redirect to the public links: `/live`, `/traffic`, `/new-pilots`, `/origins`, `/atc`, `/atc-retention`, `/monday`, `/military`, `/monthly`, `/health`. After the redirect the address bar shows the long address.
- The redirects are Traefik labels, printed by `python3 ops/grafana_public.py --traefik-labels --host vpac.mpengor.com` and pasted into `~/vatpac-proxy/docker-compose.yml` on the Traefik machine, then `docker compose up -d` there. They are outside this repository.
- How the 10 label blocks go in: on the Traefik machine, open `~/vatpac-proxy/docker-compose.yml`, paste the printed labels into the `labels:` list of `vatpac-proxy`, run `docker compose up -d` there, then check one short address. The README in that folder must be updated to mention the short addresses (and the runbook appendix and section "The Traefik machine" here, and the owner's copies in `vatpac-claude-setup/traefik/`).
- Also: README.md (public access paragraph), ARCHITECTURE.md (public-access section), DECISIONS.md ADR-029 (consequences: links are stable), CHANGELOG.md and TRACKER.md (a new row).
- Before deploying, put the fixed `ops/grafana_public.py` on the VM tree (the tree has the 848526c version, which assumes 127.0.0.1).
