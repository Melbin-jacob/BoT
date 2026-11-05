"""Thin Claude wrapper used by the research-side bots (never by Risk & Survival).

- Structured output: responses are validated against Pydantic models.
- Refusal fallback: server-side `fallbacks: "default"` is enabled so a policy decline
  is retried on a fallback model inside the same call.
- Budget: a soft monthly USD cap; when reached, LLM calls return None and bots
  fall back to rule-based output.
- If no credentials are configured, `available` is False and bots degrade gracefully.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import TypeVar

from pydantic import BaseModel

from .config import LLMCfg
from .store import Store

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

# USD per 1M tokens (input, output). Update if you change model.
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}


class LLM:
    def __init__(self, cfg: LLMCfg, store: Store):
        self.cfg, self.store = cfg, store
        self.client = None
        if cfg.enabled and (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
            try:
                import anthropic
                self.client = anthropic.Anthropic()
            except Exception as e:  # pragma: no cover
                log.warning("Anthropic client unavailable: %s", e)

    @property
    def available(self) -> bool:
        return self.client is not None and self._spent_this_month() < self.cfg.monthly_budget_usd

    def _month_key(self) -> str:
        return "llm_spend_" + datetime.now(timezone.utc).strftime("%Y-%m")

    def _spent_this_month(self) -> float:
        return float(self.store.get(self._month_key(), 0.0))

    def _charge(self, usage) -> None:
        pin, pout = PRICES.get(self.cfg.model, (4.0, 20.0))
        cost = (usage.input_tokens * pin + usage.output_tokens * pout) / 1_000_000
        self.store.set(self._month_key(), self._spent_this_month() + cost)

    def structured(self, system: str, prompt: str, schema: type[T], max_tokens: int = 16000) -> T | None:
        if not self.available:
            return None
        import anthropic
        try:
            resp = self.client.messages.parse(
                model=self.cfg.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": self.cfg.effort},
                output_format=schema,
                extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
                extra_body={"fallbacks": "default"},
            )
        except anthropic.RateLimitError as e:
            log.warning("LLM rate limited: %s", e)
            return None
        except anthropic.APIStatusError as e:
            log.error("LLM API error %s: %s", e.status_code, e.message)
            return None
        except anthropic.APIConnectionError as e:
            log.error("LLM connection error: %s", e)
            return None
        self._charge(resp.usage)
        if resp.stop_reason == "refusal":
            log.warning("LLM refused request (%s)", getattr(resp, "stop_details", None))
            return None
        if resp.stop_reason == "max_tokens":
            log.warning("LLM output truncated at max_tokens")
            return None
        return resp.parsed_output
