.PHONY: up down dev logs ps config test lint smoke size pii backup backup-test restore

up:
	docker compose up -d --build

# db published on 127.0.0.1:55432 (dev overlay)
dev:
	docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build

down:
	docker compose down

logs:
	docker compose logs -f --tail=100

ps:
	docker compose ps

config:
	docker compose config -q

lint:
	cd collector && ruff check . && mypy src

test:
	PYTHONPATH=collector/src pytest -q -m "not integration" tests collector/tests --ignore=tests/.venv

smoke:
	sh ops/smoke.sh

size:
	python ops/check_size_budget.py

pii:
	python ops/check_fixture_pii.py tests/fixtures

# take a backup now
backup:
	docker compose exec backup backup.sh once

# restore the newest backup into a scratch DB and sanity-check it
backup-test:
	docker compose exec backup restore-test.sh

# make restore FILE=/backups/vatpac_20261007T020000Z.dump [TARGET=vatpac_restore]
restore:
	docker compose exec backup restore.sh $(FILE) $(TARGET)
