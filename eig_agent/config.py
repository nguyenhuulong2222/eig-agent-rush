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
  EIG_PRICE_{REASONING,VISION}_{IN,OUT}_PER_MTOK  USD per million tokens per role's model
  EIG_PRICE_IN_PER_MTOK / EIG_PRICE_OUT_PER_MTOK   shared fallback pair
  EIG_DAILY_SPEND_CAP_USD   no new calls once today's estimated spend would pass this
  EIG_RESPONSE_FORMAT       json_schema (default) | json_object | none
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

def env(name: str, default: str = "") -> str:
    """An env value with any inline ` # comment` removed. Compose's env_file keeps the comment when
    the value itself is empty (`MODEL_VISION=   # note` arrives as "# note"), so a value that starts
    with '#' counts as empty."""
    value = os.environ.get(name)
    if value is None:
        return default
    value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
    return default if value.startswith("#") or not value else value


DEFAULT_BASE_URL = "https://api.vultrinference.com/v1"
MOCK_MODEL = "mock"


def provider() -> str:
    return env("EIG_LLM_PROVIDER", "mock").lower() or "mock"


def base_url() -> str:
    return env("VULTR_INFERENCE_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def response_format() -> str:
    return env("EIG_RESPONSE_FORMAT", "json_schema").lower()


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
        reasoning = env("MODEL_REASONING")
        fast = env("EIG_FAST_MODEL") or reasoning
        report = env("EIG_REPORT_MODEL") or reasoning
        vision = env("MODEL_VISION")
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
        return float(env(name) or default)
    except ValueError:
        return default


# Which price set a role uses. fast/report run on MODEL_REASONING unless overridden, so they bill
# at the reasoning model's price; only the vision role has its own model.
PRICE_GROUP = {"fast": "REASONING", "reasoning": "REASONING", "report": "REASONING", "vision": "VISION"}


def prices(role: str = None) -> tuple:
    """(USD per prompt token, USD per completion token) for a role.

    Vultr Serverless Inference bills per model, per token. Lookup order for each of IN / OUT:
      EIG_PRICE_{REASONING|VISION}_{IN|OUT}_PER_MTOK   (the role's own model)
      EIG_PRICE_{IN|OUT}_PER_MTOK                       (shared pair)
      1.00 / 3.00                                       (placeholder until the real price is set)
    """
    group = PRICE_GROUP.get(role or "", "REASONING")
    shared_in = _float_env("EIG_PRICE_IN_PER_MTOK", 1.0)
    shared_out = _float_env("EIG_PRICE_OUT_PER_MTOK", 3.0)
    return (_float_env(f"EIG_PRICE_{group}_IN_PER_MTOK", shared_in) / 1e6,
            _float_env(f"EIG_PRICE_{group}_OUT_PER_MTOK", shared_out) / 1e6)


def daily_spend_cap_usd() -> float:
    return _float_env("EIG_DAILY_SPEND_CAP_USD", 0.5)


def cost_usd(model: str, prompt_tokens: int, completion_tokens: int, role: str = None) -> float:
    if model == MOCK_MODEL:
        return 0.0
    p_in, p_out = prices(role)
    return prompt_tokens * p_in + completion_tokens * p_out
