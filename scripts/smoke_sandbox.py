"""Real-sandbox smoke test. Runs INSIDE the orchestrator container, through socket-proxy:

  docker compose exec -T orchestrator python scripts/smoke_sandbox.py

1. One task in the real eig-sandbox image (exactly the spec the agent uses): the container
   reports its own uid, env, writable paths and cgroup limits, and Chromium renders the
   county results page with a screenshot. Then it must be removed.
2. A task that outlives a short timeout must be killed, marked TIMEOUT and removed.
3. No sandbox container may be left behind.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eig_agent import sandbox  # noqa: E402

SITE = os.environ.get("EIG_COUNTY_URL", "http://county-site:8080").rstrip("/")
FAILED = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  -- ' + str(detail)) if detail != '' else ''}")
    if not ok:
        FAILED.append(name)


def removed(rec):
    last = rec["lifecycle"][-1] if rec["lifecycle"] else {}
    return last.get("event") == "removed" and last.get("confirmed_gone") is True


def main():
    rec = sandbox.run_job({"evidence_path": "ocr", "steps": [
        {"action": "probe"},
        {"action": "visit", "url": SITE + "/results.html", "name": "results_page", "screenshot": True}]})
    print(f"      job {rec['job_id']} container {(rec['container_id'] or '')[:12]} "
          f"{[e['event'] for e in rec['lifecycle']]} {rec['duration_ms']} ms")
    check("real sandbox run OK", rec["status"] == "OK", rec["error"] or rec["logs_tail"][-300:])
    steps = (rec["result"] or {}).get("steps", [])
    p = (steps[0].get("detail") or {}) if steps else {}
    w, cg = p.get("writable", {}), p.get("cgroup", {})
    check("runs as uid 10001", p.get("uid") == 10001, p.get("uid"))
    check("env has no secrets", not [n for n in p.get("env_names", [])
                                     if any(k in n.upper() for k in ("KEY", "TOKEN", "SECRET", "VULTR", "API"))],
          p.get("env_names"))
    check("root fs, /opt/eig, /etc read-only", not (w.get("/") or w.get("/opt/eig") or w.get("/etc")), w)
    check("task input /job/in read-only", w.get("/job/in") is False, w.get("/job/in"))
    check("/tmp, /dev/shm, /job/out writable", w.get("/tmp") and w.get("/dev/shm") and w.get("/job/out"), w)
    check("memory.max = 1 GiB, no swap", cg.get("memory.max") == str(1024 ** 3) and cg.get("memory.swap.max") == "0", cg)
    check("pids.max = 256", cg.get("pids.max") == "256", cg.get("pids.max"))
    check("cpu.max = 1 CPU", (cg.get("cpu.max") or "").split()[:1] == ["100000"], cg.get("cpu.max"))
    visit = steps[1] if len(steps) > 1 else {}
    png = sandbox.safe_read(rec["out_dir"], "results_page.png", 6_000_000) if visit.get("ok") else None
    check("Chromium renders the county page read-only in 1 GiB (screenshot)", bool(png) and png[:4] == b"\x89PNG",
          visit.get("error") or f"{len(png or b'')} bytes")
    check("OCR ran on the screenshot", (visit.get("detail") or {}).get("ocr") == "ok", (visit.get("detail") or {}).get("ocr"))
    check("container removed and confirmed gone", removed(rec), rec["lifecycle"][-1:])

    rec = sandbox.run_job({"steps": [{"action": "wait", "ms": 30000}]}, timeout=5)
    print(f"      job {rec['job_id']} {[e['event'] for e in rec['lifecycle']]} {rec['duration_ms']} ms")
    check("hard timeout -> TIMEOUT", rec["status"] == "TIMEOUT", rec["status"])
    check("timed-out container killed", any(e["event"] == "killed" for e in rec["lifecycle"]))
    check("timed-out container removed", removed(rec))
    check("finished near the timeout, not the task length", rec["duration_ms"] < 15000, rec["duration_ms"])

    status, raw = sandbox.DockerAPI().call("GET", '/containers/json?all=1&filters=' +
                                           '%7B%22label%22%3A%5B%22eig.role%3Dsandbox%22%5D%7D')
    left = json.loads(raw) if status == 200 else None
    check("no sandbox containers left behind", left == [], left if left is None else len(left))


if __name__ == "__main__":
    main()
    print(f"\n{'ALL PASS' if not FAILED else f'{len(FAILED)} FAILED: ' + ', '.join(FAILED)}")
    sys.exit(1 if FAILED else 0)
