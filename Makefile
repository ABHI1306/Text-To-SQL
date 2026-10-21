.PHONY: install check test lint format audit secrets user-bootstrap-dba user-create user-reset-dba eval redteam rls drills e2e offline day9 init-env up down logs migrate models bootstrap reindex rotate-key
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

# Known-vulnerability scan of the locked Python and UI dependencies (needs network).
audit:
	uv run -- pip-audit
	cd ui && npm audit --audit-level=high

# Scan the working tree and git history for committed secrets (needs the gitleaks binary).
secrets:
	gitleaks detect --source . --redact --no-banner

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
# INCLUDE_DRAFT=1 scores the draft references too: a provisional number, and the gate still fails.
eval:
	test -n "$(PROJECT_ID)" -a -n "$(USERNAME)" || (echo "Set PROJECT_ID and USERNAME"; exit 2)
	mkdir -p reports
	docker compose --env-file .env up -d --build api   # the golden set is baked into the image
	docker compose --env-file .env exec -T api uv run --no-sync python -m scripts.evaluate_execution \
		--project-id "$(PROJECT_ID)" --username "$(USERNAME)" $(if $(INCLUDE_DRAFT),--include-draft) > "$(EVAL_OUT)"
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

# ---- Day 10: run the stack -------------------------------------------------------------------
# GPU=1 adds the NVIDIA overlay; IDENTITY=1 adds OpenLDAP and Keycloak (needs the secrets in
# docs/RUNBOOK.md and `sh scripts/ldap/make-certs.sh`).
COMPOSE = docker compose --env-file .env -f docker-compose.yml \
	$(if $(GPU),-f docker-compose.gpu.yml) \
	$(if $(LANGSMITH),-f docker-compose.langsmith.yml) \
	$(if $(IDENTITY),-f docker-compose.ldap.yml -f docker-compose.oidc.yml --profile identity-demo)
SQL_MODEL_NAME = $(shell sed -n 's/^SQL_MODEL=//p' .env)
EMBEDDING_MODEL_NAME = $(shell sed -n 's/^EMBEDDING_MODEL=//p' .env)
SMALL_MODEL_NAME = $(shell sed -n 's/^SMALL_MODEL=//p' .env)

init-env:
	./scripts/init-env.sh

# Build and start everything, then apply migrations.
up:
	test -f .env || (echo "No .env: run 'make init-env' first"; exit 2)
	$(COMPOSE) up -d --build --wait
	$(COMPOSE) --profile tools run --rm migrate

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=100 api worker

migrate:
	$(COMPOSE) --profile tools run --rm migrate

# Model weights are never downloaded automatically. This pulls the ones named in .env.
models:
	$(COMPOSE) exec ollama ollama pull "$(SQL_MODEL_NAME)"
	$(COMPOSE) exec ollama ollama pull "$(EMBEDDING_MODEL_NAME)"
	test -z "$(SMALL_MODEL_NAME)" || $(COMPOSE) exec ollama ollama pull "$(SMALL_MODEL_NAME)"

# Create the first DBA from BOOTSTRAP_DBA_USERNAME / BOOTSTRAP_DBA_PASSWORD, then clear the password.
bootstrap:
	$(COMPOSE) run --rm api uv run --no-sync python -m app.cli.users bootstrap-dba
	sed -i 's/^BOOTSTRAP_DBA_PASSWORD=.*/BOOTSTRAP_DBA_PASSWORD=/' .env
	$(COMPOSE) up -d --force-recreate api

# Rebuild a project's search index from its deployed model (after changing EMBEDDING_MODEL).
reindex:
	test -n "$(PROJECT_ID)" || (echo "Set PROJECT_ID"; exit 2)
	$(COMPOSE) run --build --rm api uv run --no-sync python -m app.cli.maintenance reindex --project-id "$(PROJECT_ID)"

# After putting the new key in CONNECTION_ENCRYPTION_KEY: make rotate-key OLD=<previous key>
# Add SKIP_UNREADABLE=1 to leave connections the old key cannot read (already unusable).
rotate-key:
	test -n "$(OLD)" || (echo "Set OLD to the previous CONNECTION_ENCRYPTION_KEY"; exit 2)
	$(COMPOSE) run --build --rm -e OLD_CONNECTION_ENCRYPTION_KEY="$(OLD)" api \
		uv run --no-sync python -m app.cli.maintenance rotate-connection-key $(if $(SKIP_UNREADABLE),--skip-unreadable)
	$(COMPOSE) up -d --force-recreate api worker
