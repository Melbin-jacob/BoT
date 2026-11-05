"""RISK & SURVIVAL — capital preservation. Absolute veto. Deterministic code, no LLM.

It does not need a thesis. It does not argue. It fires the rules and logs the action.
It may only flatten, trim, cancel, or block — it can never open or increase risk.

Modes (most severe wins):
  LOCKDOWN  drawdown from high-water mark > max_drawdown_pct → flatten all + freeze.
            Cleared ONLY by `desk rearm` (you).
  SURVIVAL  vol spike / news shock / consecutive losses → cut gross exposure 70–100%,
            no new risk. Cleared ONLY by you, and not before min_duration_hours.
  FROZEN    daily loss > max_daily_loss_pct → no new entries until the next local day
            or until you run `desk override-freeze`.
  NORMAL

Always-on rules (any mode): single position cap, asset-class concentration cap,
gross exposure cap, correlation cap.

Execution of reductions:
  risk.auto_reduce.{paper,live} = true  → reduce-only orders are sent immediately.
                                  false → reduce-only tickets are staged as
                                          recommendations + critical alert; new risk
                                          stays blocked regardless.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..brokers.base import Broker
from ..config import Settings
from ..data.cache import MarketCache
from ..models import Portfolio, Position, RiskAction, RiskMode, Ticket, TicketStatus, utcnow
from ..notify import Notifier
from ..store import Store

log = logging.getLogger("risk_survival")
BOT = "RISK_SURVIVAL"
SEVERITY = {RiskMode.NORMAL: 0, RiskMode.FROZEN: 1, RiskMode.SURVIVAL: 2, RiskMode.LOCKDOWN: 3}
CORR_REDUCE_PCT = 25.0
CORR_COOLDOWN = timedelta(hours=4)


@dataclass
class RiskState:
    hwm: float = 0.0
    day: str = ""
    day_start_equity: float = 0.0
    frozen: bool = False
    freeze_override_day: str = ""
    survival: bool = False
    survival_since: str | None = None
    survival_reason: str = ""
    survival_target_gross: float | None = None
    lockdown: bool = False
    lockdown_since: str | None = None
    last_corr_reduce: str | None = None
    news_shock_ack: list[str] = field(default_factory=list)

    @property
    def mode(self) -> RiskMode:
        if self.lockdown:
            return RiskMode.LOCKDOWN
        if self.survival:
            return RiskMode.SURVIVAL
        if self.frozen:
            return RiskMode.FROZEN
        return RiskMode.NORMAL


@dataclass
class Context:
    """Everything the rules need besides the portfolio. Built from the cache/store, or by tests."""
    now: datetime
    vol_ratios: dict[str, float] = field(default_factory=dict)       # symbol → short/long vol
    news_shocks: list[tuple[str, str]] = field(default_factory=list)  # (record id, headline)
    recent_closed_pnls: list[float] = field(default_factory=list)     # newest first
    avg_corr: float | None = None
    corr_pairs: dict = field(default_factory=dict)
    stale_symbols: list[str] = field(default_factory=list)


class RiskSurvival:
    def __init__(self, settings: Settings, store: Store, broker: Broker, cache: MarketCache, notifier: Notifier):
        self.s, self.store, self.broker, self.cache, self.n = settings, store, broker, cache, notifier
        self.cfg = settings.risk
        self.tz = ZoneInfo(settings.timezone)

    # ── state ──────────────────────────────────────────────────────────────
    def state(self) -> RiskState:
        return RiskState(**self.store.get("risk_state", {}))

    def _save(self, st: RiskState) -> None:
        self.store.set("risk_state", st.__dict__)

    @property
    def mode(self) -> RiskMode:
        return self.state().mode

    # ── main loop entry ────────────────────────────────────────────────────
    def run_once(self) -> list[RiskAction]:
        prices = self.cache.prices()
        pf = self.broker.portfolio(prices)
        ctx = self.build_context(pf)
        actions = self.evaluate(pf, ctx)
        self.store.set("risk_heartbeat", utcnow().isoformat())
        return actions

    def build_context(self, pf: Portfolio) -> Context:
        now = utcnow()
        held = [p.symbol for p in pf.positions]
        vr = {}
        for sym in set(held) | {"SPX"}:
            v = self.cache.vol_ratio(sym)
            if v is not None:
                vr[sym] = v
        from ..models import NewsItem
        shocks = [(rid, n.headline) for rid, n in self.store.recent("news", NewsItem, since=now - timedelta(hours=6))
                  if n.severity == "critical"]
        avg_corr, pairs = self.cache.correlation(held, self.s.scout.corr_window) if len(held) >= 2 else (None, {})
        stale = [s for s in held if self.cache.is_stale(s, timedelta(minutes=10))]
        return Context(now=now, vol_ratios=vr, news_shocks=shocks,
                       recent_closed_pnls=self.store.recent_closed_pnls(self.cfg.survival.consecutive_losses),
                       avg_corr=avg_corr, corr_pairs=pairs, stale_symbols=stale)

    def evaluate(self, pf: Portfolio, ctx: Context) -> list[RiskAction]:
        """Apply every rule in priority order. Returns the actions taken/recommended."""
        st = self.state()
        acts: list[RiskAction] = []
        eq = pf.equity
        self.store.record_equity(eq, ctx.now)

        # Bookkeeping: high-water mark and trading day
        if st.hwm <= 0:
            st.hwm = eq
        st.hwm = max(st.hwm, eq) if not st.lockdown else st.hwm
        today = ctx.now.astimezone(self.tz).date().isoformat()
        if st.day != today:
            st.day, st.day_start_equity = today, eq
            if st.frozen:
                st.frozen = False
                acts.append(RiskAction(rule="max_daily_loss", action="unfreeze",
                                       detail=f"New trading day {today}: daily-loss freeze lifted."))

        # Blind spot: held positions without fresh prices
        if ctx.stale_symbols:
            acts.append(RiskAction(rule="data_integrity", action="alert",
                                   detail=f"No fresh price for held {ctx.stale_symbols} (>10 min). Risk is partially blind."))

        # 1. Drawdown from peak → LOCKDOWN
        dd = (st.hwm - eq) / st.hwm * 100 if st.hwm > 0 else 0.0
        if dd > self.cfg.max_drawdown_pct and not st.lockdown:
            st.lockdown, st.lockdown_since = True, ctx.now.isoformat()
            acts.append(RiskAction(rule="max_drawdown", action="flatten_all",
                                   detail=f"Drawdown {dd:.2f}% from peak {st.hwm:,.2f} > {self.cfg.max_drawdown_pct}%. "
                                          f"FLATTEN ALL + FREEZE. Manual re-arm required.",
                                   metrics={"equity": eq, "hwm": st.hwm, "drawdown_pct": round(dd, 3)}))
        if st.lockdown:
            self._save(st)
            acts += self._reduce(pf, {p.symbol: 1.0 for p in pf.positions}, "max_drawdown")
            return self._finish(acts)

        # 2. Daily loss → FROZEN
        dl = (st.day_start_equity - eq) / st.day_start_equity * 100 if st.day_start_equity > 0 else 0.0
        if dl > self.cfg.max_daily_loss_pct and not st.frozen and st.freeze_override_day != today:
            st.frozen = True
            acts.append(RiskAction(rule="max_daily_loss", action="freeze",
                                   detail=f"Daily loss {dl:.2f}% > {self.cfg.max_daily_loss_pct}%. "
                                          f"New entries FROZEN until next day ({self.s.timezone}) or your override.",
                                   metrics={"equity": eq, "day_start": st.day_start_equity, "daily_loss_pct": round(dl, 3)}))

        # 3. Survival Mode triggers
        sv = self.cfg.survival
        reasons = []
        spikes = {k: round(v, 2) for k, v in ctx.vol_ratios.items() if v >= sv.vol_spike_mult}
        if spikes:
            reasons.append(f"volatility spike {spikes} (≥{sv.vol_spike_mult}× normal)")
        new_shocks = [(rid, h) for rid, h in ctx.news_shocks if rid not in st.news_shock_ack]
        if sv.news_shock and new_shocks:
            reasons.append(f"news shock: {new_shocks[0][1]!r}")
            st.news_shock_ack = (st.news_shock_ack + [r for r, _ in new_shocks])[-50:]
        pnls = ctx.recent_closed_pnls[: sv.consecutive_losses]
        if len(pnls) >= sv.consecutive_losses and all(p < 0 for p in pnls):
            last_seen = self.store.get("risk_loss_streak_seen")
            sig = ",".join(f"{p:.4f}" for p in pnls)
            if last_seen != sig:
                reasons.append(f"{sv.consecutive_losses} consecutive losing trades")
                self.store.set("risk_loss_streak_seen", sig)
        if reasons and not st.survival:
            st.survival, st.survival_since = True, ctx.now.isoformat()
            st.survival_reason = "; ".join(reasons)
            st.survival_target_gross = pf.gross_exposure * (1 - sv.reduce_pct / 100)
            acts.append(RiskAction(rule="survival_mode", action="enter_survival",
                                   detail=f"SURVIVAL MODE: {st.survival_reason}. Cutting gross exposure {sv.reduce_pct:.0f}% "
                                          f"to ≤{st.survival_target_gross:,.2f}. No new risk.",
                                   metrics={"gross_before": pf.gross_exposure}))
        elif reasons:
            acts.append(RiskAction(rule="survival_mode", action="alert",
                                   detail=f"Additional survival trigger while active: {'; '.join(reasons)}"))
        if st.survival and st.survival_target_gross is not None and pf.gross_exposure > st.survival_target_gross * 1.001:
            f = 1 - st.survival_target_gross / pf.gross_exposure
            acts += self._reduce(pf, {p.symbol: f for p in pf.positions}, "survival_mode")

        # 4. Always-on structural limits (computed on the current book)
        acts += self._structural(pf, st, ctx)
        self._save(st)
        return self._finish(acts)

    # ── structural limits ──────────────────────────────────────────────────
    def _structural(self, pf: Portfolio, st: RiskState, ctx: Context) -> list[RiskAction]:
        acts: list[RiskAction] = []
        eq = pf.equity
        if eq <= 0:
            return [RiskAction(rule="equity", action="flatten_all", detail="Equity ≤ 0.")] + \
                self._reduce(pf, {p.symbol: 1.0 for p in pf.positions}, "equity")
        cut: dict[str, float] = {}

        # Single position cap
        for p in pf.positions:
            pct = abs(p.market_value) / eq * 100
            if pct > self.cfg.max_position_pct + 1e-9:
                cut[p.symbol] = max(cut.get(p.symbol, 0), 1 - self.cfg.max_position_pct / pct)
                acts.append(RiskAction(rule="max_position", action="trim", symbol=p.symbol,
                                       detail=f"{p.symbol} is {pct:.2f}% of equity > {self.cfg.max_position_pct}%. Trim to cap.",
                                       metrics={"position_pct": round(pct, 3)}))

        # Asset-class concentration
        by_class: dict[str, list[Position]] = {}
        for p in pf.positions:
            by_class.setdefault(p.asset_class, []).append(p)
        for cls, ps in by_class.items():
            cap = self.cfg.max_asset_class_pct.get(cls)
            if cap is None:
                continue
            pct = sum(abs(p.market_value) for p in ps) / eq * 100
            if pct > cap + 1e-9:
                f = 1 - cap / pct
                for p in ps:
                    cut[p.symbol] = max(cut.get(p.symbol, 0), f)
                acts.append(RiskAction(rule="concentration", action="reduce",
                                       detail=f"{cls} exposure {pct:.2f}% > cap {cap}%. Reduce class pro-rata.",
                                       metrics={"class_pct": round(pct, 3)}))

        # Gross exposure
        gross_pct = pf.gross_exposure / eq * 100
        if gross_pct > self.cfg.max_gross_exposure_pct + 1e-9:
            f = 1 - self.cfg.max_gross_exposure_pct / gross_pct
            for p in pf.positions:
                cut[p.symbol] = max(cut.get(p.symbol, 0), f)
            acts.append(RiskAction(rule="gross_exposure", action="reduce",
                                   detail=f"Gross exposure {gross_pct:.1f}% > {self.cfg.max_gross_exposure_pct}%."))

        # Correlation
        if (ctx.avg_corr is not None and ctx.avg_corr > self.cfg.max_avg_correlation
                and gross_pct > self.cfg.correlation_min_gross_pct):
            last = datetime.fromisoformat(st.last_corr_reduce) if st.last_corr_reduce else None
            if not last or ctx.now - last > CORR_COOLDOWN:
                st.last_corr_reduce = ctx.now.isoformat()
                for p in pf.positions:
                    cut[p.symbol] = max(cut.get(p.symbol, 0), CORR_REDUCE_PCT / 100)
                acts.append(RiskAction(rule="correlation", action="reduce",
                                       detail=f"Avg pairwise correlation {ctx.avg_corr:.2f} > {self.cfg.max_avg_correlation} "
                                              f"with gross {gross_pct:.1f}%. Reduce all by {CORR_REDUCE_PCT:.0f}%.",
                                       metrics={"pairs": ctx.corr_pairs}))

        if cut:
            acts += self._reduce(pf, cut, "structural_limits")
        return acts

    # ── order generation (reduce-only) ─────────────────────────────────────
    def _reduce(self, pf: Portfolio, fractions: dict[str, float], rule: str) -> list[RiskAction]:
        acts = []
        auto = self.s.risk_auto_reduce
        if fractions and any(f >= 1.0 for f in fractions.values()) and auto:
            self.broker.cancel_all_open()
        for p in pf.positions:
            f = min(1.0, fractions.get(p.symbol, 0.0))
            if f <= 0 or p.quantity == 0:
                continue
            qty = abs(p.quantity) if f >= 1.0 else abs(p.quantity) * f
            t = Ticket(id=f"RSK-{uuid.uuid4().hex[:6].upper()}", symbol=p.symbol,
                       instrument=(self.s.asset(p.symbol).ref if self.s.asset(p.symbol) else p.symbol),
                       side="sell" if p.quantity > 0 else "buy", quantity=round(qty, 8), order_type="market",
                       size_pct=0.0, sizing_calc=f"reduce {f*100:.1f}% of {p.quantity:g} ({rule})",
                       reduce_only=True, origin="risk_survival", note=rule)
            if auto:
                ok, msg = self.broker.submit(t, p.last_price)
                t.status = TicketStatus.FILLED if ok else TicketStatus.FAILED
                self.store.save_ticket(t, actor=BOT, note=msg)
                acts.append(RiskAction(rule=rule, action="flatten_all" if f >= 1 else "trim", symbol=p.symbol,
                                       detail=f"{'EXECUTED' if ok else 'FAILED'}: {t.side} {qty:g} {p.symbol} — {msg}",
                                       executed=ok))
            else:
                if any(x.origin == "risk_survival" and x.symbol == p.symbol and x.note == rule
                       for x in self.store.tickets(TicketStatus.STAGED)):
                    continue  # recommendation already outstanding
                t.note = rule
                self.store.save_ticket(t, actor=BOT, note=f"RISK RECOMMENDATION ({rule}) — approve with: desk approve {t.id}")
                acts.append(RiskAction(rule=rule, action="recommend", symbol=p.symbol,
                                       detail=f"RECOMMEND {t.side} {qty:g} {p.symbol} (ticket {t.id}). "
                                              f"New risk is blocked meanwhile."))
        return acts

    def _finish(self, acts: list[RiskAction]) -> list[RiskAction]:
        for a in acts:
            self.store.add("risk_action", a, symbol=a.symbol)
            level = "critical" if a.action in ("flatten_all", "enter_survival", "freeze", "recommend") else \
                    "warn" if a.action in ("trim", "reduce", "alert") else "info"
            self.n.send(BOT, f"[{a.rule}] {a.detail}", level)
        return acts

    # ── gate for new risk (called by Execution before staging AND before sending) ──
    def check_ticket(self, t: Ticket) -> tuple[bool, str]:
        if t.reduce_only:
            return True, "reduce-only: always allowed"
        st = self.state()
        if st.mode != RiskMode.NORMAL:
            return False, f"VETO: risk mode is {st.mode.value.upper()} — no new risk."
        prices = self.cache.prices()
        pf = self.broker.portfolio(prices)
        eq = pf.equity
        price = t.limit_price or prices.get(t.symbol)
        if not price or eq <= 0:
            return False, "VETO: no reliable price/equity to evaluate the order."
        signed = t.quantity if t.side == "buy" else -t.quantity
        cur = next((p for p in pf.positions if p.symbol == t.symbol), None)
        new_qty = (cur.quantity if cur else 0) + signed
        if cur and abs(new_qty) < abs(cur.quantity) and (new_qty == 0 or (new_qty > 0) == (cur.quantity > 0)):
            return True, "reduces existing position"
        new_val = abs(new_qty) * price
        if new_val / eq * 100 > self.cfg.max_position_pct + 1e-9:
            return False, f"VETO: post-trade {t.symbol} would be {new_val/eq*100:.2f}% > {self.cfg.max_position_pct}% cap."
        a = self.s.asset(t.symbol)
        cls = a.asset_class if a else "other"
        cap = self.cfg.max_asset_class_pct.get(cls)
        cls_val = sum(abs(p.market_value) for p in pf.positions if p.asset_class == cls and p.symbol != t.symbol) + new_val
        if cap is not None and cls_val / eq * 100 > cap + 1e-9:
            return False, f"VETO: {cls} exposure would be {cls_val/eq*100:.2f}% > {cap}% cap."
        gross = sum(abs(p.market_value) for p in pf.positions if p.symbol != t.symbol) + new_val
        if gross / eq * 100 > self.cfg.max_gross_exposure_pct + 1e-9:
            return False, f"VETO: gross exposure would be {gross/eq*100:.1f}% > {self.cfg.max_gross_exposure_pct}%."
        if t.stop_loss is None:
            return False, "VETO: every new position needs a stop / invalidation level."
        return True, "within limits"

    # ── manual controls (you only) ─────────────────────────────────────────
    def override_freeze(self, reason: str) -> str:
        st = self.state()
        if st.lockdown or st.survival:
            return "Refused: freeze override does not apply to LOCKDOWN or SURVIVAL."
        st.frozen = False
        st.freeze_override_day = utcnow().astimezone(self.tz).date().isoformat()
        self._save(st)
        self._finish([RiskAction(rule="max_daily_loss", action="unfreeze", detail=f"Manual override by owner: {reason}")])
        return "Daily-loss freeze overridden for today."

    def clear_survival(self, reason: str) -> str:
        st = self.state()
        if not st.survival:
            return "Survival Mode is not active."
        since = datetime.fromisoformat(st.survival_since)
        left = since + timedelta(hours=self.cfg.survival.min_duration_hours) - utcnow()
        if left.total_seconds() > 0:
            return f"Refused: Survival Mode must stay active for another {left}."
        st.survival, st.survival_since, st.survival_target_gross, st.survival_reason = False, None, None, ""
        self._save(st)
        self._finish([RiskAction(rule="survival_mode", action="exit_survival", detail=f"Cleared by owner: {reason}")])
        return "Survival Mode cleared."

    def rearm(self, reason: str) -> str:
        st = self.state()
        if not st.lockdown:
            return "Not in LOCKDOWN."
        pf = self.broker.portfolio(self.cache.prices())
        st.lockdown, st.lockdown_since = False, None
        st.hwm = pf.equity            # new peak = current equity after re-arm
        st.day_start_equity = pf.equity
        self._save(st)
        self._finish([RiskAction(rule="max_drawdown", action="unfreeze",
                                 detail=f"Re-armed by owner: {reason}. High-water mark reset to {pf.equity:,.2f}.")])
        return "Re-armed. High-water mark reset to current equity."

    def status(self) -> dict:
        st = self.state()
        pf = self.broker.portfolio(self.cache.prices())
        eq = pf.equity
        return {
            "mode": st.mode.value, "equity": round(eq, 2), "high_water_mark": round(st.hwm, 2),
            "drawdown_pct": round((st.hwm - eq) / st.hwm * 100, 3) if st.hwm else 0,
            "daily_pnl_pct": round((eq - st.day_start_equity) / st.day_start_equity * 100, 3) if st.day_start_equity else 0,
            "gross_exposure_pct": round(pf.gross_exposure / eq * 100, 2) if eq else 0,
            "survival_reason": st.survival_reason or None,
            "limits": {"max_drawdown_pct": self.cfg.max_drawdown_pct, "max_daily_loss_pct": self.cfg.max_daily_loss_pct,
                       "max_position_pct": self.cfg.max_position_pct},
            "positions": [{"symbol": p.symbol, "qty": p.quantity, "value": round(p.market_value, 2),
                           "pct_equity": round(abs(p.market_value) / eq * 100, 2) if eq else 0,
                           "unrealized": round(p.unrealized_pnl, 2)} for p in pf.positions],
        }
