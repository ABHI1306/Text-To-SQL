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
