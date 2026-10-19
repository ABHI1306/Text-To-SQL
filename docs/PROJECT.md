# Text-to-SQL Platform: as built (v0.1.0)

The planning document is `docs/Text-to-SQL Agent — Project Document & 10-Day Claude Code Build Plan.pdf`.
This page describes what exists. Reasons for choices are in [DECISIONS.md](DECISIONS.md); what
does not work is in [KNOWN_ISSUES.md](KNOWN_ISSUES.md).

## Shape

- **Local only.** PostgreSQL is the only data source; models run in Ollama; the Compose network
  is internal. Saved connection passwords are Fernet-encrypted; the app reaches data through a
  read-only account.
- **Hexagonal.** `app/ports` are interfaces (7+ ports: LLM, embedder, vector store, warehouse,
  catalog, directory/identity, metadata repositories, audit, scheduler). `app/adapters` are the
  implementations: Ollama, Qdrant, PostgreSQL (warehouse and metadata), LDAP, OIDC.
  `lint-imports` forbids ports and core from importing adapters.
- **Processes.** `api` (FastAPI, `app/main.py`), `worker` (APScheduler loop, `app/worker`), the
  React UI (Vite, run on the host), plus PostgreSQL x2 (metadata, demo data), Qdrant, Ollama;
  OpenLDAP and Keycloak with `IDENTITY=1`.

## A question's path

`app/agent/graph.py` (LangGraph): rewrite follow-up, classify intent (data question, clarify,
chat), retrieve model context (hybrid search with relationship expansion and column pruning),
generate logical SQL (structured output, one repair retry), compile (`app/semantic/compiler.py`:
model CTEs, calculated fields, metrics, relationship-checked joins, row policies as bound
parameters), validate (allowlist plus `EXPLAIN`), execute read-only with a statement timeout,
summarise and chart. Up to three typed corrections. Every failure is a typed, structured error.
Nothing except a compiled, validated single `SELECT` executes.

## Capabilities and roles

19 capabilities (`app/api/routes.py:_CAPABILITIES`); every route declares one or is listed as
public or any-signed-in (`tests/test_route_permissions.py` walks all routes). System roles:
**DBA** (all), **Admin** (users, roles, audit), **User** (ask, give feedback, view shared);
custom roles are built from the capability list.

## Features by area

| Area | What exists |
| --- | --- |
| Sign-in | local passwords (Argon2, lockout, rotating refresh cookie), LDAP (StartTLS), OIDC (code + PKCE), group-to-role mapping, session revocation |
| Modelling | introspection, table selection, read-only live table preview (up to 50 rows), exact row counts for the visible catalog page, profiling with redaction, canvas editor, calculated fields, metrics, deploy report, versioned deploy with atomic index switch, drift detection |
| Asking | streaming steps, logical and compiled SQL, charts, summary, follow-ups, saved threads, edit and re-run SQL |
| Knowledge | instructions, approved SQL pairs, proposals from feedback |
| Governance | row policies (attribute-based), audit log with export, allowed-hosts setting |
| Collaboration | saved questions, dashboards, scheduled questions, sharing with people or roles, notifications |
| Operations | `/health/ready`, tokens and model time per question, scheduler health, execution-accuracy runs |

## Data

Metadata PostgreSQL (migrations `0001` to `0008` in `migrations/versions`): users, roles,
sessions, projects and encrypted connections, MDL revisions, policies, audit, threads,
knowledge, jobs and leases, dashboards, shares, caches, notifications, eval runs. Qdrant holds
one collection with a project and model-version filter.

## Tooling

`make check` (ruff, mypy, import-linter, pytest), `make redteam`, `make rls`, `make drills`,
`make e2e` (Playwright on the UI mock API), `make offline`, `make eval`. Operator commands:
`make init-env | up | down | models | bootstrap | reindex | rotate-key`. CLIs in `app/cli`
(`users`, `ask`, `maintenance`).

## Coding standards and security checks

Standards are enforced by tools, not by a prose style guide. Configuration: `ruff.toml` (the only
ruff config; ruff ignores `[tool.ruff]` in `pyproject.toml` when `ruff.toml` exists), `mypy.ini`,
`[tool.importlinter]` in `pyproject.toml`.

| Check | Where | What it catches |
| --- | --- | --- |
| ruff `E F I B` | `make check`, pre-commit, CI | style errors, unused code, import order, common bugs |
| ruff `S` (flake8-bandit) | `make check`, pre-commit, CI | insecure patterns: SQL built from strings, weak hashes, unvetted URL opens |
| mypy `strict` | `make check`, CI | missing or wrong types |
| import-linter | `make check`, CI | ports and core importing adapters |
| `make redteam`, `make rls` | CI is not wired to these; run before release | hostile SQL, row-level security leaks |
| `make audit` (`pip-audit`, `npm audit --audit-level=high`) | CI | known vulnerabilities in locked dependencies |
| gitleaks | pre-commit, CI (`secrets` job), `make secrets` | committed secrets, including history |
| Dependabot (`.github/dependabot.yml`) | GitHub, weekly | outdated Python, npm and Actions dependencies |

Suppressed on purpose (`ruff.toml` explains each): `S608` in the metadata adapters, where
f-strings only join module-level column lists or fixed WHERE fragments and every value is a `%s`
parameter; `S101`, because the asserts narrow types after `INSERT ... RETURNING`; `E501`, because
line length is left to `ruff format`.

Not covered: `ruff format` is not enforced (about 80 files would change), no container image
scan, no coverage gate, and the security track has no second human reviewer (KNOWN_ISSUES).

## Definition of done, honestly

| Item | Status |
| --- | --- |
| DBA signs in (password, LDAP, SSO), connects, models, deploys, adds knowledge and policies, schedules, builds a dashboard, shares; user signs in, asks, opens shared | Built. Browser LDAP/SSO logins not automated |
| Questions stream steps, SQL, rows, answer, chart; follow-ups over 5+ turns | Built; follow-ups depend on the small model (issue 4) |
| Only a compiled, validated single SELECT executes; red-team 100% | Met (70/70) |
| Zero rows outside policy across the RLS suite, dashboards, scheduled runs, shares | Met on the live suites (118 passed, none skipped) |
| `make eval` at or above the Day 3 target, report committed | **Not met**: first real baseline is 7.7% (1 of 13) vs 70%, median 143.6 s vs 20 s; `reports/eval-20261017.json` is committed |
| Every endpoint declares a capability; route-walk and 403 tests pass | Met |
| `docker compose up` starts the stack and answers with outbound blocked; README, this page, OpenAPI current | Stack and offline test met; a full clean-machine first-answer run not yet done |
