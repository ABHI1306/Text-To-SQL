# Text-to-SQL Platform

A local-first Text-to-SQL platform for PostgreSQL. A user asks a question in plain language;
a local LLM (Ollama) writes logical SQL over a semantic model, a compiler turns it into
PostgreSQL SQL with row-level security applied, a validator checks it, and the rows come
back with a step timeline, charts and a summary. Everything runs on your own machine:
no data or prompts leave it. Only PostgreSQL is supported as a data source.

## Quick start (new machine)

### 1. Prerequisites

| Tool | Notes |
| --- | --- |
| Docker with Compose (or Podman with the Docker CLI emulation) | runs PostgreSQL, Qdrant, Ollama, the API and the worker |
| [`uv`](https://docs.astral.sh/uv/) | Python environment; only needed on the host for `make check` and the helper commands |
| Node.js 22+ and npm | the web UI runs on the host with Vite |
| 16 GB RAM minimum (project document tier A: CPU only, one 7B-class model) | a GPU with 12-16 GB VRAM (tier B) is the reference; on CPU answers are slow. Leave several GB of disk for the Ollama models |

Ports used on the host: 8000 (API), 4173 (UI), 6333 (Qdrant), and 55432 / 55433 (the metadata
and demo PostgreSQL databases; change them with `METADATA_HOST_PORT` / `DEMO_HOST_PORT` in
`.env`). Ollama runs inside Compose and is not published to the host.

### 2. Configure `.env`

```bash
cp .env.example .env
```

Edit `.env`:

1. Replace every `*_change_me` password with your own.
2. Generate the two secrets and paste them in:
   ```bash
   openssl rand -hex 32     # JWT_SECRET (at least 32 characters)
   uv run -- python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"   # CONNECTION_ENCRYPTION_KEY
   ```
3. Set `BOOTSTRAP_DBA_PASSWORD` (at least 12 characters). It is only the temporary password
   of the first account (`BOOTSTRAP_DBA_USERNAME`, default `demo.dba`).

`.env` is ignored by git. Never commit it. Do not use the placeholder values on a shared
machine or a network-reachable host.

### 3. Start the stack

```bash
docker compose --env-file .env up -d --build
docker compose --env-file .env --profile tools run --rm migrate     # apply all database migrations
make user-bootstrap-dba                                              # create the first DBA account
curl http://localhost:8000/health                                    # should answer {"status":"ok"}
```

After the account exists, delete `BOOTSTRAP_DBA_PASSWORD` from `.env` and recreate the API:
`docker compose --env-file .env up -d --force-recreate api`.

The Compose network is internal: containers can reach each other but not the internet.
Create more accounts with `make user-create USERNAME=analyst` and reset one with
`make user-reset-dba USERNAME=analyst`.

### 4. Install the local models

Model weights are not downloaded automatically (the Ollama volume starts empty):

```bash
docker compose --env-file .env exec ollama ollama pull llama3:latest
docker compose --env-file .env exec ollama ollama pull nomic-embed-text:latest
```

`SQL_MODEL` (default `llama3:latest`) writes the SQL, `EMBEDDING_MODEL` (default
`nomic-embed-text:latest`) powers retrieval, and the optional `SMALL_MODEL` is used for the
cheap routing steps and falls back to `SQL_MODEL` when empty. If you set other names in
`.env`, pull those instead. Do not use a model whose name ends in `-cloud`: it sends
inference to an external service.

### 5. Start the UI

```bash
cd ui
npm ci
npm run generate:api
npm run dev
```

Open <http://127.0.0.1:4173> and sign in with `BOOTSTRAP_DBA_USERNAME` and its temporary
password; you will be asked to change it. The dev server talks to the API at
`http://127.0.0.1:8000` (override with `VITE_API_PROXY_TARGET`). `npm run dev:mock` runs
against labelled fixtures instead of the real API.

### 6. Connect a database and ask a question

1. Open **Setup**, then the **PostgreSQL** card. To try the bundled sample data use host
   `demo-db`, port `5432`, database `demo`, user `warehouse_reader` and the
   `WAREHOUSE_READER_PASSWORD` from your `.env`.
2. **Test connection**, create the project, **select tables**, define relationships, then
   **Validate and save model**.
3. In **Connect and model** open the **Deploy report** tab and click **Deploy model**.
4. Open **Home** and ask about the data, for example "How many customers do we have?".

You can add more databases with **+ Add project** in the project switcher at the top of the
sidebar. One project (one database) is active at a time and every screen shows that
project's data.

The demo database is seeded with e-commerce and HR sample schemas only when its volume is
first created. If initialization fails, inspect the logs; remove the named demo volume only
if you intentionally want to discard its contents.

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

## Connecting your own PostgreSQL database

The API runs inside Docker, so the host you type in **Setup** is resolved from inside the
API container, not from your machine:

- `localhost` means the API container itself, not your computer.
- The Compose network (`<project>_local`, for example `texttosql_local`) is internal: it has
  no route to your host or the internet. Put the database on that network and use its
  container name as the host, for example:
  ```bash
  docker run -d --name my-pg --network texttosql_local \
    -e POSTGRES_PASSWORD=admin_pw -e POSTGRES_DB=mydb postgres:16
  ```
  (`docker network ls` shows the exact network name on your machine.)
- Add the host to `DB_HOST_ALLOWLIST` in `.env` (comma separated, no spaces; a name, an IP
  or a CIDR range) and recreate the API so it picks the change up:
  ```bash
  docker compose --env-file .env up -d --force-recreate api
  docker compose --env-file .env exec api env | grep DB_HOST_ALLOWLIST
  ```
- Connect with a read-only account that can only `SELECT` the schemas you want exposed:
  ```sql
  CREATE ROLE app_reader LOGIN PASSWORD '...';
  GRANT CONNECT ON DATABASE mydb TO app_reader;
  GRANT USAGE ON SCHEMA public TO app_reader;
  GRANT SELECT ON ALL TABLES IN SCHEMA public TO app_reader;
  ```
- The app is local-only. Do not expose a database to the internet for testing.

## Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| "Database host is not in DB_HOST_ALLOWLIST" | The host is missing from `DB_HOST_ALLOWLIST`, or the API was not recreated after editing `.env`. A `DB_HOST_ALLOWLIST` exported in your shell overrides `.env`: run `unset DB_HOST_ALLOWLIST`, recreate the API, then check with the `env \| grep` command above. |
| "Could not read database catalog" in Setup | The connected database is unreachable. Check that its container is running (`docker ps -a`), is on the Compose network, and that the host, port and read-only password are right. Containers do not restart on their own after a reboot unless created with `--restart unless-stopped`. |
| Connection test fails with a name error | The host is not resolvable from the API container. Use the container name on the Compose network, not `localhost`. |
| `docker compose` complains about `METADATA_ADMIN_PASSWORD` or similar | `.env` is missing or a required password is unset. Copy `.env.example` and fill it in. |
| The API refuses to start (secrets) | `JWT_SECRET` (32+ characters) or `CONNECTION_ENCRYPTION_KEY` (a Fernet key) is missing or invalid. |
| Questions fail with "LLM unavailable" | The Ollama models are not pulled yet, or `SQL_MODEL` / `EMBEDDING_MODEL` name a model you did not pull. |
| Home answers "No relevant models" or never returns rows | The model was saved but not deployed. Deploy it from **Connect and model**. |
| The API container shows `unhealthy` right after start | It is still starting; check `docker compose logs api` and `curl localhost:8000/health`. |
| Port already in use | Another service holds 8000, 4173, 6333, 55432 or 55433. Stop it or change the port. |
| Using Podman | The Docker CLI emulation works, but commands may print an emulation notice. Create `/etc/containers/nodocker` to silence it. |

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

## Day 4 progress

Implemented locally: the semantic compiler (`app/semantic/compiler.py`) with model CTEs,
calculated fields, relationship-checked joins, metric expansion, bound-parameter row
policies and typed errors; a 40-case compile test set that also runs against PostgreSQL;
DDL-style prompt context (`app/semantic/prompt_context.py`); `POST /projects/{id}/compile-preview`
(draft MDL, preview as a user, optional 10-row sample); row-policy storage, save-time
validation and admin API (`/projects/{id}/row-policies`); an audit service and
`GET /audit` browser with filters and paging; migration `0004_day4`. Apply it with
`docker compose --env-file .env --profile tools run --rm migrate` before using the new endpoints.

Also: the Connect and model screen now has a canvas with drag-to-relate, a side panel for
descriptions, keys and calculated fields with live compiler checks, a metrics editor that
previews compiled SQL and a sample result, and a deploy report tab
(`GET /projects/{id}/deploy-report`). Row policies and Audit screens are still placeholders.

Not done: the human checkpoint (3 test users, policy approval, DBA review of the compile
cases), and routing questions through the compiler (Day 5). See `docs/DECISIONS.md` for the
choices and their limits.

## Day 5 progress

Questions now run through one graph (`app/agent/graph.py`): retrieve, generate logical SQL,
compile under the asker's row policies, validate (allowlist plus an `EXPLAIN` dry run), execute,
with up to three typed corrections. `POST /projects/{id}/query` returns rows (logical SQL for
everyone, compiled SQL only with `rerun_sql`); `POST /projects/{id}/query/stream` streams the
steps as server-sent events, and the Home screen renders them as a timeline with the results
table, a SQL viewer, bar/line charts, retry and clarification quick replies.

From the command line (inside the api container):

    docker compose exec api uv run --no-sync python -m app.cli.ask \
        --project-id <uuid> --username <account> --show-compiled --events "How many orders per status?"

Safety suites: `tests/test_redteam.py` (30 cases) and `tests/test_rls_suite.py` (3 users x 39
shapes, against PostgreSQL; needs `WAREHOUSE_DSN`). LDAP login is wired into `/auth/login` when
`LDAP_URI` is set (see `.env.example`); groups map to roles through `PUT /identity/mappings`.
To try it with the OpenLDAP demo container:

    sh scripts/ldap/make-certs.sh
    docker compose -f docker-compose.yml -f docker-compose.ldap.yml --profile identity-demo up -d

Apply migration `0005_day5` first (`docker compose --env-file .env --profile tools run --rm migrate`).
Execution accuracy runner: `scripts/evaluate_execution.py`; the baseline and its caveats are in
`docs/DECISIONS.md` and `reports/`. Day 5 is not signed off: the references behind the baseline
are unapproved drafts, and the compiler/validator line-by-line review and the hostile-query
session are still open.

OpenID Connect login (Day 6) is enabled by `OIDC_ISSUER` (see `.env.example`): `GET
/api/v1/auth/oidc/login` redirects to the provider (authorization code + PKCE) and
`GET /api/v1/auth/oidc/callback` returns the same session as `/auth/login`. Map provider groups
to roles with `PUT /identity/mappings` using `"source": "oidc"`. To try it with Keycloak:

    # set OIDC_CLIENT_SECRET and KEYCLOAK_ADMIN_PASSWORD in .env, apply migration 0006_day6
    docker compose -f docker-compose.yml -f docker-compose.oidc.yml --profile identity-demo up -d
    # then open http://localhost:8000/api/v1/auth/oidc/login (demo users: scripts/oidc/realm-text2sql.json)
    OIDC_TEST_ISSUER=http://localhost:8081/realms/text2sql OIDC_TEST_CLIENT_SECRET=... uv run pytest tests/test_oidc_live.py

The `worker` service runs scheduled profiling and drift detection. Set a schedule with
`PUT /api/v1/projects/{id}/profiling-schedule` (cron, at most every 15 minutes; the job runs as
the person who set it), read results at `GET /api/v1/projects/{id}/drift-reports`, and turn a
report into a draft MDL revision with `POST .../drift-reports/{reportId}/draft`. Migration
`0006_day6` (the single Day 6 migration) also adds the job, lease, drift-report and OIDC-state tables.
