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
docker compose --env-file .env up -d --build
docker compose --env-file .env --profile tools run --rm migrate
```

The `.env.example` values are development-only placeholders. Replace them before
sharing the machine or exposing any port to a network. Do not commit `.env`.

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

## UI against the mock API

```bash
cd ui
npm run generate:api
npm run dev:mock
```

Open <http://127.0.0.1:4173>. The local mock account and project are fixtures, not real
authentication or database access. The OpenAPI contract is in `docs/openapi.yaml`.

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
