# Text-to-SQL Platform

## Project goals
- Local-only Text-to-SQL service
- PostgreSQL warehouse only
- Semantic compiler before database access
- Local LLM via Ollama
- Row-level security enforced by compiler
- Hexagonal architecture with ports and adapters

## Day 1 rules
- Keep the repo green
- Define ports before feature work
- Keep adapters behind interfaces
- Add tests for all contracts
- Make health checks and config available

## Commands
- Install: `uv sync` (UI: `npm --prefix ui ci`)
- Stack: `make init-env && make up && make models && make bootstrap` (`GPU=1`, `IDENTITY=1` add overlays)
- Gate: `make check` (ruff, mypy, import-linter, pytest); also `make redteam rls drills e2e offline eval`
- Operator tasks: `make reindex PROJECT_ID=...`, `make rotate-key OLD=...` (see docs/RUNBOOK.md)
- Docs: docs/PROJECT.md (as built), docs/DECISIONS.md (why), docs/KNOWN_ISSUES.md (what is open)
