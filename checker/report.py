"""Render a Report as JSON (for the Duo agent and artifacts) and Markdown (for people)."""

from __future__ import annotations

import hashlib
import json

from . import __version__
from .model import BLOCK, INFO, PASS, WARN, Report

JSON_BEGIN = "----- EIG-REPORT-JSON-BEGIN -----"
JSON_END = "----- EIG-REPORT-JSON-END -----"

_HEADLINE = {
    BLOCK: "This change must not be published as is.",
    WARN: "No hard failures, but a person must review the warnings below.",
    PASS: "No integrity problems found.",
}


def to_dict(report: Report, context: dict) -> dict:
    return {
        "tool": "election-integrity-guard",
        "version": __version__,
        "verdict": report.verdict,
        "summary": {"block": report.count(BLOCK), "warn": report.count(WARN), "info": report.count(INFO)},
        "context": context,
        "findings": [f.to_dict() for f in report.sorted_findings()],
    }


def to_json(report: Report, context: dict) -> str:
    return json.dumps(to_dict(report, context), indent=2, ensure_ascii=False) + "\n"


def _cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def to_markdown(report: Report, context: dict) -> str:
    lines = [
        "## Election Integrity Guard: deterministic check",
        "",
        f"**Verdict: {report.verdict}.** {_HEADLINE[report.verdict]}",
        "",
        f"{report.count(BLOCK)} blocking · {report.count(WARN)} warnings · {report.count(INFO)} notes"
        f" · base `{(context.get('base') or 'none')[:12]}` · checker v{__version__}",
        "",
    ]
    findings = report.sorted_findings()
    if findings:
        lines += ["| Severity | Rule | Where | Finding |", "|---|---|---|---|"]
        for f in findings:
            where = f.file or "MR"
            if f.line:
                where += f":{f.line}"
            lines.append(f"| {f.severity} | {f.rule_id} | `{_cell(where)}` | **{_cell(f.title)}.** {_cell(f.detail)} |")
        fixes = [f for f in findings if f.suggestion]
        if fixes:
            lines += ["", "### Suggested fixes", ""]
            lines += [f"- **{f.rule_id}** ({f.file or 'MR'}): {f.suggestion}" for f in fixes]
    lines += ["", "_The checker is the gate. The Duo review explains it; a human approves the release._", ""]
    return "\n".join(lines)


# GitLab Code Quality report: shows BLOCK/WARN findings in the MR widget on every tier,
# with no token needed. https://docs.gitlab.com/ci/testing/code_quality/
_CQ_SEVERITY = {BLOCK: "blocker", WARN: "major"}
_MR_DESCRIPTION_PROXY = ".gitlab/merge_request_templates/Default.md"


def to_codequality(report: Report) -> str:
    items = []
    for f in report.sorted_findings():
        if f.severity not in _CQ_SEVERITY:
            continue
        path = f.file or _MR_DESCRIPTION_PROXY
        key = "|".join([f.rule_id, path, str(f.line or ""), f.title, f.detail])
        items.append({
            "description": f"[{f.severity}] {f.rule_id}: {f.title}. {f.detail}",
            "check_name": f.rule_id,
            "fingerprint": hashlib.sha256(key.encode("utf-8")).hexdigest(),
            "severity": _CQ_SEVERITY[f.severity],
            "location": {"path": path, "lines": {"begin": f.line or 1}},
        })
    return json.dumps(items, indent=2, ensure_ascii=False) + "\n"
