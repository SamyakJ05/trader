.PHONY: setup up down logs migrate makemigration seed bootstrap-admin test test-integration lint fmt psql redis api-shell reset-db

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

# Create or promote the first operator. Registration is invite-only, so a
# fresh deployment needs this once: make bootstrap-admin email=you@example.com
bootstrap-admin:
	docker compose exec api python -m app.seeds.bootstrap_admin $(email)

test:
	docker compose exec api pytest -q

# The Postgres integration tests (concurrent fills, settlement, ledger
# invariants) SKIP without a database, and they cover the code most likely to
# corrupt money. This runs them against the compose Postgres on a throwaway
# database so a green suite means what it looks like it means.
test-integration:
	docker compose exec -T db psql -U trader -c "DROP DATABASE IF EXISTS trader_test" postgres
	docker compose exec -T db psql -U trader -c "CREATE DATABASE trader_test" postgres
	docker compose exec -e DATABASE_URL=postgresql+asyncpg://trader:trader@db:5432/trader_test api alembic upgrade head
	docker compose exec -e TEST_DATABASE_URL=postgresql+asyncpg://trader:trader@db:5432/trader_test api pytest -q

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
