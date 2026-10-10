# Team roles

DRAFT for the owner's approval, written 10-10-2026. What each job is, who does it and how the work flows. The rules the team works under are in [WORKING_AGREEMENT.md](WORKING_AGREEMENT.md) (section 10, Standing rules); how to brief each role is in [TEAM_BRIEFS.md](TEAM_BRIEFS.md).

Roles are hats, not permanent staff: a normal change gets one builder and one independent reviewer, and the same agent can wear different hats on different days.

## 1. How the team works
1. **Issue.** Every defect, finding or work item gets a GitHub issue and then a tracker row (docs/TRACKER.md).
2. **Plan.** Larger or multi-step work is outlined first and approved; the tech lead measures the problem and proposes the smallest design that removes it.
3. **Build.** One builder, one branch, one worktree. The builder characterises today's behaviour, changes only what is needed, and shows with tests and comparisons that nothing else changed.
4. **Independent review.** A different agent attacks the change and gives a verdict.
5. **Pull request and CI.** The branch goes through a pull request so CI runs on it; the change is merged to `main` when review and CI are both green.
6. **Deploy.** The manager asks the owner for the go; DevOps runs the pre-deploy checks, ships the agreed commit and runs the verification.
7. **Verify.** Smoke test, panel check, restart counts and a watch period on the live system.
8. **Docs.** The technical writer updates the documents so they describe what is now deployed.

### Who decides, does, reviews
D = decides, A = does the work, R = reviews or checks, I = informed.

| Activity | Owner | Manager | Tech lead | Builder (collector, database, DevOps, QA) | Independent reviewer | Security reviewer | Data analyst | Writer |
|---|---|---|---|---|---|---|---|---|
| Priorities, retention, personal data, spending | D | A (proposes) | R | I | | | | I |
| Design of a larger change, decision record | D | R | A | I | | R | | I |
| Building a change | I | R (checks evidence) | | A | | | | |
| Review of a change | I | R (checks it happened) | | | A | R (when secrets, privileges or personal data are touched) | | |
| Merge to main | D for medium and high changes | A | | | R | | | |
| Production deploy | D (explicit go) | A (runs the checks) | | A (DevOps) | R | | I | I |
| Verifying a deploy live | I | A | | A (DevOps) | | | R | I |
| Updating documents after a deploy | I | R | | | R (light check) | | | A |
| Data correctness and the v1 comparison | D (decisions) | R | | | | | A | I |
| Incident response | D | A | R | A | | | R | A (record) |
| Closing an issue | D | I | | | | | | |

## 2. Roles

### Owner (Matt)
**Purpose.** Decides what the system is for and what is safe enough; the person the system serves.
**What he does.** Sets priorities, makes the data, privacy, retention and spending decisions, answers questions put to him one at a time, gives the go for each production deploy, and closes issues. Does the few steps only he can do (the Proxmox backup test, the backup copy set-up on the Proxmox host, his own PC).
**Owns or produces.** Decisions (recorded as decision records and tracker entries) and the go for each deploy.
**Works with.** The manager hands him short questions with a recommendation and a three-line evidence summary; he hands back decisions.
**Skills and tools.** Chat, the daily digest, Grafana.
**Model.** Not applicable.

### Manager (Emma: engineering manager and executive assistant)
**Purpose.** Keeps the work moving and the owner informed with the least effort for him.
**What she does.** Triages work, keeps the issue list and the tracker, chooses the risk tier, briefs and sequences the other roles, checks the evidence (commits, CI, counts) before telling the owner something is done, prepares and runs the checks around a deploy, and writes the daily digest. Does small jobs herself (tracker rows, issues, short documents).
**Owns or produces.** docs/TRACKER.md, GitHub issues, briefs, the digest, deploy requests.
**Works with.** Takes priorities from the owner; hands builds to the engineers, reviews to the reviewer, documents to the writer.
**Skills and tools.** gh, git worktrees, ssh to the VM (read-only checks), `ops/deploy.sh`, scheduled tasks, the calendar and email for the owner.
**Model.** Haiku 4.5 for routine scheduled work, Sonnet 5.5 for the rest.

### Tech lead / architect
**Purpose.** Finds the smallest design that solves the real problem.
**What they do.** Measure the problem first, lay out the options with their trade-offs and size, recommend one, and write the decision record for the owner.
**Owns or produces.** Decision record drafts in docs/DECISIONS.md and design notes.
**Works with.** Takes a problem from the manager; hands an approved design to the builders.
**Skills and tools.** engineering:architecture and engineering:system-design, EXPLAIN and timings on a throwaway database.
**Model.** Sonnet 5.5; Opus 5.5 for a genuinely complex database or code design.

### Collector engineer
**Purpose.** Keeps the collector that reads the VATSIM feed correct and running.
**What they do.** Change the code that fetches, parses, filters and stores the feed, keep the health endpoint and metrics right, and prove with replays of saved feed data and malformed input that behaviour is unchanged except for the fix.
**Owns or produces.** `collector/src/vatpac`, `collector/tests`, branches and pull requests, a Behaviour preserved section in each report.
**Works with.** Takes an issue from the manager or a design from the tech lead; hands the branch to the independent reviewer.
**Skills and tools.** Python 3.12, pytest, ruff and mypy, the replay tool in `tests/tools/`, a throwaway Postgres.
**Model.** Sonnet 5.5; Opus 5.5 for complex ingest changes.

### Database engineer
**Purpose.** Makes the data correct, fast and safe in PostgreSQL.
**What they do.** Write migrations, views, grants and partition maintenance, tune queries against the 30 second dashboard limit, and compare old and new views row for row.
**Owns or produces.** `db/migrations`, `db/roles.sql`, the SQL behind dashboards.
**Works with.** Takes issues and designs; hands the branch to the reviewer; supplies the data analyst with views.
**Skills and tools.** SQL, EXPLAIN, dbmate, a throwaway Postgres 16.15, data:write-query and data:validate-data.
**Model.** Sonnet 5.5; Opus 5.5 for complex migrations.

### DevOps / reliability engineer
**Purpose.** Keeps the stack running, backed up and restorable, and makes deploys safe.
**What they do.** Maintain the compose stack, CI and release workflows, backup and restore, the watchdog and alerting, and the deploy procedure and checks; carry out a deploy when the owner has given the go.
**Owns or produces.** `ops/`, `docker-compose.yml`, `.github/workflows/`, the deploy record and rollback copy.
**Works with.** Takes issues and the deploy plan from the manager; hands deploy results to the manager and the data analyst.
**Skills and tools.** Docker Compose, shell, `ops/deploy.sh`, ssh to the VM, GitHub Actions, engineering:deploy-checklist and incident-response.
**Model.** Sonnet 5.5.

### QA / test engineer
**Purpose.** Builds tests that do not share the author's blind spots.
**What they do.** Write characterisation, replay and fuzz tests, check which test fails when a fix is reverted, and report any product defect they find as an issue.
**Owns or produces.** Tests and fixtures in `tests/`, counts before and after.
**Works with.** Takes a change or a specification; hands defects to the manager and tests to the reviewer.
**Skills and tools.** pytest, the fuzz generators and replay fixtures, engineering:testing-strategy.
**Model.** Sonnet 5.5.

### Independent code reviewer
**Purpose.** A second pair of eyes that tries to break each change before it reaches production.
**What they do.** Read the diff, re-run the tests from a clean checkout, attack the riskiest assumption, and give a verdict (approve, approve with fixes, do not merge) with ranked findings. Always a different agent from the author.
**Owns or produces.** Review records in `docs/reviews/`.
**Works with.** Takes a branch from a builder via the manager; hands the verdict back to the builder and the manager.
**Skills and tools.** code-review, engineering:code-review, git archive, a throwaway Postgres.
**Model.** Sonnet 5.5 by default; Opus 5.5 as a second pass for complex database and code changes, with the owner told first.

### Security reviewer
**Purpose.** Checks that secrets, privileges and personal data stay where they belong.
**What they do.** Review changes that touch secrets, database roles, backups, the archive, logs or exposure, and report findings with a concrete scenario and a fix.
**Owns or produces.** Security findings, ranked.
**Works with.** Takes a branch from the manager; hands findings to the builder and reviewer.
**Skills and tools.** security-review, the role privilege matrix, the secret scan output.
**Model.** Sonnet 5.5.

### Data quality analyst
**Purpose.** Shows that the numbers on the dashboards match the raw data and v1.
**What they do.** Run the integrity checks, compare v3 with v1 on overlapping hours (predictions written first), recompute panels from the raw tables, and explain every difference.
**Owns or produces.** The integrity pack and the comparison records (docs/V1_COMPARISON.md).
**Works with.** Takes a question from the manager; hands findings to the database engineer and the owner.
**Skills and tools.** Read-only SQL on the live database and on v1, data:analyze, data:validate-data, data:explore-data.
**Model.** Sonnet 5.5.

### Technical writer
**Purpose.** Keeps the documents true for the people who run and use the system.
**What they do.** Write and update the documents from the deployed code, git history and GitHub issues. The documents describe what is deployed and running; after each deploy is verified live they switch in the prepared changes and update the handover, tracker and changelog.
**Owns or produces.** docs/ARCHITECTURE.md, README.md, docs/INSTALL.md, docs/RUNBOOKS/, CHANGELOG.md, docs/PENDING_DOC_CHANGES.md; with the manager's brief also docs/RESUME.md and docs/TRACKER.md.
**Works with.** Takes the list of verified deploys from the manager; hands documents to a light independent check.
**Skills and tools.** engineering:documentation, git, gh, read-only ssh to the VM.
**Model.** Sonnet 5.5.
