"""Start one agent run through the local web container, wait for it, print a compact report.

  python3 scripts/run_task.py check [--goal "..."]
  python3 scripts/run_task.py redteam
  EIG_TOKEN="$(grep '^EIG_APPROVER_TOKEN=' .env | cut -d= -f2-)" python3 scripts/run_task.py approve <proposal-id>
  python3 scripts/run_task.py proposals

Stdlib only; runs on the VM host (http://127.0.0.1:8000, the web container). The token is read
from $EIG_TOKEN and never printed. One run = the normal rate limits and spend cap apply.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("EIG_BASE", "http://127.0.0.1:8000")


def api(path, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers=headers, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        with exc:
            try:
                return exc.code, json.load(exc)
            except ValueError:
                return exc.code, {}


def wait(job):
    started = time.time()
    while True:
        status, job = api("/api/jobs/" + job["id"])
        if status != 200 or job.get("status") != "running":
            return job, time.time() - started
        time.sleep(1)


def calls_table(calls):
    if not calls:
        print("  model calls: none")
        return
    print(f"  {'task':<24} {'model':<22} {'in':>6} {'out':>6} {'reason':>6} {'sec':>5} {'est $':>9}  status")
    for c in calls:
        status = "ok" if c["ok"] else "FAIL " + (c.get("error") or "")[:70]
        print(f"  {c['task'][:24]:<24} {c['model'][-22:]:<22} {c['prompt_tokens']:>6} {c['completion_tokens']:>6} "
              f"{c['reasoning_tokens']:>6} {c['latency_ms'] / 1000:>5.1f} {c['cost_usd']:>9.5f}  {status}")


def report(job, wall):
    r = job.get("result") or {}
    print(f"job {job.get('id')} {job.get('scenario')}: {job.get('status')} in {wall:.1f} s"
          + (f"  ERROR {job.get('error')}" if job.get("error") else ""))
    for key, step in (job.get("steps") or {}).items():
        secs = (step.get("finished_at") or 0) - (step.get("started_at") or 0)
        print(f"  step {key:<9} {step.get('status'):<7} {secs:5.1f}s  {(step.get('detail') or '')[:90]}")
    sb = r.get("sandbox") or r.get("drill")
    if sb:
        print(f"  sandbox {sb.get('status')} container {(sb.get('container_id') or '')[:12]} "
              f"{' -> '.join(e['event'] for e in sb.get('lifecycle', []))}")
    if r.get("kind") == "check":
        seal = r.get("seal") or {}
        print(f"  seal match={seal.get('match')} changed_fields={len(seal.get('diff') or [])} "
              f"rules={[f['rule_id'] for f in seal.get('findings') or []]}")
        print(f"  decision {(r.get('decision') or {}).get('action')}: {(r.get('decision') or {}).get('reason')}")
        for i, v in enumerate(r.get("votes") or []):
            model = (r.get("vote_models") or ["?", "?"])[i] if i < len(r.get("vote_models") or []) else "?"
            print(f"  verdict {i + 1} [{model}]: " + (f"{v['verdict']} {v['confidence']:.2f} -> {v['recommended_action']} | "
                                            f"{v['summary'][:160]}" if v else "none (invalid reply)"))
        if r.get("votes_same_model"):
            print("  NOTE: both verdicts came from the same model (MODEL_TRIAGE_2 not set)")
        if r.get("triage_error"):
            print(f"  triage error: {r['triage_error'][:200]}")
        n = r.get("evidence_note")
        if n:
            print(f"  evidence note ({n.get('source', '-')}): " + (n.get("error") or
                  f"{n.get('page_shows_changed_values')} / {n.get('severity')} | {n.get('explanation', '')[:160]}"))
        ml = r.get("multilingual") or {}
        if ml.get("skipped"):
            print(f"  translations: skipped ({ml['skipped']})")
        elif ml:
            print(f"  translations: {len(ml.get('mismatches') or [])} mismatches "
                  f"{[m['id'] for m in ml.get('mismatches') or []]} errors={ml.get('errors')}")
        if r.get("incident_report_md"):
            print(f"  incident report: {len(r['incident_report_md'])} chars")
        if r.get("proposal"):
            print(f"  PROPOSAL {r['proposal']['id']} {r['proposal']['status']}")
    elif r.get("kind") == "redteam":
        print(f"  summary {r.get('summary')}")
        for e in r.get("events") or []:
            print(f"  {e['verdict']:<9} {e['action']:<12} {str(e['target'])[:34]:<34} {(e.get('evidence') or '')[:70]}")
    elif r.get("kind") == "approve":
        print(f"  proposal {(r.get('proposal') or {}).get('status')}  verify seal_match={(r.get('verify') or {}).get('seal_match')}")
    calls_table(r.get("calls") or [])
    t = r.get("totals") or {}
    if t:
        print(f"  totals: {t.get('model_calls')} calls, {t.get('prompt_tokens')} in / {t.get('completion_tokens')} out "
              f"tokens, est. ${t.get('cost_usd', 0):.5f} (ceiling prices, not list prices)")
    _, st = api("/api/status")
    if st.get("spend"):
        print(f"  spend today (estimate): ${st['spend']['spent_usd']:.5f} of ${st['spend']['cap_usd']:.2f}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("kind", choices=["check", "redteam", "approve", "proposals"])
    ap.add_argument("proposal", nargs="?")
    ap.add_argument("--goal", default="")
    a = ap.parse_args(argv)
    if a.kind == "proposals":
        print(json.dumps(api("/api/proposals")[1], indent=1))
        return 0
    if a.kind == "approve":
        token = os.environ.get("EIG_TOKEN", "")
        if not a.proposal or not token:
            sys.exit("usage: EIG_TOKEN=... run_task.py approve <proposal-id>")
        status, job = api(f"/api/proposals/{a.proposal}/approve", {}, token)
    else:
        body = {"kind": a.kind, "goal": a.goal} if a.kind == "check" else {"kind": a.kind}
        status, job = api("/api/tasks", body)
    if status != 202:
        print(f"refused: HTTP {status} {job.get('error', '')}")
        return 1
    job, wall = wait(job)
    report(job, wall)
    return 0 if job.get("status") == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
