# Work tracker

Single source of truth for open work on the v3 goal. Update it in the same commit as the change it describes.
Times are UTC. Statuses: TODO, IN PROGRESS, BLOCKED (owner), WAITING (time), DONE.

Last updated: 07-10-2026 22:55 UTC

| ID | Item | Status | Detail / next step |
|----|------|--------|--------------------|
| T1 | 24 h gap-free soak on the VM | WAITING | Clock restarted by the reboot test (one 45 s gap). Earliest PASS about 21:50 UTC on 08-10-2026. Check: `sh ops/soak_report.sh 24` on the VM. |
| T2 | VM reboot test | DONE | 07-10-2026 21:48 UTC: all five containers returned unaided, smoke passes, 0 panel errors, 0 restarts. |
| T3 | CI green on main | IN PROGRESS | `lint` fixed in 6e3d131, `size-budget` fixed in 7f5641a. Owner to confirm the newest `ci` run is green; other jobs unverified until then. |
| T4 | Telegram alert delivery | BLOCKED (owner) | Needs a bot token and chat id in the VM's `.env`. Alerts are only logged today. Then send a test alert. |
| T5 | Retention and reactivation charts | BLOCKED (owner) | Unverified. Needs the Metabase export to compare against. |
| T6 | Names/CIDs retention rule | BLOCKED (owner) | Owner decision needed; recommendation to be given when asked. |
| T7 | Deploy 6e3d131 and 7f5641a to the VM | WAITING | Do after T1 passes (a redeploy would break the soak). Method: `git archive HEAD \| ssh vatpac-v3 'cd ~/vatpac-statsV2 && tar -x'` then `docker compose up -d --build`. |
| T8 | Read-only deploy key so the VM can `git pull` | TODO | Optional; needs the owner to add the key in GitHub settings. |
| T9 | Triage seven Dependabot PRs | TODO | Hold until main is green. Grafana 11 to 13 needs dashboard testing first. Review action bumps before merging. |
| T10 | Pin workflow actions to commit SHAs | TODO | See TODO notes in `.github/workflows/ci.yml`. Also move off Node 20 action versions. |
| T11 | Record soak evidence in docs/ARCHITECTURE.md | TODO | Only after T1, T2, T3 and T4 are all met; then tell the owner the goal is met. |

## Goal-met checklist (verbatim gate)
- [ ] Soak PASS (T1)
- [x] Reboot test (T2)
- [ ] CI green (T3)
- [ ] Test alert delivered (T4)
- [ ] Evidence recorded and pushed (T11)
