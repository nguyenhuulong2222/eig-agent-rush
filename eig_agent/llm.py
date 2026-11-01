"""Nebius Token Factory client: OpenAI SDK, role-based routing, JSON validation, usage log.

Keys are read from the environment only (a local .env is loaded into the environment
if present). They are never printed or logged. The usage log records model, tokens,
latency and estimated cost per call, never prompts, replies or keys.
"""

from __future__ import annotations

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
USAGE_LOG = os.environ.get("EIG_USAGE_LOG", os.path.join(ROOT, "eig_agent", "logs", "usage.jsonl"))
_log_lock = threading.Lock()


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
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))


def no_progress(step: str, status: str, detail: str = "") -> None:
    """Default progress callback. The web demo passes one that records each pipeline step."""


class LLMError(RuntimeError):
    """A model call failed or never produced valid JSON. `calls` keeps the usage of every attempt."""

    def __init__(self, message: str, calls: Optional[list] = None):
        super().__init__(message)
        self.calls = calls or []


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


# A transport sends one chat completion and returns a normalized dict. Tests pass a fake.
Transport = Callable[..., dict]


def openai_transport() -> Transport:
    from openai import OpenAI  # imported lazily so offline tests do not need the SDK

    load_env()
    key = os.environ.get("NEBIUS_API_KEY")
    if not key:
        raise LLMError("NEBIUS_API_KEY is not set (put it in .env or the environment)")
    client = OpenAI(base_url=config.BASE_URL, api_key=key,
                    timeout=config.TIMEOUT_S, max_retries=config.TRANSPORT_RETRIES)

    def send(*, model, messages, max_tokens, temperature, thinking, schema_name=None, schema=None):
        kwargs = {}
        if schema is not None:
            kwargs["response_format"] = {"type": "json_schema",
                                         "json_schema": {"name": schema_name or "out", "strict": True,
                                                         "schema": schema}}
        resp = client.chat.completions.create(
            model=model, messages=messages, max_tokens=max_tokens, temperature=temperature,
            extra_body={"chat_template_kwargs": {"enable_thinking": thinking}}, **kwargs)
        choice = resp.choices[0]
        extra = choice.message.model_extra or {}
        details = getattr(resp.usage, "completion_tokens_details", None)
        reasoning = extra.get("reasoning_content") or extra.get("reasoning") or ""
        return {
            "content": choice.message.content or "",
            "finish_reason": choice.finish_reason,
            "prompt_tokens": resp.usage.prompt_tokens or 0,
            "completion_tokens": resp.usage.completion_tokens or 0,
            # Not every model fills completion_tokens_details; 0 means "not reported".
            "reasoning_tokens": (getattr(details, "reasoning_tokens", None) or 0) if details else 0,
            "reasoning_chars": len(reasoning),
        }

    return send


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
    with _log_lock:
        os.makedirs(os.path.dirname(USAGE_LOG), exist_ok=True)
        with open(USAGE_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")


class NemotronClient:
    def __init__(self, transport: Optional[Transport] = None, routes: Optional[dict] = None):
        self._transport = transport
        self.routes = routes or config.routes()

    @property
    def transport(self) -> Transport:
        if self._transport is None:
            self._transport = openai_transport()
        return self._transport

    def complete_json(self, *, task: str, role: str, system: str, user: str,
                      schema: dict, schema_name: str, model: Optional[str] = None,
                      check: Optional[Callable[[dict], Optional[str]]] = None) -> LLMResult:
        """One bounded JSON call: at most 1 + JSON_RETRIES requests (plus SDK transport retries).

        `check` adds a task-specific test on top of the schema; a returned message counts as an
        invalid reply (for example, a triage that skipped some of the input items).
        """
        route = self.routes[role]
        model = model or route.model
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        calls, last_error = [], "no attempt"
        for attempt in range(1, config.JSON_RETRIES + 2):
            started = time.monotonic()
            try:
                out = self.transport(model=model, messages=messages, max_tokens=route.max_tokens,
                                     temperature=route.temperature, thinking=route.thinking,
                                     schema_name=schema_name, schema=schema)
            except Exception as exc:  # network, auth, 4xx after SDK retries
                rec = CallRecord(task, role, model, 0, 0, 0, int((time.monotonic() - started) * 1000),
                                 0.0, False, attempt, f"{type(exc).__name__}: {str(exc)[:200]}")
                _write_usage(rec)
                calls.append(rec)
                raise LLMError(f"{task}: {rec.error}", calls) from exc
            latency = int((time.monotonic() - started) * 1000)
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
                             out["reasoning_tokens"], latency,
                             round(config.cost_usd(model, out["prompt_tokens"], out["completion_tokens"]), 7),
                             error is None, attempt, error)
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
