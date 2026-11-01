"""Orchestrator API (stdlib http.server). Private: reachable only from the web container.

  python -m eig_agent orchestrator [--port 8001]

This process holds the Vultr Inference key, applies the rate limits and the spend cap, and (from
M3) launches sandbox containers through the Docker socket proxy. It serves JSON only; the UI is
served by the web container (eig_agent/web.py), which forwards /api/* here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import agent, config, demo, sandbox
from .llm import SPEND

# Each proxy in front of this process appends the address it saw to X-Forwarded-For; only entries
# counted from the right are trustworthy. Dev: browser -> web -> here (1 hop). Prod: browser ->
# Caddy -> web -> here (2 hops).
TRUSTED_PROXY_HOPS = int(os.environ.get("EIG_TRUSTED_PROXY_HOPS", "1"))
JOBS = demo.Jobs()
LIMITS = demo.Limits()
TAMPER_LIMITS = demo.Limits(daily=int(os.environ.get("EIG_TAMPER_DAILY_CAP", "100")),
                            per_ip=int(os.environ.get("EIG_TAMPER_IP_HOURLY_CAP", "6")))
AGENT = agent.Agent()
TASK_KINDS = ("check", "redteam")
_SBX_FILE = re.compile(r"^/api/sandbox/(j[0-9a-f]{16})/([a-z0-9_-]{1,40}\.(png|txt|ocr\.txt))$")
_PROPOSAL = re.compile(r"^/api/proposals/(p[0-9a-f]{12})/(approve|reject)$")
MAX_BODY = 2048
SECURITY_HEADERS = {"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"}


def client_ip(forwarded: str, peer: str, hops: int) -> str:
    """The visitor's IP for rate limiting: the entry `hops` places from the right of X-Forwarded-For.

    Leading entries are whatever the client chose to send, so they are never used. With no
    trusted proxy (hops 0) or a header shorter than the proxy chain, fall back to the peer.
    """
    entries = [e.strip() for e in forwarded.split(",") if e.strip()]
    if hops <= 0 or len(entries) < hops:
        return peer
    return entries[-hops]


MIN_TOKEN_LEN = 16
# approver: the county official. demo: published for judges so they can drive the flow themselves;
# same checks, its own label in the ledger.
TOKEN_ENVS = {"approver": "EIG_APPROVER_TOKEN", "demo": "EIG_DEMO_TOKEN"}


def token_id(token: str) -> str:
    """Short, non-reversible label for the ledger: never the token itself."""
    return hashlib.sha256(token.encode()).hexdigest()[:10]


def authorize(header: str) -> tuple:
    """(http_status, who). 200 + "role:hash" when the Bearer token matches a configured token.

    Fail closed: with no token configured (or one shorter than MIN_TOKEN_LEN) approvals are
    disabled (503). Missing/malformed header -> 401, wrong token -> 403. Constant-time compare.
    """
    tokens = {role: os.environ.get(env, "") for role, env in TOKEN_ENVS.items()}
    tokens = {role: tok for role, tok in tokens.items() if len(tok) >= MIN_TOKEN_LEN}
    if not tokens:
        return 503, "approvals are disabled: no approver token is configured on the server"
    scheme, _, given = (header or "").partition(" ")
    if scheme.lower() != "bearer" or not given.strip():
        return 401, "an approver token is required (Authorization: Bearer <token>)"
    given = given.strip().encode()
    match = None
    for role, tok in tokens.items():  # check every token so timing does not reveal which one exists
        if hmac.compare_digest(given, tok.encode()):
            match = f"{role}:{token_id(tok)}"
    return (200, match) if match else (403, "that token is not valid")


def approvals_enabled() -> bool:
    return any(len(os.environ.get(env, "")) >= MIN_TOKEN_LEN for env in TOKEN_ENVS.values())


def visitor_id(ip: str) -> str:
    """Ledger label for a visitor: a salted hash, never the IP."""
    salt = os.environ.get("EIG_LEDGER_SALT", "eig")
    return "visitor:" + hashlib.sha256((salt + ip).encode()).hexdigest()[:10]


class AutoReset:
    """Every EIG_AUTO_RESET_MIN minutes (default 15, 0 = off): restore the certified results and
    expire pending proposals, so each judge starts from a clean site. Waits while a run is active."""

    def __init__(self, agent_, jobs, minutes: float = None):
        self.agent, self.jobs = agent_, jobs
        self.minutes = float(os.environ.get("EIG_AUTO_RESET_MIN", "15")) if minutes is None else minutes
        self.next_at = time.time() + self.minutes * 60 if self.minutes > 0 else None
        self.last = None

    def tick(self, now: float = None) -> bool:
        now = now or time.time()
        if self.next_at is None or now < self.next_at:
            return False
        if self.jobs.running():
            self.next_at = now + 60  # never reset under a running check or restore
            return False
        try:
            self.last = {"at": now, "result": self.agent.reset("auto")}
        except OSError as exc:  # county data missing (e.g. local dev): try again next period
            self.last = {"at": now, "error": str(exc)[:200]}
        self.next_at = now + self.minutes * 60
        return True

    def run_forever(self) -> None:
        while self.next_at is not None:
            self.tick()
            time.sleep(15)


AUTO_RESET = AutoReset(AGENT, JOBS)


def status() -> dict:
    r = config.routes()
    return {"provider": config.provider(), "base_url": config.base_url() if config.provider() == "vultr" else None,
            "models": {role: r[role].model or None for role in ("reasoning", "vision")},
            "vision_path": ("mock" if config.provider() == "mock" else
                            "vision model" if config.has_vision() else "OCR text (no MODEL_VISION)"),
            "evidence_path": agent.evidence_path(),
            "approvals_enabled": approvals_enabled(),
            "auto_reset": {"every_min": AUTO_RESET.minutes or None, "next_at": AUTO_RESET.next_at},
            "server_time": time.time(),
            "spend": SPEND.status(), "limits": {"daily": LIMITS.daily, "per_ip_hourly": LIMITS.per_ip,
                                                "tamper_per_ip_hourly": TAMPER_LIMITS.per_ip}}


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "eig-orchestrator"
    sys_version = ""

    def log_message(self, fmt, *args):  # no query strings or bodies in logs
        print(f"{self.command} {urlsplit(self.path).path} -> {args[1] if len(args) > 1 else ''}")

    def send_json(self, status_code: int, payload, extra: dict = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in {**SECURITY_HEADERS, **(extra or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def client_ip(self) -> str:
        return client_ip(self.headers.get("X-Forwarded-For", ""), self.client_address[0], TRUSTED_PROXY_HOPS)

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self.send_json(413, {"error": "request too large"})
            return None
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self.send_json(400, {"error": "invalid JSON"})
            return None
        if not isinstance(body, dict):
            self.send_json(400, {"error": "expected a JSON object"})
            return None
        return body

    def busy_or_limited(self) -> bool:
        """Live work costs money and sandbox time: one run at a time, per-IP and daily caps."""
        if JOBS.running() >= demo.MAX_CONCURRENT:
            self.send_json(429, {"error": "A run is already in progress. Try again in a few seconds."})
            return True
        refusal = LIMITS.take(self.client_ip())
        if refusal:
            self.send_json(429, {"error": refusal})
            return True
        return False

    def do_POST(self):
        path = urlsplit(self.path).path
        body = self.read_body()
        if body is None:
            return None
        if path == "/api/tasks":
            kind, goal = body.get("kind", "check"), body.get("goal") or ""
            if kind not in TASK_KINDS or not isinstance(goal, str):
                return self.send_json(400, {"error": "kind must be check or redteam; goal must be text"})
            if self.busy_or_limited():
                return None
            fn = (lambda progress: AGENT.check(goal[:agent.MAX_GOAL], progress)) if kind == "check" else AGENT.redteam
            return self.send_json(202, JOBS.start(kind, fn))
        if path == "/api/simulate-tamper":
            mode = body.get("mode", "one")
            if mode not in ("one", "swap"):
                return self.send_json(400, {"error": "mode must be one or swap"})
            if JOBS.running():
                return self.send_json(429, {"error": "A run is in progress. Tamper after it finishes."})
            refusal = TAMPER_LIMITS.take(self.client_ip())
            if refusal:
                return self.send_json(429, {"error": refusal.replace("live runs", "simulated tampers")})
            return self.send_json(200, AGENT.simulate_tamper(mode, by=visitor_id(self.client_ip())))
        m = _PROPOSAL.match(path)
        if m:
            pid, verb = m.groups()
            code, who = authorize(self.headers.get("Authorization", ""))
            if code != 200:
                extra = {"WWW-Authenticate": "Bearer"} if code == 401 else None
                return self.send_json(code, {"error": who}, extra)
            prop = next((p for p in AGENT.list_proposals() if p["id"] == pid), None)
            if prop is None:
                return self.send_json(404, {"error": "unknown proposal"})
            if prop["status"] != "PENDING_APPROVAL":
                return self.send_json(409, {"error": f"proposal is {prop['status']}"})
            if verb == "reject":
                return self.send_json(200, AGENT.reject(pid, by=who))
            if JOBS.running() >= demo.MAX_CONCURRENT:
                return self.send_json(429, {"error": "A run is already in progress. Try again in a few seconds."})
            return self.send_json(202, JOBS.start("approve", lambda progress: AGENT.approve(pid, by=who, progress=progress)))
        if path != "/api/run":
            return self.send_json(404, {"error": "not found"})
        sid = body.get("scenario")
        if sid not in demo.SCENARIOS:
            return self.send_json(400, {"error": "unknown scenario"})
        if not body.get("live"):
            job = JOBS.recorded_job(sid)
            return self.send_json(200, job) if job else self.send_json(404, {"error": "no recorded run; try a live run"})
        if self.busy_or_limited():
            return None
        return self.send_json(202, JOBS.start(sid))

    def send_file(self, job: str, name: str) -> None:
        try:
            data = sandbox.safe_read(os.path.join(sandbox.job_dir(job), "out"), name, 6_000_000)
        except ValueError:
            data = None
        if data is None:
            return self.send_json(404, {"error": "no such evidence file"})
        ctype = "image/png" if name.endswith(".png") else "text/plain; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", "inline")
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/api/scenarios":
            return self.send_json(200, {"scenarios": demo.scenario_list(), "routing": demo.routing(),
                                        "limits": {"daily": LIMITS.daily, "per_ip_hourly": LIMITS.per_ip}})
        if path == "/api/proposals":
            return self.send_json(200, {"proposals": AGENT.list_proposals()})
        if path == "/api/ledger":
            return self.send_json(200, {"verify": AGENT.ledger.verify(), "entries": AGENT.ledger.entries(limit=100)})
        m = _SBX_FILE.match(path)
        if m:
            return self.send_file(m.group(1), m.group(2))
        if path == "/api/status":
            return self.send_json(200, status())
        if path.startswith("/api/jobs/"):
            job = JOBS.get(path[len("/api/jobs/"):])
            return self.send_json(200, job) if job else self.send_json(404, {"error": "unknown job"})
        if path == "/healthz":
            return self.send_json(200, {"ok": True, "commit": os.environ.get("EIG_GIT_COMMIT", "local")[:7]})
        return self.send_json(404, {"error": "not found"})

    do_HEAD = do_GET


def serve(port: int, host: str = "0.0.0.0") -> None:
    if AUTO_RESET.next_at is not None:
        threading.Thread(target=AUTO_RESET.run_forever, daemon=True, name="auto-reset").start()
    httpd = ThreadingHTTPServer((host, port), ApiHandler)
    print(f"Orchestrator API on http://{host}:{port} (provider: {config.provider()})")
    httpd.serve_forever()
