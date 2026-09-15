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

# Take a backup of the live database.
#
# Run from a throwaway postgres image rather than the api container: the api
# image carries no client tools, and pinning the image to the server's major
# version avoids the version-skew that silently produces an unrestorable dump.
# Set PG_MAJOR to match your managed instance.
PG_MAJOR ?= 16
backup:
	@test -n "$$DATABASE_URL" || { echo "DATABASE_URL must be set"; exit 1; }
	docker run --rm -e DATABASE_URL="$$DATABASE_URL" postgres:$(PG_MAJOR) \
		pg_dump "$$DATABASE_URL" --format=custom > backup-$$(date +%F).dump
	@echo "wrote backup-$$(date +%F).dump — now verify it: make verify-restore file=backup-$$(date +%F).dump"

# A backup you have not restored is not a backup. This restores one into a
# throwaway database and checks it is not hollow -- the tables that would
# matter come back non-empty, and the cash ledger still balances. It drops the
# scratch database afterwards and never touches the live one.
#   make verify-restore file=backup-2026-09-15.dump
verify-restore:
	@test -n "$(file)" || { echo "usage: make verify-restore file=<dump>"; exit 1; }
	DATABASE_URL="$${DATABASE_URL}" ops/verify_restore.sh "$(file)"

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
