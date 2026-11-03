#!/usr/bin/env bash
# Deploy EIG Sandbox Agent on a fresh Vultr VM (Ubuntu 24.04, x86_64). Idempotent: safe to re-run.
#
#   cd ~/eig-agent-rush && ./deploy.sh            # first deploy or update
#   EIG_DOMAIN=eig.example.org ./deploy.sh        # use your own domain instead of sslip.io
#
# What it does, in order (each step checks first and only changes what is missing):
#   1. checks: Ubuntu 24.04, x86_64, passwordless sudo; installs make/git/curl/openssl/ufw if missing
#   2. Docker CE from Docker's official apt repo if missing; fails early unless Engine >= 26
#   3. 4 GB swap file if the VM has no swap
#   4. ufw: deny incoming except 22, 80, 443 (tcp)
#   5. .env: created from .env.example if absent; EIG_APPROVER_TOKEN / EIG_DEMO_TOKEN generated
#      with openssl if empty (printed ONCE, right after generation); EIG_HOSTNAME from the VM IP
#   6. builds every image ON THIS VM (never ship images built on another CPU architecture)
#   7. docker compose up -d, then `make smoke` and an HTTPS health check
#
# It never creates, changes or deletes Vultr resources.
set -euo pipefail
cd "$(dirname "$0")"

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '   ok   %s\n' "$*"; }
die()  { printf '\n\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
# Non-interactive sudo everywhere (-n): any sudo call fails fast instead of waiting for a password.
SUDO=""; [ "$(id -u)" -eq 0 ] || SUDO="sudo -n"

# ---------------------------------------------------------------------------------------------
say "1/7 Host checks"
[ -r /etc/os-release ] || die "no /etc/os-release: this script targets Ubuntu 24.04"
. /etc/os-release
[ "${ID:-}" = "ubuntu" ] && [ "${VERSION_ID:-}" = "24.04" ] || die "Ubuntu 24.04 required (found ${ID:-?} ${VERSION_ID:-?})"
ok "Ubuntu ${VERSION_ID}"
ARCH=$(uname -m)
[ "$ARCH" = "x86_64" ] || die "x86_64 expected (found $ARCH); the images are built for the VM's own architecture"
ok "arch $ARCH"
# `sudo -v` would prompt when sudoers uses verifypw=all and the user also matches the sudo group's
# password rule, even with a NOPASSWD entry. `sudo -n true` checks what this script actually needs.
[ -z "$SUDO" ] || $SUDO true || die "passwordless sudo is required (see DEPLOY.md step 2: /etc/sudoers.d/90-eig)"
need=()
for pkg in make git curl openssl ufw ca-certificates; do
  dpkg -s "$pkg" >/dev/null 2>&1 || need+=("$pkg")
done
if [ ${#need[@]} -gt 0 ]; then
  $SUDO apt-get update -qq && $SUDO apt-get install -y -qq "${need[@]}"
fi
ok "packages: make git curl openssl ufw"

# ---------------------------------------------------------------------------------------------
say "2/7 Docker CE (official repo, Engine >= 26)"
if ! command -v docker >/dev/null 2>&1; then
  $SUDO install -m 0755 -d /etc/apt/keyrings
  $SUDO curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  $SUDO chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    | $SUDO tee /etc/apt/sources.list.d/docker.list >/dev/null
  $SUDO apt-get update -qq
  $SUDO apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  $SUDO systemctl enable --now docker
fi
if ! docker info >/dev/null 2>&1; then
  if [ -n "$SUDO" ] && ! id -nG | grep -qw docker; then
    $SUDO usermod -aG docker "$USER"
    die "added $USER to the docker group: log out and back in (or run 'newgrp docker'), then re-run ./deploy.sh"
  fi
  die "the Docker daemon is not reachable"
fi
SERVER_VERSION=$(docker version --format '{{.Server.Version}}')
MAJOR=${SERVER_VERSION%%.*}
[ "$MAJOR" -ge 26 ] 2>/dev/null || die "Docker Engine >= 26 required for volume subpaths (found $SERVER_VERSION). Upgrade from download.docker.com."
ok "Docker Engine $SERVER_VERSION"
docker compose version >/dev/null 2>&1 || die "docker compose plugin missing (apt-get install docker-compose-plugin)"
ok "$(docker compose version)"

# ---------------------------------------------------------------------------------------------
say "3/7 Swap"
if [ -z "$(swapon --show --noheadings)" ]; then
  $SUDO fallocate -l 4G /swapfile
  $SUDO chmod 600 /swapfile
  $SUDO mkswap /swapfile >/dev/null
  $SUDO swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' | $SUDO tee -a /etc/fstab >/dev/null
  echo 'vm.swappiness=10' | $SUDO tee /etc/sysctl.d/99-eig-swap.conf >/dev/null
  $SUDO sysctl -q -p /etc/sysctl.d/99-eig-swap.conf
  ok "4 GB swap added"
else
  ok "swap present: $(swapon --show --noheadings | awk '{print $1, $3}' | tr '\n' ' ')"
fi

# ---------------------------------------------------------------------------------------------
say "4/7 Firewall (ufw: 22, 80, 443 only)"
$SUDO ufw default deny incoming >/dev/null
$SUDO ufw default allow outgoing >/dev/null
for rule in 22/tcp 80/tcp 443/tcp; do $SUDO ufw allow "$rule" >/dev/null; done
$SUDO ufw --force enable >/dev/null
ok "$($SUDO ufw status | head -1)"
# Docker publishes ports through its own iptables chain, which bypasses ufw. That is why only
# Caddy publishes public ports (80/443); web is bound to 127.0.0.1 and nothing else is published.

# ---------------------------------------------------------------------------------------------
say "5/7 Configuration (.env)"
[ -f .env ] || { cp .env.example .env; ok ".env created from .env.example"; }
chmod 600 .env
getv() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- | sed 's/[[:space:]]*#.*$//; s/^[[:space:]]*//; s/[[:space:]]*$//'; }
setv() {  # replace KEY=... (keeping any inline comment out) or append
  if grep -qE "^$1=" .env; then
    local tmp; tmp=$(mktemp)
    awk -v k="$1" -v v="$2" 'BEGIN{FS=OFS="="} $1==k {print k "=" v; next} {print}' .env > "$tmp" && cat "$tmp" > .env && rm -f "$tmp"
  else
    printf '%s=%s\n' "$1" "$2" >> .env
  fi
}
newtoken() { openssl rand -base64 30 | tr '+/' '-_' | tr -d '=\n'; }
NEW_TOKENS=""
for key in EIG_APPROVER_TOKEN EIG_DEMO_TOKEN; do
  cur=$(getv "$key")
  if [ ${#cur} -lt 16 ]; then
    setv "$key" "$(newtoken)"
    NEW_TOKENS="$NEW_TOKENS $key"
  fi
done
if [ -n "${EIG_DOMAIN:-}" ]; then
  HOST="$EIG_DOMAIN"
else
  IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<NF;i++) if ($i=="src") print $(i+1)}')
  [ -n "$IP" ] || die "could not find this VM's public IPv4; set EIG_DOMAIN=your.domain and re-run"
  case "$IP" in 10.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*|192.168.*)
    die "the default route uses a private address ($IP); set EIG_DOMAIN to a name that points at the VM";;
  esac
  HOST="${IP//./-}.sslip.io"
fi
setv EIG_HOSTNAME "$HOST"
setv COMPOSE_FILE "docker-compose.yml:docker-compose.prod.yml"
setv EIG_TRUSTED_PROXY_HOPS 2
ok "EIG_HOSTNAME=$HOST"
ok "provider: $(getv EIG_LLM_PROVIDER || true) (set EIG_LLM_PROVIDER=vultr + key + MODEL_REASONING on Nov 3)"
if [ -n "$NEW_TOKENS" ]; then
  printf '\n   \033[33mNew tokens (shown once; they are stored only in .env on this VM):\033[0m\n'
  for key in $NEW_TOKENS; do printf '     %s=%s\n' "$key" "$(getv "$key")"; done
  printf '   Keep EIG_APPROVER_TOKEN private. EIG_DEMO_TOKEN is the one you publish for judges.\n'
fi

# ---------------------------------------------------------------------------------------------
say "6/7 Build images on this VM ($ARCH)"
export EIG_GIT_COMMIT
EIG_GIT_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)
docker compose --profile build build sandbox
docker compose build
for img in eig-sandbox:latest eig-orchestrator eig-web eig-county-site; do
  a=$(docker image inspect --format '{{.Architecture}}' "$img" 2>/dev/null || echo missing)
  [ "$a" = "amd64" ] || die "image $img is '$a', expected amd64"
done
ok "all images amd64 (commit $EIG_GIT_COMMIT)"
docker builder prune -f >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------------------------
say "7/7 Start, smoke test, health check"
docker compose up -d --remove-orphans
sleep 5
make smoke
URL="https://$HOST"
for _ in $(seq 1 30); do
  if curl -fsS --max-time 10 "$URL/healthz" >/dev/null 2>&1 && curl -fsS --max-time 10 "$URL/api/status" >/dev/null 2>&1; then
    ok "HTTPS health check passed: $URL"
    printf '\n\033[32mDeployed.\033[0m  Console: %s/   County mirror: %s/county/\n' "$URL" "$URL"
    exit 0
  fi
  sleep 5   # first start: Caddy is getting the certificate
done
die "HTTPS health check failed for $URL (see: docker compose logs caddy)"
