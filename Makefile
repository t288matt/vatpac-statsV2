.PHONY: up down logs ps config test lint smoke restore

up:
	docker compose up -d --build

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
	cd collector && pytest -q

smoke:
	sh ops/smoke.sh

restore:
	sh ops/backup/restore.sh $(FILE)
