"""Orchestrator API (stdlib http.server). Private: reachable only from the web container.

  python -m eig_agent orchestrator [--port 8001]

This process holds the Vultr Inference key, applies the rate limits and the spend cap, and (from
M3) launches sandbox containers through the Docker socket proxy. It serves JSON only; the UI is
served by the web container (eig_agent/web.py), which forwards /api/* here.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import config, demo
from .llm import SPEND

# Each proxy in front of this process appends the address it saw to X-Forwarded-For; only entries
# counted from the right are trustworthy. Dev: browser -> web -> here (1 hop). Prod: browser ->
# Caddy -> web -> here (2 hops).
TRUSTED_PROXY_HOPS = int(os.environ.get("EIG_TRUSTED_PROXY_HOPS", "1"))
JOBS = demo.Jobs()
LIMITS = demo.Limits()
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


def status() -> dict:
    r = config.routes()
    return {"provider": config.provider(), "base_url": config.base_url() if config.provider() == "vultr" else None,
            "models": {role: r[role].model or None for role in ("reasoning", "vision")},
            "vision_path": ("mock" if config.provider() == "mock" else
                            "vision model" if config.has_vision() else "OCR text (no MODEL_VISION)"),
            "spend": SPEND.status(), "limits": {"daily": LIMITS.daily, "per_ip_hourly": LIMITS.per_ip}}


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "eig-orchestrator"
    sys_version = ""

    def log_message(self, fmt, *args):  # no query strings or bodies in logs
        print(f"{self.command} {urlsplit(self.path).path} -> {args[1] if len(args) > 1 else ''}")

    def send_json(self, status_code: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def client_ip(self) -> str:
        return client_ip(self.headers.get("X-Forwarded-For", ""), self.client_address[0], TRUSTED_PROXY_HOPS)

    def do_POST(self):
        if urlsplit(self.path).path != "/api/run":
            return self.send_json(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self.send_json(413, {"error": "request too large"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self.send_json(400, {"error": "invalid JSON"})
        sid = body.get("scenario")
        if sid not in demo.SCENARIOS:
            return self.send_json(400, {"error": "unknown scenario"})
        if not body.get("live"):
            job = JOBS.recorded_job(sid)
            return self.send_json(200, job) if job else self.send_json(404, {"error": "no recorded run; try a live run"})
        if JOBS.running() >= demo.MAX_CONCURRENT:
            return self.send_json(429, {"error": "A live run is already in progress. Try again in a few seconds."})
        refusal = LIMITS.take(self.client_ip())
        if refusal:
            return self.send_json(429, {"error": refusal})
        return self.send_json(202, JOBS.start(sid))

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/api/scenarios":
            return self.send_json(200, {"scenarios": demo.scenario_list(), "routing": demo.routing(),
                                        "limits": {"daily": LIMITS.daily, "per_ip_hourly": LIMITS.per_ip}})
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
    httpd = ThreadingHTTPServer((host, port), ApiHandler)
    print(f"Orchestrator API on http://{host}:{port} (provider: {config.provider()})")
    httpd.serve_forever()
