"""Load config, gather head and base state, and run every rule."""

from __future__ import annotations

import glob
import json
import os
from typing import Optional

from . import gitutil
from .htmlscan import parse_html
from .model import BLOCK, INFO, Finding, Report
from .rules_governance import check_codeowners, check_issue_link, check_protected_paths
from .rules_results import check_anomalies, check_results
from .rules_site import (check_allowlist_change, check_assets, check_csp, check_links,
                         check_security_policy_change, scan_js)

POLICY = "config/policy.json"
ALLOWLIST = "config/allowlist.json"
SECURITY = "config/security-policy.json"
CODEOWNERS = ".gitlab/CODEOWNERS"


def _read(root: str, rel: str) -> Optional[str]:
    try:
        with open(os.path.join(root, rel), encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def _load_json(root: str, rel: str) -> dict:
    text = _read(root, rel)
    if text is None:
        raise FileNotFoundError(f"required config file {rel} is missing")
    return json.loads(text)


def _json_or_none(text: Optional[str]):
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _previous_update(root: str, base: Optional[str], policy: dict, current: dict):
    """The update this one should be compared with: the base version if it differs, else the newest history file."""
    results_rel = policy["results_file"]
    cur_seq = (current.get("update") or {}).get("sequence") if isinstance(current, dict) else None
    if base:
        base_doc = _json_or_none(gitutil.show(root, base, results_rel))
        if isinstance(base_doc, dict) and (base_doc.get("update") or {}).get("sequence") != cur_seq:
            return base_doc, f"the base version of {results_rel}"
    best = None
    for path in glob.glob(os.path.join(root, policy["history_dir"], "*.json")):
        doc = _json_or_none(_read(root, os.path.relpath(path, root)))
        seq = (doc or {}).get("update", {}).get("sequence") if isinstance(doc, dict) else None
        if isinstance(seq, int) and (cur_seq is None or seq < cur_seq) and (best is None or seq > best[0]):
            best = (seq, doc, os.path.relpath(path, root))
    if best:
        return best[1], f"update #{best[0]} ({best[2]})"
    return None, None


def run(root: str, base: Optional[str], description: str, is_mr: bool):
    policy = _load_json(root, POLICY)
    allow = _load_json(root, ALLOWLIST)
    security = _load_json(root, SECURITY)
    report = Report()

    changed = gitutil.changed_files(root, base) if base else None
    if base and changed is None:
        report.extend([Finding("SYS-001", INFO, "system", "Could not diff against base",
                               f"git diff against {base[:12]} failed; base comparisons were skipped.")])

    # Results arithmetic and anomalies
    results_rel = policy["results_file"]
    text = _read(root, results_rel)
    if text is None:
        report.extend([Finding("RES-001", BLOCK, "results-arithmetic", "Results file is missing",
                               f"{results_rel} does not exist.", results_rel)])
    else:
        try:
            current = json.loads(text)
        except json.JSONDecodeError as exc:
            current = None
            report.extend([Finding("RES-001", BLOCK, "results-arithmetic", "Results file is not valid JSON",
                                   str(exc), results_rel, exc.lineno)])
        if current is not None:
            report.extend(check_results(current, results_rel))
            previous, label = _previous_update(root, base, policy, current)
            report.extend(check_anomalies(current, previous, results_rel,
                                          float(policy["anomaly"]["max_share_swing_pp"]), label or ""))

    # History files must be internally consistent too
    for path in sorted(glob.glob(os.path.join(root, policy["history_dir"], "*.json"))):
        rel = os.path.relpath(path, root)
        if changed is None or rel in changed:
            doc = _json_or_none(_read(root, rel))
            if doc is None:
                report.extend([Finding("RES-001", BLOCK, "results-arithmetic", "History file is not valid JSON",
                                       f"{rel} could not be parsed.", rel)])
            else:
                report.extend(check_results(doc, rel))

    # Site: links, assets, CSP
    site = policy["site_dir"]
    for path in sorted(glob.glob(os.path.join(root, site, "**", "*.html"), recursive=True)):
        rel = os.path.relpath(path, root)
        doc = parse_html(_read(root, rel) or "")
        base_text = gitutil.show(root, base, rel) if base else None
        base_doc = parse_html(base_text) if base_text else None
        report.extend(check_links(doc, rel, allow))
        report.extend(check_assets(doc, rel, allow))
        report.extend(check_csp(doc, rel, security, allow, base_doc))
    for path in sorted(glob.glob(os.path.join(root, site, "**", "*.js"), recursive=True)):
        rel = os.path.relpath(path, root)
        report.extend(scan_js(_read(root, rel) or "", rel))

    # Policy files must not be loosened
    if base:
        report.extend(check_security_policy_change(security, _json_or_none(gitutil.show(root, base, SECURITY)), SECURITY))
        report.extend(check_allowlist_change(allow, _json_or_none(gitutil.show(root, base, ALLOWLIST)), ALLOWLIST))
        base_policy = _json_or_none(gitutil.show(root, base, POLICY))
        if isinstance(base_policy, dict):
            before = float(base_policy.get("anomaly", {}).get("max_share_swing_pp", 0))
            after = float(policy["anomaly"]["max_share_swing_pp"])
            if after > before:
                report.extend([Finding("POL-001", "WARN", "governance", "Anomaly threshold raised",
                                       f"max_share_swing_pp went from {before:g} to {after:g}; "
                                       "larger swings will no longer be flagged.", POLICY)])
            if base_policy.get("require_issue_reference") and not policy.get("require_issue_reference"):
                report.extend([Finding("POL-002", BLOCK, "governance", "Issue-link requirement turned off",
                                       "require_issue_reference was disabled.", POLICY)])

    # Governance
    report.extend(check_issue_link(description, is_mr, bool(policy.get("require_issue_reference", True))))
    report.extend(check_codeowners(_read(root, CODEOWNERS), ["/results/", "/config/"]))
    if changed:
        report.extend(check_protected_paths(changed, policy.get("code_owner_paths", [])))

    return report, changed
