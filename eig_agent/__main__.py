"""CLI for the EIG Sandbox Agent (orchestrator side).

  python -m eig_agent models [--json]         list Vultr Inference models, flag image input
  python -m eig_agent triage --case ID|PATH   run one tamper-triage fixture end to end
  python -m eig_agent triage --live URL|PATH [--certified PATH] [--meta PATH]
  python -m eig_agent multilingual [--scenario consistent|drift]
  python -m eig_agent orchestrator [--port 8001]  private API (key, limits, sandbox runs)
  python -m eig_agent web [--port 8000] [--upstream URL]  public UI + /api forwarder (no secrets)
  python -m eig_agent eval [--repeats N] [--suite triage|drift]
  python -m eig_agent record [SCENARIO ...]   save live runs as the demo's recorded runs
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import config, triage
from .llm import LLMClient, load_env

PKG = os.path.dirname(os.path.abspath(__file__))
CERTIFIED = os.path.join(PKG, "fixtures", "certified", "results.json")
CASES = os.path.join(PKG, "fixtures", "cases")


def load_case(ref: str) -> dict:
    path = ref if os.path.exists(ref) else os.path.join(CASES, ref.lower() + ".json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def case_live_bytes(case: dict, certified_bytes: bytes) -> bytes:
    from .seal import apply_patches
    live = apply_patches(json.loads(certified_bytes), case["patches"])
    return (json.dumps(live, indent=2, ensure_ascii=False) + "\n").encode()


def print_calls(calls) -> None:
    total = 0.0
    for c in calls:
        total += c.cost_usd
        status = "ok" if c.ok else f"FAILED ({c.error})"
        print(f"  {c.task:<16} {c.model:<40} in={c.prompt_tokens:>5} out={c.completion_tokens:>5} "
              f"(reasoning {c.reasoning_tokens:>4})  {c.latency_ms / 1000:5.1f}s  ${c.cost_usd:.5f}  {status}")
    print(f"  total estimated cost ${total:.5f}")


def cmd_models(args) -> int:
    from .models import main
    return main(as_json=args.json)


def cmd_triage(args) -> int:
    certified_bytes = open(args.certified, "rb").read()
    if args.case:
        case = load_case(args.case)
        live_bytes, meta = case_live_bytes(case, certified_bytes), case["metadata"]
        print(f"Case {case['id']}: {case['title']} (expected: {case['expected']})")
    else:
        from .seal import read_source
        live_bytes = read_source(args.live)
        meta = json.load(open(args.meta, encoding="utf-8")) if args.meta else {}
    out = triage.run(certified_bytes, live_bytes, meta, LLMClient(), report=not args.no_report, gate=args.gate)
    seal = out["seal"]
    if args.json:
        print(json.dumps({**out, "calls": [c.__dict__ for c in out["calls"]]}, indent=2, ensure_ascii=False))
        return 0
    print(f"Seal: {'MATCH' if seal['match'] else 'MISMATCH'}  certified {seal['certified_sha256'][:12]}…  "
          f"live {seal['live_sha256'][:12]}…  ({len(seal['diff'])} changed fields)")
    for f in seal["findings"]:
        print(f"  [{f['severity']}] {f['rule_id']} {f['title']}")
    for i, t in enumerate(out["votes"]):
        if not t:
            continue
        print(f"Model verdict #{i + 1}: {t['verdict'].upper()}  confidence {t['confidence']:.2f}  "
              f"-> {t['recommended_action']}\n  {t['summary']}")
        for e in t["evidence"] if i == 0 else []:
            print(f"  - ({e['points_to']}) {e['change']}: {e['observation']}")
    if out["triage_error"]:
        print(f"Model verdict error: {out['triage_error']}")
    d = out["decision"]
    print(f"Gate decision: {d['action']}  ({d['reason']})")
    for r in out["risk_signals"]:
        print(f"  risk signal for the official: {r['id']} {r['title']} ({r['detail']})")
    if out["incident_report_md"]:
        print("\n" + out["incident_report_md"] + "\n")
    print("Model calls:")
    print_calls(out["calls"])
    return 0


def cmd_multilingual(args) -> int:
    from . import multilingual
    out = multilingual.run(multilingual.load_pages(args.scenario), LLMClient())
    if args.json:
        print(json.dumps({**out, "calls": [c.__dict__ for c in out["calls"]]}, indent=2, ensure_ascii=False))
        return 0
    print(f"Scenario: {args.scenario}")
    for lang, data in out["languages"].items():
        if data:
            f = data["facts"]
            print(f"  {lang}: day={f['election_day']['value']} polls={f['polls_open']['value']}-"
                  f"{f['polls_close']['value']} site={f['polling_place_site']['value']} "
                  f"boxes={len(data['drop_box_addresses'])}")
    print(f"Deterministic mismatches: {len(out['mismatches'])}")
    notes = {e["id"]: e for e in out["explanations"] or []}
    for m in out["mismatches"]:
        print(f"  [{m['kind']}] {m['id']}: certified {m['certified']!r}, page {m['page']!r}  (quote: {m['quote']!r})")
        e = notes.get(m["id"])
        if e:
            print(f"      {e['severity'].upper()}: {e['explanation']}\n      impact: {e['voter_impact']}"
                  f"\n      fix: {e['suggested_fix']}")
    if out["summary"]:
        print(f"Summary: {out['summary']}")
    for err in out["errors"]:
        print(f"  error: {err}")
    print("Model calls:")
    print_calls(out["calls"])
    return 0


def cmd_eval(args) -> int:
    from . import evaluate
    res = evaluate.main(args.repeats, tuple(args.suite or ("triage", "drift")),
                        tuple(args.split or ("dev", "heldout", "heldout2")), tuple(args.gate or ("v1",)),
                        tuple(args.model) if args.model else None)
    print(open(os.path.join(evaluate.OUT_DIR, "RESULTS.md"), encoding="utf-8").read())
    return 0


def cmd_record(args) -> int:
    from . import demo
    demo.record(args.scenario)
    return 0


def cmd_orchestrator(args) -> int:
    from .server import serve
    serve(args.port)
    return 0


def cmd_web(args) -> int:
    from .web import serve
    serve(args.port, args.upstream)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eig_agent")
    sub = parser.add_subparsers(dest="cmd", required=True)
    mo = sub.add_parser("models", help="list Vultr Inference models and image support")
    mo.add_argument("--json", action="store_true")
    t = sub.add_parser("triage", help="tamper triage of certified vs live results")
    src = t.add_mutually_exclusive_group(required=True)
    src.add_argument("--case", help="fixture id (t01) or path")
    src.add_argument("--live", help="live results URL or path")
    t.add_argument("--certified", default=CERTIFIED)
    t.add_argument("--meta", help="change metadata JSON (with --live)")
    t.add_argument("--no-report", action="store_true", help="skip the incident report call")
    t.add_argument("--json", action="store_true")
    t.add_argument("--gate", choices=["v1", "v2"], default=config.GATE)
    m = sub.add_parser("multilingual", help="check en/vi/es/tl pages against the certified calendar")
    m.add_argument("--scenario", default="drift")
    m.add_argument("--json", action="store_true")
    o = sub.add_parser("orchestrator", help="private orchestrator API")
    o.add_argument("--port", type=int, default=int(os.environ.get("EIG_ORCHESTRATOR_PORT", "8001")))
    w = sub.add_parser("web", help="public UI + /api forwarder")
    w.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    w.add_argument("--upstream", default=None, help="orchestrator URL (default EIG_ORCHESTRATOR_URL)")
    e = sub.add_parser("eval", help="accuracy, latency and cost per model on the fixtures")
    e.add_argument("--repeats", type=int, default=1)
    e.add_argument("--suite", action="append", choices=["triage", "drift"])
    e.add_argument("--split", action="append", choices=["dev", "heldout", "heldout2"])
    e.add_argument("--gate", action="append", choices=["v1", "v2"])
    e.add_argument("--model", action="append", help="limit to these model ids")
    rec = sub.add_parser("record", help="run demo scenarios live and save them as recorded runs")
    rec.add_argument("scenario", nargs="*")
    args = parser.parse_args(argv)
    load_env()  # before any role or provider is resolved
    return {"models": cmd_models, "eval": cmd_eval, "record": cmd_record, "triage": cmd_triage, "multilingual": cmd_multilingual,
"orchestrator": cmd_orchestrator, "web": cmd_web}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
