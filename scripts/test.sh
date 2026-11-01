#!/bin/sh
# Run the unit tests inside the orchestrator image (python:3.12-slim), offline and without secrets.
set -eu
cd "$(dirname "$0")/.."
[ -f .env ] || : > .env   # compose needs the file to exist; the test service does not read it
docker compose --profile test build test
docker compose --profile test run --rm test "$@"
