.PHONY: test test-local smoke up down models sandbox tamper tamper-swap reset check redteam ledger

test:            ## unit tests in the production image (python:3.12-slim, offline, no secrets)
	./scripts/test.sh

test-local:      ## unit tests with the local Python
	python3 -m unittest discover -s . -t .

smoke:           ## isolation smoke test against the running stack
	./scripts/smoke.sh

up:
	docker compose up -d --build

down:
	docker compose down

models:          ## list Vultr Inference models (needs VULTR_INFERENCE_API_KEY in .env)
	python3 scripts/list_models.py

sandbox:         ## build the sandbox image (Chromium + tesseract)
	docker compose --profile build build sandbox

tamper:          ## demo: change one Mayor number on the live site (BLOCK rule -> ROLLBACK proposal)
	docker compose --profile tools run --rm ops python scripts/tamper.py --mode one

tamper-swap:     ## demo: move votes, totals kept (the model's verdicts decide)
	docker compose --profile tools run --rm ops python scripts/tamper.py --mode swap

reset:           ## demo: put the certified results back
	docker compose --profile tools run --rm ops python scripts/reset.py

check:           ## start an agent check (prints the job)
	curl -s -X POST localhost:8000/api/tasks -H 'Content-Type: application/json' -d '{"kind":"check"}'; echo

redteam:         ## start the red-team drill (prints the job)
	curl -s -X POST localhost:8000/api/tasks -H 'Content-Type: application/json' -d '{"kind":"redteam"}'; echo

ledger:          ## show the ledger and its verification
	curl -s localhost:8000/api/ledger | python3 -m json.tool | head -60
