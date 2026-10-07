# Handover for the next agent session

Written 07-10-2026 at the end of the first build session. Read this first, then `SPEC.md` and `docs/DECISIONS.md`.
British English, dates DD-MM-YYYY, no emoji (the owner's standing rules).

## 1. The goal (the owner's words, set as a session goal)
"I want to be able to reproduce these graphs and have them live and available 24/7. I want the system to be dockerised."

The "graphs" are the owner's Metabase charts of VATSIM pilots and controllers in the VATPAC (Australia/Pacific) region. v3 rebuilds them as a small, dockerised system (Postgres 16 + a Python collector + Grafana provisioned from git).
A stop hook keeps firing because 24/7 is not yet proven. Do not claim success until section 4 is done.

## 2. State at hand-over

| Item | State |
|---|---|
| Repo | `t288matt/vatpac-statsV2` (private), branch `main`. Local clone: `C:\Users\mdora\Claude\vatpac-statsV2`. Last pushed commit before this file: `0aac76b` |
| Local stack (this PC, Docker Desktop) | Running since about 19:32 UTC on 07-10-2026: db, collector, grafana, backup, watchdog healthy; Grafana at http://localhost:3300. Soak report was INCOMPLETE (about 1.8 h of 24 h). This PC is NOT a valid 24/7 host |
| VM (always-on target) | Proxmox VM "100", a clone of the owner's v1 box. SSH alias on this PC: `ssh vatpac-v3` (user `matt`, key `~/.ssh/vatpac_ubuntu`, docker group, `sudo` needs a password). Ubuntu 24.04, 4 cores, 7.8 GB RAM, ~197 GB disk. Wiped clean of v1 on 07-10-2026 (see section 6) and v3 deployed from commit `0aac76b` into `~/vatpac-statsV2` with its own `.env`. Grafana bound to the VM's LAN address on port 3300. **Its verification result is NOT recorded here: run the checks in section 5 first.** |
| Tests | 326 passed, 1 skipped (194 unit + 94-plus integration against a real Postgres 16.4), ruff and mypy clean |
| Dashboards | 8 dashboards, 61 panel queries, all run through Grafana's API with 0 errors (about 45 to 47 return rows; the rest are empty because the data is young) |
| v1 | Owner's old system on the original VM. **Do not touch it.** It is read-only reference only |

## 3. What v3 is (one paragraph)
`collector` polls `https://data.vatsim.net/v3/vatsim-data.json` every 15 s (the feed's own cadence), keeps pilots inside the Australian boundary polygon and controllers on the 254-entry callsign whitelist (both in `config/`), and writes one transaction per snapshot: `pilot_session`, `pilot_position` (daily partitions, 90-day retention), `controller_session`, `network_snapshot`, `poll_log`. Flights are an incrementally maintained table `flight_summary` (refreshed every 5 min). Grafana reads views only through the read-only role `grafana_ro`. A `backup` container dumps nightly; a `watchdog` container checks health and alerts (Telegram) but cannot restart anything (by the owner's choice). Full detail: `docs/ARCHITECTURE.md`, `docs/DATA_DICTIONARY.md`, `docs/DEFINITIONS.md`, `docs/CONFIG_MAP.md`.

## 4. What is outstanding (in priority order)
1. **Verify the VM deployment** (section 5). If healthy, this is where the 24/7 clock starts. The VM must stay up and the soak must run there.
2. **24-hour soak on the VM:** `make soak` (or `sh ops/soak_report.sh 24`) must say PASS. It says INCOMPLETE until 24 h exist. Do a reboot test too (`sudo reboot` needs the owner's sudo password; ask them), confirm everything returns by itself.
3. **GitHub Actions CI has never run.** The agent cannot see Actions on a private repo. Ask the owner what the Actions tab shows and fix failures. Known to-dos in the workflows: actions are pinned by tag with `# TODO pin to SHA` (SHAs could not be looked up offline).
4. **Telegram alerts** are only logged: the owner must put `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in the VM's `.env` (never in git), then prove delivery with a test.
5. **Retention charts** (active controllers, lapsed 30 days, retention rate, reactivated after 60+ days, "who might need help reactivating", "who reactivated") are UNVERIFIED. The owner will export their definitions from the live Metabase. Port them exactly into `db/migrations` (views) and `grafana/dashboards/atc/atc-retention.json`, and update `docs/CHART_DEFINITIONS.md`. Screenshots show the layout only; the numbers can't be derived (178/(178+28) is not 84%).
6. **Open owner decisions:** (a) a retention or anonymisation rule for names and CIDs (kept forever today; backups contain them; ADR-009); (b) whether the backup container should stop holding the owner DB password by adding a dedicated restore role (ADR-017). Movements are decided: same as v1 (ADR-018).
7. **Later, owner-managed:** Cloudflare exposure and any Docker-API/socket-proxy so the watchdog can restart unhealthy containers. Neither is in the repo by design (ADR-012). Real names are visible on the dashboards: put a login in front of any public exposure and have the owner check VATSIM's data-use policy first.
8. **Deploy-key / git on the VM:** the VM has no git credentials. The code was copied with `git archive HEAD | ssh vatpac-v3 'tar -x ...'`. A proper deploy key (read-only) would allow `git pull` there.

## 5. Commands you will need
Run from the repo root.
```bash
# VM state (alias in ~/.ssh/config on the owner's PC)
ssh vatpac-v3 'cd ~/vatpac-statsV2 && docker compose ps'
ssh vatpac-v3 'cd ~/vatpac-statsV2 && sh ops/smoke.sh'          # expects SMOKE PASS
ssh vatpac-v3 'cd ~/vatpac-statsV2 && python3 ops/check_panels.py'   # every panel query via Grafana's API; 0 errors expected
ssh vatpac-v3 'cd ~/vatpac-statsV2 && sh ops/soak_report.sh 24'      # PASS only for a full, gap-free 24 h
curl -s http://<VM LAN IP>:3300/api/health                       # from the owner's PC
```
Deploy a new commit to the VM (no credentials needed on the VM):
```bash
git archive --format=tar HEAD | ssh vatpac-v3 'cd ~/vatpac-statsV2 && tar -x'    # keeps the VM's .env (not in git)
ssh vatpac-v3 'cd ~/vatpac-statsV2 && sh ops/check_env.sh && docker compose up -d --build'
```
The VM's `.env` was generated there by `sh ops/gen_env.sh .env` (random passwords; Grafana admin password is `GRAFANA_ADMIN_PASSWORD`; read it on the VM, do not paste it into chat). Local `.env` on this PC is separate and git-ignored.

Run the whole test suite on a scratch Postgres (the integration tests TRUNCATE; the database name must end in `_test`):
```bash
docker run -d --name vp-test -p 127.0.0.1:55436:5432 -e POSTGRES_PASSWORD=x postgres:16.4-bookworm
docker exec vp-test psql -U postgres -c "CREATE DATABASE vatpac_test"
for f in db/migrations/*.sql; do sed '/^-- migrate:down/,$d' "$f" | docker exec -i vp-test psql -U postgres -d vatpac_test -v ON_ERROR_STOP=1 -q; done
docker exec -i vp-test psql -U postgres -d vatpac_test -v ON_ERROR_STOP=1 -v grafana_ro_password=testpw -v collector_password=cpw -v backup_password=bpw -q < db/roles.sql
export VATPAC_TEST_DSN=postgresql://postgres:x@127.0.0.1:55436/vatpac_test VATPAC_TEST_GRAFANA_RO_DSN=postgresql://grafana_ro:testpw@127.0.0.1:55436/vatpac_test
collector/.venv/Scripts/python.exe -m pytest tests collector/tests -q        # expect 326 passed, 1 skipped
cd collector && ./.venv/Scripts/python.exe -m ruff check src && ./.venv/Scripts/python.exe -m mypy src
python ops/check_size_budget.py collector/src/vatpac && python ops/check_config_drift.py && python grafana/validate_dashboards.py
docker rm -f vp-test
```
`collector/.venv` is git-ignored; recreate it with Python 3.11+ and `pip install -r collector/requirements.txt pytest pytest-asyncio ruff mypy`.

## 6. What was done to the VM (so you do not repeat or undo it by accident)
The owner asked for "VM 100 wiped clean and used". It was a Proxmox clone of the v1 box, so it presented the SAME SSH host key as the original (verified by fingerprint) and started the cloned v1 stack on boot. On 07-10-2026 I removed: all containers (v1 app, v1 Postgres, a monitor container that pushed "OK" to the owner's Uptime Kuma push monitor and could have masked a real outage on the original box, and a Tailscale container with a cloned identity), all images and both v1 volumes, the v1 folders in the home directory, and the user crontab (v1's twice-daily DB backups with remote transfer). The OS, the `matt` user, SSH keys and `get-docker.sh` were left alone. The stale `known_hosts` entries for that IP on this PC were replaced. Nothing on the original v1 box was touched.
If you re-create the VM, check for these clone side effects again (cron, Tailscale identity, monitor pushes).

## 7. Decisions (full list: `docs/DECISIONS.md`, ADR-001 to ADR-018)
No frequency matching and no sector matching (so about 21 of the 57 live v1 charts are intentionally dropped: `docs/DASHBOARDS_SIMPLIFIED.md`); plain Postgres (no TimescaleDB); Grafana as code; 15 s polling aligned to the feed timestamp; every sample stored, raw positions kept 90 days; sessions keyed `(cid, callsign, logon_time)`; flight = dep and arr non-empty (same as v1, no minimum samples); filters kept: pilot polygon + controller whitelist (fail closed); callsign-pattern and frequency filters removed (they were inactive in v1); names/CIDs stored and shown as v1; REST data API retired (health only); no backfill (charts start empty at go-live); watchdog alert-only; movements same as v1 (includes parked connections, about 12% of home-region departures); least-privilege DB roles; the collector package size budget is 1400 lines (ADR-014).

## 8. Repo map
`SPEC.md` (authority) · `docs/` (ARCHITECTURE, DATA_DICTIONARY, DEFINITIONS, CONFIG_MAP, CHART_DEFINITIONS, DASHBOARDS_SIMPLIFIED, DECISIONS, RUNBOOKS/, `reference/v1_metabase_cards.json` = the recovered SQL of all 118 v1 Metabase cards, 57 live) · `collector/src/vatpac/` · `db/migrations/` (dbmate, forward-only) + `db/roles.sql` · `grafana/` (provisioning + dashboards) · `ops/` (check_env, gen_env, smoke, check_panels, soak_report, check_*.py, backup/, migrate/, watchdog/) · `tests/` and `collector/tests/` · `.github/workflows` · `.claude/agents/*.md` (role definitions: data-engineer, analytics, sre, qa, reviewer, docs-writer) · `config/` (polygon, whitelist, and `australian_airspace_sectors.geojson` which v3 does not read, kept in case sector work returns).

## 9. Gotchas that cost time (learn from them)
- **Line endings:** files written on Windows can contain CRLF; Linux containers then fail (`set: illegal option -`). `.gitattributes` forces LF in git, but local working copies need `sed -i 's/\r$//'`. Never run Python `splitlines()` on a file that may contain a bare CR (it splits the line). Writing a literal backslash-r through the shell tool got mangled several times; use `chr(92)` or avoid it.
- **Edit tool needs a prior Read of the file**; a Grep does not count. Files written by sub-agents count as unread.
- **`$$` inside `sh -c '...'` is expanded to a PID.** Use `chr(NN)` or quote differently in SQL passed through `docker compose exec ... sh -c`.
- **dbmate sends a file as one command:** two `CREATE INDEX CONCURRENTLY` in one migration fail; use one per file with `-- migrate:up transaction:false`.
- **Grafana datasource password** comes from the env var `GRAFANA_RO_DB_PASSWORD` (not `GRAFANA_RO_PASSWORD`). A mismatch makes the datasource fail with "password authentication failed"; only running the stack reveals it.
- **`docker kill` is treated as a manual stop** (not auto-restarted); a real crash (OOM) is. `docker compose up -d` after any kill.
- **Passwords are placed in connection URLs:** letters and digits only (`ops/check_env.sh` enforces).
- **Postgres `trim()` strips spaces only:** the flight gate uses `btrim(x, E' \t\r\n')`.
- **Docker Desktop on this PC** needed a manual start; it is fine for development only.
- **The auto-mode classifier occasionally fails to give a verdict** on a command; retry once, then do other work. Some live-database reads against the owner's v1 box were blocked once and allowed after the owner confirmed.
- **Sub-agents** (general-purpose type) hit a usage limit once and were cut off mid-work; check what is on disk, then restart them with "resume" prompts. Give each its own directories to avoid collisions; agents must not commit (the lead commits and pushes).
- **Do not paste or print secrets** (`.env`, tokens). Do not commit `.env`, real dumps, real names/CIDs in fixtures (fixtures are anonymised: names `Pilot NNNN`, CIDs 9000000 to 9999999; `ops/check_fixture_pii.py` enforces), or internal IP addresses.

## 10. Owner profile and working rules (from their global instructions)
Matt wants thorough, direct answers; warm tone; British English; DD-MM-YYYY dates; multi-step work outlined first (he asked for plan mode earlier and then approved "execute the plan"). He prefers new commits (never amend), commits only when asked (this project: he approved building and pushing to `main`). Telegram, not email, for notifications. He decided: Cloudflare and the Docker socket proxy are his to do later; the VM details are above; v1 is his to manage.

## 11. First five actions for the next session
1. `ssh vatpac-v3 'cd ~/vatpac-statsV2 && docker compose ps && sh ops/smoke.sh'`; if anything is unhealthy, read `docker compose logs <service>` and fix.
2. Run `python3 ops/check_panels.py` on the VM; confirm Grafana is reachable on the LAN from the owner's PC.
3. Start the 24 h clock: note the time, then run `sh ops/soak_report.sh 24` after 24 h. Record the verdict in `docs/ARCHITECTURE.md` (the verified table) and commit.
4. Ask the owner for: the Actions tab status, Telegram token/chat id (into the VM `.env`), the Metabase export, and the two open decisions (section 4 item 6).
5. Only when 24 h has passed on the VM, the reboot test is done and CI is green: tell the owner the goal is met, with the soak output as evidence.
