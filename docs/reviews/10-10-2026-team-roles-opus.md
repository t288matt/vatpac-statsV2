# Review: team design against standard agile and DevOps practice

Date: 10-10-2026. Reviewer: Opus 5.5 (engineering-management hat). Read-only review of `docs/TEAM_ROLES.md` and `docs/WORKING_AGREEMENT.md` sections 2 to 10 on branch `docs-roles`, plus a check of `.github/workflows/ci.yml`. No other files read, no tests run.

## Summary

The design is sound in spirit: one decision maker, author and reviewer kept apart, small frequent releases, evidence over assertion, a work-in-progress limit. Its weakness is the opposite of what one would expect from a small team: it copies the shape of a ten-person organisation (eleven roles, a RACI table, a three-tier change policy, weekly DORA measures) while missing three cheap mechanical guards that would have prevented today's problems. Judgement-heavy process is being used where a checklist or a script would do.

Key fact found: CI (`ci.yml`) already runs the integration tests against a Postgres 16.15 service on every pull request, in UTC and in Australia/Sydney. So there is no need for agents to run database tests on the live VM at all. Both of today's problems (main red three times; two live snapshot timeouts from test containers on the VM) come from bypassing a guard that already exists.

## 1. Missing roles or practices that matter here

| Practice | Why it matters for this system | Smallest useful version |
|---|---|---|
| Definition of done / merge checklist | Main went red three times because DB tests were merged unrun. "Done when" lines exist per issue, but there is no common bar for merge. | Five lines in the working agreement: PR open; CI green on the PR head commit (link); review verdict (link); behaviour-preserved section for Medium/High; tracker row updated. The manager pastes the links before merging. |
| Incident routine and short write-up | Incident #113 happened today; the table gives incident response four "A"s and no first responder, severity or write-up rule. | One runbook page: who acts first (manager), stop-the-bleeding steps, when to wake the owner (data loss risk or dashboard down over N minutes), and a ten-line blameless write-up within 24 h (what happened, impact, cause, fix, one prevention action as an issue). |
| A service target (SLO) | Section 6 says the owner sets uptime, gap and wrong-number targets "once" set; they are not set, so no one can say whether a deploy or incident mattered. | Two numbers: polling gap (for example no gap over 5 minutes more than twice a week) and dashboard availability. No error-budget policy; just report against them in the digest. |
| Retrospective | Today produced three lessons; they were captured ad hoc as standing rules. | Five minutes, after each incident or every second week in the digest: keep, stop, try; each "try" becomes an issue or a rule edit. No meeting. |
| Release notes | CHANGELOG exists via the writer; fine. | Leave as is, but have the manager add the one-line entry at deploy time rather than waiting for a writer batch. |
| On-call / outside-hours cover | Alerts go to the owner on Telegram; agents cannot respond unprompted. | Already covered by the "if Claude is unavailable" page and the watchdog. Leave as is; make sure the incident page links to it. |

Not needed: scrum ceremonies, sprints, story points, a separate release manager, a change advisory board, a product owner separate from the owner.

## 2. Over-built for one owner and one VM

1. **Eleven roles and an eight-column RACI table.** Section 2a already admits a change normally gets two agents. The real roles are: owner, manager, builder, reviewer, plus occasional specialists. Merge tech lead, security reviewer, data analyst and writer into "specialist hats the manager may call" with one line each; keep the long job descriptions in TEAM_BRIEFS.md where briefs need them. The full collector/database/DevOps/QA split is a brief scope, not a role.
2. **Low tier "deploys without a yes once #104 exists".** The owner approved only the trimmed #104 (no runner, no automatic rollback) and the standing rule says every deploy needs his go, every time. The clause can never fire; it confuses section 5. Cut it and say: every deploy needs the go; tiers decide review depth and rehearsal only.
3. **Weekly DORA measures.** With roughly ten deploys and no failures, deploy frequency, lead time, change-failure rate and time to restore are noise week to week. Keep one: tokens per merged change (the owner's actual pain), and report the DORA four monthly in one line.
4. **Two owner gates per Medium change.** The RACI gives the owner D for merge to main for Medium and High, and section 5 asks for his yes on evidence, and every deploy needs his go. That is potentially two asks per change. Merge should be decided by review plus green CI; the owner's single gate is the deploy go (with the three-line evidence in the same message).
5. **The planned `Reviewed-by:` CI tripwire.** Worth little when the manager is the only person merging; the merge checklist does the same job. Park it.
6. **Owner closes every issue.** Makes the owner a bottleneck for bookkeeping. The manager should close with an evidence link; the owner closes only issues he raised or asked to close.

## 3. Overlaps and ambiguities

- **Who runs a deploy.** TEAM_ROLES step 6 says DevOps runs checks and ships; the RACI gives both manager and DevOps an "A"; the manager role says she "prepares and runs the checks"; DevOps "carries out a deploy". Pick one: the manager runs `ops/deploy.sh` after the go (it is a scripted, routine act and keeps context in one place); DevOps builds and maintains the script and is called in only when a check fails.
- **How merge happens.** TEAM_ROLES step 5 says PR, then merge when review and CI are green; WORKING_AGREEMENT section 3 step 5 says fast-forward to main after review, then read CI on main. The second is exactly the pattern that turned main red. Section 3 must match section 10 and TEAM_ROLES: CI green on the PR head first.
- **Who may merge.** Manager is "A" for merge, fine; but section 4 lets the manager merge docs-only changes without review while section 10 says every change gets an independent review. State the docs-only exception once, in one place.
- **Incident response** has four doers and no lead (see section 1).
- **Stale text:** section 7 says test containers are allowed on the VM "until #104 moves them off"; trimmed #104 will not move them anywhere.
- Single decision maker: good. Author and reviewer separation: good. These match practice.

## 4. Practices that would have prevented today's problems

| Problem | Absent practice | Cheap fix |
|---|---|---|
| Main red three times | Merge checklist; CI-green-before-merge as a hard rule (branch protection unavailable on the free plan) | Merge checklist (above) plus a local `pre-push` hook in the manager's clone that refuses a push to `main` unless `gh` reports the commit's PR checks green. Size S. |
| Two live snapshot timeouts | No rule against using production as a test bench | Agents run database tests only through CI on a pull request. No test containers on the live VM at all, except a named, owner-approved exception. Section 10's container caps then become the exception rule, not the norm. |
| Over-built designs | Already fixed by the standing rule "size against the real problem". | Leave as is. |
| Lessons lost after restarts | No retrospective / incident write-up | Ten-line write-up and keep/stop/try in the digest. |

WIP limit of 3: right size; leave as is. Release notes: exist.

## 5. AI agent hats: structural risks

1. **Same model family reviewing.** Real risk of shared blind spots, already acknowledged. Mitigations that cost little: give the reviewer the issue and the diff but not the builder's report or reasoning (blind review); require the reviewer to name the one test that fails when the fix is reverted; lean on mechanical checks (CI, replay fixtures, row-for-row comparisons) for the safety case, and treat the review as a second line. #105 (outside review) can stay parked.
2. **Context loss and killed agents.** The handover-note rule in 2a is right. Add: one agent run should fit in one session; anything bigger is split before briefing. The post-restart sweep (worktrees, branches, non-vatpac containers) should be a short script or checklist the manager runs, not memory.
3. **The manager as single point of failure.** The manager triages, briefs, merges, runs deploys, verifies and reports. That concentration is acceptable for a one-owner system only because the owner gives the go on evidence links, not on her summary. Keep the "evidence links, not summaries" rule strict; it is the main control on the manager.
4. **Agents acting on production.** The rule set rightly forbids it; making CI the only place DB tests run removes the most likely accident.
5. **Model choice drift.** TEAM_ROLES lists Opus "for complex ingest changes" in several roles; section 10 says Opus only as a second pass. Align the role text to section 10.

## 6. Prioritised changes (at most eight)

1. **ADD (S): merge checklist plus "CI green on the PR head before merge", backed by a pre-push hook on main in the manager's clone.** Removes: main going red. Recommended first.
2. **CUT (S): database tests on the live VM.** CI already runs them with Postgres in both time zones; agents push a branch and read CI. Removes: snapshot timeouts and the container-cap ritual.
3. **CLARIFY (S): one deploy operator.** Manager runs the script after the go; DevOps owns the script. Removes: two doers on the riskiest step.
4. **CLARIFY (S): owner has one gate per change, the deploy go; merge is review plus green CI; the manager closes issues with evidence.** Removes: double asks and bookkeeping load on the owner.
5. **ADD (S): incident page with first responder, wake-the-owner threshold and a ten-line blameless write-up; each write-up yields one prevention issue.** Removes: ad hoc handling and lost lessons.
6. **CUT (M, docs only): collapse eleven roles to owner, manager, builder, reviewer plus occasional specialists; drop the RACI to five rows; drop the dead Low-tier no-go clause; report DORA monthly, tokens weekly.** Removes: reading load and contradictory text, and token cost of briefing.
7. **ADD (S): two service targets (polling gap, dashboard availability) set by the owner and reported in the digest.** Removes: no way to tell whether a problem matters.
8. **CLARIFY (S): blind review (reviewer gets issue and diff, not the builder's report) and the revert-test rule.** Mitigates same-model blind spots at no extra cost.

Leave as is: WIP limit 3, author/reviewer separation, single decision maker, evidence-before-done, handover notes on branches, docs describe production only, "smallest version first".

## Could not judge

- Whether the pre-push hook would be honoured by every agent session (depends on which clone pushes to main).
- Actual token cost per role, and how often specialist hats are used in practice.
- Whether the owner wants targets at all (a product decision).
- TEAM_BRIEFS.md and DESIGN_SCRIPTED_DEPLOY.md were not read, so any contradictions there are unchecked.
