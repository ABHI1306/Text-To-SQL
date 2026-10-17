# Day 1 model selection notes

## Current status (2026-10-12)

Ran the 10-case schema-constrained prompt test against both installed, local-capable
generation models. The full comparative data is in
`reports/day1-model-bakeoff-20261012.json`.

| Model | Schema-valid | Expected tables present | Wall time | Digest |
|---|---:|---:|---:|---|
| `llama3:latest` | 9/10 | 10/10 | 67.76 s | `365c0bd3c000a25d28ddbf732fe1c6add414de7275464c4e4d1c3b5fcb5d8ad1` |
| `llava:latest` | 7/10 | 7/10 | 190.99 s | `8dd30f6b0cb19f555f2c7a7ebda861449ea2cc76bf1f44e262931f45fc81d081` |

`llama3:latest` is the provisional local development candidate based on this smoke
comparison; this is not a production/model-selection decision. One response misspelled
`ecommerce` as `eecommerce`. Schema adherence does not establish correct filters, joins,
aggregations, execution accuracy, or performance under the real agent. The separate
refreshed baseline is in `reports/day1-model-smoke-20261012.json`.

`gpt-oss:120b-cloud` is excluded because the product runtime must not send data to a
cloud service. The plan's Day 3 model decision should be made using the compiler path and
golden set when those exist.

## Hardware tier

The checked host has 8 logical CPUs and 18.8 GiB RAM. It exposes Intel Iris Xe integrated
graphics but no NVIDIA GPU or discrete GPU VRAM. This is below the document's Tier B
reference (12–16 GB GPU VRAM or Apple Silicon with 24–32 GB unified memory), and is
closest to Tier A for local model sizing; it is not a Tier B reference machine. The user
confirmed this is the target demo machine and accepted the Tier A-class limitation.

## Review checkpoint

The plan recommends naming a second reviewer for the platform/security track. No second
reviewer is currently available; the user confirmed “no one.” This remains an open
human checkpoint and should be revisited if a reviewer becomes available.

## Day 1 boundary

Execution accuracy is not a Day 1 exit gate. The plan establishes its baseline on Day 3
and measures the target through the compiler path later, so it cannot be honestly
reported until that pipeline and the golden set exist. The current repository does not
yet contain that path.

The remaining Day 1 human checkpoint is to name a second reviewer; none is currently
available.

## Day 3 decisions (2026-10-12)

### Local model bake-off on the full golden set (D3-A5, partial)

`scripts/golden_bakeoff.py` ran all 40 candidate golden questions against the full
17-table demo schema (`reports/day3-golden-bakeoff-20261012.json`). Only
`llama3:latest` (digest `365c0bd3c000a25d28ddbf732fe1c6add414de7275464c4e4d1c3b5fcb5d8ad1`)
and `nomic-embed-text:latest` are installed; no other candidate was compared.

| Metric (llama3:latest, Tier A CPU host) | Result |
|---|---|
| Schema-valid single read-only SELECT over known tables | 39/40 (97.5%) |
| Every expected model appears in the generated SQL | 30/40 (75%) — easy 7/7, medium 18/25, hard 5/8 |
| Generation latency p50 / p95 | 12.6 s / 27.0 s (generation only, no retrieval, compile or summary) |
| Median speed | 4.7 tokens/s |

Caveats: the golden expected models are unapproved proposals, so a "missing" model is
sometimes a legitimate alternative query; this is a prompt-adherence signal, not
execution accuracy. The one invalid response used the wrong schema for an HR table.

**Decision:** keep `llama3:latest` and `nomic-embed-text:latest` (768 dimensions) as the
provisional development pair. Not decided: the SQL-model and embedding-model choice, which
needs Qwen2.5-Coder and a second embedding model to be pulled and compared; and the
execution-accuracy target. The plan's provisional 70% goal / 80% stretch stays in force
until approved reference SQL and the Day 4 compiler path exist to measure a baseline.

### Capability and access fixes

- `GET /projects/{id}/mdl` now requires `edit_mdl`. It previously required only a login, so
  any user with project access could read the whole semantic model (the plan hides the
  model browser from users).
- DBAs now reach every project (section 8: "every project for a DBA"). Previously only
  project members did, so a newly created DBA saw no projects.
- Every route either declares a capability or is listed as public/authenticated-only in
  `tests/test_route_permissions.py`; a new route without one fails the build.
- The E-commerce sample now selects all 12 demo tables. It had 8, so six golden cases
  could never be retrieved. Projects created before this change keep their old 8 tables.

### Roles

There is no built-in Admin role (plan sections 2 and 9). "Admin" is a custom role holding
the administration capabilities `manage_users`, `manage_roles`, `view_audit`, `run_eval`.

### Still open for Day 3

- DBA approval or rejection of the 40 golden cases; recall can only be reported as a
  baseline afterwards (`scripts/evaluate_retrieval.py --include-pending` gives a labelled
  provisional number meanwhile).
- Compile test set v1 (40 cases) is Day 4; `tests/data/compile_cases.json` is a v0 draft of
  30 cases awaiting review.

## Day 4: semantic compiler, audit and row policies

- **Compiler location and shape.** `app/semantic/compiler.py` (the repo has no `application/`
  package). `compile_query(logical_sql, mdl, principal, policies)` is pure and returns a
  `CompiledQuery` (SQL, bound parameters, `policy_context` hash, applied policies, missing
  attributes, typed errors). Parameters use psycopg pyformat (`%(p_region)s`); literal `%` in
  the emitted SQL is doubled, so the query must always be executed with a parameter mapping.
  `PostgresWarehouseAdapter` now un-doubles `%%` before its own SELECT-only parse.
- **`principal=None` applies no policies.** It exists for structural checks (canvas live
  compile, the plan's "compile for a test user" without a user). Every question path must pass
  the asker's principal; the Day 5 graph must not call the compiler without one.
- **Metrics** expand into a CTE over the base model written as logical SQL, so the base
  model's row policies apply to them like any other use. The v0 test overlay's cross-model
  dimension (`via: orders`) cannot be expressed by the MDL (metric expressions may only use the
  base model's columns), so `monthly_revenue` in the v1 set is defined over `payments`.
- **Hardening found while testing.** Data-modifying CTEs (`WITH d AS (DELETE ... RETURNING *)`)
  first compiled as a SELECT; every DML/DDL/command node anywhere in the tree is now rejected.
  Also rejected: table-valued functions (`generate_series`, ...), `pg_*`/`lo_*`/`dblink*` and a
  small denylist of session/privilege functions, `SELECT INTO`, row locks, query parameters,
  and `WITH` names that shadow a model or metric. This is a denylist plus the "relations must
  be deployed models" rule; it is not a proof that no other function leaks information.
- **Joins** must match a declared relationship in either direction and contain its equality
  condition; a missing `ON` on a declared relationship is filled in; `CROSS JOIN` between two
  models is rejected.
- **Row policies.** Stored in `row_policies` (migration `0004_day4`), not in the MDL. Missing or
  null attribute: predicate becomes `FALSE` and the result lists `missing_attributes` for the
  caller to audit (the compiler cannot write). List attributes render `= ANY(%(p)s)` (also for a
  single-placeholder `IN`). Predicates may not contain subqueries. A role listed in
  `exemptRoles` is exempt; nobody is exempt by default, DBA included.
- **Save-time validation** compiles `SELECT * FROM <model>` for the chosen test user with the
  policy forced to apply (so a role scope cannot hide a broken predicate). Attributes must
  already exist on at least one account; a test user lacking an attribute is a warning.
- **Audit.** `audit_events` stays insert/select-only for the app role. Policy writes insert
  their audit row in the same transaction. Other route-level events (project, MDL save, role,
  access and attribute changes, preview-as-user) are written after the action succeeds, so a
  failed audit insert returns 500 after the change was made; they are not atomic.
  `GET /audit` pages newest-first with a cursor. Existing `audit_events` columns are unchanged
  apart from a nullable `ip` that nothing fills yet.
- **Compile test set v1** (`tests/data/compile_cases.json`, 40 cases): `expect.sql` is the
  compiler's own output reviewed by the developer, not independently derived, and the set is
  still `draft_pending_human_review`. Every ok case also runs on PostgreSQL in
  `tests/test_compiler_live.py`.
- **Not wired yet.** `QuestionService` still validates model-written physical SQL (Day 3). Routing
  questions through the compiler, and reporting execution accuracy, is Day 5.

### Still open for Day 4

- Human checkpoint: define the 3 test users and attributes for the row-level security suite and
  approve the policy predicates; DBA review of the 40 compile cases.
- The Row policies and Audit screens remain placeholders: Day 4's UI tickets (D4-C1..C3) cover
  the canvas, metrics and deploy report only; the policy and audit APIs are ready for them.

### Day 4 UI notes

- The Connect and model screen has three tabs (Canvas, Metrics, Deploy report) over one local
  draft. Nothing is sent until **Save model**, which replaces the old "Save layout"; unsaved
  changes disable Deploy. Live compiler checks send the draft to `compile-preview`, leaving out
  relationships whose condition is still blank.
- Dragging between cards infers a `MANY_TO_ONE` condition from a `<target>_id` column and the
  target's primary key; when nothing matches the condition is blank and saving is blocked.
- `GET /projects/{id}/deploy-report` (capability `deploy_mdl`) reports description coverage and
  compiles every calculated field, metric and relationship. A failing compile blocks the Deploy
  button in the UI only; `POST /deploy` itself does not yet re-check it, and the plan's
  golden-set description gate is still not enforced.

## Day 5: question graph, row-level enforcement and LDAP

- **Only route to rows.** `app/agent/graph.py` (LangGraph `StateGraph`): retrieve_context ->
  generate_sql -> compile_sql -> validate_sql -> execute_sql, with correct_sql looping back
  through the compiler (at most 3 corrections) and structured_failure as the only other end.
  `QuestionService` (the Day 3 path that executed model-written physical SQL) is deleted;
  `POST /projects/{id}/query` and the new `.../query/stream` (SSE) both run the graph. The
  topology is asserted in `tests/test_agent_graph.py`. Added over the plan's drawing: generate_sql
  and retrieve_context can go straight to structured_failure (LLM down, nothing retrieved).
  Not built yet (Day 6/7): rewrite_followup, classify_intent, summarize, respond, ask_user and
  the `clarification`/`suggestions` events (the UI already renders them).
- **Validator** (`app/semantic/validator.py`): a pure allowlist over the compiled SQL, then an
  `EXPLAIN` dry run through `WarehousePort.explain`. A dry-run error reaches the model only as
  `DRY_RUN_FAILED (SQLSTATE x)`, because PostgreSQL's text names physical objects.
- **Prompt hardening found by tests.** A question containing `</question>` could close its own
  delimiter; `<` and `>` are now escaped in the JSON-encoded question. The model never sees
  physical names or policies (asserted).
- **The adversarial review found a real bypass**, fixed: a WITH name equal to a catalog view
  (`WITH pg_user AS (SELECT * FROM pg_user) SELECT * FROM pg_user`) passed both the compiler and
  the validator, because "is this a CTE reference" was decided by name instead of by SQL scoping.
  It is now decided by sqlglot scopes (`cte_reference_ids`) in both. Also fixed: deep nesting
  crashed with `RecursionError` (now a typed error, input capped at 20,000 characters); the
  function **blocklist became an allowlist for functions sqlglot does not model**
  (`ALLOWED_UNTYPED_FUNCTIONS`), plus `generate_series` everywhere, `reg*` casts and
  `current_catalog`; token-prefix collision between attributes `a` and `a__b`; policy predicates
  now obey the function rules; policy role names match case-insensitively and a policy scoped to a
  role that does not exist is rejected at save (it would otherwise apply to nobody).
  Remaining by design: any non-empty `appliesTo.roles` leaves other roles unrestricted, and the
  allowlist is still a judgement about which functions are safe, not a proof.
- **Safety suites.** Red-team set: 30 cases (`tests/data/redteam_cases.json`), all blocked or
  neutralized without reaching the database. Row-level security suite: 39 query shapes x 3 users =
  117 cases plus 2 sanity tests, each compared with an independent hand-written physical oracle on
  PostgreSQL; removing the policies makes 102 of them fail, so the suite does detect leaks. Paths
  that do not exist yet (dashboards, scheduled runs, shared views, cached results, follow-ups) are
  not covered and must be added when they are built.
- **LDAP** (`app/adapters/identity/ldap.py`, ldap3): service bind, unique user search with an
  escaped filter, user bind, groups (`memberOf` and a group search), attribute mapping to policy
  attributes. LDAPS or StartTLS with certificate verification is required (`allow_insecure` is
  for tests). Empty passwords never reach the server (anonymous bind). A directory outage is a
  generic login failure, never a fallback to another method. Accounts are created on first login
  with `auth_source='ldap'` and no password; a local username is never taken over, and local
  accounts never consult the directory. Roles come from `identity_mappings` (first matching
  mapping in creation order), re-applied at every login; removal from a group removes the role at
  the next login (an already issued access token keeps its role until it expires; the role is
  re-read on each request).
  Directory-mapped attributes replace earlier directory-sourced ones; manual attributes win.
  Verified against the OpenLDAP demo container (compose profile `identity-demo`,
  `docker-compose.ldap.yml`, `scripts/ldap/make-certs.sh`; the image's own CA expired in
  Jan 2026) and through the real API login.
- **Bug found by running it for real**: the Day 4 governance repository did not accept the API's
  `postgresql+psycopg://` URL, so every Day 4 endpoint and audit write would have failed in the
  composed stack while the unit and live tests (plain DSN) passed.
- **Dependencies**: `langgraph` and `ldap3` added; resolving them moved `websockets` from 17.2 to
  16.1.1.
- **Execution accuracy (provisional)**: with `llama3:latest`, 15 scorable e-commerce cases:
  3 correct (20%), 9 wrong rows, 3 failed after 3 corrections; first-attempt validity 47%,
  correction success 62%, latency p50 69 s / p95 116 s on this CPU. All are below the plan's
  targets (70% accuracy, 20 s p50). Four references (ecom-14, 15, 16, 24) did not compile and are
  unscored. Rows are compared exactly, so an answer with different columns than the reference
  counts as wrong even when reasonable (e.g. category_id instead of category name). Details:
  `reports/day5-baseline-provisional-20261013.json`. The SQL-model choice (Qwen2.5-Coder etc.)
  is still undecided and is the main lever.
  Earlier note: see `reports/day5-baseline-*.json`. The references in
  `tests/data/execution_references.json` are developer drafts, not DBA-approved, so the number is
  provisional; five of the 24 e-commerce cases cannot be answered from the demo schema at all.
- **Not done**: OIDC (Day 6), the preview-as-user selector in the chat screen is still a design
  placeholder, `rerun_sql` re-runs by asking the question again (no endpoint re-executes stored
  SQL yet), and the sql-safety-reviewer / rls-reviewer agents from the plan do not exist here, so
  two adversarial review passes were run with general-purpose agents instead. The human checkpoint
  (line-by-line review of the compiler and validator, ten hostile queries as a restricted user)
  is still yours.

## Day 6 Track A (knowledge, summary and chart, evaluation)

- **Knowledge is advisory data, never authority.** Instructions and approved SQL pairs live in
  `project_instructions` / `sql_pairs` (migration `0006_day6`), are embedded through
  `EmbedderPort` and indexed in `VectorStorePort` under the namespaces `knowledge:instructions` and
  `knowledge:sql_pairs` (the existing `model_version` filter, so schema search never sees them).
  The index is only a ranking aid: results are re-read from PostgreSQL and must be approved/active,
  so a deleted or rejected record cannot come back even if the vector delete failed.
- **Prompt use.** One JSON-encoded `<knowledge>` block (`<`/`>` escaped) between MODELS and
  `<question>`, in generation and correction prompts, with a note that it cannot change the rules.
  Pairs are only included when their SQL compiles against the models shown to the model, so
  physical names or stale names never reach it. With no knowledge the prompt is byte-identical.
- **Workflow and capabilities.** Existing capabilities, no new migration for them: instructions
  and listing/deleting pairs need `manage_knowledge`; feedback needs `give_feedback`; proposals,
  approve/reject and adding an (immediately approved) pair need `approve_pairs`. Thumbs-up becomes
  a `proposed` pair; saving or approving requires the SQL to compile against the deployed model.
  Every state change writes an audit event in the same transaction as the change.
- **summarize / chart** run on the small model after `execute_sql`, only when a presentation model
  is configured. The chart spec is rebuilt from an allow-list (marks, channels, aggregates,
  fields that exist in the result, numeric where quantitative); the summary may only quote numbers
  that are in the rows (above 10). Any failure leaves the rows untouched.
- **Evaluation.** `tests/data/intent_eval.json` (30), `tests/data/followup_chains.json` (10 chains),
  runner `app/evaluation/conversation.py`, CLI `scripts/evaluate_intents.py`. CI uses a scripted
  model (checks data and scoring, not model quality); the live test skips without Ollama.
- **Not verified here**: migration 0006_day6 and the PostgreSQL repository were not run against a live
  database in this session (only the generated SQL and the in-memory twin were exercised).
## Day 6 Track B: OIDC, scheduler worker and drift detection

- **OIDC** (`app/adapters/identity/oidc.py`, Authlib + joserfc): authorization code flow with
  PKCE (S256), `state` and `nonce`. The ID token is accepted only when its signature verifies
  against the provider's JWKS with an asymmetric algorithm (so `alg: none` and HS256-signed-with-the-public-key
  are refused) and `iss`, `aud` (plus `azp` when there are several audiences), `exp`, `nbf`, `sub`
  and `nonce` are valid. JWKS is cached and refetched at most every 30 s on an unknown key id.
  Discovery's `issuer` must equal the configured one. https is required unless
  `OIDC_ALLOW_INSECURE` (Keycloak demo). The token exchange is a plain httpx call: Authlib 1.8's own
  client rides on `httpx2`, which cannot share a transport with the rest of the app. Authlib builds
  the authorization URL and the PKCE challenge.
- **Same session**: `AuthService.login_external` shares `_finish_external` with LDAP login. The role
  comes from `identity_mappings` for source `oidc`, re-applied at every login (a removed group
  changes or removes the role at the next login; no matching mapping refuses the login). The account
  is provisioned with `auth_source='oidc'` and no password, local names are never taken over,
  disabled accounts stay disabled. Every failure is the same generic 401; the reason goes to the audit log.
- **State**: only the SHA-256 of `state` is stored, with the nonce and PKCE verifier, in
  `oidc_login_states`. It is deleted atomically on first use (`DELETE ... RETURNING`), expires after
  10 minutes and is capped at 10 000 pending logins. The raw state also travels in an HttpOnly
  `SameSite=Lax` cookie that the callback must match (login CSRF). Lax, not Strict, because the
  browser returns from the provider on a cross-site navigation. An in-memory store exists for
  single-process use.
- **Known limit**: the local username is the casefolded `preferred_username` claim and the OIDC
  `sub` is not stored, so a provider that recycles usernames could hand an old account to a new
  person. Keep usernames unique and immutable at the provider, or add a subject column later.
- **Shared adapter contract**: `tests/support/identity_contract.py` runs against LDAP (ldap3 mock
  server) and OIDC (local fake IdP with real RSA keys, `tests/support/fake_idp.py`), both through
  `VerifiedIdentity` (`app/ports/identity.py`).
- **Scheduler**: jobs live in `scheduled_jobs` (migration `0006_day6`, which also carries
  the drift and OIDC-state tables). APScheduler is the engine,
  not the store: one interval job polls the table, claims due slots and runs each in its thread
  pool. A slot is a row in `job_leases` keyed `(job_id, slot)`. The claim moves `next_run_at` under a
  row lock and inserts the lease in one transaction, so two workers never run one slot. Missed slots
  coalesce into one catch-up run after downtime. A worker that dies mid-run leaves a `running` lease
  that expires and is taken over (at-least-once; drift reports are unique per slot, so a retry stores
  no duplicate). There is no lease heartbeat: keep `SCHEDULER_LEASE_SECONDS` above the longest run.
- **Runs as the owner, fail closed**: the owner's context is re-read for every slot. A missing,
  disabled or locked owner, or one without `manage_profiling` or project access, pauses the job
  (`paused_reason`) and runs nothing. Re-saving the schedule re-activates it under the saver. Audit
  events: `scheduled_job_run`, `scheduled_job_paused`, `profiling_schedule_set`,
  `profiling_schedule_removed`, `drift_draft_created`. Schedules are cron with a 15 minute minimum gap.
- **Drift** (`app/semantic/drift.py`): only tables and columns the deployed MDL maps are compared
  (new unmodelled tables are not drift): `table_removed`, `column_removed`, `column_added`,
  `type_changed` (using the MDL's type-compatibility groups, so `int4` vs `integer` is not drift).
  A report is stored on every run, including "no drift". `POST .../draft` applies a report's findings
  to the MDL revision it compared and saves a new `pending` revision, linked to the report in the same
  transaction (one draft per report). If a calculated column, metric or relationship still depends on
  something removed it answers 409 and saves nothing.
- **Verified**: the scheduler tables of the Day 6 migration applied on a scratch Postgres 16; lease races
  (8 threads), takeover, report idempotency, one draft per report and one-time OIDC state ran against
  it (`tests/test_scheduler_live.py`, needs `METADATA_APP_DSN`). Keycloak was not started here, so
  `tests/test_oidc_live.py` and the realm import are untested against a real Keycloak.

## Day 7: streaming ask, dashboards, scheduled questions, sharing, audit and evaluation

Written without running the test suite or migrations (see "Not verified").

- **Thread endpoints** (`app/api/thread_routes.py`): `POST /projects/{id}/threads`,
  `GET /threads/{id}` (owner, `view_all_threads`, or a share with `view_shared`),
  `POST /threads/{id}/ask` (SSE, section 8 vocabulary: `intent`, `answer_token`, `chart`
  `{vega_lite_spec}`, `clarification {question, options}`, `suggestions`, `done`, `error`),
  `POST /threads/{id}/rerun-sql`, `POST /messages/{id}/feedback`. The old
  `/projects/{id}/query[/stream]` endpoints are unchanged (the UI still uses them); the contract
  vocabulary is opt-in through `AskService.stream(contract=True)`.
- **Assumptions**: `preview_as` and compiled SQL both need `rerun_sql`; a preview run reads the
  owner's history and writes nothing; `tokens` is 0 and `gpu_ms` null because the Ollama adapter
  reports neither yet; `max_rows` can only lower the configured limit; message ids are the
  `thread_turns` ids; only the thread owner may ask in a thread.
- **One run path**: `app/semantic/runner.py` (compile, validate, plan, execute) is used by
  rerun-sql, tiles, scheduled questions and the evaluation references.
- **Safe sharing** (`app/collab/service.py`): resources store logical SQL only. A viewer's rows are
  compiled and run under the viewer's own principal; `result_cache` is keyed by
  (resource, `policy_context`, model version) and reused only on an exact match. Not-visible and
  not-found are the same 404. Role shares use the role *name* as `grantee_id`.
- **Scheduled questions** reuse the Day 6 job engine (migration `0007_day7`): `scheduled_jobs` gets
  kind `saved_question` and a `saved_question_id` column, the project-and-kind uniqueness now
  applies to `profile_drift` only, and the runner checks `schedule_question` (not
  `manage_profiling`) for these jobs. There is no separate `scheduled_questions` table: the job row
  is the schedule. `question_runs` keeps the last 20 runs; a run-now with the same
  `Idempotency-Key` returns the earlier run.
- **Routes that need `view_shared`**: opening a dashboard or reading a tile result declares
  `view_shared`, so an owner whose custom role lacks it cannot open their own dashboard.
- **Evaluation** (`app/evaluation/execution.py`): the golden set through the real pipeline, with
  the failing stage recorded per case and a diff against the previous completed run. The intent and
  follow-up sets stay on the CLI; the compile and red-team sets stay CI tests (they pin the demo
  model).
- **Audit browser**: `/audit-events` (same handler as `/audit`) and `/audit-events/export.csv`
  (10,000-row cap; cells starting with `=`, `+`, `-`, `@` are prefixed with `'`).
- **Notifications** (`GET /notifications`, `POST /notifications/{id}/read`): failed scheduled runs
  and new drift reports (to the job owner).
- **Allowed hosts**: `GET/PUT /settings/allowed-hosts` (`manage_connection`) stored in
  `app_settings`; link-local and metadata addresses are refused in the editor and at test time.
- **UI**: Audit, Drift and profiling, Evaluation and Shared with me screens, sign-in options from
  `GET /auth/methods`, and the notification count. The Dashboards and Scheduled questions screens
  are Day 8.
- **Not verified**: migration `0007_day7`, the PostgreSQL collaboration store and the UI were not
  run; `docs/openapi.yaml` and `ui/src/api/schema.d.ts` were not regenerated.

## Day 8: collaboration screens, performance and accuracy tooling

- **UI**: Dashboards screen (tile grid, each tile labelled "Computed for you", refresh, rename,
  remove tile, delete), Scheduled questions screen (saved-question picker, preset or cron
  schedule, time zone, run now, pause/resume, run history), one `ShareDialog` used for dashboards,
  saved questions and conversations (replaces the free-text share form in chat), and a notices
  panel behind the bell (failed scheduled runs, new drift reports, mark as read).
- **Share pickers**: `GET /share-targets` (`share` capability) returns active accounts other than
  the caller (id, username, role) and roles that hold `view_shared`. It is separate from `/users`
  so owners do not need `manage_users`. It lists every active account, not only project members;
  `create_share` still checks the grantee exists, and a grantee outside the project sees nothing.
- **Compiled-output cache** (`app/semantic/runner.py`, `CompileCache`): key is a hash of the
  logical SQL, the whole model, every enabled policy and the principal's id, role and attributes,
  so compiled SQL (which embeds policy values) is never shared across policy contexts. Only
  successes are cached; 256 entries, in process.
- **Retrieval cache** (`RetrievalCache`, `RETRIEVAL_CACHE_SECONDS`, default 60, 0 turns it off):
  keyed by project, model version, question and limits. Retrieval holds schema text and approved
  knowledge only, so it is shared between users; the TTL bounds how stale a new pair can be.
- **Connection pooling** (`app/adapters/warehouse/pool.py`, `WAREHOUSE_POOL_MAX_SIZE`, default 5):
  one `psycopg_pool` pool per DSN, used when the package is installed and no custom CA is set;
  otherwise connections are opened per query as before. `pyproject.toml` now asks for
  `psycopg[binary,pool]`; run `uv lock` to refresh `uv.lock`.
- **Rate limits** (`app/api/rate_limit.py`): per user, per minute, in process. Ask, stream and
  SQL rerun 30; run-now 10; reading shared results 600. Over the limit returns 429 with
  `Retry-After`. With more than one API process the limit is per process.
- **Accuracy (Track A)**: `scripts/compare_eval.py` clusters a run's failures by stage and diffs
  it against an earlier run, appending one line per lever to a log. No lever has been pulled and
  no bake-off rerun yet: those need Ollama, Qdrant and a DBA-approved baseline, so no accuracy
  number is claimed for Day 8.
- **Not verified**: the new UI, `tests/test_day8_performance.py`, the pool and `pyproject.toml`
  change were not run or installed; `docs/openapi.yaml`, `ui/src/api/schema.d.ts` and the UI mock
  API were not regenerated or extended for `/share-targets`.

### Day 8 security review (second reviewer, read-only pass)

Fixed (tests in `tests/test_day8_security.py`, not yet run):
- **`POST /users`** now requires the new account's role capabilities to be a subset of the
  caller's, like role assignment.
- **`compile-preview` with `sample`** needs a preview user and the saved model (no client `mdl`),
  and the compiled SQL passes the validator before it runs.
- **Shared threads**: a reader who is not the owner gets questions and logical SQL only; the
  stored answer, summary, chart, suggestions and trace are withheld.
- **`preview_as`** (and compile-preview's preview user): a non-DBA needs `manage_policies` and
  the target's capabilities must be a subset of theirs. This narrows the Day 7 behaviour where
  any `rerun_sql` holder could preview as any project member.
- **User management**: role, attributes and project-access changes refuse self-edits and
  accounts holding capabilities the caller lacks (DBA exempt).
- **LIMIT**: a written LIMIT above 10,001 is clamped by the compiler.
- **Allowed hosts**: IPv4-mapped IPv6 and names resolving to link-local or metadata addresses are
  refused.
- **`/query` with a foreign `thread_id`** is a 404 before any work runs.
- `sqlglot` minimum raised to 30.0.0.

Second batch (all remaining findings; tests added, `make check` green):
- **Tokens and sessions**: an access token is accepted only while its refresh-token family has an
  unrevoked, unexpired member (`session_is_alive`), so logout, "sign out other sessions" and
  refresh-token reuse end it at once. Role, attribute and project-access changes now clear the
  cached auth context (one account, or all accounts for a role change).
- **Login limits**: an address may fail 10 times the per-account limit before it is refused, so
  one shared proxy address no longer locks everyone out; the per-account limit is unchanged.
  Starting a single sign-on attempt is limited to 20 a minute per address. Both limiters are
  per process.
- **Role names as identity**: renaming a role moves the shares and policy scopes that name it;
  deleting one removes its shares and its `exemptRoles` entries. A policy's `roles` list is left
  alone on delete, so it names nobody instead of widening.
- **OIDC identity**: migration `0008_day8` adds `users.external_subject`. An OIDC account is
  pinned to the provider's `sub` at first login; a different `sub` for the same username is
  refused as an account conflict. Existing OIDC accounts are pinned at their next login.
- **Run-now idempotency**: requests with one key are serialised in the process, and a key whose
  stored run failed returns an error instead of replaying the failure as a result. Across several
  API processes the database's unique key still stops a second record, not a second query.
- **Disabled owners**: a disabled or locked owner's dashboards and saved questions are not
  visible through shares.
- **Demo data** (this install only, not code): E-commerce policies `orders_region` and
  `customers_region` (`region = {{ user.region }}`, DBA exempt), account `asha` (role User,
  region South), and DEMO dashboard, questions and shares.

## Day 9: integration, drills and the accuracy gate

Final state (2026-10-17). Run and passing: `make check` (898 passed), `make drills` (29),
`make redteam` (70), UI build, vitest (36) and all 75 Playwright tests, and the full suite
inside the Compose `tests` service against the live databases (1064 passed, 14 skipped; this
includes the row-level-security and database-permission suites, none skipped). One run of that
service showed 2 failures that did not repeat in four further runs; the cause was not found.
`make offline` passed (details below).

**The accuracy gate is not met and cannot be this session.** Zero of the golden-set
references are approved (all `draft`), so `make eval` would be refused by the gate as
provisional; approving them is a DBA review, not something to script. The only number is the
provisional 20% (`reports/day5-baseline-provisional-20261013.json`) against 70%. The host has
no GPU (see Hardware tier), and recent questions took 37-144 s, so the 20 s median target is
also out of reach here until the model or hardware changes.
- **D9-1 Playwright per role** (`ui/e2e/day9-roles.spec.ts`, 29 tests): DBA, User, custom role
  (`Analyst+`), LDAP user and OIDC user against the mock API, which now has personas
  (`mock-token:<username>`), the Day 7/8 endpoints and `/__mock/*` test switches. These cover UI
  behaviour only; real LDAP and Keycloak logins are checked by `make offline`.
- **D9-2 `make eval`**: runs the golden set in the api container, then `scripts/eval_gate.py`
  fails unless references are approved (`status == baseline`), accuracy >= `EVAL_TARGET`
  (0.70) and p50 <= 20 s. Needs `PROJECT_ID` and `USERNAME`.
- **D9-3 `make offline`** (`scripts/offline-test.sh`, `docker-compose.offline.yml`): brings up
  the stack with the LDAP, OIDC and offline overlays; probes egress from all eight services,
  checks DNS, `/health/ready`, local and LDAP login, Keycloak discovery and the worker.
  Finding: Keycloak sat on the non-internal `edge` network, so it could reach the internet.
  The overlay makes `edge` internal and drops the host port (OIDC from a browser is unavailable
  in that mode). Result: all 8 services blocked from 1.1.1.1, public DNS fails, `/health/ready`
  ok, OpenLDAP StartTLS binds work, Keycloak discovery works, worker running. Needs
  `OIDC_CLIENT_SECRET` and `KEYCLOAK_ADMIN_PASSWORD` set (they were missing from `.env`; the
  run used throwaway values). Local and full LDAP app logins are skipped unless
  `OFFLINE_LOCAL_USER/PASSWORD` or `OFFLINE_LDAP_USER/PASSWORD` are given: no group-to-role
  mappings exist (`identity_mappings` is empty), so directory logins are denied by design.
  Compose finding: the api healthcheck (`python -c "import ...; ..."`) is split at spaces by
  this host's podman-backed compose and always failed; rewritten without spaces, the api now
  reports healthy.
- **D9-4 drills** (`tests/test_day9_drills.py`) and the fixes they forced:
  - A database that was down came back as non-retryable `DRY_RUN_FAILED` and burned correction
    attempts. It is now `DATABASE_UNAVAILABLE` (retryable), at both the dry run and execution.
  - Self-issued access tokens had zero clock tolerance; a clock stepped back a few seconds
    rejected fresh tokens. Added 30 s leeway (OIDC already had 60 s).
  - Found by the new Playwright run: pausing or resuming a scheduled question blanked the whole
    screen, because the API's JSON reply was stored as the notice text and React could not render
    it (`ui/src/ScheduledScreen.tsx`). Fixed.
  - Worker killed mid-job and scheduler clock back/forward are covered; existing behaviour held.
- **D9-5 tracing and metrics**: the Ollama adapter records prompt/answer tokens and model busy
  time (`app/observability/usage.py`); `done` events now carry real `tokens` and `gpu_ms`.
  `GET /api/v1/ops/usage`, `/ops/scheduler` (`view_audit`) and `GET /health/ready` (dependency
  up/down, 503 when any is down); new Operations screen. Counters are per process and reset on
  restart. `gpu_ms` is Ollama's total call duration, not measured GPU utilisation. Langfuse
  from the plan is **not** done.
- **D9-6 open items**:
  - Approve the golden-set references (DBA), then `make eval PROJECT_ID=... USERNAME=...`.
  - Langfuse from the plan's tooling table is not built; the in-process counters and the
    Operations screen cover the D9-5 views.
  - `docs/openapi.yaml` had the knowledge-endpoint paths and the `SqlPair` schema defined
    twice since Day 6, so `npm run generate:api` (run by CI) failed. The older copy was removed,
    `/ops/usage` and `/ops/scheduler` were added, and `ui/src/api/schema.d.ts` was regenerated.
    `/health/ready` is not in the spec (it sits outside the `/api/v1` base, like `/health`).
  - `uv.lock` changed (`tzdata`, needed for legacy zone names such as `Asia/Calcutta`).
