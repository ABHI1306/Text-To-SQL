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
