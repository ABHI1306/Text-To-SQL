# Final evaluation report (v0.1.0)

**Result: the accuracy gate is not met, and it is not close.** On the first real baseline
(13 references, approved on the DBA's instruction), `llama3:latest` scored **7.7%** execution
accuracy against the 70% target, with a **143.6 s** median answer time against the 20 s target.
`make eval` ran and the gate failed on accuracy and latency; it passed only the "references are
approved" check.

## Execution accuracy (baseline, 2026-10-17)

Source: `reports/eval-20261017.json`, `make eval`, `llama3:latest`, 8-core CPU host, the E-commerce
sample project, 13 approved references (`tests/data/execution_references.json`).

| Measure | Result | Target |
| --- | --- | --- |
| Execution accuracy | **7.7%** (1 of 13) | 70% (80% stretch) |
| First-attempt validity | 38.5% | n/a |
| Correction success | 62.5% | n/a |
| Latency p50 / p95 | 143.6 s / 172.8 s | 20 s p50 |
| Outcomes | 1 correct, 9 wrong rows, 3 failed after 3 corrections | |

**What kind of wrong.** Six of the nine wrong-row answers are real model errors, for example
`SUM(p.product_id)` as inventory value and `AVG(p.product_id)` as average order value. Three
(ecom-05, 07, 08) are reasonable answers that returned ids where the reference returns names; the
scorer compares rows exactly, so they count as wrong. A lenient scorer would reach about 4 of 13
(31%), still far below target. Failures were `DRY_RUN_FAILED` (ecom-02) and `UNKNOWN_COLUMN`
(ecom-12, 21).

**How the references were approved.** Not by a person. The DBA delegated the review to Claude,
which compiled each reference, ran it, and compared the rows with SQL written independently from
the question and the physical tables (all 13 matched), without looking at model output. 13 were
approved; ecom-14, 15, 16, 24 were rejected (the sample model has no `reviews` or `coupons`) and
ecom-04, 20 were rejected (they return no rows on the demo data, so they cannot tell a right query
from a wrong one). The demo data is tiny (3 customers, 4 orders): this is a regression signal, not
a benchmark.

**Compared with the earlier provisional run** (20%, 69 s p50, 15 draft references on 2026-10-13):
the sets differ, so the numbers are not comparable, and the median time doubled. The cause of the
slower answers was not investigated; knowledge, summary and suggestion calls were added after
the earlier run and each adds model time on a CPU.

## Model prompt-adherence (not accuracy)

Source: `reports/day3-golden-bakeoff-20261012.json`, 40 questions, generation only.

| Measure | Result |
| --- | --- |
| Valid single read-only SELECT over known tables | 39 of 40 (97.5%) |
| Every expected model present in the SQL | 30 of 40 (75%) |
| Generation latency p50 / p95 | 12.6 s / 27.0 s |
| Speed | 4.7 tokens/s |

## Safety and access (these are gates, and they pass)

| Suite | Result |
| --- | --- |
| Red-team (`make redteam`) | 70 passed |
| Row-level security and database permissions, live PostgreSQL | all passed, none skipped (118) |
| Whole test suite in the Compose `tests` service | 1064 passed, 14 skipped |
| `make check` (ruff, mypy, import-linter, pytest) | 898 passed, 180 skipped (live tests need the stack) |
| Failure drills (`make drills`) | 29 passed |
| UI unit tests / Playwright (mock API) | 36 / 75 passed |
| `make offline` (all services blocked from the internet) | passed |

## What would move the number

1. A stronger SQL model: only `llama3:latest` has ever been tried. Pull a code-tuned model (the
   plan names Qwen2.5-Coder), set `SQL_MODEL`, `make models`, `make eval`.
2. A GPU (`make up GPU=1`): the only way to approach the 20 s target.
3. Read the failures case by case (prompts, retrieval, validator messages) and change one lever
   per run with `scripts/compare_eval.py`. Do not edit the references to fit the model.
4. Have a person review the references; replace the delegated approvals and re-run.
