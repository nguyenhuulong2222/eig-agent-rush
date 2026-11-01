"""Model routing, per-call limits and prices for the Nemotron Integrity Agent.

Every model call names a *role*, never a model id. The role decides the model, the
token cap and whether Nemotron's reasoning ("thinking") is on:

  fast       high-volume extraction and triage           thinking off, small cap
  reasoning  tamper verdicts, mismatch explanations       thinking on, larger cap
  report     final incident report (confirmed tampering)  thinking off, rare calls

Model ids and prices were read from GET /v1/models?verbose=true on Nebius Token Factory
(snapshot 2026-10-24, USD per token). `python -m eig_agent models` re-checks them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

BASE_URL = "https://api.tokenfactory.nebius.com/v1/"

LIGHTNING = "nvidia/Nemotron-3_5-Lightning"
NANO = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
SUPER = "nvidia/nemotron-3-super-120b-a12b"
ULTRA = "nvidia/Nemotron-3-Ultra-550b-a55b"
SUPER_VL = "nvidia/Nemotron-3.5-Super-VL"

PRICES_SOURCE = "GET /v1/models?verbose=true, snapshot 2026-10-24"
# model id -> (USD per prompt token, USD per completion token)
PRICES = {
    LIGHTNING: (0.00000006, 0.00000024),
    NANO: (0.00000006, 0.00000024),
    SUPER: (0.0000003, 0.0000009),
    ULTRA: (0.000001, 0.000003),
    SUPER_VL: (0.0000003, 0.0000009),
}

FAST_CANDIDATES = (LIGHTNING, NANO)


@dataclass(frozen=True)
class Route:
    role: str
    model: str
    max_tokens: int
    thinking: bool
    temperature: float = 0.0


def routes() -> dict:
    """Role -> Route. Model ids can be overridden with EIG_<ROLE>_MODEL (used by the eval)."""
    def pick(role: str, default: str) -> str:
        return os.environ.get(f"EIG_{role.upper()}_MODEL", default)

    return {
        "fast": Route("fast", pick("fast", LIGHTNING), max_tokens=1200, thinking=False),
        # Reasoning tokens count against max_tokens, so the cap must leave room for them.
        "reasoning": Route("reasoning", pick("reasoning", SUPER), max_tokens=6000, thinking=True),
        "report": Route("report", pick("report", ULTRA), max_tokens=1800, thinking=False),
    }


TIMEOUT_S = 90.0
TRANSPORT_RETRIES = 2   # SDK-level retries on timeouts, 429 and 5xx
JSON_RETRIES = 1        # one extra call if the reply is not valid JSON for the schema

# Gate (see triage.decide / triage.decide_v2).
TAMPER_CONFIDENCE = 0.8
GATE = os.environ.get("EIG_GATE", "v2")  # v2: two independent Super verdicts must agree


def cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    p_in, p_out = PRICES.get(model, (0.0, 0.0))
    return prompt_tokens * p_in + completion_tokens * p_out
