.PHONY: test test-local smoke up down models

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
