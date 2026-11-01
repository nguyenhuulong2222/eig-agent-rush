# Deploying EIG Sandbox Agent on Vultr

> Requirements only for now; the full step-by-step (VM, Caddy, ufw, `deploy.sh`) is written in M5.

## Target

- **Ubuntu 24.04 LTS** VM, **4 GB RAM** plan, SSH key auth. Add a **2–4 GB swap file**: Chromium in
  the sandbox can use up to its 1 GiB cap while the other services run.
- **Docker CE from Docker's official apt repository** (not the Ubuntu `docker.io` package).
  **Docker Engine ≥ 26 is required**: the sandbox mounts its per-job input/output with
  *volume subpaths*, which older engines do not support. `deploy.sh` checks `docker version`
  and stops early on anything older than 26.
- Open ports: 22, 80, 443 only (ufw). Caddy terminates HTTPS; nothing else is published.

## Disk and build time

| Image | Size | Notes |
|---|---|---|
| `eig-sandbox` | **~3.7 GB** | Playwright 1.64.0 base (Chromium) + tesseract (eng/vie/spa/fil). The biggest build step: expect several minutes for the first pull/build on a small VM. Build it once; it is reused for every task. |
| `eig-orchestrator`, `eig-web`, `eig-county-site` | ~150–200 MB each | `python:3.12-slim` |
| `tecnativa/docker-socket-proxy` | ~10 MB | pinned by digest |

Plan for **≥ 10 GB free disk** before the first build (images + build cache), and prune the
build cache afterwards (`docker builder prune`).

## Secrets

Only the orchestrator reads `.env`: `VULTR_INFERENCE_API_KEY`, `EIG_APPROVER_TOKEN`,
`EIG_DEMO_TOKEN`. Never commit it; `.env.example` lists every variable.
