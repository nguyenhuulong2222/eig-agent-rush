"""Results Seal: deterministic comparison of certified results with the live copy.

The SHA-256 of the certified results file is the seal. If the live bytes hash the same,
nothing happens and no model is called. If they differ, this module produces a
field-level diff and runs the existing checker rules on the live copy, so Nemotron
reasons over facts that were computed, not guessed.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import urllib.request

from checker.rules_results import check_anomalies, check_results

MAX_SWING_PP = 5.0  # same threshold as config/policy.json
_TOKEN = re.compile(r"\.?([A-Za-z_][A-Za-z0-9_]*)|\[id=([^\]]+)\]|\[(\d+)\]")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_source(src: str, timeout: float = 15.0) -> bytes:
    """Bytes from a local path or an http(s) URL."""
    if src.startswith(("http://", "https://")):
        req = urllib.request.Request(src, headers={"User-Agent": "eig-nemotron-agent/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    with open(src, "rb") as fh:
        return fh.read()


def _child_label(item, index: int) -> str:
    return f"[id={item['id']}]" if isinstance(item, dict) and "id" in item else f"[{index}]"


def flatten(value, prefix: str = "") -> dict:
    """Map of path -> scalar. Lists of objects with an 'id' are keyed by id, others by index."""
    out = {}
    if isinstance(value, dict):
        for key, sub in value.items():
            out.update(flatten(sub, f"{prefix}.{key}" if prefix else key))
    elif isinstance(value, list):
        for i, sub in enumerate(value):
            out.update(flatten(sub, prefix + _child_label(sub, i)))
        if not value:
            out[prefix] = []
    else:
        out[prefix] = value
    return out


def diff(certified, live) -> list:
    a, b = flatten(certified), flatten(live)
    missing = object()
    rows = []
    for path in list(a) + [p for p in b if p not in a]:
        before, after = a.get(path, missing), b.get(path, missing)
        if before != after or type(before) is not type(after):
            rows.append({"path": path,
                         "certified": None if before is missing else before,
                         "live": None if after is missing else after,
                         "kind": "added" if before is missing else "removed" if after is missing else "changed"})
    return rows


def _resolve(doc, token):
    key, ident, index = token
    if key:
        return doc, key
    if ident is not None:
        for i, item in enumerate(doc):
            if isinstance(item, dict) and str(item.get("id")) == ident:
                return doc, i
        raise KeyError(f"no item with id={ident}")
    return doc, int(index)


def apply_patches(doc, patches: list):
    """Return a copy of doc with each {"path", "value"} set. Paths use the flatten() syntax."""
    doc = copy.deepcopy(doc)
    for patch in patches:
        tokens = [m.groups() for m in _TOKEN.finditer(patch["path"])]
        node = doc
        for token in tokens[:-1]:
            parent, key = _resolve(node, token)
            node = parent[key]
        parent, key = _resolve(node, tokens[-1])
        parent[key] = patch["value"]
    return doc


def compare(certified_bytes: bytes, live_bytes: bytes) -> dict:
    """Deterministic part of the gate. No model calls."""
    seal, live_hash = sha256(certified_bytes), sha256(live_bytes)
    result = {"certified_sha256": seal, "live_sha256": live_hash, "match": seal == live_hash,
              "diff": [], "findings": [], "blocking": False}
    if result["match"]:
        return result
    certified = json.loads(certified_bytes)
    try:
        live = json.loads(live_bytes)
    except json.JSONDecodeError as exc:
        result["findings"] = [{"rule_id": "SEAL-001", "severity": "BLOCK",
                               "title": "Live results are not valid JSON", "detail": str(exc)}]
        result["blocking"] = True
        return result
    findings = check_results(live, "live/results.json") + check_anomalies(
        live, certified, "live/results.json", MAX_SWING_PP, "the certified copy")
    # ANOM-004 is about consecutive updates. An unchanged sequence number is expected when the
    # live copy is compared with its own certified version, so only a decrease is kept.
    seq = lambda d: (d.get("update") or {}).get("sequence") if isinstance(d, dict) else None  # noqa: E731
    if seq(live) == seq(certified):
        findings = [f for f in findings if f.rule_id != "ANOM-004"]
    result["diff"] = diff(certified, live)
    result["findings"] = [f.to_dict() for f in findings]
    result["blocking"] = any(f.severity == "BLOCK" for f in findings)
    return result
