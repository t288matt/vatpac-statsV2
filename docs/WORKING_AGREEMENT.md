# Working agreement

Status: APPROVED by the owner in chat on 09-10-2026 (section 9).The rules in `docs/RESUME.md` section 4 still apply where this document is silent.
Language: British English. Dates DD-MM-YYYY, times UTC. Plain words; no emoji.

## 1. Purpose
Small, frequent, safe releases of VATPAC Stats, with the owner deciding what matters and not having to read everything. The aim is to remove the owner as the bottleneck without removing the safety: risk decides how much approval a change needs, and evidence replaces summaries.

## 2. The team
The job description of every role is in `TEAM_ROLES.md` (a Claude working file, kept outside the repo); how to brief each role is in `TEAM_BRIEFS.md` (a Claude working file, kept outside the repo). The table below is the short form.
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
### 2b. The technical writer keeps the documents current (owner decisions 10-10-2026)
The documents describe what is deployed and running in production, for maintainers (how to run, check, back up, restore, deploy and fix) and users (what each dashboard shows). Merged work that is not yet deployed is listed only in `CHANGELOG.md` and `docs/RESUME.md`, and the prepared document changes wait in `docs/PENDING_DOC_CHANGES.md`. The trigger is a deploy: after each deploy is VERIFIED LIVE, the writer switches in the prepared changes and updates `docs/RESUME.md`, `docs/TRACKER.md`, `CHANGELOG.md` and the affected runbooks within one working day, working from merged code, git history and GitHub issues (not agents' reports). The writer does not close issues and may edit RESUME and TRACKER for this purpose. A light independent check precedes each merge. Documents must not quote test counts.
How to brief each role (the common preamble, role scopes, skills and the proof each must return): `docs/TEAM_BRIEFS.md`.

## 3. How work flows (small and often)
1. **Ready:** an issue and a tracker row exist (issue first, then the row), with a "done when" line.
2. **Build:** one agent, one branch, one worktree (`.worktrees/<name>`). Characterise today's behaviour first, then change only the defect.
3. **Check:** unit tests, ruff, mypy, the validators and the size budget locally; the full integration suite in UTC and Australia/Sydney runs in CI on the pull request (and on the VM only under the container rule in section 10).
4. **Review:** a different agent reviews the branch (section 4).
5. **Merge:** open a pull request so CI runs on the branch first. Merge only after an approved independent review AND green CI on the pull request's LATEST commit, then read CI on `main`. Merge checklist (the manager pastes the links): pull request open; CI green on the head commit (link); review verdict (link); Behaviour preserved section for Medium and High; the fix names a test that fails if it is reverted; tracker row and issue updated. A push to `main` from the manager's clone is blocked by a local hook unless the checks are green (the owner adds the hook; until then the manager checks by hand). The repo token cannot merge pull requests, so the manager fast-forwards `main` locally once the pull request's CI is green.
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
| Low | Documentation; tests; dashboard query or layout fixes that keep every other panel identical; comment-only migrations; alert wording; scripts that are not deployed | Independent review plus green CI. Deployed only on the owner's go, like every other change. |
| Medium | Collector code; new views or columns that keep existing ones identical; watchdog logic; CI and release workflows | Independent review, behaviour-preserved proof, then merge on a green pull request; the owner's go is for the deploy. |
| High | `roles.sql` and grants; backup, restore and the off-VM copy; destructive or irreversible migrations; retention settings; anything that deletes data; secrets | Independent review, a rehearsal on a throwaway database, a fresh backup, and the owner's explicit go for each deploy. |
Every production deploy needs the owner's explicit go, whatever the tier; tiers decide review depth and rehearsal only, and the go message carries the three-line evidence. A deploy always starts with a fresh dump and a rollback copy.

## 6. Decisions
- The manager brings decisions to the owner one at a time with a recommendation first, or in a short daily digest ("N things need a yes"). Each has a default taken if the owner does not answer within two working days, only for tier Low or for questions the owner has pre-agreed defaults for.
- Owner-only decisions: priorities; retention and personal data; anything irreversible; spending; what "good enough" means (the uptime, gap and wrong-number targets, once the owner has set them).

## 7. Limits on every agent
- Never print, store or commit secrets; never enter credentials; never expose Grafana publicly.
- Never run `db/roles.sql` by hand on the live database; never run tests or throwaway databases on the production VM without memory, swap, CPU and time caps and cleanup. **One test container at a time on the live VM, and only while the load average is below 2.0** (check `uptime` first; incident 10-10-2026, issue #113: several at once pushed the load to about 24 and two snapshots timed out). Memory caps alone are not enough. (Section 10 repeats this rule.)
- Never deploy, restart live services, send messages to the owner's real Telegram, close issues or delete branches, volumes or data without the approval the tier requires.
- Never skip hooks or checks, force-push, or amend commits. Never edit applied migrations; add a new numbered one.
- Stay inside the assigned files. Do not edit `docs/TRACKER.md` or `docs/RESUME.md` from a branch (the manager updates them), except the technical writer when the manager's brief says so (section 2b).
- Report honestly: what was done, what failed, what was not verified.

## 8. Measures (kept by the manager in the digest)
Weekly: agent tokens per merged change (from the agents' own usage reports; the manager's own conversation is not counted) and the open count of issues by tier. Monthly, in one line: deploys, time from ready to live, share of deploys rolled back, time to fix a failed deploy. These replace opinions about "too slow" or "too risky".

## 9. Approval
| Item | Decision |
|---|---|
| Team in section 2 | agreed by the owner in chat, 09-10-2026 |
| Tier table in section 5 | APPROVED as written by the owner, 09-10-2026 |
| Daily digest and default answers (section 6) | APPROVED by the owner, 09-10-2026: a short daily digest, recommendation first; a default is taken after two working days only for Low-tier or pre-agreed questions, never for Medium, High, retention or data |

## 10. Standing rules (owner decisions, 10-10-2026 unless dated)
- Every production deploy needs the owner's explicit go: ONE go per deploy. The manager runs the deploy after the go, with `ops/deploy.sh --preflight` before and `--verify` after; DevOps builds and maintains the script and is called in when a check fails. Ship the exact commit named in the plan (deploys 1 and 2: 520dc3e, never the tip of main).
- A merge needs an independent review plus green CI on the pull request's latest commit; the owner's single gate is the deploy go. Main must stay green: no push to `main` until the change's database tests have passed; open a pull request so CI runs on the branch first; read CI after every push to `main`.
- Never pin, skip or hide a product defect to get green: fix it or do not merge.
- Every change gets an independent review by a different agent than the author. Blind review: the reviewer gets the issue and the diff, not the builder's report. Every fix names a test that fails if the fix is reverted. (Docs-only changes that state no operational fact: section 4.)
- ONE test container at a time on the live VM, only with the load average below 2.0 (`uptime`) and MemAvailable above 3000 MB, capped at 512 MB memory, 512 MB swap and 1 CPU, named so it does not start with `vatpac-`, removed afterwards (incident #113).
- New work gets a GitHub issue first, then a tracker row. Closing an issue is ask-first (the owner closes, or says so). In pull requests write `Refs #N`, never `Fixes #N`, so GitHub does not close an issue by accident.
- Designs are tested against the real problem and sized against the pain they remove (the #104 lesson); ask for the smallest version first.
- No new test infrastructure: the restore-test extension, weekly replay, a test VM and staging were dropped.
- Opus 5.5 only as a second pass for complex database and code changes (owner told first), never for alerting or docs; Sonnet 5.5 for everything else.
- Agent reports stay under 300 words, with detail in files. One agent run fits in one session; split bigger work before briefing.
- Incidents: [INCIDENTS.md](INCIDENTS.md).
- Documents describe what is deployed and running, and are updated after each deploy is verified live (docs/PENDING_DOC_CHANGES.md holds the prepared changes).
- British English, dates DD-MM-YYYY, times UTC, no emoji.
