# Work tracker

Single source of truth for open work on the v3 goal. Update it in the same commit as the change it describes.
Times are UTC. Statuses: TODO, IN PROGRESS, BLOCKED (owner), WAITING (time), DONE.

Last updated: 08-10-2026 08:00 UTC

| ID | Item | Status | Detail / next step |
|----|------|--------|--------------------|
| T1 | 24 h gap-free soak on the VM | WAITING | Clock restarted by the reboot test (one 45 s gap). Earliest PASS about 21:50 UTC on 08-10-2026. Check: `sh ops/soak_report.sh 24` on the VM. |
| T2 | VM reboot test | DONE | 07-10-2026 21:48 UTC: all five containers returned unaided, smoke passes, 0 panel errors, 0 restarts. |
| T3 | CI green on main | DONE | 07-10-2026: lint fixed in 6e3d131, size-budget fixed in 7f5641a; owner confirmed the newest ci run on main is green. |
| T4 | Telegram alert delivery | DONE | 07-10-2026: credentials set on the VM, watchdog recreated, test alert accepted by Telegram (HTTP 200) and confirmed received by the owner. Accepted risk (owner, 07-10-2026): the bot token appeared in a session transcript via a debug script and was not rotated; rotate via BotFather if in doubt. |
| T5 | Retention and reactivation charts | IN PROGRESS | 08-10-2026: live Metabase found on the owner's docker host; card SQL exported (161 cards, ids to 186). Real definitions differ from the v3 assumptions (active = session in last 30 days; reactivated = session in last 30 days and none 30-90 days ago; help = all recent sessions under 18 min; lapsed 30/60/90+ bands). Rebuild pending, see T13. |
| T6 | Names/CIDs retention rule | DONE | 07-10-2026: owner chose to keep names and CIDs forever, no rule (ADR-019). |
| T7 | Deploy 6e3d131 and 7f5641a to the VM | WAITING | Do after T1 passes (a redeploy would break the soak). Method: `git archive HEAD \| ssh vatpac-v3 'cd ~/vatpac-statsV2 && tar -x'` then `docker compose up -d --build`. |
| T8 | Read-only deploy key so the VM can `git pull` | TODO | Optional; needs the owner to add the key in GitHub settings. |
| T9 | Triage seven Dependabot PRs | TODO | Hold until main is green. Grafana 11 to 13 needs dashboard testing first. Review action bumps before merging. |
| T10 | Pin workflow actions to commit SHAs | TODO | See TODO notes in `.github/workflows/ci.yml`. Also move off Node 20 action versions. |
| T11 | Record soak evidence in docs/ARCHITECTURE.md | TODO | Only after T1, T2, T3 and T4 are all met; then tell the owner the goal is met. |
| T12 | Data retention rules for all data (LOW priority) | TODO | Owner request 07-10-2026: define retention for every data set, not only names/CIDs (ADR-019 covers those). Scope to cover: position snapshots, sessions, poll log, near-miss and reference-change records, flights archive, backups (count, age, location) and Grafana's own data. Needs a decision per data set (keep, age out, or aggregate), a new ADR, and numbered migrations for any purge job. Disk growth figures from the soak will help size it. No deadline. |
| T13 | Rebuild controller retention dashboard from the real v1 SQL | TODO | 11 cards (13 minus two duplicates): active, retention rate, lapsed 30/60/90+, reactivated count and list, help list, S1 cohort, activity levels, engagement index, activity trends. New migration plus Grafana panels plus fixture tests. Deploy after the soak. |
| T14 | Re-check 19 v1 cards edited after the 28-09-2025 snapshot | TODO | Repo definitions were copied from the older SQL: 41, 43, 55, 61, 63, 66, 78, 93, 94, 95, 98, 108, 116, 129, 131, 133, 134, 135, 137. Card 55 (movements) first: ADR-018 may rest on the old definition. |
| T15 | Decide and build the other cards created after the snapshot (ids 140-186) | TODO | About 30 candidates after excluding duplicates (178, 179), sector charts (159, 160, still dropped per decision 1) and v1 internal integrity (147): pilot onboarding and retention (3), military RAAFv (4), management (2), ATC providers and session lengths (5), pilots (155), Monday (156), 4 h in 8 h window (186). Owner picks scope. |

## Goal-met checklist (verbatim gate)
- [ ] Soak PASS (T1)
- [x] Reboot test (T2)
- [x] CI green (T3)
- [x] Test alert delivered (T4)
- [ ] Evidence recorded and pushed (T11)
