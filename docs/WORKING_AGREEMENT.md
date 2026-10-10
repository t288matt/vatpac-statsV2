# Working agreement

Status: APPROVED by the owner in chat on 09-10-2026 (section 9). The low-tier deploy rule in section 5 only takes effect once the scripted deploy with automatic rollback exists (issue #104); until then every production deploy needs the owner's explicit go. The rules in `docs/RESUME.md` section 4 still apply where this document is silent.
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

### 2a. Lean staffing: hats, not staff (owner decision 10-10-2026)
The roles above are hats. They are not nine permanent staff and not nine agent runs per change. Agent runs cost tokens and a session limit can kill them mid-task, so:
1. **A change normally gets two agents: one builder and one independent reviewer.** The builder wears the collector, database, DevOps or QA hat as needed (it writes the characterisation tests and the behaviour-preserved proof). The reviewer wears the code-review hat and checks the security points relevant to the change from its checklist.
2. **Extra agents only when the task is of that kind:** the tech lead for a real design problem or a decision record; the data quality analyst for the v1 comparison and the replay checks; the security reviewer as its own run only for high-tier changes that touch secrets, privileges, personal data or exposure; the technical writer in a batch when documents have drifted, not per change.
3. **The manager (Emma) does small work directly:** tracker rows, issues, ADR text, short documents, pointers. No agent for what takes a few steps.
4. **Check before briefing.** Search the repository, `docs/TRACKER.md` and the open issues for existing work before starting an agent (the #32/#33 duplicate cost about 100k tokens).
5. **Review depth follows the tier (section 5):** Low = a light independent check (a short read and the tests, no full attack); Medium = the full review; High = the full review plus a rehearsal and the security checklist. A re-review after fixes covers only the new commits.
6. **Finish before a session limit.** Agents write progress notes to their branch as they go (a short handover file or commit message) so a killed agent can be resumed or replaced cheaply. After a restart the manager looks for orphaned branches, worktrees and throwaway containers first.
7. **Measure it.** The weekly measures (section 8) include agent tokens per merged change, reported from the agents' own usage, so cost is seen and not guessed.
### 2b. The technical writer keeps the documents current (owner decision 10-10-2026)
The owner wants the documents kept up to date as changes merge. After each merge to `main` the manager passes the writer a list of the merges since the last batch. The writer then updates `docs/RESUME.md`, `docs/TRACKER.md`, `CHANGELOG.md` and the affected runbooks within one working day, working from merged code, git history and GitHub issues (not from agents' reports), and does not close issues. The writer may edit RESUME and TRACKER for this purpose; a light independent check precedes the merge of each batch. Documents must not quote test counts.
How to brief each role (the common preamble, role scopes, skills and the proof each must return): `docs/TEAM_BRIEFS.md`.

## 3. How work flows (small and often)
1. **Ready:** an issue and a tracker row exist (issue first, then the row), with a "done when" line.
2. **Build:** one agent, one branch, one worktree (`.worktrees/<name>`). Characterise today's behaviour first, then change only the defect.
3. **Check:** unit tests, the full integration suite in UTC and Australia/Sydney, ruff, mypy, the validators, size budget.
4. **Review:** a different agent reviews the branch (section 4).
5. **Merge:** fast-forward to `main` only after an approved review; read CI on `main`. **Main must stay green:** do not push to `main` until the change's database tests have passed; open a pull request so CI runs on the branch first (owner OK 10-10-2026). The repo token cannot merge pull requests, so the manager fast-forwards `main` locally once the pull request's CI is green.
6. **Deploy:** per the tier (section 5). Verify with the smoke test, panel check, restart counts, identity check and polling gap. Roll back if any fails.
7. **Close:** the issue is closed with the evidence; the owner closes anything they asked to close themselves.
Work in progress limit: at most 3 code branches in flight at once; docs-only branches do not count towards the limit (owner decision 10-10-2026). Finish and merge before starting another. Releases are small and frequent; there is no weekly batch.

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
- Never run `db/roles.sql` by hand on the live database; never run tests or throwaway databases on the production VM without memory, swap, CPU and time caps and cleanup. **One test container at a time on the live VM, and only while the load average is below 2.0** (check `uptime` first; incident 10-10-2026, issue #113: several at once pushed the load to about 24 and two snapshots timed out). Memory caps alone are not enough.
- Never deploy, restart live services, send messages to the owner's real Telegram, close issues or delete branches, volumes or data without the approval the tier requires.
- Never skip hooks or checks, force-push, or amend commits. Never edit applied migrations; add a new numbered one.
- Stay inside the assigned files. Do not edit `docs/TRACKER.md` or `docs/RESUME.md` from a branch (the manager updates them), except the technical writer when the manager's brief says so (section 2b).
- Report honestly: what was done, what failed, what was not verified.

## 8. Measures (weekly, kept by the manager in the digest)
Deploys per week; time from a change being ready to being live; share of deploys that needed a rollback; time to fix a failed deploy. Plus the open count of issues by tier, and agent tokens per merged change (from the agents' own usage reports; the manager's own conversation is not counted). These replace opinions about "too slow" or "too risky".

## 9. Approval
| Item | Decision |
|---|---|
| Team in section 2 | agreed by the owner in chat, 09-10-2026 |
| Tier table in section 5 | APPROVED as written by the owner, 09-10-2026 |
| Low tier deploys without a further yes once #104 exists | APPROVED by the owner, 09-10-2026 (effective only after #104 exists and is proven) |
| Daily digest and default answers (section 6) | APPROVED by the owner, 09-10-2026: a short daily digest, recommendation first; a default is taken after two working days only for Low-tier or pre-agreed questions, never for Medium, High, retention or data |
