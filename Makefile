.PHONY: install check test lint format user-bootstrap-dba user-create user-reset-dba
ROLE ?= DBA

install:
	uv sync

check:
	uv run -- ruff check app migrations scripts tests
	uv run -- mypy app
	uv run -- lint-imports
	uv run -- pytest -q

test:
	uv run -- pytest -q

lint:
	uv run -- ruff check app migrations scripts tests

format:
	uv run -- ruff format app migrations scripts tests

api-types:
	cd ui && npm run generate:api

user-bootstrap-dba:
	docker compose --env-file .env run --rm api uv run --no-sync python -m app.cli.users bootstrap-dba

user-create:
	test -n "$(USERNAME)" || (echo "Set USERNAME"; exit 2)
	docker compose --env-file .env run --rm api uv run --no-sync python -m app.cli.users create "$(USERNAME)" --role "$(ROLE)"

user-reset-dba:
	test -n "$(USERNAME)" || (echo "Set USERNAME"; exit 2)
	docker compose --env-file .env run --rm api uv run --no-sync python -m app.cli.users reset-dba "$(USERNAME)"
