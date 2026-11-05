"""Typed configuration loaded from config/settings.yaml."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator


class Asset(BaseModel):
    symbol: str
    asset_class: str
    provider: str
    ref: str
    perp: bool = False


class Schedule(BaseModel):
    risk_survival: int = 15
    market_scout: int = 60
    news_narrative: int = 120
    research_analyst: int = 900
    desk_lead_brief_time: str = "07:30"
    heartbeat_stale_after: int = 120


class ScoutCfg(BaseModel):
    bar_interval: str = "15m"
    lookback_bars: int = 200
    volume_z: float = 3.0
    return_z: float = 3.0
    breakout_lookback: int = 96
    funding_extreme_abs: float = 0.0005
    corr_window: int = 96
    corr_shift: float = 0.5
    regime_vol_mult: float = 2.0
    dedupe_minutes: int = 60


class Feed(BaseModel):
    name: str
    url: str


class NewsCfg(BaseModel):
    rss_feeds: list[Feed] = []
    x_accounts: list[str] = []
    min_relevance: float = 0.5
    shock_keywords: list[str] = []


class ResearchCfg(BaseModel):
    min_conviction: float = 0.7
    risk_per_trade_pct: float = 0.5
    max_ideas_per_run: int = 3


class SurvivalCfg(BaseModel):
    reduce_pct: float = Field(70, ge=70, le=100)
    vol_spike_mult: float = 3.0
    consecutive_losses: int = 3
    news_shock: bool = True
    min_duration_hours: float = 24


class AutoReduce(BaseModel):
    paper: bool = True
    live: bool = False


class RiskCfg(BaseModel):
    max_drawdown_pct: float = 5.0
    max_daily_loss_pct: float = 2.5
    max_position_pct: float = 8.0
    max_asset_class_pct: dict[str, float] = {}
    max_gross_exposure_pct: float = 100.0
    max_avg_correlation: float = 0.8
    correlation_min_gross_pct: float = 20.0
    survival: SurvivalCfg = SurvivalCfg()
    auto_reduce: AutoReduce = AutoReduce()

    @field_validator("max_drawdown_pct", "max_daily_loss_pct", "max_position_pct")
    @classmethod
    def positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("risk limits must be > 0")
        return v


class ExecutionCfg(BaseModel):
    broker: str = "paper"
    live_enabled: bool = False
    ticket_ttl_minutes: int = 60
    max_slippage_pct: float = 0.3
    paper_starting_equity: float = 100_000


class LLMCfg(BaseModel):
    enabled: bool = True
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    monthly_budget_usd: float = 200


class NotifyCfg(BaseModel):
    console: bool = True
    file: str | None = "var/alerts.log"
    telegram: bool = True


class Settings(BaseModel):
    mode: Literal["paper", "live"] = "paper"
    base_currency: str = "USD"
    timezone: str = "UTC"
    db_path: str = "var/desk.sqlite"
    watchlist: list[Asset]
    schedule: Schedule = Schedule()
    scout: ScoutCfg = ScoutCfg()
    news: NewsCfg = NewsCfg()
    research: ResearchCfg = ResearchCfg()
    risk: RiskCfg = RiskCfg()
    execution: ExecutionCfg = ExecutionCfg()
    llm: LLMCfg = LLMCfg()
    notify: NotifyCfg = NotifyCfg()

    def asset(self, symbol: str) -> Asset | None:
        return next((a for a in self.watchlist if a.symbol == symbol), None)

    @property
    def is_live(self) -> bool:
        return self.mode == "live" and self.execution.live_enabled

    @property
    def risk_auto_reduce(self) -> bool:
        return self.risk.auto_reduce.live if self.is_live else self.risk.auto_reduce.paper


def load_settings(path: str | Path = "config/settings.yaml") -> Settings:
    data = yaml.safe_load(Path(path).read_text())
    return Settings.model_validate(data)
