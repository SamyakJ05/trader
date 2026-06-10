.PHONY: setup up down logs migrate makemigration seed test lint fmt psql redis api-shell reset-db

setup: ## copy env file
	cp -n .env.example .env || true
	@echo "Edit .env, then run: make up && make migrate && make seed"

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f api worker

migrate:
	docker compose exec api alembic upgrade head

makemigration:
	docker compose exec api alembic revision --autogenerate -m "$(m)"

seed:
	docker compose exec api python -m app.seeds.seed

test:
	docker compose exec api pytest -q

lint:
	docker compose exec api ruff check app tests

fmt:
	docker compose exec api ruff format app tests

psql:
	docker compose exec db psql -U trader trader

redis:
	docker compose exec redis redis-cli

api-shell:
	docker compose exec api bash

reset-db:
	docker compose down -v
	docker compose up -d --build
	sleep 5
	docker compose exec api alembic upgrade head
	docker compose exec api python -m app.seeds.seed
