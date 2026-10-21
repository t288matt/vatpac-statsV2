# ops/audit: the two files the watchdog uses

`independent.py` and `invariants.sql` are baked into the watchdog image (`ops/check_pulled_images.sh` checks them) and are
the only files kept here. The one-off scripts of the original data-integrity audit (T30, 08-10-2026; see
`docs/DATA_INTEGRITY_AUDIT.md`) were removed on 21-10-2026 under issue #229 because nothing referenced them and the
ones that mattered had been overtaken (for example `poll_phase.py` was written for 15-second polling). They are in git
history: the commit that removed them is the one to `git show` or `git checkout <commit>^ -- ops/audit/<file>` from.

| File | Purpose | Touches |
|---|---|---|
| `independent.py` | USED BY THE WATCHDOG (14-day archive cross-check). Own point-in-polygon (exact rational arithmetic, no shapely), whitelist, near-miss, frequency, caps; `analyse(payload)` recomputes the expected rows from a raw feed payload with none of the collector's code. | nothing (pure functions) |
| `invariants.sql` | USED BY THE WATCHDOG (3-day sweep). Read-only SQL pack (one repeatable-read transaction): the integrity invariants including the identity-flap detector. | live DB, read only |

Lint: `ruff check --config ops/ruff.toml ops/audit`.
