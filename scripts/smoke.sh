#!/bin/sh
# Isolation smoke test against the running stack (`docker compose up -d --build` first).
# Prints PASS/FAIL per check; exit 1 if any check fails.
set -u
cd "$(dirname "$0")/.."
FAIL=0
pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1${2:+  -- $2}"; FAIL=1; }
# expect_fail NAME SERVICE CMD...: the command must exit non-zero
expect_fail() { name=$1; svc=$2; shift 2
  if out=$(docker compose exec -T "$svc" "$@" 2>&1); then fail "$name" "$out"; else pass "$name"; fi; }
expect_ok() { name=$1; svc=$2; shift 2
  if out=$(docker compose exec -T "$svc" "$@" 2>&1); then pass "$name"; else fail "$name" "$out"; fi; }
py() { echo "import sys, urllib.request as u; $1"; }

echo "== county-site"
expect_fail "county-site cannot reach the internet" county-site \
  python -c "$(py "u.urlopen('https://example.com', timeout=5)")"
expect_fail "county-site cannot write its results dir" county-site sh -c 'echo x > /srv/site/data/results.json'
expect_fail "county-site cannot write its root fs" county-site sh -c 'echo x > /srv/site/index.html'
expect_ok   "county-site serves results" county-site \
  python -c "$(py "assert u.urlopen('http://127.0.0.1:8080/data/results.json', timeout=5).status == 200")"

echo "== web (public)"
# Exact allowlist of env names. GPG_KEY is the public CPython release-signing fingerprint set by
# the official python image, not a secret.
expect_ok   "web env is exactly the allowlist (no secrets, no DOCKER_HOST)" web \
  python -c "import os,sys; ok={'EIG_ORCHESTRATOR_URL','GPG_KEY','HOME','HOSTNAME','LANG','PATH','PYTHONDONTWRITEBYTECODE','PYTHONUNBUFFERED','PYTHON_SHA256','PYTHON_VERSION'}; bad=sorted(set(os.environ)-ok); print(bad); sys.exit(1 if bad else 0)"
expect_fail "web has no Docker socket" web test -e /var/run/docker.sock
expect_fail "web has no .env file" web sh -c 'ls /app/.env /.env 2>/dev/null | grep -q .'
expect_fail "web image has no orchestrator code" web test -e /app/eig_agent/llm.py
expect_fail "web has no openai SDK" web python -c "import openai"
expect_fail "web cannot reach socket-proxy" web \
  python -c "$(py "u.urlopen('http://socket-proxy:2375/_ping', timeout=5)")"
expect_fail "web cannot reach county-site" web \
  python -c "$(py "u.urlopen('http://county-site:8080/', timeout=5)")"
expect_fail "web cannot write its root fs" web sh -c 'echo x > /app/eig_agent/web.py'
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/status)
[ "$code" = 200 ] && pass "web forwards /api to orchestrator (host -> :8000)" || fail "web forwards /api" "$code"

code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -d '{}' http://127.0.0.1:8000/api/proposals/p000000000000/approve)
case "$code" in 401|503) pass "approve without a token is refused ($code)";; *) fail "approve without a token" "$code";; esac
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -d '{}' -H 'Authorization: Bearer wrong-token-wrong-token' http://127.0.0.1:8000/api/proposals/p000000000000/reject)  # gitleaks:allow (deliberately invalid)
case "$code" in 403|503) pass "reject with a wrong token is refused ($code)";; *) fail "reject with a wrong token" "$code";; esac

echo "== orchestrator (private)"
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:8001/healthz)
[ "$code" = 000 ] && pass "orchestrator not published on the host" || fail "orchestrator published on host" "$code"
expect_fail "orchestrator has no Docker socket" orchestrator test -e /var/run/docker.sock
expect_fail "orchestrator cannot reach county-site directly" orchestrator \
  python -c "$(py "u.urlopen('http://county-site:8080/', timeout=5)")"

echo "== socket-proxy policy + one sandbox lifecycle (from inside the orchestrator)"
if ! docker image inspect eig-sandbox:smoke >/dev/null 2>&1; then
  docker pull -q python:3.12-slim >/dev/null && docker tag python:3.12-slim eig-sandbox:smoke
fi
docker compose exec -T orchestrator python scripts/smoke_proxy.py || FAIL=1

echo "== real sandbox image: limits from inside, Chromium, timeout, cleanup"
docker image inspect eig-sandbox:latest >/dev/null 2>&1 || docker compose --profile build build sandbox
docker compose exec -T orchestrator python scripts/smoke_sandbox.py || FAIL=1

echo
[ $FAIL = 0 ] && echo "SMOKE: ALL PASS" || echo "SMOKE: FAILURES ABOVE"
exit $FAIL
