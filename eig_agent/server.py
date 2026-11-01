"""Demo web server (stdlib http.server). API keys stay in this process; the browser never sees them.

  python -m eig_agent serve [--port 8000]
"""

from __future__ import annotations

import ipaddress
import json
import os
import posixpath
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import demo

PKG = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(PKG, "web")
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml", ".json": "application/json"}
ROBOTS = b"User-agent: *\nDisallow: /\n"
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
                               "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
                               "frame-ancestors 'none'",
}
# Render's proxy appends the address it sees to X-Forwarded-For and keeps whatever the client
# sent before it, so only entries counted from the right are trustworthy. TRUSTED_PROXY_HOPS is
# the number of trusted proxies in front of this process (checked on Render with /api/whoami).
TRUSTED_PROXY_HOPS = int(os.environ.get("EIG_TRUSTED_PROXY_HOPS", "1"))
JOBS = demo.Jobs()
LIMITS = demo.Limits()
MAX_BODY = 2048


def client_ip(forwarded: str, peer: str, hops: int) -> str:
    """The visitor's IP for rate limiting: the entry `hops` places from the right of X-Forwarded-For.

    Leading entries are whatever the client chose to send, so they are never used. With no
    trusted proxy (hops 0) or a header shorter than the proxy chain, fall back to the peer.
    """
    entries = [e.strip() for e in forwarded.split(",") if e.strip()]
    if hops <= 0 or len(entries) < hops:
        return peer
    return entries[-hops]


def _mask_private(entry: str) -> str:
    """Show the caller's public addresses; hide internal proxy addresses."""
    try:
        return "private" if ipaddress.ip_address(entry).is_private else entry
    except ValueError:
        return "not-an-ip"


class Handler(BaseHTTPRequestHandler):
    server_version = "eig-nemotron"
    sys_version = ""

    def log_message(self, fmt, *args):  # no query strings or bodies in logs
        print(f"{self.command} {urlsplit(self.path).path} -> {args[1] if len(args) > 1 else ''}")

    def send(self, status: int, body: bytes, ctype: str, extra: dict = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in {**SECURITY_HEADERS, **(extra or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def static(self, rel: str, extra: dict = None) -> None:
        rel = posixpath.normpath(rel).lstrip("/")
        full = os.path.join(WEB, *rel.split("/"))
        if rel.startswith("..") or not full.startswith(WEB + os.sep) or not os.path.isfile(full):
            return self.send(404, b"not found\n", "text/plain; charset=utf-8")
        with open(full, "rb") as fh:
            body = fh.read()
        self.send(200, body, TYPES.get(os.path.splitext(full)[1], "application/octet-stream"), extra)

    def send_json(self, status: int, payload) -> None:
        self.send(status, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8",
                  {"Cache-Control": "no-store"})

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
            return self.send_json(429, {"error": "Two live runs are already in progress. Try again in a few seconds."})
        refusal = LIMITS.take(self.client_ip())
        if refusal:
            return self.send_json(429, {"error": refusal})
        return self.send_json(202, JOBS.start(sid))

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/api/scenarios":
            return self.send_json(200, {"scenarios": demo.scenario_list(), "routing": demo.routing(),
                                   "limits": {"daily": LIMITS.daily, "per_ip_hourly": LIMITS.per_ip}})
        if path == "/api/whoami":  # the caller's own address as the rate limiter sees it
            entries = [e.strip() for e in self.headers.get("X-Forwarded-For", "").split(",") if e.strip()]
            return self.send_json(200, {"ip": self.client_ip(), "forwarded_entries": len(entries),
                                        "chain": [_mask_private(e) for e in entries],
                                        "trusted_proxy_hops": TRUSTED_PROXY_HOPS})
        if path.startswith("/api/jobs/"):
            job = JOBS.get(path[len("/api/jobs/"):])
            return self.send_json(200, job) if job else self.send_json(404, {"error": "unknown job"})
        if path == "/robots.txt":
            return self.send(200, ROBOTS, "text/plain; charset=utf-8")
        if path == "/healthz":  # deploy.sh sets EIG_GIT_COMMIT; it shows which commit is live
            commit = os.environ.get("EIG_GIT_COMMIT", "local")[:7]
            return self.send(200, f"ok {commit}\n".encode(), "text/plain; charset=utf-8")
        if path in ("/", "/index.html"):
            return self.static("index.html")
        if path.startswith("/static/"):
            return self.static(path[len("/static/"):])
        return self.send(404, b"not found\n", "text/plain; charset=utf-8")

    do_HEAD = do_GET


def serve(port: int) -> None:
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Serving on http://0.0.0.0:{port}")
    httpd.serve_forever()
