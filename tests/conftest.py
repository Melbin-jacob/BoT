import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deskbot.bots.execution import Execution  # noqa: E402
from deskbot.bots.risk_survival import RiskSurvival  # noqa: E402
from deskbot.brokers.paper import PaperBroker  # noqa: E402
from deskbot.config import load_settings  # noqa: E402
from deskbot.data.cache import MarketCache, Series  # noqa: E402
from deskbot.models import Bar, utcnow  # noqa: E402
from deskbot.notify import Notifier  # noqa: E402
from deskbot.store import Store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def flat_series(price: float, n: int = 200) -> Series:
    now = utcnow()
    bars = [Bar(ts=now - timedelta(minutes=15 * (n - i)), open=price, high=price, low=price, close=price, volume=1000)
            for i in range(n)]
    return Series(bars, "test", None)


class Env:
    def __init__(self, tmp_path, **risk_overrides):
        self.s = load_settings(ROOT / "config/settings.yaml")
        self.s.db_path = str(tmp_path / "t.sqlite")
        self.s.notify.file = None
        self.s.notify.console = False
        self.s.notify.telegram = False
        self.s.timezone = "UTC"
        for k, v in risk_overrides.items():
            setattr(self.s.risk, k, v)
        self.store = Store(self.s.db_path)
        self.n = Notifier(self.s.notify)
        self.cache = MarketCache()
        self.broker = PaperBroker(self.s, self.store)
        self.risk = RiskSurvival(self.s, self.store, self.broker, self.cache, self.n)
        self.exe = Execution(self.s, self.store, self.broker, self.cache, self.risk, self.n)

    def set_price(self, sym: str, price: float):
        self.cache.put(sym, flat_series(price))

    def hold(self, sym: str, qty: float, price: float):
        """Put a position on the paper book directly (bypasses gating — test setup only)."""
        pos = self.store.get("paper_positions", {})
        pos[sym] = {"qty": qty, "avg": price, "last": price}
        self.store.set("paper_positions", pos)
        self.store.set("paper_cash", float(self.store.get("paper_cash")) - qty * price)
        self.set_price(sym, price)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)
