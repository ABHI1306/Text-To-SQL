# Text-to-SQL Platform

A local-first Text-to-SQL platform for PostgreSQL. The model is intended to produce
logical SQL; the semantic compiler and validator must be the only route to warehouse
execution. The Day 1 repository currently contains foundation code and placeholders,
not the complete product.

## Local setup

Requirements: `uv`, Docker Compose (or a compatible Compose provider), Node.js 22+, npm,
and Ollama for local model testing.

```bash
cp .env.example .env
uv sync
```

The `.env.example` values are development-only placeholders. Before starting, generate
a strong `JWT_SECRET` (at least 32 characters) and a Fernet key for
`CONNECTION_ENCRYPTION_KEY`, then set both in `.env`:

```bash
openssl rand -hex 32
uv run -- python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Also set a temporary `BOOTSTRAP_DBA_PASSWORD` with at least 12 characters. Replace the
database placeholder passwords before sharing the machine or exposing any port to a
network. Do not commit `.env`. Non-development API startup refuses missing or invalid
JWT/encryption secrets.

The bootstrap command creates the initial local DBA account and forces a password
change at first sign-in. After it succeeds, remove `BOOTSTRAP_DBA_PASSWORD` from `.env`
and recreate the API container. Create/reset local accounts with `make user-create
USERNAME=analyst` and `make user-reset-dba USERNAME=analyst`; both commands prompt for
the temporary password without echoing it.

Then start the services, apply migrations, and create the first DBA:

```bash
docker compose --env-file .env up -d --build
docker compose --env-file .env --profile tools run --rm migrate
make user-bootstrap-dba
```

The Compose network is internal to the local stack. The Compose-managed Ollama volume
starts empty; model weights are not downloaded automatically. The smoke harness below
uses the host Ollama service, which is separate from Compose Ollama. A model ending in
`-cloud` is not suitable because it sends inference to an external service.

Check the API:

```bash
curl http://localhost:8000/health
```

The demo database is initialized with e-commerce and HR sample schemas only when its
volume is first created. If initialization fails, inspect the logs; only remove/recreate
the named demo volume if you intentionally want to discard its contents.

## Checks

```bash
make check
cd ui
npm ci
npm run generate:api
npm run test
npm run build
npm run test:e2e
```

Live database permission tests run in the Compose test service after migrations:

```bash
cd ..
docker compose --env-file .env --profile test run --rm tests
```

The API and worker containers are on the internal Compose network. After the stack is
running, the Day 1 outbound-connectivity check is:

```bash
./scripts/test-egress.sh
```

## UI

```bash
cd ui
npm run generate:api
npm run dev
```

To use the mock account and fixtures instead, run:

```bash
npm run dev:mock
```

Open <http://127.0.0.1:4173>. The Vite dev server defaults to the real local API at
`http://127.0.0.1:8000`; set `VITE_API_PROXY_TARGET` to override it. The mock account,
database catalog, and model descriptions are fixtures, not real authentication or
database access. The OpenAPI contract is in `docs/openapi.yaml`.

## Day 1 model smoke check

With a local Ollama server and model installed, run:

```bash
uv run -- python scripts/model_bakeoff.py --models llama3:latest --limit 10
```

Results are written to `reports/day1-model-smoke.json`. This is a prompt/schema smoke
test only; execution accuracy cannot be measured until the semantic compiler and
validator are implemented. Current status and limitations are recorded in
`docs/DECISIONS.md`.

## Current Day 1 scope

Implemented foundations include the seven ports, a PostgreSQL read-only adapter,
metadata migration, local demo seed data, `/health`, a UI shell using a mock API,
OpenAPI-generated TypeScript types, and CI checks. The target demo machine has been
accepted as Tier A-class, below the plan's Tier B reference. The second-reviewer
checkpoint remains open. The schema smoke results do not by themselves justify a final
model selection; see
`docs/DECISIONS.md` and the dated reports in `reports/`. Execution accuracy is a later
evaluation gate and requires the compiler path and golden set.

## Day 2 progress

The semantic-model domain, read-only catalog profiling, encrypted project connection
storage, setup APIs, and local authentication are implemented. The UI includes
first-login/password-change flows and a setup wizard to test a connection, select and
profile tables, and validate/save an editable MDL. Refresh tokens are rotated in an
HttpOnly cookie; access tokens remain in browser memory. Profiling samples redact
common sensitive columns. Local Ollama description generation still needs to be run
against an installed model, and the DBA should review/correct descriptions for five
important tables before Day 2 is marked complete.
