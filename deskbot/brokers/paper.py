"""Paper broker: simulated fills at the last price ± slippage, state persisted in the store."""
from __future__ import annotations

from ..config import Settings
from ..models import Portfolio, Position, Ticket
from ..store import Store
from .base import Broker


class PaperBroker(Broker):
    name = "paper"
    is_paper = True

    def __init__(self, settings: Settings, store: Store):
        self.s, self.store = settings, store
        if self.store.get("paper_cash") is None:
            self.store.set("paper_cash", settings.execution.paper_starting_equity)
            self.store.set("paper_positions", {})

    def _load(self) -> tuple[float, dict]:
        return float(self.store.get("paper_cash")), dict(self.store.get("paper_positions", {}))

    def portfolio(self, prices: dict[str, float]) -> Portfolio:
        cash, pos = self._load()
        out = []
        for sym, p in pos.items():
            a = self.s.asset(sym)
            out.append(Position(symbol=sym, quantity=p["qty"], avg_price=p["avg"],
                                last_price=prices.get(sym, p.get("last", p["avg"])),
                                asset_class=a.asset_class if a else "other"))
        return Portfolio(cash=cash, positions=out)

    def submit(self, ticket: Ticket, price: float) -> tuple[bool, str]:
        cash, pos = self._load()
        slip = self.s.execution.max_slippage_pct / 100 / 3  # assume a third of max slippage
        fill = price * (1 + slip) if ticket.side == "buy" else price * (1 - slip)
        signed = ticket.quantity if ticket.side == "buy" else -ticket.quantity
        cur = pos.get(ticket.symbol, {"qty": 0.0, "avg": fill})
        q0, avg0 = cur["qty"], cur["avg"]

        if ticket.reduce_only:
            if q0 == 0 or (q0 > 0) == (signed > 0):
                return False, "reduce_only order would not reduce the position"
            signed = max(-abs(q0), min(abs(q0), signed))  # clamp: never flip

        q1 = q0 + signed
        realized = 0.0
        if q0 != 0 and (q0 > 0) != (signed > 0):          # reducing / closing
            closed = min(abs(signed), abs(q0))
            realized = closed * (fill - avg0) * (1 if q0 > 0 else -1)
        if q1 == 0:
            pos.pop(ticket.symbol, None)
        else:
            if q0 == 0 or (q0 > 0) == (signed > 0):       # opening / adding
                avg1 = (q0 * avg0 + signed * fill) / q1
            elif (q0 > 0) == (q1 > 0):                    # partial reduce
                avg1 = avg0
            else:                                         # flipped through zero
                avg1 = fill
            pos[ticket.symbol] = {"qty": q1, "avg": avg1, "last": fill}
        cash -= signed * fill
        self.store.set("paper_cash", cash)
        self.store.set("paper_positions", pos)
        if realized:
            self.store.record_closed_trade(ticket.symbol, realized)
        return True, f"paper fill {ticket.side} {abs(signed):g} {ticket.symbol} @ {fill:.4f} (realized {realized:+.2f})"

    def cancel_all_open(self) -> int:
        return 0  # paper fills are immediate; nothing rests
