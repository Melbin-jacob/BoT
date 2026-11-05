"""Broker interface. Live adapters (IBKR, OANDA, Binance, Coinbase…) implement this.

Keys used by live adapters MUST be trade-only, withdrawals disabled.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Portfolio, Ticket


class Broker(ABC):
    name: str
    is_paper: bool

    @abstractmethod
    def portfolio(self, prices: dict[str, float]) -> Portfolio: ...

    @abstractmethod
    def submit(self, ticket: Ticket, price: float) -> tuple[bool, str]:
        """Send an order. Returns (ok, message)."""

    @abstractmethod
    def cancel_all_open(self) -> int: ...
