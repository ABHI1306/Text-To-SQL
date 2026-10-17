# Build log (Days 1-6)

Progress notes moved out of the README at Day 10. Decisions and their reasons are in
[DECISIONS.md](DECISIONS.md); what is built now is in [PROJECT.md](PROJECT.md).

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
