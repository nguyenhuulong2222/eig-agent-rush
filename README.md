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
- **Only the orchestrator** talks to Docker, to Vultr Inference, and writes the county site
  (restore, after human approval).

## Layout

| Path | What |
|---|---|
| `eig_agent/` | Orchestrator + web UI (Python 3.12, stdlib HTTP server, OpenAI-compatible client) |
| `checker/` | Deterministic results and site rules (stdlib only) |
| `public/`, `results/` | Fictional Kanaloa County site and its certified results |
| `scripts/build_site.py` | Builds the site's `data/results.json` and SHA-256 manifest |
| `docker/` | Images: `app`, `county-site`, `sandbox` |
| `docker-compose.yml` | Services and networks |

## Run (development)

```bash
cp .env.example .env                 # leave EIG_LLM_PROVIDER=mock until you have a key
python3 -m unittest discover -s . -t .
docker compose up --build            # app on http://127.0.0.1:8000
```

Status: M1 skeleton. Vultr Inference client (M2), sandbox runner (M3), UI (M4),
Vultr deploy (M5) and submission docs (M6) follow.

## License

MIT — see [LICENSE](LICENSE).
