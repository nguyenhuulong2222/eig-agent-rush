"""JSON schemas for every model output, and a small stdlib validator for them.

The same schema is sent to Token Factory as `response_format: json_schema` and checked
again here, because a schema hint is not a guarantee.
"""

from __future__ import annotations

_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool,
    "integer": int, "number": (int, float), "null": type(None),
}


def validate(value, schema: dict, path: str = "$") -> list:
    """Return a list of error strings; empty means valid. Supports the subset used below."""
    errors = []
    expected = schema.get("type")
    if expected:
        kinds = expected if isinstance(expected, list) else [expected]
        ok = any(isinstance(value, _TYPES[k]) and not (k in ("integer", "number") and isinstance(value, bool))
                 for k in kinds)
        if not ok:
            return [f"{path}: expected {expected}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in {schema['enum']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} < {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {value} > {schema['maximum']}")
    if isinstance(value, str) and "maxLength" in schema and len(value) > schema["maxLength"]:
        errors.append(f"{path}: longer than {schema['maxLength']} characters")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing '{key}'")
        if schema.get("additionalProperties") is False:
            errors.extend(f"{path}: unexpected '{k}'" for k in value if k not in props)
        for key, sub in props.items():
            if key in value:
                errors.extend(validate(value[key], sub, f"{path}.{key}"))
    if isinstance(value, list):
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: more than {schema['maxItems']} items")
        if "items" in schema:
            for i, item in enumerate(value):
                errors.extend(validate(item, schema["items"], f"{path}[{i}]"))
    return errors


def _obj(props: dict, required=None) -> dict:
    return {"type": "object", "properties": props,
            "required": list(required if required is not None else props), "additionalProperties": False}


_STR = {"type": "string"}

TAMPER_TRIAGE = _obj({
    "verdict": {"type": "string", "enum": ["legitimate", "tampering", "uncertain"]},
    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    "evidence": {"type": "array", "maxItems": 8, "items": _obj({
        "change": _STR,          # which field changed, old -> new
        "observation": _STR,     # what it means
        "points_to": {"type": "string", "enum": ["legitimate", "tampering", "neutral"]},
    })},
    "recommended_action": {"type": "string",
                           "enum": ["no_action", "human_review", "escalate_to_human", "rollback_to_certified"]},
    "summary": _STR,
})

INCIDENT_REPORT = _obj({"incident_report_md": _STR})

# Election facts stated on one page. "" means the page does not state it.
FACT_KEYS = ("election_day", "polls_open", "polls_close", "drop_box_close", "mail_ballots_sent",
             "service_centers_open", "certification_date", "polling_place_site", "registration_site", "phone")
_FACT = _obj({"value": _STR, "quote": _STR})
ELECTION_FACTS = _obj({
    "language": _STR,
    "facts": _obj({key: _FACT for key in FACT_KEYS}),
    "drop_box_addresses": {"type": "array", "maxItems": 10, "items": _FACT},
})

MISMATCH_EXPLANATIONS = _obj({
    "explanations": {"type": "array", "maxItems": 20, "items": _obj({
        "id": _STR,              # "<lang>:<field>" as given in the input
        "explanation": _STR,
        "voter_impact": _STR,
        "severity": {"type": "string", "enum": ["high", "medium", "low"]},
        "suggested_fix": _STR,   # corrected sentence in the page's language
    })},
    "summary": _STR,
})

SEARCH_TRIAGE = _obj({
    "items": {"type": "array", "maxItems": 30, "items": _obj({
        "id": _STR,
        "about_county": {"type": "boolean"},  # about the Kanaloa County election at all
        "category": {"type": "string",
                     "enum": ["official", "impersonation", "misinformation", "news", "unrelated", "other"]},
        "reason": _STR,
    })},
})

HUNTER_REPORT = _obj({
    "findings": {"type": "array", "maxItems": 10, "items": _obj({
        "url": _STR,
        "risk": {"type": "string", "enum": ["high", "medium", "low"]},
        "false_claims": {"type": "array", "maxItems": 8, "items": _STR},
        "lookalike_domains": {"type": "array", "maxItems": 8, "items": _STR},
        "recommended_action": _STR,
    })},
    "report_md": _STR,
})
