# Text-to-SQL Platform

A local-first Text-to-SQL platform for PostgreSQL. The Home screen can run a prototype
question-to-SQL flow against a deployed semantic model and the connected PostgreSQL
project. The flow enforces read-only SQL, table/column allowlists, safe function checks,
statement timeouts, and row caps. This is not yet the planned logical-SQL compiler or a
production-ready policy engine.

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

The setup above applies the Day 3 migration. To enable real question results, install
both local Ollama models explicitly, save and deploy the project's semantic model, then
ask a question on Home:

```bash
docker compose --env-file .env exec ollama ollama pull llama3:latest
docker compose --env-file .env exec ollama ollama pull nomic-embed-text:latest
```

`SQL_MODEL` defaults to `llama3:latest` and `EMBEDDING_MODEL` defaults to
`nomic-embed-text:latest`. If you configure different model names in `.env`, pull those
names instead; no model is fetched automatically. In the UI, finish Setup and save the
MDL, open **Connect and model**, and choose **Deploy model**. Then open Home and ask
about the project's data. Start the UI with `npm run dev` against the local API for
PostgreSQL results; `npm run dev:mock` deliberately shows labeled mock preview data.

Check the API:

```bash
curl http://localhost:8000/health
```

The demo database is initialized with e-commerce and HR sample schemas only when its
volume is first created. If initialization fails, inspect the logs; only remove/recreate
the named demo volume if you intentionally want to discard its contents.

## External PostgreSQL sample databases

These projects publish sample data or SQL scripts that you can download and restore into
a PostgreSQL server for additional testing. They are datasets, not guaranteed public
database endpoints:

| Sample database | Example data | Source and compatibility |
| --- | --- | --- |
| Chinook | Digital media store: artists, albums, tracks, invoices, and customers | [Project and downloads](https://github.com/lerocha/chinook-database); choose its PostgreSQL SQL script. |
| AdventureWorks for PostgreSQL | Larger business schema with HR, sales, products, and purchasing | [Project scripts](https://github.com/lorint/AdventureWorks-for-Postgres). |
| Pagila | DVD rental store with films, customers, inventory, rentals, and payments | [Project and scripts](https://github.com/devrimgunduz/pagila). The current release documents PostgreSQL 18+; this project’s Compose databases use PostgreSQL 16, so check the selected Pagila release’s requirements before restoring it. |
| Airlines Demo Database | Airline, airport, and flight sample data | [Download and setup information](https://postgrespro.com/education/demodb). |

For a first external dataset, Chinook is a small, straightforward SQL-script option.
AdventureWorks is a closer match when you want broader business data. The local Compose
demo already includes `customers`, `products`, `orders`, and `inventory`, so an external
dataset is optional.

Restore downloaded data into a local PostgreSQL server and connect with a read-only
database account. Do not expose a database to the internet for this test. To connect a
different server through the Compose API, add only its intended hostname or IP address
to `DB_HOST_ALLOWLIST` in `.env`; the application rejects hosts not on that allowlist.

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

## Day 3 progress

Implemented locally: MDL chunking and version-scoped vector indexing; Ollama embeddings;
Qdrant project/version filters; hybrid retrieval with relationship expansion and column
pruning; a deployment endpoint that indexes before atomically switching the active MDL;
custom-role, user, project-access, and user-attribute APIs; User system-role migration;
sample E-commerce and HR project templates; and a distinct React Flow Connect and model
canvas that saves MDL layout positions. The Home screen now sends questions through
retrieval, local SQL generation, SQL safety validation, and the project's read-only
PostgreSQL connection; it displays returned columns and rows, generated SQL, and result
provenance rather than invented answers. Qdrant and Ollama are local services; install
the needed models explicitly as described above.

Also added since: route-walk and generated 403 tests plus a DBA/User/custom-role matrix
(`tests/test_route_permissions.py`); sample-project endpoint tests; a draft compile test
set (`tests/data/compile_cases.json`); a golden-set bake-off script
(`scripts/golden_bakeoff.py`, results in `reports/`); the four-group sidebar, a mobile
bottom bar and Playwright coverage for the canvas. `GET /projects/{id}/mdl` now requires
`edit_mdl`, and DBAs can reach every project.

Day 3 is not fully signed off. The 40 reference-query cases still need DBA approval or
rejection, the retrieval recall target has not been measured against an approved set,
and model/embedding bake-off results are not claimed. Deployment and retrieval tests
cover the local adapter contracts; live Qdrant/PostgreSQL failure and permission tests
still need to be run in the Compose test environment. Day 2's five-table description
review also remains open until the code is pushed to the selected remote repository.

The candidate cases and recall runner are at `tests/data/retrieval_golden.json` and
`scripts/evaluate_retrieval.py`. After DBA review, set each accepted case's
`approval_status` to `approved` and add its reviewed reference SQL before using the
dataset as a baseline. Then run
`uv run -- python scripts/evaluate_retrieval.py --project-id <project-uuid> --k 5`.
Until approved cases exist, the runner reports `not_measurable`; it does not fabricate
a recall score or claim SQL compilation accuracy.
