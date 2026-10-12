#!/usr/bin/env sh
set -eu

docker compose exec -T api uv run --no-sync python scripts/test-egress.py
