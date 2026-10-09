# Working agreement

Status: DRAFT written 09-10-2026 for the owner's approval. It takes effect when the owner approves it (section 9). Until then the rules in `docs/RESUME.md` section 4 and the owner's standing instructions apply.
Language: British English. Dates DD-MM-YYYY, times UTC. Plain words; no emoji.

## 1. Purpose
Small, frequent, safe releases of VATPAC Stats, with the owner deciding what matters and not having to read everything. The aim is to remove the owner as the bottleneck without removing the safety: risk decides how much approval a change needs, and evidence replaces summaries.

## 2. The team
| Role | Who | Owns |
|---|---|---|
| Owner and product lead | Matt | Priorities; approval of medium and high changes; data, privacy and retention decisions; every production deploy outside the pre-approved tier (section 5). |
| Engineering manager | Emma (this assistant) | Triage, work-in-progress limit, the daily digest, the tracker and issues, risk tiers, the review gate, escalation. Does not write production code. |
| Tech lead / architect | agent | Design and decision records for larger changes before anyone builds them. |
| Collector engineer | agent | `collector/` (parsing, ingest, health). |
| Database engineer | agent | Migrations, views, roles and grants, partitions, query performance. |
| DevOps / reliability engineer | agent | CI, deploy script, backups and restores, the watchdog, monitoring. |
| QA / test engineer | agent | Characterisation, replay and fuzz tests; proof that behaviour is preserved. |
| Independent code reviewer | a different agent from the author, every time | Reviews every change (section 4). Never reviews its own work. |
| Security reviewer | agent | Secrets, privileges, personal data in dumps and the archive. |
| Data quality analyst | agent | Do the dashboard numbers match the raw data; integrity checks; the v1 comparison. |
| Technical writer | agent | Runbooks, `RESUME.md`, short tracker rows, plain-language alert wording. |
Not on the team for now: an outside non-Claude reviewer (future work, issue #105), incident responder, UX designer, compliance advisor, release manager.
Limit of the team: all agent roles are the same model family, so they can share blind spots. That is why section 4 asks for evidence, and why #103 and #105 exist.

## 3. How work flows (small and often)
1. **Ready:** an issue and a tracker row exist (issue first, then the row), with a "done when" line.
2. **Build:** one agent, one branch, one worktree (`.worktrees/<name>`). Characterise today's behaviour first, then change only the defect.
3. **Check:** unit tests, the full integration suite in UTC and Australia/Sydney, ruff, mypy, the validators, size budget.
4. **Review:** a different agent reviews the branch (section 4).
5. **Merge:** fast-forward to `main` only after an approved review; read CI on `main`.
6. **Deploy:** per the tier (section 5). Verify with the smoke test, panel check, restart counts, identity check and polling gap. Roll back if any fails.
7. **Close:** the issue is closed with the evidence; the owner closes anything they asked to close themselves.
Work in progress limit: at most 3 branches in flight at once. Finish and merge before starting another. Releases are small and frequent; there is no weekly batch.

## 4. Review and evidence
- Every change gets an independent review before it reaches `main` (owner rule of 09-10-2026). Docs-only changes that state no operational fact may be merged by the manager.
- Every change that alters behaviour comes with a **Behaviour preserved** section: what could have changed, how it was proved not to (old and new compared on the same data), and the residual risk.
- The owner is asked to approve evidence (three lines: review verdict, test counts, behaviour-preserved proof), not to read a summary.
- Agent reports are not proof. The manager checks that commits, CI and test counts exist before reporting anything as done.
- A CI check that every code commit carries a `Reviewed-by:` line and a review record under `docs/reviews/` is planned (decision of 09-10-2026). It is a tripwire, not proof.

## 5. Change tiers and who approves
| Tier | Examples | Approval |
|---|---|---|
| Low | Documentation; tests; dashboard query or layout fixes that keep every other panel identical; comment-only migrations; alert wording; scripts that are not deployed | Independent review plus green CI. May be deployed without a further yes **only once** the scripted deploy with automatic rollback exists (issue #104) and the owner has approved this tier in section 9. |
| Medium | Collector code; new views or columns that keep existing ones identical; watchdog logic; CI and release workflows | Independent review, behaviour-preserved proof, then the owner's yes to a three-line evidence summary. |
| High | `roles.sql` and grants; backup, restore and the off-VM copy; destructive or irreversible migrations; retention settings; anything that deletes data; secrets | Independent review, a rehearsal on a throwaway database, a fresh backup, and the owner's explicit go for each deploy. |
Until #104 exists, every production deploy needs the owner's explicit go, whatever the tier. A deploy always starts with a fresh dump and a rollback copy.

## 6. Decisions
- The manager brings decisions to the owner one at a time with a recommendation first, or in a short daily digest ("N things need a yes"). Each has a default taken if the owner does not answer within two working days, only for tier Low or for questions the owner has pre-agreed defaults for.
- Owner-only decisions: priorities; retention and personal data; anything irreversible; spending; what "good enough" means (the uptime, gap and wrong-number targets, once the owner has set them).

## 7. Limits on every agent
- Never print, store or commit secrets; never enter credentials; never expose Grafana publicly.
- Never run `db/roles.sql` by hand on the live database; never run tests or throwaway databases on the production VM without memory, swap, CPU and time caps and cleanup (until #104 moves them off).
- Never deploy, restart live services, send messages to the owner's real Telegram, close issues or delete branches, volumes or data without the approval the tier requires.
- Never skip hooks or checks, force-push, or amend commits. Never edit applied migrations; add a new numbered one.
- Stay inside the assigned files. Do not edit `docs/TRACKER.md` or `docs/RESUME.md` from a branch (the manager updates them).
- Report honestly: what was done, what failed, what was not verified.

## 8. Measures (weekly, kept by the manager in the digest)
Deploys per week; time from a change being ready to being live; share of deploys that needed a rollback; time to fix a failed deploy. Plus the open count of issues by tier. These replace opinions about "too slow" or "too risky".

## 9. Approval
| Item | Decision |
|---|---|
| Team in section 2 | agreed by the owner in chat, 09-10-2026 |
| Tier table in section 5 | PENDING |
| Low tier deploys without a further yes once #104 exists | PENDING |
| Daily digest and default answers (section 6) | PENDING |
