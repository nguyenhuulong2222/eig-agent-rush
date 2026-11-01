Built on my earlier Election Integrity Guard; the Vultr sandbox agent, Vultr Inference integration and Vultr deployment were added during Agent Rush (Nov 3–8, 2026).

# EIG Sandbox Agent — Kanaloa County Elections

Vultr "Agent Rush" hackathon, Challenge 1: **Blast Radius Zero**.

An integrity agent for a fictional county election website. It reads the live site through a
throwaway, locked-down browser sandbox, checks the published results against a SHA-256
**Results Seal**, and asks an LLM on Vultr Serverless Inference to explain what changed and
how serious it is. A person approves any restore. The sandbox never holds a secret, never
reaches the internet, and never has write access to the site.

> **Fictional demo data.** Kanaloa County does not exist. No real election, candidate or result is shown.

## Principles

- **The Results Seal is the only tamper detector.** Code does the math (hashes, diffs,
  arithmetic rules). The LLM only explains, classifies severity and routes
  (ROLLBACK / ESCALATE / HUMAN_SIGNOFF).
- **Real work runs in a sandbox.** One fresh container per task: non-root, read-only rootfs,
  no capabilities, memory/CPU/PID/time caps, internal network that reaches only the county site,
  no env vars, destroyed afterwards.
- **Only the orchestrator** talks to Docker (through a policy proxy), to Vultr Inference, and
  writes the county results (restore, after human approval).

## Containment design (layers)

Each layer holds only what it needs; compromising one does not hand over the next.

| Layer | Holds | Can reach | Cannot |
|---|---|---|---|
| **web** (public) | static UI, a `/api` forwarder (stdlib only) | orchestrator | see any secret, reach Docker, reach the county site; its image does not even contain the orchestrator code or the OpenAI SDK |
| **orchestrator** (private) | Vultr Inference key, rate limits, spend cap, ledger | Vultr Inference (egress), socket-proxy | reach the Docker socket directly, reach the county site over the network |
| **socket-proxy** (private) | the Docker socket (read-only mount) | Docker engine | be reached by web or the sandbox |
| **county-site** | the fictional site; results mounted **read-only** | nothing outside `eig_sandbox_net` | reach the internet, rewrite its own results |
| **sandbox** (per task) | nothing: no env, no secrets | county-site only | reach the internet, write its rootfs, gain privileges, outlive the task |

**Docker socket proxy.** `tecnativa/docker-socket-proxy` (pinned by digest) runs with our own
HAProxy template (`docker/socket-proxy/haproxy.cfg.template`) instead of the stock switches,
because the stock `POST=1` flag would also allow image pulls/deletes and archive uploads.
Allowed: `_ping`, `version`, container `create/start/stop/kill/wait/inspect/logs/remove`,
image inspect. Everything else (exec, build, volumes, networks, image pull/push/delete,
archive, commit, swarm, system, events…) returns 403. A `containers/create` body must name
the `eig-sandbox` image and `eig_sandbox_net`, and is refused if it asks for privileged mode,
added capabilities, bind mounts or `Binds`, any volume other than `eig_sandbox_out`, host
namespaces, devices, the Docker socket, or relaxed seccomp/AppArmor. Refused as well: bodies
over 16 KB, chunked bodies, duplicate keys and any backslash escape (the Docker engine's JSON
decoder matches keys case-insensitively and decodes `\u` escapes, so these would otherwise
slip past a pattern). The body check is defense in depth on top of the orchestrator building
the spec in code; it is a pattern filter, not a JSON parser.

**Networks.** `edge` (web ↔ host/Caddy), `control` (internal: web → orchestrator), `egress`
(orchestrator → Vultr Inference), `docker_api` (internal: orchestrator → socket-proxy),
`eig_sandbox_net` (internal: sandbox → county-site).

Verified by `make smoke` (see below): web/county-site/orchestrator isolation, 15 forbidden
Docker API calls and 14 forbidden create specs refused, and one sandbox-shaped container
created → run → inspected → destroyed through the proxy, reporting from inside: non-root,
read-only rootfs, no secret env, county-site reachable, internet blocked.

## LLM provider

All model calls go through one OpenAI-compatible client (`eig_agent/llm.py`).

| `EIG_LLM_PROVIDER` | What happens |
|---|---|
| `mock` (default) | Offline, free, deterministic. Replies are schema-valid and labeled `[MOCK]`. `EIG_MOCK_VERDICT=tampering\|legitimate\|uncertain` picks the triage verdict so each gate branch can be exercised. |
| `vultr` | Vultr Serverless Inference at `VULTR_INFERENCE_BASE_URL` with `VULTR_INFERENCE_API_KEY`; `MODEL_REASONING` for text roles, `MODEL_VISION` for screenshots (if empty, the sandbox's OCR text goes to the reasoning model). |

Spend control: a daily estimated-spend cap (`EIG_DAILY_SPEND_CAP_USD`) reserves each call's
worst case before sending and is re-read from the usage log on restart; per-IP and daily
live-run limits; one live run at a time; the eval refuses more than `EIG_EVAL_MAX_RUNS` paid runs.

`python scripts/list_models.py` lists the available models and flags which accept image input.

## Layout

| Path | What |
|---|---|
| `eig_agent/` | Orchestrator (`server.py`), public web (`web.py`), LLM client, seal, triage, multilingual check |
| `checker/` | Deterministic results and site rules (stdlib only) |
| `public/`, `results/` | Fictional Kanaloa County site and its certified results |
| `docker/` | Images: `web`, `orchestrator`, `county-site`, `sandbox`; socket-proxy policy |
| `scripts/` | `build_site.py`, `list_models.py`, `test.sh`, `smoke.sh` |

The agent uses `checker/`'s results, anomaly and domain rules only. Its repo-governance rules
(CODEOWNERS, merge-request checks from the original GitLab project) are not used by the agent.

## Run (development)

```bash
cp .env.example .env            # leave EIG_LLM_PROVIDER=mock until you have a key
make test                       # unit tests in the production image (python:3.12-slim), offline
docker compose up -d --build    # UI on http://127.0.0.1:8000
make smoke                      # isolation + socket-proxy policy checks against the running stack
```

Status: M2. Sandbox runner (M3), UI (M4), Vultr deploy (M5) and submission docs (M6) follow.

## License

MIT — see [LICENSE](LICENSE).
