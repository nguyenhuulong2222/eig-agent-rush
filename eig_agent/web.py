"""Public web container: static UI + a thin /api/* forwarder to the private orchestrator.

  python -m eig_agent web [--port 8000] [--upstream http://orchestrator:8001]

Standard library only, and it imports nothing else from eig_agent: this process holds no
secrets, cannot reach Docker, and never calls a model. If it is compromised, the attacker
gets a static file server and the same /api the browser already has.

/county/ is a read-only mirror of the fictional county site for visitors: the site's files and
the live results volume are mounted read-only here, so the county-site container itself stays
on the internal network only. GET/HEAD only.
"""

from __future__ import annotations

import os
import posixpath
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

PKG = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(PKG, "web")
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml", ".json": "application/json",
         ".png": "image/png"}
ROBOTS = b"User-agent: *\nDisallow: /\n"
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
                               "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
                               "frame-ancestors 'none'",
}
COUNTY_PUBLIC = "/srv/county/public"   # ./public, mounted read-only
COUNTY_DATA = "/srv/county/data"       # county_data volume (live results.json), mounted read-only
# Same policy as the county pages' own meta CSP.
COUNTY_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
              "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
MAX_BODY = 2048
# The console's own files: revalidate on every load, so a deploy never leaves a stale app.js/styles.css.
REVALIDATE = {"Cache-Control": "no-cache"}
# Upstream content types passed through to the browser (sandbox screenshots and OCR text); anything else
# is labeled JSON. nosniff is always set, so a mislabeled body is never executed.
PASS_TYPES = {"image/png": "image/png", "text/plain": "text/plain; charset=utf-8"}
MAX_UPSTREAM_BYTES = 8 * 1024 * 1024
UPSTREAM_TIMEOUT_S = 15.0


class WebHandler(BaseHTTPRequestHandler):
    server_version = "eig-web"
    sys_version = ""
    upstream = os.environ.get("EIG_ORCHESTRATOR_URL", "http://orchestrator:8001").rstrip("/")
    county_public, county_data = COUNTY_PUBLIC, COUNTY_DATA

    def log_message(self, fmt, *args):
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

    def static(self, rel: str, root: str = None, extra: dict = None) -> None:
        root = root or STATIC
        rel = posixpath.normpath("/" + rel).lstrip("/")
        full = os.path.realpath(os.path.join(root, *rel.split("/")))
        base = os.path.realpath(root)
        if not full.startswith(base + os.sep) or not os.path.isfile(full):
            return self.send(404, b"not found\n", "text/plain; charset=utf-8")
        with open(full, "rb") as fh:
            body = fh.read()
        self.send(200, body, TYPES.get(os.path.splitext(full)[1], "application/octet-stream"), extra)

    def county(self, path: str) -> None:
        """Read-only mirror of the county site: /county/data/* from the results volume, rest from public/."""
        rel = path[len("/county/"):] or "index.html"
        headers = {"Content-Security-Policy": COUNTY_CSP, "X-Robots-Tag": "noindex, nofollow",
                   "Cache-Control": "no-store"}
        if rel.startswith("data/"):
            return self.static(rel[len("data/"):], self.county_data, headers)
        if rel == "data" or rel.startswith("manifest"):
            return self.send(404, b"not found\n", "text/plain; charset=utf-8")
        return self.static(rel, self.county_public, headers)

    def forward(self) -> None:
        """Forward one /api request. Only the path, the JSON body and X-Forwarded-For go upstream."""
        data = None
        if self.command == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self.send(413, b'{"error": "request too large"}', "application/json")
            data = self.rfile.read(length)
        prior = self.headers.get("X-Forwarded-For", "")
        chain = f"{prior}, {self.client_address[0]}" if prior else self.client_address[0]
        headers = {"Content-Type": "application/json", "X-Forwarded-For": chain}
        if self.headers.get("Authorization"):  # approver token: passed through, never stored or logged here
            headers["Authorization"] = self.headers["Authorization"][:512]
        req = urllib.request.Request(self.upstream + urlsplit(self.path).path, data=data, method=self.command,
                                     headers=headers)
        ctype = "application/json; charset=utf-8"
        try:
            with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT_S) as resp:
                status, body = resp.status, resp.read(MAX_UPSTREAM_BYTES)
                ctype = PASS_TYPES.get(resp.headers.get_content_type(), ctype)
        except urllib.error.HTTPError as exc:
            status, body = exc.code, exc.read(MAX_UPSTREAM_BYTES)
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            status, body = 502, b'{"error": "orchestrator unavailable"}'
        self.send(status, body, ctype, {"Cache-Control": "no-store"})

    def do_POST(self):
        path = urlsplit(self.path).path
        if path.startswith("/api/"):
            return self.forward()
        if path == "/county" or path.startswith("/county/"):
            return self.send(405, b"read-only mirror\n", "text/plain; charset=utf-8", {"Allow": "GET, HEAD"})
        return self.send(404, b"not found\n", "text/plain; charset=utf-8")

    def not_allowed(self):
        return self.send(405, b"method not allowed\n", "text/plain; charset=utf-8", {"Allow": "GET, HEAD, POST"})

    do_PUT = do_DELETE = do_PATCH = not_allowed

    def do_GET(self):
        path = urlsplit(self.path).path
        if path.startswith("/api/"):
            return self.forward()
        if path == "/county":
            return self.send(301, b"", "text/plain; charset=utf-8", {"Location": "/county/"})
        if path.startswith("/county/"):
            return self.county(path)
        if path == "/robots.txt":
            return self.send(200, ROBOTS, "text/plain; charset=utf-8")
        if path == "/healthz":
            return self.send(200, b"ok\n", "text/plain; charset=utf-8")
        if path in ("/", "/index.html"):
            return self.static("index.html", extra=REVALIDATE)
        if path.startswith("/static/"):
            return self.static(path[len("/static/"):], extra=REVALIDATE)
        return self.send(404, b"not found\n", "text/plain; charset=utf-8")

    def do_HEAD(self):
        if urlsplit(self.path).path.startswith("/api/"):
            return self.send(405, b"", "text/plain; charset=utf-8")
        return self.do_GET()


def serve(port: int, upstream: str = None, host: str = "0.0.0.0") -> None:
    if upstream:
        WebHandler.upstream = upstream.rstrip("/")
    httpd = ThreadingHTTPServer((host, port), WebHandler)
    print(f"Web on http://{host}:{port} -> orchestrator {WebHandler.upstream}")
    httpd.serve_forever()


if __name__ == "__main__":  # `python -m eig_agent.web` works without importing the rest of the package
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--upstream", default=None)
    a = ap.parse_args()
    serve(a.port, a.upstream)
