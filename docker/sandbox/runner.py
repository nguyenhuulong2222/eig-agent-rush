"""EIG sandbox runner. Runs inside a throwaway container; holds no secrets.

Reads  /job/in/task.json   (read-only mount, written by the orchestrator)
Writes /job/out/           (this job's own output subpath)

task.json: {"job_id": str, "evidence_path": "vision"|"ocr", "steps": [step, ...]}
Steps (executed in order, each recorded with ok/error, never aborting the run):
  visit        {url, name, screenshot?, ocr_lang?}  Chromium: DOM html + innerText (+ screenshot, OCR)
  fetch        {url, name}                           raw bytes of one URL (the Results Seal hashes these)
  read_file    {path}                                red-team drill: try to read a file
  dump_env     {}                                    red-team drill: print the environment
  http_post    {url, body_from: "env"}               red-team drill: try to send data out
  browser_goto {url}                                 red-team drill: try to navigate the browser out
  wait         {ms}                                  pause (used by the timeout test)
  probe        {}                                    report this container's own uid, env names,
                                                     writable paths and cgroup limits (smoke test)

result.json is rewritten after every step, so a run killed at the timeout still leaves a
partial record. Output names are [a-z0-9_.-] only; the orchestrator reads them without
following symlinks.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

IN = os.environ.get("EIG_RUNNER_IN", "/job/in/task.json")
OUT = os.environ.get("EIG_RUNNER_OUT", "/job/out")
MAX_STEPS = 20
MAX_FETCH_BYTES = 2_000_000
NAV_TIMEOUT_MS = 15_000
NET_TIMEOUT_S = 5
OCR_LANGS = {"en": "eng", "vi": "vie", "es": "spa", "tl": "fil"}  # Tagalog = Filipino traineddata
_NAME = re.compile(r"^[a-z0-9_-]{1,40}$")


def _write(name: str, data: bytes) -> None:
    with open(os.path.join(OUT, name), "wb") as fh:
        fh.write(data)


def _err(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:300]}"


class Browser:
    """Chromium, started only when a step needs it."""

    def __init__(self):
        self.pw = self.browser = self.page = None
        self.failed_requests = []

    def page_(self):
        if self.page is None:
            from playwright.sync_api import sync_playwright
            self.pw = sync_playwright().start()
            # The container is the sandbox: Chromium's own sandbox needs privileges we dropped.
            self.browser = self.pw.chromium.launch(headless=True)
            self.page = self.browser.new_page(viewport={"width": 1280, "height": 900})
            self.page.on("requestfailed", lambda r: self.failed_requests.append(
                {"url": r.url[:200], "error": (r.failure or "")[:200]}))
        return self.page

    def close(self):
        for closer in (lambda: self.browser and self.browser.close(), lambda: self.pw and self.pw.stop()):
            try:
                closer()
            except Exception:
                pass


def visit(br: Browser, step: dict, task: dict) -> dict:
    name = step["name"]
    page = br.page_()
    resp = page.goto(step["url"], wait_until="networkidle", timeout=NAV_TIMEOUT_MS)
    html = page.content()
    text = page.inner_text("body")
    _write(f"{name}.html", html.encode())
    _write(f"{name}.txt", text.encode())
    out = {"status": resp.status if resp else None, "html_bytes": len(html), "text_chars": len(text),
           "files": [f"{name}.html", f"{name}.txt"]}
    if step.get("screenshot"):
        page.screenshot(path=os.path.join(OUT, f"{name}.png"), full_page=True)
        out["files"].append(f"{name}.png")
        if task.get("evidence_path") == "ocr":
            lang = OCR_LANGS.get(step.get("ocr_lang", "en"), "eng")
            proc = subprocess.run(["tesseract", os.path.join(OUT, f"{name}.png"), os.path.join(OUT, f"{name}.ocr"),
                                   "-l", lang], capture_output=True, timeout=20)
            out["ocr"] = "ok" if proc.returncode == 0 else proc.stderr.decode(errors="replace")[:200]
            if proc.returncode == 0:
                out["files"].append(f"{name}.ocr.txt")
    return out


def fetch(step: dict) -> dict:
    req = urllib.request.Request(step["url"], headers={"User-Agent": "eig-sandbox/1.0", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=NET_TIMEOUT_S) as resp:
        data = resp.read(MAX_FETCH_BYTES + 1)
        status = resp.status
    if len(data) > MAX_FETCH_BYTES:
        raise ValueError(f"response larger than {MAX_FETCH_BYTES} bytes")
    _write(f"{step['name']}.bin", data)
    return {"status": status, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "files": [f"{step['name']}.bin"]}


def read_file(step: dict) -> dict:
    with open(step["path"], "rb") as fh:
        data = fh.read(4096)
    return {"bytes": len(data), "preview": data[:200].decode(errors="replace")}


def dump_env(_step: dict) -> dict:
    return {"env": dict(sorted(os.environ.items()))}


def http_post(step: dict) -> dict:
    body = json.dumps(dict(os.environ)).encode()
    req = urllib.request.Request(step["url"], data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=NET_TIMEOUT_S) as resp:
        return {"status": resp.status}


def browser_goto(br: Browser, step: dict) -> dict:
    resp = br.page_().goto(step["url"], timeout=NAV_TIMEOUT_MS)
    return {"status": resp.status if resp else None}


def _cgroup(name: str):
    try:
        with open(f"/sys/fs/cgroup/{name}") as fh:
            return fh.read().strip()
    except OSError as exc:
        return _err(exc)


def _writable(path: str) -> bool:
    try:
        with open(os.path.join(path, ".eig-probe"), "w") as fh:
            fh.write("x")
        os.unlink(os.path.join(path, ".eig-probe"))
        return True
    except OSError:
        return False


def probe(_step: dict) -> dict:
    return {"uid": os.getuid(), "gid": os.getgid(), "env_names": sorted(os.environ),
            "writable": {p: _writable(p) for p in ("/", "/opt/eig", "/etc", "/job/in", "/tmp", "/dev/shm", "/job/out")},
            "cgroup": {k: _cgroup(k) for k in ("memory.max", "memory.swap.max", "pids.max", "cpu.max")}}


def main() -> int:
    started = time.monotonic()
    with open(IN, encoding="utf-8") as fh:
        task = json.load(fh)
    record = {"job_id": task.get("job_id"), "uid": os.getuid(), "steps": [], "done": False}
    br = Browser()

    def save():
        record["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        record["failed_requests"] = br.failed_requests[-20:]
        _write("result.json", json.dumps(record, ensure_ascii=False).encode())

    for i, step in enumerate(task.get("steps", [])[:MAX_STEPS]):
        action = step.get("action")
        entry = {"i": i, "action": action, "target": step.get("url") or step.get("path") or step.get("name"),
                 "ok": False}
        t0 = time.monotonic()
        try:
            if "name" in step and not _NAME.match(step["name"]):
                raise ValueError("bad output name")
            if action == "visit":
                entry["detail"] = visit(br, step, task)
            elif action == "fetch":
                entry["detail"] = fetch(step)
            elif action == "read_file":
                entry["detail"] = read_file(step)
            elif action == "dump_env":
                entry["detail"] = dump_env(step)
            elif action == "http_post":
                entry["detail"] = http_post(step)
            elif action == "browser_goto":
                entry["detail"] = browser_goto(br, step)
            elif action == "probe":
                entry["detail"] = probe(step)
            elif action == "wait":
                time.sleep(min(float(step.get("ms", 0)), 600_000) / 1000)
                entry["detail"] = {}
            else:
                raise ValueError(f"unknown action {action!r}")
            entry["ok"] = True
        except Exception as exc:  # record and continue: a failed step is evidence, not a crash
            entry["error"] = _err(exc)
        entry["ms"] = int((time.monotonic() - t0) * 1000)
        record["steps"].append(entry)
        save()
    br.close()
    record["done"] = True
    save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
