# Prompt for the next agent session (written 09-10-2026, evening)

Copy everything below the line into a new Claude Code session.

---

You are continuing the VATPAC Stats v3 project with me (Matt). Start here:

1. Work in C:\Users\mdora\Claude\vatpac-statsV2 (the v3 repo). NOT the folder "VATPAC-Stats V2" (the old v1 system: read-only, never touch it, never delete it).
2. Run `git fetch` and `git status -sb` first. Then read docs/RESUME.md (from origin/main if the local copy is behind), then docs/TRACKER.md (the map of GitHub issues is at the top) and docs/RUNBOOKS/deploy-to-vm.md. Check CI on main with "C:\Program Files\GitHub CLI\gh.exe" run list --repo t288matt/vatpac-statsV2 --branch main --limit 3.
3. Use a git worktree under .worktrees/ for any work. Other sessions switch the shared folder's branch.

STATE (09-10-2026 evening): the stack is deployed and healthy on the VM (commit 8905d85 plus later docs-only commits). The Dependabot image bumps are live and verified (Postgres 16.15, Grafana 11.6.5, dbmate 2.36.0; issue #31 closed). CI runs on postgres 16.15 and is green. Issues #45 to #57, #33 and #24 are closed. A power cut on 09-10-2026 (issue #76, tracker T51) was recovered with no corruption; its three open items are in the tracker. Rollback copies are on the VM (`~/vatpac-statsV2.rollback-09-10-bumps`, Docker volume `vatpac_grafana_data_pre_bumps`): delete after about a week of clean running.

YOUR FIRST JOB: #20, make the data-integrity checks permanent (building blocks in ops/audit/). Issue #32 needs nothing from you: its comment-only migration (20261009000150) is already on main (tracker T52) and applies at the next deploy; close #32 then, after checking `SELECT obj_description('movements'::regclass,'pg_class')` returns the new text.

THEN, in this order (use agents):
1. #21 define data retention rules for every data set: inventory sizes and growth first, then ask me the decisions one at a time, then write an ADR.
2. #27 VM performance baseline and CPU steal check (read-only).
3. #18 fault injection (database down, feed blocked, recovery and alerts): it adds a deliberate gap, so ask me for a time first.
4. #25 (re-time cards 186 and 156 once there is more data) and #16 (card 65, parked: ask me first). #22: look at CI after 19-10-2026 (runner image moves to Ubuntu 26; pin ubuntu-24.04 if a job goes red).
5. Read the weekly restore-test result (first run Sunday 11-10-2026, docs/RESTORE_DRILL.md) and record it in the tracker. Check the next scheduled dump completed after the power cut.

THINGS ONLY I CAN DO (instructions in docs/OWNER_ACTIONS.md): #44 protect main in GitHub, #26 Proxmox backup restore test, #29 Metabase host cleanup, #23 folder rename (last, with no Claude session open in either folder), #28 deploy key (I recommend skipping), plus the UPS decision from #76 and checking the VatpacV3Heartbeat task is running again. Remind me once, briefly.

Also offer to delete the old wip-backup/* and finished agent branches and worktrees (ask first; delete only branches that show 0 commits ahead of main).

HOW I WORK (follow these):
- Do every task you can do yourself, using background agents for execution (each in its own worktree, with the capped VM test script described in RESUME.md). Do not hand me work you can do. Keep me in the conversation for decisions.
- Ask me questions with the AskUserQuestion tool: concrete options, say exactly what will change, one recommendation first. One decision at a time.
- Plain language. Explain with a real example from the data. If you find you were wrong, say so openly.
- British English, dates DD-MM-YYYY, times in UTC, no emoji, new commits only (never amend).
- Evidence over assertion: show exact test counts and CI results. CI only runs on pushes to main and on PRs, so review agent-built branches yourself, then fast-forward main and read CI there. If the permission system blocks a push or a deploy, stop and tell me; do not work around it.
- Never print or copy secrets. Never run db/roles.sql on the live cluster. Every throwaway container on the VM needs --memory and --memory-swap caps, a CPU cap and a timeout. Deploy only when I say.
- The VM is `ssh vatpac-v3`, user matt, no sudo, no git there; the compose project is `vatpac`.

Start by telling me in a few lines what you found (CI state, deployed commit, anything unexpected), then begin #20 (outline first and wait for my approval).
