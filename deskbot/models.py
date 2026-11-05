"""Shared data types passed between bots. Every observation carries source + timestamps."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Bar(BaseModel):
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class Signal(BaseModel):
    """Market Scout output. An observation, never advice."""
    bot: str = "market_scout"
    kind: Literal["volume_spike", "price_spike", "breakout", "breakdown",
                  "funding_extreme", "correlation_shift", "regime_shift", "data_gap"]
    symbol: str
    severity: Literal["info", "warn", "critical"] = "info"
    summary: str
    metrics: dict = {}
    source: str            # e.g. "binance:klines BTCUSDT 15m"
    source_url: str | None = None
    data_ts: datetime      # timestamp of the data point that triggered it
    observed_at: datetime = Field(default_factory=utcnow)


class NewsItem(BaseModel):
    bot: str = "news_narrative"
    headline: str
    what_happened: str
    why_it_matters: str
    assets: list[str] = []
    horizon_hours: int = 48
    relevance: float = 0.5
    severity: Literal["info", "warn", "critical"] = "info"
    source: str
    source_url: str | None = None
    published_at: datetime | None = None
    observed_at: datetime = Field(default_factory=utcnow)


class TradeIdea(BaseModel):
    bot: str = "research_analyst"
    id: str | None = None
    symbol: str
    thesis: str
    direction: Literal["long", "short"]
    entry_low: float
    entry_high: float
    stop: float
    targets: list[float]
    size_pct: float = 0.0       # filled in deterministically from risk rules, not by the LLM
    horizon: str
    key_risks: list[str]
    conviction: float
    based_on: list[str] = []    # signal/news ids
    risk_verdict: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class TicketStatus(str, Enum):
    STAGED = "staged"
    APPROVED = "approved"
    REJECTED = "rejected"
    VETOED = "vetoed"           # blocked by Risk & Survival
    SENT = "sent"
    FILLED = "filled"
    EXPIRED = "expired"
    FAILED = "failed"


class Ticket(BaseModel):
    id: str
    idea_id: str | None = None
    symbol: str
    instrument: str             # exact venue instrument, e.g. "BTCUSDT" / "GC=F"
    side: Literal["buy", "sell"]
    quantity: float
    order_type: Literal["limit", "market", "stop"]
    limit_price: float | None = None
    stop_loss: float | None = None
    take_profit: list[float] = []
    size_pct: float
    sizing_calc: str            # human-readable derivation of quantity
    reduce_only: bool = False   # risk-reducing orders (trims/flattens)
    origin: str = "execution"   # execution | risk_survival
    status: TicketStatus = TicketStatus.STAGED
    note: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Position(BaseModel):
    symbol: str
    quantity: float             # signed: + long, − short
    avg_price: float
    last_price: float
    asset_class: str = "other"

    @property
    def market_value(self) -> float:
        return self.quantity * self.last_price

    @property
    def unrealized_pnl(self) -> float:
        return (self.last_price - self.avg_price) * self.quantity


class Portfolio(BaseModel):
    cash: float
    positions: list[Position] = []
    as_of: datetime = Field(default_factory=utcnow)

    @property
    def equity(self) -> float:
        return self.cash + sum(p.market_value for p in self.positions)

    @property
    def gross_exposure(self) -> float:
        return sum(abs(p.market_value) for p in self.positions)


class RiskMode(str, Enum):
    NORMAL = "normal"
    FROZEN = "frozen"           # daily loss: no new entries
    SURVIVAL = "survival"       # risk cut 70–100%, no new risk
    LOCKDOWN = "lockdown"       # drawdown breach: flat + frozen until manual re-arm


class RiskAction(BaseModel):
    rule: str                   # e.g. "max_drawdown"
    action: Literal["flatten_all", "freeze", "trim", "reduce", "veto", "unfreeze", "enter_survival",
                    "exit_survival", "recommend", "alert"]
    symbol: str | None = None
    detail: str
    metrics: dict = {}
    executed: bool = False      # True if orders were actually sent
    ts: datetime = Field(default_factory=utcnow)
