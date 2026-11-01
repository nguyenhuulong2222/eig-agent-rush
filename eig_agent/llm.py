"""LLM client: one OpenAI-compatible transport (Vultr Serverless Inference) or an offline MOCK.

Role-based routing, JSON-schema validation with one retry, a per-call usage log and a daily
spend cap. Keys are read from the environment only (a local .env is loaded if present); they
are never printed or logged. The usage log records model, tokens, latency and estimated cost,
never prompts, replies or keys.

Only the orchestrator process imports this module. The web container and the sandbox never do.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

from . import config
from .schemas import validate

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_log_lock = threading.Lock()


def usage_log_path() -> str:
    return os.environ.get("EIG_USAGE_LOG", os.path.join(ROOT, "eig_agent", "logs", "usage.jsonl"))


def load_env(path: str = os.path.join(ROOT, ".env")) -> None:
    """Copy KEY=value lines from .env into os.environ without overriding real env vars."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip().removeprefix("export ").strip()
            value = value.split(" #", 1)[0].strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def no_progress(step: str, status: str, detail: str = "") -> None:
    """Default progress callback. The web demo passes one that records each pipeline step."""


class LLMError(RuntimeError):
    """A model call failed or never produced valid JSON. `calls` keeps the usage of every attempt."""

    def __init__(self, message: str, calls: Optional[list] = None):
        super().__init__(message)
        self.calls = calls or []


class SpendCapReached(LLMError):
    """Today's estimated spend would pass EIG_DAILY_SPEND_CAP_USD; no request was sent."""


@dataclass
class CallRecord:
    task: str
    role: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    latency_ms: int
    cost_usd: float
    ok: bool
    attempt: int
    error: Optional[str] = None


@dataclass
class LLMResult:
    data: dict
    calls: list = field(default_factory=list)

    @property
    def model(self) -> str:
        return self.calls[-1].model


# --- spend cap -------------------------------------------------------------------------

class SpendCap:
    """Daily (UTC) estimated spend. Each call reserves its worst case (prompt + max_tokens)
    before it is sent and settles to the real usage after, so concurrent calls cannot overshoot.
    On first use, today's spend is re-read from the usage log so a restart does not reset it."""

    def __init__(self, cap_usd: Optional[float] = None):
        self._cap = cap_usd
        self.lock = threading.Lock()
        self.day, self.spent, self.reserved = None, 0.0, 0.0

    @property
    def cap(self) -> float:
        return self._cap if self._cap is not None else config.daily_spend_cap_usd()

    def _roll(self) -> None:
        today = dt.datetime.now(dt.timezone.utc).date()
        if self.day != today:
            self.day, self.spent, self.reserved = today, _logged_spend(today), 0.0

    def reserve(self, usd: float) -> Optional[str]:
        with self.lock:
            self._roll()
            if self.spent + self.reserved + usd > self.cap:
                return (f"daily spend cap reached: ${self.spent:.4f} spent today, "
                        f"cap ${self.cap:.2f} (EIG_DAILY_SPEND_CAP_USD)")
            self.reserved += usd
            return None

    def settle(self, reserved: float, actual: float) -> None:
        with self.lock:
            self._roll()
            self.reserved = max(0.0, self.reserved - reserved)
            self.spent += actual

    def status(self) -> dict:
        with self.lock:
            self._roll()
            return {"spent_usd": round(self.spent, 6), "cap_usd": self.cap}


def _logged_spend(day: dt.date) -> float:
    path = usage_log_path()
    if os.environ.get("EIG_USAGE_LOG") == "off" or not os.path.exists(path):
        return 0.0
    total, prefix = 0.0, day.isoformat()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(row.get("ts", "")).startswith(prefix):
                total += float(row.get("cost_usd") or 0)
    return total


SPEND = SpendCap()


# --- transports ------------------------------------------------------------------------
# A transport sends one chat completion and returns a normalized dict. Tests pass a fake.
Transport = Callable[..., dict]


def vultr_transport() -> Transport:
    from openai import OpenAI  # imported lazily so offline tests do not need the SDK

    load_env()
    key = os.environ.get("VULTR_INFERENCE_API_KEY")
    if not key:
        raise LLMError("VULTR_INFERENCE_API_KEY is not set (put it in .env), or use EIG_LLM_PROVIDER=mock")
    client = OpenAI(base_url=config.base_url(), api_key=key,
                    timeout=config.TIMEOUT_S, max_retries=config.TRANSPORT_RETRIES)
    fmt = config.response_format()

    def send(*, model, messages, max_tokens, temperature, schema_name=None, schema=None):
        kwargs = {}
        if schema is not None and fmt == "json_schema":
            kwargs["response_format"] = {"type": "json_schema",
                                         "json_schema": {"name": schema_name or "out", "strict": True,
                                                         "schema": schema}}
        elif schema is not None and fmt == "json_object":
            kwargs["response_format"] = {"type": "json_object"}
        resp = client.chat.completions.create(model=model, messages=messages, max_tokens=max_tokens,
                                              temperature=temperature, **kwargs)
        choice = resp.choices[0]
        usage = resp.usage
        details = getattr(usage, "completion_tokens_details", None) if usage else None
        return {
            "content": choice.message.content or "",
            "finish_reason": choice.finish_reason,
            "prompt_tokens": (usage.prompt_tokens or 0) if usage else 0,
            "completion_tokens": (usage.completion_tokens or 0) if usage else 0,
            # Not every model fills completion_tokens_details; 0 means "not reported".
            "reasoning_tokens": (getattr(details, "reasoning_tokens", None) or 0) if details else 0,
        }

    return send


MOCK_NOTE = "[MOCK] No model was called (EIG_LLM_PROVIDER=mock)."
_MOCK_TEXT_KEYS = {"summary", "explanation", "observation", "reason", "incident_report_md", "report_md",
                   "voter_impact", "explanation_md"}


def _mock_value(schema: dict, key: str = ""):
    kind = schema.get("type")
    kind = kind[0] if isinstance(kind, list) else kind
    if "enum" in schema:
        return schema["enum"][0]
    if kind == "object":
        return {k: _mock_value(sub, k) for k, sub in schema.get("properties", {}).items()}
    if kind == "array":
        return []
    if kind in ("number", "integer"):
        return schema.get("minimum", 0)
    if kind == "boolean":
        return False
    return MOCK_NOTE if key in _MOCK_TEXT_KEYS else ""


def mock_transport() -> Transport:
    """Offline, free, deterministic. Replies are schema-valid and clearly labeled [MOCK]; they are
    not a judgment. The triage verdict comes from EIG_MOCK_VERDICT (default "uncertain", which the
    gate turns into ESCALATE), so UI flows for every gate branch can be exercised offline."""

    def send(*, model, messages, max_tokens, temperature, schema_name=None, schema=None):
        data = _mock_value(schema or {"type": "object", "properties": {}})
        if schema_name == "tamper_triage":
            v = os.environ.get("EIG_MOCK_VERDICT", "uncertain")
            v = v if v in ("legitimate", "tampering", "uncertain") else "uncertain"
            data.update(verdict=v, confidence=0.95 if v != "uncertain" else 0.5,
                        recommended_action={"tampering": "rollback_to_certified", "legitimate": "human_review",
                                            "uncertain": "escalate_to_human"}[v])
        text = json.dumps(data)
        return {"content": text, "finish_reason": "stop", "prompt_tokens": 0,
                "completion_tokens": 0, "reasoning_tokens": 0}

    return send


def default_transport() -> Transport:
    kind = config.provider()
    if kind == "mock":
        return mock_transport()
    if kind == "vultr":
        return vultr_transport()
    raise LLMError(f"unknown EIG_LLM_PROVIDER={kind!r} (use mock or vultr)")


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")
_THINK = re.compile(r"<think>.*?</think>", re.S)


def parse_json(text: str):
    """Parse a model reply as JSON, tolerating whitespace, code fences and <think> blocks."""
    text = _FENCE.sub("", _THINK.sub("", text).strip()).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(text[start:end + 1])


def _write_usage(rec: CallRecord) -> None:
    if os.environ.get("EIG_USAGE_LOG") == "off":
        return
    row = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), **asdict(rec)}
    path = usage_log_path()
    with _log_lock:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")


def _user_content(user: str, images: Optional[list]):
    if not images:
        return user
    parts = [{"type": "text", "text": user}]
    for img in images:
        parts.append({"type": "image_url",
                      "image_url": {"url": "data:image/png;base64," + base64.b64encode(img).decode()}})
    return parts


class LLMClient:
    def __init__(self, transport: Optional[Transport] = None, routes: Optional[dict] = None,
                 spend: Optional[SpendCap] = None):
        self._transport = transport
        self.routes = routes or config.routes()
        self.spend = spend or SPEND

    @property
    def transport(self) -> Transport:
        if self._transport is None:
            self._transport = default_transport()
        return self._transport

    def complete_json(self, *, task: str, role: str, system: str, user: str,
                      schema: dict, schema_name: str, model: Optional[str] = None,
                      check: Optional[Callable[[dict], Optional[str]]] = None,
                      images: Optional[list] = None) -> LLMResult:
        """One bounded JSON call: at most 1 + JSON_RETRIES requests (plus SDK transport retries).

        `check` adds a task-specific test on top of the schema; a returned message counts as an
        invalid reply (for example, a triage that skipped some of the input items).
        `images` (PNG bytes) are only accepted on the vision role.
        """
        route = self.routes[role]
        model = model or route.model
        if not model:
            raise LLMError(f"{task}: no model configured for role '{role}' "
                           f"({'MODEL_VISION' if role == 'vision' else 'MODEL_REASONING'} is empty)")
        if images and role != "vision":
            raise LLMError(f"{task}: images are only sent on the vision role")
        messages = [{"role": "system", "content": system}, {"role": "user", "content": _user_content(user, images)}]
        calls, last_error = [], "no attempt"
        for attempt in range(1, config.JSON_RETRIES + 2):
            # Worst case for this request: every prompt char a token, plus the full completion cap.
            prompt_chars = sum(len(m["content"]) if isinstance(m["content"], str) else len(user) + 1500
                               for m in messages)
            budget = config.cost_usd(model, prompt_chars, route.max_tokens)
            refusal = self.spend.reserve(budget)
            if refusal:
                raise SpendCapReached(f"{task}: {refusal}", calls)
            started = time.monotonic()
            try:
                out = self.transport(model=model, messages=messages, max_tokens=route.max_tokens,
                                     temperature=route.temperature, schema_name=schema_name, schema=schema)
            except Exception as exc:  # network, auth, 4xx after SDK retries
                self.spend.settle(budget, 0.0)
                rec = CallRecord(task, role, model, 0, 0, 0, int((time.monotonic() - started) * 1000),
                                 0.0, False, attempt, f"{type(exc).__name__}: {str(exc)[:200]}")
                _write_usage(rec)
                calls.append(rec)
                raise LLMError(f"{task}: {rec.error}", calls) from exc
            latency = int((time.monotonic() - started) * 1000)
            cost = round(config.cost_usd(model, out["prompt_tokens"], out["completion_tokens"]), 7)
            self.spend.settle(budget, cost)
            data, error = None, None
            if out["finish_reason"] == "length":
                error = f"reply cut off at max_tokens={route.max_tokens}"
            else:
                try:
                    data = parse_json(out["content"])
                    problems = validate(data, schema)
                    if problems:
                        error = "schema: " + "; ".join(problems[:5])
                    elif check is not None:
                        error = check(data)
                except (json.JSONDecodeError, ValueError) as exc:
                    error = f"invalid JSON: {exc}"
            rec = CallRecord(task, role, model, out["prompt_tokens"], out["completion_tokens"],
                             out["reasoning_tokens"], latency, cost, error is None, attempt, error)
            _write_usage(rec)
            calls.append(rec)
            if error is None:
                return LLMResult(data, calls)
            last_error = error
            messages = messages + [
                {"role": "assistant", "content": out["content"][:2000]},
                {"role": "user", "content": f"That reply was rejected ({error}). "
                                            "Reply again with only one JSON object that matches the schema."},
            ]
        raise LLMError(f"{task}: no valid JSON after {len(calls)} attempts ({last_error})", calls)
