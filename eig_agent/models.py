"""List the models on Vultr Serverless Inference and say which can take image input.

  python scripts/list_models.py [--json]
  python -m eig_agent models [--json]

GET {VULTR_INFERENCE_BASE_URL}/models with the API key. The response shape is OpenAI-style
({"data": [{"id": ...}, ...]}); any extra metadata fields are printed so we can see what Vultr
reports. Image support is taken from metadata when present ("metadata"), else guessed from the
model id ("name only" — verify with one real image call before relying on it).
One GET request; no tokens are spent.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request

from . import config
from .llm import load_env

_VISION_NAME = re.compile(r"(^|[-_/.])(vl|vision|llava|pixtral|multimodal|omni)([-_/.]|$)|"
                          r"qwen[\d.]*-?vl|gemma-?3|llama-?3\.2-(11|90)b|llama-?4|internvl|minicpm-?v", re.I)
_VISION_WORDS = ("image", "vision", "multimodal")


def _flatten_strings(value) -> list:
    if isinstance(value, str):
        return [value.lower()]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _flatten_strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _flatten_strings(v)]
    return []


def vision_support(model: dict) -> tuple:
    """(True/False, how we know). Metadata beats the name heuristic."""
    for key in ("input_modalities", "modalities", "capabilities", "features", "architecture", "type",
                "model_type", "tags"):
        if key in model and model[key] not in (None, "", [], {}):
            words = _flatten_strings(model[key])
            if words:
                return any(any(w in s for w in _VISION_WORDS) for s in words), f"metadata: {key}"
    return bool(_VISION_NAME.search(model.get("id", ""))), "name only"


def fetch(base_url: str = None, key: str = None, timeout: float = 30.0) -> list:
    load_env()
    key = key or os.environ.get("VULTR_INFERENCE_API_KEY")
    if not key:
        raise SystemExit("VULTR_INFERENCE_API_KEY is not set (put it in .env)")
    req = urllib.request.Request((base_url or config.base_url()) + "/models",
                                 headers={"Authorization": "Bearer " + key, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.load(resp)
    return body.get("data", body) if isinstance(body, dict) else body


def report(models: list) -> dict:
    rows = []
    for m in models:
        ok, how = vision_support(m)
        rows.append({"id": m.get("id"), "vision": ok, "vision_source": how,
                     "fields": sorted(k for k in m if k not in ("id", "object"))})
    vision = [r["id"] for r in rows if r["vision"]]
    return {"models": rows, "vision_models": vision,
            "path": "vision model" if vision else "OCR fallback (tesseract in the sandbox; LLM reasons over text)"}


def main(as_json: bool = False) -> int:
    out = report(fetch())
    if as_json:
        print(json.dumps(out, indent=2))
        return 0
    print(f"{len(out['models'])} models at {config.base_url()}/models")
    for r in out["models"]:
        tag = "IMAGE IN" if r["vision"] else "text"
        print(f"  {r['id']:<55} {tag:<9} ({r['vision_source']})  fields: {', '.join(r['fields']) or '-'}")
    print(f"Vision-capable: {', '.join(out['vision_models']) or 'none'}")
    print(f"Path: {out['path']}")
    configured = {"MODEL_REASONING": os.environ.get("MODEL_REASONING", ""),
                  "MODEL_VISION": os.environ.get("MODEL_VISION", "")}
    ids = {r["id"] for r in out["models"]}
    for name, value in configured.items():
        if value:
            print(f"{name}={value}: {'available' if value in ids else 'NOT in the list'}")
    return 0
