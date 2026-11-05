"""RESEARCH ANALYST — turns Scout signals + News into structured, high-conviction trade ideas. Never executes.

The LLM proposes thesis/direction/levels. Position size is NOT chosen by the LLM:
it is computed deterministically from the risk rules:

    size_pct = min( risk_per_trade_pct / stop_distance_pct × 100 , max_position_pct )

Ideas are then pre-checked against the current risk mode; if Risk & Survival would
veto, the idea is still logged (for the brief) but marked VETOED.
"""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel

from ..config import Settings
from ..data.cache import MarketCache
from ..llm import LLM
from ..models import NewsItem, RiskMode, Signal, TradeIdea, utcnow
from ..notify import Notifier
from ..store import Store
from .risk_survival import RiskSurvival

log = logging.getLogger("research")
BOT = "RESEARCH_ANALYST"


class IdeaDraft(BaseModel):
    symbol: str
    thesis: str
    direction: Literal["long", "short"]
    entry_low: float
    entry_high: float
    stop: float
    targets: list[float]
    horizon: str
    key_risks: list[str]
    conviction: float
    based_on: list[str]


class IdeaBatch(BaseModel):
    ideas: list[IdeaDraft]
    edge_assessment: str


SYSTEM = """You are the Research Analyst on a multi-asset research desk. You turn monitored signals and news into
structured trade ideas for a human portfolio manager who makes every decision. You never execute.

Rules:
- Only propose HIGH-conviction ideas where signal + catalyst + level structure agree. Zero ideas is a valid answer.
- Be honest when edge is weak: say so in edge_assessment and return an empty list.
- Every idea must have a concrete invalidation (stop) level on the correct side of entry, and targets on the other side.
- Prices must be consistent with the latest prices provided. Do not invent data you were not given.
- conviction is 0–1, calibrated: 0.7 = clearly better than a coin flip after costs; >0.85 is rare.
- key_risks: specific, including correlation to existing positions and scheduled events.
- based_on: the record ids of the signals/news you relied on.
- Do NOT propose a position size; it is computed from risk rules downstream."""


def size_from_risk(entry: float, stop: float, risk_pct: float, max_pos_pct: float) -> tuple[float, str]:
    dist = abs(entry - stop) / entry * 100
    if dist <= 0:
        return 0.0, "invalid stop (zero distance)"
    raw = risk_pct / dist * 100
    size = min(raw, max_pos_pct)
    return round(size, 2), (f"risk {risk_pct}% ÷ stop distance {dist:.2f}% = {raw:.2f}% of equity"
                            + (f", capped at {max_pos_pct}%" if raw > max_pos_pct else ""))


def validate_levels(d: IdeaDraft) -> str | None:
    mid = (d.entry_low + d.entry_high) / 2
    if d.entry_low <= 0 or d.entry_high < d.entry_low:
        return "bad entry zone"
    if d.direction == "long" and not (d.stop < d.entry_low and all(t > d.entry_high for t in d.targets)):
        return "long: stop must be below entry and targets above"
    if d.direction == "short" and not (d.stop > d.entry_high and all(t < d.entry_low for t in d.targets)):
        return "short: stop must be above entry and targets below"
    if not d.targets:
        return "no targets"
    if abs(mid - d.stop) / mid > 0.25:
        return "stop more than 25% away — not a defined-risk idea"
    return None


class ResearchAnalyst:
    def __init__(self, settings: Settings, store: Store, llm: LLM, cache: MarketCache,
                 risk: RiskSurvival, notifier: Notifier):
        self.s, self.store, self.llm, self.cache, self.risk, self.n = settings, store, llm, cache, risk, notifier

    def run_once(self) -> list[TradeIdea]:
        since = utcnow() - timedelta(hours=6)
        sigs = self.store.recent("signal", Signal, since=since, limit=60)
        news = self.store.recent("news", NewsItem, since=since - timedelta(hours=18), limit=40)
        if not sigs and not news:
            return []
        if self.risk.mode != RiskMode.NORMAL:
            log.info("risk mode %s — research continues but ideas will be marked vetoed", self.risk.mode.value)
        if not self.llm.available:
            log.info("LLM unavailable — Research Analyst idle (no rule-based idea generation by design)")
            return []
        prices = {k: round(v, 6) for k, v in self.cache.prices().items()}
        positions = self.risk.status()["positions"]
        prompt = (
            f"Now (UTC): {utcnow():%Y-%m-%d %H:%M}\nLatest prices: {prices}\nOpen positions: {positions}\n"
            f"Regime: {self.store.get('scout_regime', 'unknown')} {self.store.get('scout_regime_detail', {})}\n\n"
            "SIGNALS (Market Scout):\n" + "\n".join(
                f"- [{rid}] {s.data_ts:%m-%d %H:%M} {s.kind} {s.symbol}: {s.summary} (src {s.source})" for rid, s in sigs) +
            "\n\nNEWS (News & Narrative):\n" + "\n".join(
                f"- [{rid}] {n.headline} | {n.why_it_matters} | assets {n.assets} | {n.severity} (src {n.source})"
                for rid, n in news) +
            f"\n\nTradeable symbols: {[a.symbol for a in self.s.watchlist]}. "
            f"Return at most {self.s.research.max_ideas_per_run} ideas.")
        batch = self.llm.structured(SYSTEM, prompt, IdeaBatch)
        if batch is None:
            return []
        self.store.set("research_last_edge", batch.edge_assessment)
        out = []
        for d in batch.ideas:
            if d.conviction < self.s.research.min_conviction or self.s.asset(d.symbol) is None:
                continue
            err = validate_levels(d)
            if err:
                log.info("discarded idea %s: %s", d.symbol, err)
                continue
            mid = (d.entry_low + d.entry_high) / 2
            size, calc = size_from_risk(mid, d.stop, self.s.research.risk_per_trade_pct, self.s.risk.max_position_pct)
            idea = TradeIdea(id=f"IDEA-{uuid.uuid4().hex[:6].upper()}", size_pct=size,
                             **d.model_dump())
            idea.risk_verdict = (f"Sizing: {calc}. " +
                                 ("Risk mode NORMAL." if self.risk.mode == RiskMode.NORMAL
                                  else f"VETOED while risk mode is {self.risk.mode.value.upper()}."))
            self.store.add("idea", idea, symbol=idea.symbol, rid=idea.id)
            self.n.send(BOT, f"{idea.id} {idea.direction.upper()} {idea.symbol} entry {idea.entry_low}-{idea.entry_high} "
                             f"stop {idea.stop} tgt {idea.targets} size {idea.size_pct}% conv {idea.conviction:.2f} — "
                             f"{idea.thesis}", "info")
            out.append(idea)
        return out
