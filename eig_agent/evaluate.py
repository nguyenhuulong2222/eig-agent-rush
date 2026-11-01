"""Eval: accuracy, latency and cost per model on the fixture cases.

  python -m eig_agent eval [--repeats 2] [--suite triage|drift] [--split dev|heldout|heldout2]
                                [--gate v1|v2] [--model MODEL_ID]

Two suites:
  triage   tamper triage (dev T01-T08, held-out H01-H07, held-out 2 H08-H14), run on the
           REASONING role with each candidate model, thinking on, through gate v1 (one verdict)
           and/or v2 (two independent verdicts must agree). Scored on the verdict label (v1) and
           on the gate decision (both): tampering -> ROLLBACK, legitimate -> HUMAN_SIGNOFF,
           uncertain -> ESCALATE.
  drift    translation-drift scenarios D1-D7, FAST extraction only (no explanation call),
           with each FAST candidate. Scored on the set of (language, field) mismatches.

Writes eig_agent/eval/results-<timestamp>.json (one file per run) and RESULTS.md (latest run).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

from . import config, multilingual, triage
from .__main__ import CASES, CERTIFIED, case_live_bytes, load_case
from .llm import NemotronClient

PKG = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(PKG, "eval")
HELDOUT = os.path.join(os.path.dirname(CASES), "heldout")
SPLITS = {"dev": CASES, "heldout": HELDOUT, "heldout2": os.path.join(os.path.dirname(CASES), "heldout2")}
EXPECTED_DECISION = {"tampering": "ROLLBACK", "legitimate": "HUMAN_SIGNOFF", "uncertain": "ESCALATE"}
TRIAGE_MODELS = (config.SUPER, config.LIGHTNING, config.NANO)
FAST_MODELS = (config.LIGHTNING, config.NANO)
WORKERS = 4


def _cases(directory: str) -> list:
    return [load_case(os.path.join(directory, n)) for n in sorted(os.listdir(directory)) if n.endswith(".json")]


def _usage(calls) -> dict:
    return {"latency_ms": sum(c.latency_ms for c in calls), "cost_usd": sum(c.cost_usd for c in calls),
            "prompt_tokens": sum(c.prompt_tokens for c in calls),
            "completion_tokens": sum(c.completion_tokens for c in calls), "calls": len(calls)}


def run_triage_case(case: dict, model: str, split: str, repeat: int, gate: str = "v1") -> dict:
    started = time.monotonic()
    cert = open(CERTIFIED, "rb").read()
    client = NemotronClient(routes={**config.routes(),
                                    "reasoning": config.Route("reasoning", model, 6000, True)})
    out = triage.run(cert, case_live_bytes(case, cert), case["metadata"], client, report=False, gate=gate)
    votes = [(v["verdict"], v["confidence"]) if v else ("error", None) for v in out["votes"]]
    decision = out["decision"]["action"]
    return {"suite": "triage", "split": split, "case": case["id"], "model": model, "gate": gate,
            "repeat": repeat, "expected": case["expected"], "votes": votes,
            "got": votes[0][0], "confidence": votes[0][1], "correct": votes[0][0] == case["expected"],
            "decision": decision, "expected_decision": EXPECTED_DECISION[case["expected"]],
            "decision_correct": decision == EXPECTED_DECISION[case["expected"]],
            "error": out["triage_error"], "wall_ms": int((time.monotonic() - started) * 1000),
            **_usage(out["calls"])}


def run_drift_case(name: str, spec: dict, model: str, repeat: int) -> dict:
    started = time.monotonic()
    out = multilingual.run(multilingual.load_pages(name), NemotronClient(), fast_model=model, explain=False)
    got = sorted({(m["lang"], m["field"]) for m in out["mismatches"]})
    want = sorted({tuple(x) for x in spec["expected"]})
    tp = len(set(got) & set(want))
    return {"suite": "drift", "split": "eval", "case": spec["eval_id"], "model": model, "gate": "-", "repeat": repeat,
            "expected": want, "got": got, "tp": tp, "fp": len(got) - tp, "fn": len(want) - tp,
            "correct": got == want, "kinds": sorted({m["kind"] for m in out["mismatches"]}),
            "error": "; ".join(out["errors"]) or None, "wall_ms": int((time.monotonic() - started) * 1000),
            **_usage(out["calls"])}


def summarize(rows: list) -> list:
    groups = {}
    for r in rows:
        groups.setdefault((r["suite"], r["split"], r["model"], r.get("gate", "v1")), []).append(r)
    out = []
    for (suite, split, model, gate), rs in sorted(groups.items()):
        row = {"suite": suite, "split": split, "model": model, "gate": gate, "runs": len(rs),
               "cases": len({r["case"] for r in rs}),
               "correct": sum(r["correct"] for r in rs),
               "accuracy": sum(r["correct"] for r in rs) / len(rs),
               "median_latency_s": statistics.median(r["wall_ms"] for r in rs) / 1000,
               "mean_cost_usd": sum(r["cost_usd"] for r in rs) / len(rs),
               "errors": sum(1 for r in rs if r["error"])}
        if suite == "triage":
            not_tamper = [r for r in rs if r["expected"] != "tampering"]
            tamper = [r for r in rs if r["expected"] == "tampering"]
            row.update(decision_correct=sum(r.get("decision_correct", False) for r in rs),
                       escalations=sum(r["decision"] == "ESCALATE" for r in rs),
                       false_rollback=sum(r["decision"] == "ROLLBACK" for r in not_tamper),
                       not_tamper_runs=len(not_tamper),
                       missed_tamper=sum(r["decision"] == "HUMAN_SIGNOFF" for r in tamper),
                       tamper_runs=len(tamper))
        if suite == "drift":
            tp, fp, fn = (sum(r[k] for r in rs) for k in ("tp", "fp", "fn"))
            row.update(tp=tp, fp=fp, fn=fn, precision=tp / (tp + fp) if tp + fp else 1.0,
                       recall=tp / (tp + fn) if tp + fn else 1.0)
        out.append(row)
    return out


def to_markdown(summary: list, rows: list, meta: dict) -> str:
    short = lambda m: m.split("/")[-1]  # noqa: E731
    lines = [f"# Eval results ({meta['date']})", "",
             f"Repeats per case and model: {meta['repeats']}. Temperature 0. Triage prompt SHA-256 "
             f"`{meta['triage_prompt_sha256'][:12]}…`. Small fixture sets: read these as a sanity check, "
             "not a benchmark. Latency is wall-clock per run with 4 runs in flight at once (drift runs its "
             "4 language extractions in parallel); a run that ends in an error still counts as wrong.", "",
             "## Summary", "",
             "Triage \"verdict accuracy\" scores the first verdict; \"decision correct\" scores what the gate did.",
             "",
             "| Suite | Split | Model | Gate | Cases | Runs | Verdict accuracy | Decision correct | Median latency | Mean cost/run |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for s in summary:
        extra = f" (P {s['precision']:.2f} / R {s['recall']:.2f})" if s["suite"] == "drift" else ""
        dec = f"{s['decision_correct']}/{s['runs']}" if s["suite"] == "triage" else "–"
        lines.append(f"| {s['suite']} | {s['split']} | {short(s['model'])} | {s['gate']} | {s['cases']} | {s['runs']} | "
                     f"{s['correct']}/{s['runs']} ({s['accuracy']:.0%}){extra} | {dec} | {s['median_latency_s']:.1f} s | "
                     f"${s['mean_cost_usd']:.5f} |")
    lines += ["", "## Gate outcomes (triage)", "",
              "What the gate did with each verdict. A false rollback replaces live results with the "
              "certified copy and alerts an official; a missed tampering leaves tampered numbers live "
              "until the official's sign-off review.", "",
              "| Split | Model | Gate | False rollbacks (legitimate or uncertain cases) | Tampering sent to sign-off as legitimate | Escalations (all runs) |",
              "|---|---|---|---|---|---|"]
    for s in summary:
        if s["suite"] == "triage":
            lines.append(f"| {s['split']} | {short(s['model'])} | {s['gate']} | {s['false_rollback']} of {s['not_tamper_runs']} | "
                         f"{s['missed_tamper']} of {s['tamper_runs']} | {s['escalations']} of {s['runs']} |")
    lines += ["", "## Per case", "", "| Suite | Case | Model | Gate | Expected | Got (per repeat) |",
              "|---|---|---|---|---|---|"]
    per = {}
    for r in rows:
        per.setdefault((r["suite"], r["case"], r["model"], r.get("gate", "v1")), []).append(r)
    fmt = lambda v: v if isinstance(v, str) else (", ".join(f"{a}:{b}" for a, b in v) or "none")  # noqa: E731

    def cell(r):
        if r["suite"] == "drift":
            return ("✅ " if r["correct"] else "❌ ") + fmt(r["got"])
        votes = " + ".join(f"{v} {c:.2f}" if c is not None else v for v, c in r.get("votes", [(r["got"], r["confidence"])]))
        return ("✅ " if r.get("decision_correct", r["correct"]) else "❌ ") + f"{votes} → {r['decision']}"

    for (suite, case, model, gate), rs in sorted(per.items()):
        got = " / ".join(cell(r) for r in sorted(rs, key=lambda r: r["repeat"]))
        exp = fmt(rs[0]["expected"]) + (f" ({rs[0]['expected_decision']})" if suite == "triage" else "")
        lines.append(f"| {suite} | {case} | {short(model)} | {gate} | {exp} | {got} |")
    return "\n".join(lines) + "\n"


def write(result: dict, stamp: str) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, f"results-{stamp}.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=1, ensure_ascii=False)
    with open(os.path.join(OUT_DIR, "RESULTS.md"), "w", encoding="utf-8") as fh:
        fh.write(to_markdown(result["summary"], result["rows"], result["meta"]))


def main(repeats: int = 1, suites=("triage", "drift"), splits=("dev", "heldout", "heldout2"),
         gates=("v1",), models=None) -> dict:
    import hashlib
    jobs = []
    if "triage" in suites:
        for split in splits:
            for case in _cases(SPLITS[split]):
                for model in models or TRIAGE_MODELS:
                    for gate in gates:
                        for rep in range(repeats):
                            jobs.append(lambda c=case, m=model, s=split, r=rep, g=gate: run_triage_case(c, m, s, r, g))
    if "drift" in suites:
        with open(os.path.join(multilingual.I18N, "scenarios.json"), encoding="utf-8") as fh:
            scenarios = {k: v for k, v in json.load(fh).items() if not k.startswith("_") and "eval_id" in v}
        for name, spec in scenarios.items():
            for model in models or FAST_MODELS:
                for rep in range(repeats):
                    jobs.append(lambda n=name, sp=spec, m=model, r=rep: run_drift_case(n, sp, m, r))
    NemotronClient().transport  # fail fast on a missing key
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        rows = list(pool.map(lambda job: job(), jobs))
    meta = {"date": dt.date.today().isoformat(), "repeats": repeats, "workers": WORKERS,
            "suites": list(suites), "splits": list(splits), "gates": list(gates),
            "triage_prompt_sha256": hashlib.sha256(triage.TRIAGE_SYSTEM.encode()).hexdigest(),
            "prices_source": config.PRICES_SOURCE}
    result = {"meta": meta, "summary": summarize(rows), "rows": rows}
    write(result, dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    return result
