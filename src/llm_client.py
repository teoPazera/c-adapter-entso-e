"""Minimal JSON-only LLM clients for the S4 context adapter."""

from __future__ import annotations

import json
import os
import random
import time
from typing import Any, Protocol

from dotenv import load_dotenv


class JsonClient(Protocol):
    def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return parsed JSON plus non-secret request metadata."""


class OpenAIJsonClient:
    """Rate-limited, retrying LangChain wrapper for strict JSON responses.

    The Lounge endpoint does not expose stable rate-limit headers in the
    observed responses. We therefore track conservative client-side pacing
    and use exponential backoff for transient HTTP 404/429/5xx/connection
    failures. Actual failures and waits are returned in metadata.
    """

    def __init__(
        self,
        model: str = "gpt-5.4-nano-20260317-global",
        base_url: str = "https://genai-lounge-nx-litellm-uat-emea.zurich.com",
        temperature: float = 0.4,
        min_interval_seconds: float = 8.0,
        max_retries: int = 8,
        initial_backoff_seconds: float = 20.0,
        max_backoff_seconds: float = 300.0,
    ) -> None:
        load_dotenv()
        api_key = os.getenv("LLM_API_KEY")
        if not api_key:
            raise RuntimeError("LLM_API_KEY is required for --provider openai")
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("Install langchain-openai before using --provider openai") from exc
        self.model = model
        self.temperature = temperature
        self.min_interval_seconds = min_interval_seconds
        self.max_retries = max_retries
        self.initial_backoff_seconds = initial_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self._last_request_started: float | None = None
        self._llm = ChatOpenAI(model=model, api_key=api_key, base_url=base_url, temperature=temperature)

    def _pace(self) -> float:
        if self._last_request_started is None:
            return 0.0
        elapsed = time.monotonic() - self._last_request_started
        wait = max(0.0, self.min_interval_seconds - elapsed)
        if wait:
            time.sleep(wait)
        return wait

    @staticmethod
    def _retryable(exc: Exception) -> bool:
        text = str(exc).lower()
        return any(token in text for token in (
            "429", "rate limit", "too many requests", "503", "502", "500",
            "connection error", "connecterror", "timeout", "temporarily", "notfounderror", "404",
        ))

    def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        waited = 0.0
        errors: list[str] = []
        for attempt in range(self.max_retries + 1):
            waited += self._pace()
            self._last_request_started = time.monotonic()
            try:
                response = self._llm.bind(
                    response_format={
                        "type": "json_schema",
                        "json_schema": {"name": "availability_effect", "strict": True, "schema": schema},
                    }
                ).invoke([("system", system), ("human", user)])
                content = response.content
                if isinstance(content, list):
                    content = "".join(str(part) for part in content)
                parsed = json.loads(content)
                metadata = dict(response.response_metadata or {})
                metadata["retry"] = {"attempt": attempt + 1, "waited_seconds": round(waited, 3), "prior_errors": errors}
                return parsed, metadata
            except Exception as exc:
                if not self._retryable(exc) or attempt >= self.max_retries:
                    raise RuntimeError(
                        f"LLM request failed after {attempt + 1} attempt(s), waited {waited:.1f}s: {exc}"
                    ) from exc
                errors.append(f"attempt {attempt + 1}: {type(exc).__name__}: {exc}")
                backoff = min(self.max_backoff_seconds, self.initial_backoff_seconds * (2 ** attempt))
                backoff *= random.uniform(0.85, 1.15)
                print(f"  LLM transient failure; waiting {backoff:.1f}s before retry {attempt + 2}/{self.max_retries + 1}", flush=True)
                time.sleep(backoff)
                waited += backoff
        raise AssertionError("unreachable")


class MockJsonClient:
    """Deterministic zero-cost fallback for pipeline wiring tests."""

    def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        del system, user, schema
        return (
            {
                "reasoning": "Mock response: apply a full-day 1,000 MW generation reduction.",
                "p_active": 1.0,
                "start_hour": {"mean": 0.0, "sd": 0.0},
                "end_hour": {"mean": 24.0, "sd": 0.0},
                "size_mw": {"p10": 1000.0, "p50": 1000.0, "p90": 1000.0},
                "direction": "reduce",
            },
            {"provider": "mock", "token_usage": {"prompt_tokens": 0, "completion_tokens": 0}, "retry": {"attempt": 1, "waited_seconds": 0.0, "prior_errors": []}},
        )
