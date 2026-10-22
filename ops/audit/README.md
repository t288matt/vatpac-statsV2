# ops/audit: the three files the watchdog uses

`independent.py`, `invariants.sql` and `identity_flap.sql` are baked into the watchdog image (`ops/check_pulled_images.sh`
checks them) and are the only files kept here. `identity_flap.sql` is also run by `ops/deploy.sh --verify`.
The one-off scripts of the original data-integrity audit (T30, 08-10-2026; the written record is kept locally, not in this repository) were removed on 21-10-2026 under
issue #229 because nothing referenced them and the ones that mattered had been overtaken (for example `poll_phase.py` was written for 15-second polling). They are in git
history: the commit that removed them is the one to `git show` or `git checkout <commit>^ -- ops/audit/<file>` from.

| File | Purpose | Touches |
|---|---|---|
| `independent.py` | USED BY THE WATCHDOG (14-day archive cross-check). Own point-in-polygon (exact rational arithmetic, no shapely), whitelist, near-miss, frequency, caps; `analyse(payload)` recomputes the expected rows from a raw feed payload with none of the collector's code. | nothing (pure functions) |
| `invariants.sql` | USED BY THE WATCHDOG (3-day sweep). Read-only SQL pack (one repeatable-read transaction): the integrity invariants including the identity-flap detector. | live DB, read only |
| `identity_flap.sql` | USED BY THE WATCHDOG (every cycle, last 24 hours) and by `ops/deploy.sh --verify` (whole history). The one definition of "two sessions of one `cid` and callsign less than 2 s apart"; two rows (pilot, controller), every `bad` should be 0. Can be run by hand (see the file header). | live DB, read only |

Lint: `ruff check --config ops/ruff.toml ops/audit`.
