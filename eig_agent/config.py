"""Provider, model routing, per-call limits, prices and the spend cap.

Every model call names a *role*, never a model id:

  fast       fact extraction from page text                 small cap
  reasoning  tamper verdicts, mismatch explanations          larger cap (reasoning models think in-band)
  report     incident report (only after a ROLLBACK decision)
  vision     screenshot explanation (only if MODEL_VISION is set; otherwise OCR text goes to reasoning)

Env (orchestrator only; the web container and the sandbox never see these):
  EIG_LLM_PROVIDER          mock (default, offline, free) | vultr
  VULTR_INFERENCE_API_KEY   secret
  VULTR_INFERENCE_BASE_URL  default https://api.vultrinference.com/v1
  MODEL_REASONING           model id for fast/reasoning/report (EIG_FAST_MODEL / EIG_REPORT_MODEL override)
  MODEL_VISION              model id that accepts image input, or empty
  EIG_PRICE_IN_PER_MTOK / EIG_PRICE_OUT_PER_MTOK   USD per million tokens, for the cost estimate
  EIG_DAILY_SPEND_CAP_USD   no new calls once today's estimated spend would pass this
  EIG_RESPONSE_FORMAT       json_schema (default) | json_object | none
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_BASE_URL = "https://api.vultrinference.com/v1"
MOCK_MODEL = "mock"


def provider() -> str:
    return os.environ.get("EIG_LLM_PROVIDER", "mock").strip().lower() or "mock"


def base_url() -> str:
    return (os.environ.get("VULTR_INFERENCE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def response_format() -> str:
    return os.environ.get("EIG_RESPONSE_FORMAT", "json_schema").strip().lower()


@dataclass(frozen=True)
class Route:
    role: str
    model: str
    max_tokens: int
    temperature: float = 0.0


def routes() -> dict:
    """Role -> Route. With the mock provider every role reports the model id "mock"."""
    if provider() == "mock":
        reasoning = fast = report = vision = MOCK_MODEL
    else:
        reasoning = os.environ.get("MODEL_REASONING", "").strip()
        fast = os.environ.get("EIG_FAST_MODEL", "").strip() or reasoning
        report = os.environ.get("EIG_REPORT_MODEL", "").strip() or reasoning
        vision = os.environ.get("MODEL_VISION", "").strip()
    return {
        "fast": Route("fast", fast, max_tokens=1500),
        # Reasoning models spend part of max_tokens on their thinking, so leave room for it.
        "reasoning": Route("reasoning", reasoning, max_tokens=6000),
        "report": Route("report", report, max_tokens=1800),
        "vision": Route("vision", vision, max_tokens=1500),
    }


def has_vision() -> bool:
    return bool(routes()["vision"].model)


TIMEOUT_S = 90.0
TRANSPORT_RETRIES = 2   # SDK-level retries on timeouts, 429 and 5xx
JSON_RETRIES = 1        # one extra call if the reply is not valid JSON for the schema

# Gate (see triage.decide / triage.decide_v2).
TAMPER_CONFIDENCE = 0.8
GATE = os.environ.get("EIG_GATE", "v2")  # v2: two independent verdicts must agree


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def prices() -> tuple:
    """(USD per prompt token, USD per completion token). Conservative defaults until the
    real Vultr price is confirmed; set EIG_PRICE_*_PER_MTOK to the published numbers."""
    return (_float_env("EIG_PRICE_IN_PER_MTOK", 1.0) / 1e6, _float_env("EIG_PRICE_OUT_PER_MTOK", 3.0) / 1e6)


def daily_spend_cap_usd() -> float:
    return _float_env("EIG_DAILY_SPEND_CAP_USD", 2.0)


def cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    if model == MOCK_MODEL:
        return 0.0
    p_in, p_out = prices()
    return prompt_tokens * p_in + completion_tokens * p_out
