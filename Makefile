.PHONY: soak up deploy down dev logs ps config test lint smoke size backup backup-test restore gen-env check-env drift

# Local builds. Refuses to start with blank/weak secrets (ops/check_env.sh). No .env yet? run: make gen-env
# Refuses when VATPAC_IMAGE_PREFIX is set (environment or .env): compose would build locally and tag the
# build with the registry name, which defeats the pull. In registry mode use: make deploy
up: check-env
	@mode="$$(sh ops/image_mode.sh .env)"; \
	if [ "$$mode" != local ]; then \
	  echo "make up: VATPAC_IMAGE_PREFIX is set (environment or .env), so this is registry mode." >&2; \
	  echo "make up would build locally and retag the build with the registry name. Use 'make deploy' to pull and start," >&2; \
	  echo "or unset VATPAC_IMAGE_PREFIX to build locally." >&2; exit 1; \
	fi
	docker compose up -d --build

# Registry mode (images pulled, never built): VATPAC_IMAGE_PREFIX is set once in .env; the tag defaults to latest, so no .env
# edit per deploy. Order: fast-forward the checkout, pull, check the pulled images were built from this checkout, only then up.
# Run the release workflow for the commit first; if its images are not published yet this stops before touching anything running.
# On a detached checkout (after a rollback) run `git checkout main` first.
deploy: check-env
	@mode="$$(sh ops/image_mode.sh .env)"; \
	if [ "$$mode" != registry ]; then \
	  echo "make deploy: VATPAC_IMAGE_PREFIX is not set (environment or .env); mode is: $$mode." >&2; \
	  echo "Use 'make up' for local builds." >&2; exit 1; \
	fi
	docker compose config --images
	git pull --ff-only
	docker compose pull
	sh ops/check_pulled_images.sh
	docker compose up -d --no-build

# db published on 127.0.0.1:55432 (dev overlay). Builds locally, so it refuses in registry mode too.
dev: check-env
	@mode="$$(sh ops/image_mode.sh .env)"; \
	if [ "$$mode" != local ]; then \
	  echo "make dev: VATPAC_IMAGE_PREFIX is set (environment or .env); the dev overlay builds locally." >&2; \
	  echo "Unset it to use make dev." >&2; exit 1; \
	fi
	docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build

down:
	docker compose down

logs:
	docker compose logs -f --tail=100

ps:
	docker compose ps

# write .env from .env.example with random passwords (never prints them; refuses to overwrite)
gen-env:
	sh ops/gen_env.sh

check-env:
	sh ops/check_env.sh .env

# Validates the compose files with a generated throwaway env: .env.example has blank secrets by
# design, so compose cannot be validated against it.
config:
	@tmp="$$(mktemp)"; rm -f "$$tmp"; \
	sh ops/gen_env.sh "$$tmp" >/dev/null && \
	docker compose --env-file "$$tmp" config -q && \
	docker compose --env-file "$$tmp" -f docker-compose.yml -f docker-compose.dev.yml config -q; \
	rc=$$?; rm -f "$$tmp"; exit $$rc

lint:
	cd collector && ruff check . && mypy src
	ruff check --config ops/ruff.toml ops

# collector settings vs compose vs .env.example
drift:
	python ops/check_config_drift.py

test:
	PYTHONPATH=collector/src pytest -q -m "not integration" tests collector/tests --ignore=tests/.venv

smoke:
	sh ops/smoke.sh

size:
	python ops/check_size_budget.py

# take a backup now
backup:
	docker compose exec backup backup.sh once

# restore the newest backup into a scratch DB and sanity-check it
backup-test:
	docker compose exec backup restore-test.sh

# Safe restore: restores into <TARGET>_restoring, checks it, then swaps (old DB kept as <TARGET>_old).
# make restore FILE=/backups/vatpac_20261007T020000Z.dump [TARGET=vatpac_restore] CONFIRM=<same as TARGET>
# Replacing the live DB: stop collector/grafana/watchdog first, TARGET=<POSTGRES_DB> (see docs/RUNBOOKS/restore.md)
TARGET ?= vatpac_restore
CONFIRM ?=
restore:
	docker compose exec -e RESTORE_CONFIRM=$(CONFIRM) backup restore.sh $(FILE) $(TARGET)

HOURS ?= 24
soak:
	sh ops/soak_report.sh $(HOURS)
