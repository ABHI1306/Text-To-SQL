.PHONY: install check test lint format user-bootstrap-dba user-create user-reset-dba eval redteam rls drills e2e offline day9
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

# ---- Day 9: every suite, one target each ------------------------------------------------------
PROJECT_ID ?=
USERNAME ?=
EVAL_TARGET ?= 0.70
EVAL_OUT ?= reports/eval-$(shell date +%Y%m%d).json

# D9-2: the golden set through the real pipeline (needs the stack, Ollama and Qdrant running),
# then the accuracy gate. Fails below EVAL_TARGET or when the references are unapproved drafts.
eval:
	test -n "$(PROJECT_ID)" -a -n "$(USERNAME)" || (echo "Set PROJECT_ID and USERNAME"; exit 2)
	mkdir -p reports
	docker compose --env-file .env exec -T api uv run --no-sync python scripts/evaluate_execution.py \
		--project-id "$(PROJECT_ID)" --username "$(USERNAME)" > "$(EVAL_OUT)"
	uv run -- python scripts/eval_gate.py "$(EVAL_OUT)" --target $(EVAL_TARGET)

redteam:
	uv run -- pytest -q tests/test_redteam.py tests/test_compiler_hardening.py tests/test_day8_security.py

rls:
	uv run -- pytest -q tests/test_rls_suite.py tests/test_database_permissions.py

# D9-4 failure drills and D9-5 metrics tests
drills:
	uv run -- pytest -q tests/test_day9_drills.py tests/test_day9_observability.py tests/test_eval_gate.py

# D9-1 Playwright flows per role (against the UI mock API)
e2e:
	cd ui && npm run test:e2e

# D9-3 the whole stack with outbound traffic blocked
offline:
	./scripts/offline-test.sh

# Everything the Day 9 exit gate names, in the order of the plan's primary prompt.
day9: check redteam rls drills e2e offline eval
