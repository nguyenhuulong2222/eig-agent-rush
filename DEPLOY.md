# Deploying EIG Sandbox Agent on Vultr

Everything below is done by you. The scripts in this repo never create, change or delete Vultr
resources; `deploy.sh` only configures the VM it runs on.

**What runs on the VM** (one `docker compose` project): Caddy (HTTPS, the only public ports) →
web (UI + read-only county mirror) → orchestrator (key, limits, ledger) → socket-proxy (Docker
API policy) → one throwaway sandbox container per task → county-site (internal network only).

## Requirements

- **Ubuntu 24.04 LTS x64** VM with SSH key login. **Deployed on `vx1-g-2c-8g-120s`** (2 vCPU,
  8 GB RAM, 120 GB local NVMe, Silicon Valley); 4 GB RAM is the minimum. `deploy.sh` adds a
  **4 GB swap file** if there is none (Chromium in the sandbox can use its full 1 GiB).
- **Docker CE from Docker's official apt repository**, **Engine ≥ 26** (sandbox input/output use
  *volume subpaths*). `deploy.sh` installs it if missing and **stops early** on anything older.
- Open ports **22, 80, 443** only (ufw, set by `deploy.sh`). Docker's published ports bypass ufw,
  so only Caddy publishes public ports; web is bound to `127.0.0.1`, nothing else is published.
- **Disk: ≥ 10 GB free** before the first build.

| Image | Size | Notes |
|---|---|---|
| `eig-sandbox` | **~3.7 GB** | Playwright 1.64.0 (Chromium) + tesseract eng/vie/spa/fil. The slowest build step: several minutes on first pull/build on a small VM. Built once, reused for every task. |
| `eig-orchestrator`, `eig-web`, `eig-county-site` | ~150–200 MB each | `python:3.12-slim` |
| `caddy:2.11.7-alpine`, `tecnativa/docker-socket-proxy:v0.5.0` | ~50 MB, ~10 MB | pinned by digest |

> **Architecture risk (arm64 → x86_64).** Everything was built and tested on an arm64 Mac.
> The VM is x86_64, so `deploy.sh` builds **all images on the VM** and refuses images that are
> not `amd64` (never ship Mac-built images). Playwright/Chromium rendering, tesseract output and
> the timing of the 60 s cap are **untested on x86 until the first deploy**; `deploy.sh` runs
> `make smoke`, which exercises all three from inside a real sandbox, before declaring success.

## Step by step

### 1. Create the VM (Vultr console)

1. **Products → Compute → Deploy → Cloud Compute** (Shared CPU is enough).
2. Location: one close to you / the judges.
3. Image: **Ubuntu 24.04 LTS x64**.
4. Plan: used here **`vx1-g-2c-8g-120s`** (2 vCPU, 8 GB RAM, 120 GB NVMe). 4 GB RAM is the minimum; disk ≥ 50 GB.
5. **SSH Keys → Add**: paste your public key (`cat ~/.ssh/id_ed25519.pub` on the Mac).
6. Hostname `eig-agent`. Deploy, then note the **public IPv4**.

### 2. First login and a non-root user

```bash
ssh root@<VM_IP>
adduser --disabled-password --gecos "" eig
usermod -aG sudo eig
echo 'eig ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/90-eig && chmod 440 /etc/sudoers.d/90-eig
rsync -a --chown=eig:eig /root/.ssh /home/eig/
sudo -l -U eig | grep -q NOPASSWD && echo passwordless-sudo-ok   # deploy.sh needs passwordless sudo (it never prompts)
# keep this root session open; from a NEW terminal on the Mac:  ssh eig@<VM_IP>
# that login must work before you lock root out below
```

Then, as root (in the still-open root session), key-only SSH and no root login. Ubuntu 24.04
cloud images ship `/etc/ssh/sshd_config.d/50-cloud-init.conf`, which can turn password login
back on; sshd uses the first value it reads, so a `00-` drop-in wins:

```bash
printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\n' > /etc/ssh/sshd_config.d/00-eig-hardening.conf
sshd -t && systemctl restart ssh
sshd -T | grep -E '^(passwordauthentication|kbdinteractiveauthentication|permitrootlogin) '
```

### 3. Get the private repo onto the VM (read-only deploy key)

On the VM:

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/eig_deploy -C "eig-agent VM deploy key"
cat ~/.ssh/eig_deploy.pub
printf 'Host github.com\n  IdentityFile ~/.ssh/eig_deploy\n  IdentitiesOnly yes\n' >> ~/.ssh/config
```

On the Mac (adds the key to this repo only, read-only):

```bash
gh repo deploy-key add - --repo nguyenhuulong2222/eig-agent-rush --title "vultr-vm" <<< "<paste the .pub line>"
```

Back on the VM:

```bash
git clone git@github.com:nguyenhuulong2222/eig-agent-rush.git ~/eig-agent-rush
cd ~/eig-agent-rush
```

(Alternative: `gh auth login` on the VM. The deploy key is narrower: one repo, read-only.)

### 4. Configuration

**Do not copy the Mac's `.env`**: its tokens are local test tokens. Let `deploy.sh` create `.env`
from `.env.example` and generate fresh tokens. Until Nov 3 the provider stays `mock`.

If you want your Mac settings (not secrets) on the VM, copy only the non-token lines:

```bash
grep -vE '^EIG_(APPROVER|DEMO)_TOKEN=' .env | ssh eig@<VM_IP> 'cat > ~/eig-agent-rush/.env && chmod 600 ~/eig-agent-rush/.env'
```

### 5. Deploy

```bash
cd ~/eig-agent-rush
./deploy.sh
# own domain instead of sslip.io (an A record pointing at the VM first):
# EIG_DOMAIN=eig.example.org ./deploy.sh
```

The first run installs Docker and may stop once, asking you to log out and in (docker group);
run it again. It prints the new **EIG_APPROVER_TOKEN** and **EIG_DEMO_TOKEN once**. Keep
the approver token private; the demo token is the one published in the submission. It ends
with `make smoke` and an HTTPS check, then prints:

```
Deployed.  Console: https://<a-b-c-d>.sslip.io/   County mirror: https://<a-b-c-d>.sslip.io/county/
```

### Update, operate, rotate

```bash
git pull && ./deploy.sh                 # update (rebuilds on the VM, re-runs smoke)
docker compose logs -f caddy orchestrator
make reset                              # put certified results back (auto-reset does it every 15 min)
make ledger                             # ledger + chain verification
# rotate a token: empty it in .env, re-run ./deploy.sh (prints the new one once)
sed -i 's/^EIG_DEMO_TOKEN=.*/EIG_DEMO_TOKEN=/' .env && ./deploy.sh
```

### Troubleshooting

| Symptom | Fix |
|---|---|
| HTTPS check fails on first start | Caddy is still getting the certificate. `docker compose logs caddy`; port 80 must be reachable from the internet (ufw allows it; check no Vultr firewall group blocks it). |
| `permission denied ... docker.sock` | Log out and back in after `deploy.sh` added you to the `docker` group. |
| Smoke fails at "real sandbox" | `docker compose logs orchestrator`; check `docker image ls eig-sandbox` exists and is `amd64`; `free -h` shows swap. |
| Disk full | `docker builder prune -af` and `docker image prune -f`. |

## Nov 3 checklist (after credits are applied)

Keep model spend tiny while you check: `EIG_DAILY_SPEND_CAP_USD=0.50` (the `.env.example` default)
stays until the real per-token prices are set in step 5.

1. **Apply the credits** in the Vultr console (Billing).
2. **Create a Serverless Inference API key** (Products → Serverless Inference). Put it in the
   VM's `.env` as `VULTR_INFERENCE_API_KEY=` (never in git, never on the Mac's clipboard history).
3. **List the models** (one GET, no tokens spent):
   ```bash
   docker compose exec orchestrator python scripts/list_models.py
   ```
   Pick `MODEL_REASONING` (and `MODEL_VISION` if any model takes images) and set them in `.env`.
4. **Choose the evidence path**: `EIG_EVIDENCE_PATH=vision` if a vision model exists, else `ocr`.
   Both are in the sandbox image, so this is config only.
5. **Set the real prices.** Vultr Serverless Inference is usage-billed **per model, per token**
   (no monthly fee; months under $1.00 are not charged). Look up the chosen model's input and
   output price on Vultr's model pricing page and set them per role (USD per 1M tokens):
   `EIG_PRICE_REASONING_IN_PER_MTOK` / `_OUT_` for `MODEL_REASONING` (also used by the planner,
   fact extraction and the incident report) and `EIG_PRICE_VISION_IN_PER_MTOK` / `_OUT_` for
   `MODEL_VISION`. Empty role prices fall back to the shared placeholder pair
   `EIG_PRICE_IN_PER_MTOK` / `EIG_PRICE_OUT_PER_MTOK`. Keep `EIG_DAILY_SPEND_CAP_USD=0.50` until
   then; raise it only on purpose afterwards.
6. **Switch the provider**: `EIG_LLM_PROVIDER=vultr`, then `docker compose up -d orchestrator`.
   If the model rejects `json_schema`, set `EIG_RESPONSE_FORMAT=json_object` (or `none`).
7. **Five capped live runs** (each is rate-limited and counted against the spend cap):
   1. clean check (also runs the 4-language translation check against the live pages)
   2. `make tamper` → check → expect ROLLBACK (RES-003); approve with the approver token
   3. `make tamper-swap` → check → the model's verdicts decide (record what it says)
   4. multilingual drift on the fixtures:
      `docker compose exec orchestrator python -m eig_agent multilingual --scenario drift`
   5. red-team drill → expect 5 × CONTAINED

   Write the outcomes (verdicts, latency, cost per run) into the README's evaluation section.
8. **Re-record the replay test** (it is skipped until a recorded run exists). On the Mac with
   the key in the local `.env` and `EIG_LLM_PROVIDER=vultr`:
   `python3 -m eig_agent record tamper`, then `make test` (the skipped test now runs) and commit
   `eig_agent/web/recorded/tamper.json`.
9. **Deploy**: `git pull && ./deploy.sh` on the VM.
10. **Verify the public URL on your phone**: console loads, Simulate tamper → Run check →
    Approve (demo token) → Seal MATCH; red-team drill; `/county/` opens; ledger badge is green.
