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
