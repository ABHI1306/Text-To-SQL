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
