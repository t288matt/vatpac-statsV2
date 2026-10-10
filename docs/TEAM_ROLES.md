# Team roles

DRAFT for the owner's approval, written 10-10-2026. What each job is, who does it and how the work flows. The rules the team works under are in [WORKING_AGREEMENT.md](WORKING_AGREEMENT.md) (section 10, Standing rules); how to brief each role is in [TEAM_BRIEFS.md](TEAM_BRIEFS.md).

The team is the owner, the manager, a builder and an independent reviewer. Specialists are called in only when a task needs them. Roles are hats, not permanent staff.

## 1. How the team works
1. **Issue.** Every defect, finding or work item gets a GitHub issue and then a tracker row (docs/TRACKER.md).
2. **Plan.** Larger or multi-step work is outlined first and approved; the tech lead measures the problem and proposes the smallest design that removes it.
3. **Build.** One builder, one branch, one worktree. The builder characterises today's behaviour, changes only what is needed, and shows with tests and comparisons that nothing else changed.
4. **Independent review.** A different agent attacks the change and gives a verdict.
5. **Pull request and CI.** The branch goes through a pull request so CI runs on it; the change is merged to `main` when the review and CI on the latest commit are both green.
6. **Deploy.** The manager asks the owner for the go (one go per deploy), then runs the deploy script's checks, ships the agreed commit and runs the verification. DevOps builds and maintains the script and steps in when a check fails.
7. **Verify.** Smoke test, panel check, restart counts and a watch period on the live system.
8. **Docs.** The technical writer updates the documents so they describe what is now deployed.

### Who decides, does, reviews
D = decides, A = does the work, R = reviews or checks, I = informed.

| Activity | Owner | Manager | Builder | Reviewer | Specialist |
|---|---|---|---|---|---|
| Priorities, retention, personal data, spending | D | A (proposes) | I | | R (tech lead) |
| Building a change | I | R (checks evidence) | A | | A (database, DevOps, QA as the hat needed) |
| Review and merge | I | A (merges when review and CI are green) | | A (review) | R (security, when secrets or privileges are touched) |
| Production deploy | D (one go) | A (runs the checks) | | | A (DevOps builds the script, fixes a failed check) |
| Documents after a verified deploy | I | R | | R (light check) | A (technical writer) |

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
**What she does.** Triages work, keeps the issue list and the tracker, chooses the risk tier, briefs and sequences the other roles, checks the evidence (commits, CI, counts) before telling the owner something is done, runs each deploy (after the owner's go) with the deploy script's checks, and writes the daily digest. Does small jobs herself (tracker rows, issues, short documents).
**Owns or produces.** docs/TRACKER.md, GitHub issues, briefs, the digest, deploy requests.
**Works with.** Takes priorities from the owner; hands builds to the engineers, reviews to the reviewer, documents to the writer.
**Skills and tools.** gh, git worktrees, ssh to the VM (read-only checks), `ops/deploy.sh`, scheduled tasks, the calendar and email for the owner.
**Model.** Haiku 4.5 for routine scheduled work, Sonnet 5.5 for the rest.

### Builder (collector, database, DevOps or QA hat)
**Purpose.** Makes one change at a time, and shows that nothing else changed.
**What they do.** Take an issue, characterise today's behaviour, change only what is needed, write tests (one of them fails if the fix is reverted), compare old and new on the same data, open a pull request and report briefly with a Behaviour preserved section.
**Owns or produces.** The branch and pull request, the tests, the report. Hats: collector (`collector/src/vatpac`), database (`db/migrations`, views, grants, partitions), DevOps (`ops/`, compose, CI, backup, the watchdog and the deploy script), QA (replay, fuzz and characterisation tests in `tests/`).
**Works with.** Takes an issue from the manager (or a design from the tech lead); hands the branch to the reviewer.
**Skills and tools.** Python 3.12, SQL, shell, pytest, ruff, mypy, dbmate, Docker Compose, the replay tool, CI.
**Model.** Sonnet 5.5; Opus 5.5 as a second pass only for complex database and code changes, owner told first.

### Independent code reviewer
**Purpose.** A second pair of eyes that tries to break each change before it reaches production.
**What they do.** Get the issue and the diff (not the builder's report), re-run the tests from a clean checkout, attack the riskiest assumption, name the test that fails if the fix is reverted, and give a verdict (approve, approve with fixes, do not merge) with ranked findings. Always a different agent from the builder.
**Owns or produces.** Review records in `docs/reviews/`.
**Works with.** Takes a branch via the manager; hands the verdict to the builder and the manager.
**Skills and tools.** code-review, engineering:code-review, git archive, CI.
**Model.** Sonnet 5.5; Opus 5.5 as a second pass only for complex database and code changes, never for alerting or docs, owner told first.

### Specialists (called in when a task needs them)
- **Tech lead / architect.** Measures a problem, lays out options with their size, recommends the smallest design that removes it, drafts the decision record. Skills: engineering:architecture, engineering:system-design. Sonnet 5.5.
- **Security reviewer.** Checks changes that touch secrets, database roles, backups, the archive, logs or exposure, and reports ranked findings with a scenario and a fix. Skill: security-review. Sonnet 5.5.
- **Data quality analyst.** Runs the integrity checks, compares v3 with v1 (predictions written first), recomputes panels from raw tables, explains every difference; read-only SQL. Skills: data:analyze, data:validate-data. Sonnet 5.5.
- **Technical writer.** Keeps ARCHITECTURE.md, README.md, INSTALL.md, the runbooks, CHANGELOG.md and PENDING_DOC_CHANGES.md true. The documents describe what is deployed; after each deploy is verified live the writer switches in the prepared changes and updates RESUME.md and TRACKER.md (with the manager's brief). Skill: engineering:documentation. Sonnet 5.5.
