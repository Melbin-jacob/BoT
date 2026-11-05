"""EXECUTION — prepares exact, ready-to-send tickets. Sends NOTHING without your approval by ticket ID.

Flow:  idea → `prepare(idea_id)` → STAGED ticket (risk-checked) → you: `desk approve <TICKET>`
       → risk re-check at send time → paper/live broker.
Live sending additionally requires settings: mode=live AND execution.live_enabled=true.
Risk & Survival's own reduce-only tickets also go through `approve` when auto_reduce is off.
"""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from ..brokers.base import Broker
from ..config import Settings
from ..data.cache import MarketCache
from ..models import Ticket, TicketStatus, TradeIdea, utcnow
from ..notify import Notifier
from ..store import Store
from .risk_survival import RiskSurvival

log = logging.getLogger("execution")
BOT = "EXECUTION"


class Execution:
    def __init__(self, settings: Settings, store: Store, broker: Broker, cache: MarketCache,
                 risk: RiskSurvival, notifier: Notifier):
        self.s, self.store, self.broker, self.cache, self.risk, self.n = settings, store, broker, cache, risk, notifier

    def prepare(self, idea_id: str) -> Ticket:
        rows = [i for rid, i in self.store.recent("idea", TradeIdea, limit=500) if rid == idea_id]
        if not rows:
            raise ValueError(f"idea {idea_id} not found")
        idea = rows[0]
        a = self.s.asset(idea.symbol)
        equity = self.broker.portfolio(self.cache.prices()).equity
        entry = idea.entry_high if idea.direction == "long" else idea.entry_low   # worst edge of the zone
        notional = equity * idea.size_pct / 100
        qty = notional / entry
        calc = (f"equity {equity:,.2f} × size {idea.size_pct}% = {notional:,.2f} notional ÷ entry {entry} "
                f"= {qty:.6f} units. Loss at stop ≈ {abs(entry - idea.stop) * qty:,.2f} "
                f"({abs(entry - idea.stop) * qty / equity * 100:.2f}% of equity).")
        t = Ticket(id=f"TKT-{uuid.uuid4().hex[:6].upper()}", idea_id=idea.id, symbol=idea.symbol,
                   instrument=a.ref if a else idea.symbol, side="buy" if idea.direction == "long" else "sell",
                   quantity=round(qty, 6), order_type="limit", limit_price=entry, stop_loss=idea.stop,
                   take_profit=idea.targets, size_pct=idea.size_pct, sizing_calc=calc)
        ok, why = self.risk.check_ticket(t)
        t.status = TicketStatus.STAGED if ok else TicketStatus.VETOED
        t.note = why
        self.store.save_ticket(t, actor=BOT, note=why)
        self.n.send(BOT, self.render(t), "info" if ok else "warn")
        return t

    def approve(self, ticket_id: str, actor: str = "owner") -> tuple[bool, str]:
        t = self.store.get_ticket(ticket_id)
        if not t:
            return False, "ticket not found"
        if t.status != TicketStatus.STAGED:
            return False, f"ticket is {t.status.value}, not staged"
        if utcnow() - t.created_at > timedelta(minutes=self.s.execution.ticket_ttl_minutes):
            t.status = TicketStatus.EXPIRED
            self.store.save_ticket(t, actor=BOT, note="expired before approval")
            return False, "ticket expired — prepare a fresh one (prices have moved)"
        if self.s.mode == "live" and not self.s.execution.live_enabled:
            return False, "live mode configured but execution.live_enabled is false — refusing to send"
        ok, why = self.risk.check_ticket(t)          # re-check at send time: state may have changed
        if not ok:
            t.status = TicketStatus.VETOED
            self.store.save_ticket(t, actor="RISK_SURVIVAL", note=why)
            self.n.send(BOT, f"{t.id} VETOED at send time: {why}", "warn")
            return False, why
        price = self.cache.last_price(t.symbol)
        if price is None:
            return False, "no current price — refusing to send"
        if t.order_type == "limit" and t.limit_price:
            dev = abs(price - t.limit_price) / t.limit_price * 100
            if dev > self.s.execution.max_slippage_pct and not t.reduce_only:
                if (t.side == "buy" and price > t.limit_price) or (t.side == "sell" and price < t.limit_price):
                    return False, f"market {price} is {dev:.2f}% through limit {t.limit_price}; not chasing"
        t.status = TicketStatus.APPROVED
        self.store.save_ticket(t, actor=actor, note="approved by owner")
        if t.order_type == "market" or t.reduce_only or not t.limit_price:
            ref = price
        else:  # limit: never fill worse than the limit (paper approximation; live brokers rest the order)
            ref = min(price, t.limit_price) if t.side == "buy" else max(price, t.limit_price)
        ok, msg = self.broker.submit(t, ref)
        t.status = TicketStatus.FILLED if ok else TicketStatus.FAILED
        self.store.save_ticket(t, actor=BOT, note=msg)
        self.n.send(BOT, f"{t.id}: {msg}", "info" if ok else "warn")
        return ok, msg

    def reject(self, ticket_id: str, reason: str = "") -> str:
        t = self.store.get_ticket(ticket_id)
        if not t or t.status != TicketStatus.STAGED:
            return "no staged ticket with that id"
        t.status = TicketStatus.REJECTED
        self.store.save_ticket(t, actor="owner", note=reason or "rejected by owner")
        return f"{t.id} rejected"

    def expire_old(self) -> None:
        cutoff = utcnow() - timedelta(minutes=self.s.execution.ticket_ttl_minutes)
        for t in self.store.tickets(TicketStatus.STAGED):
            if t.created_at < cutoff and t.origin != "risk_survival":
                t.status = TicketStatus.EXPIRED
                self.store.save_ticket(t, actor=BOT, note="TTL expired")

    @staticmethod
    def render(t: Ticket) -> str:
        return (f"TICKET {t.id} [{t.status.value.upper()}] {t.side.upper()} {t.quantity:g} {t.instrument} ({t.symbol})\n"
                f"  type {t.order_type} | entry {t.limit_price} | stop {t.stop_loss} | TP {t.take_profit}\n"
                f"  size {t.size_pct}% — {t.sizing_calc}\n  risk check: {t.note}\n"
                + (f"  → approve with: desk approve {t.id}" if t.status == TicketStatus.STAGED else ""))
