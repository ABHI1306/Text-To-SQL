.PHONY: install check test lint format

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
