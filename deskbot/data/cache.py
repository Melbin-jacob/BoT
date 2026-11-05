"""In-memory market snapshot shared by Scout (writer) and Risk/Execution (readers)."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

from ..models import Bar, utcnow


@dataclass
class Series:
    bars: list[Bar]
    source: str
    source_url: str | None
    fetched_at: datetime = field(default_factory=utcnow)


class MarketCache:
    def __init__(self):
        self._d: dict[str, Series] = {}
        self._lock = threading.RLock()

    def put(self, symbol: str, s: Series) -> None:
        with self._lock:
            self._d[symbol] = s

    def get(self, symbol: str) -> Series | None:
        return self._d.get(symbol)

    # Persistence so separate processes (core / research / CLI) share one market view.
    def save(self, store, symbol: str) -> None:
        s = self._d[symbol]
        store.set(f"bars:{symbol}", {"source": s.source, "url": s.source_url, "fetched_at": s.fetched_at.isoformat(),
                                     "bars": [b.model_dump(mode="json") for b in s.bars]})

    def hydrate(self, store, symbols: list[str]) -> None:
        for sym in symbols:
            d = store.get(f"bars:{sym}")
            if d:
                self.put(sym, Series([Bar.model_validate(b) for b in d["bars"]], d["source"], d["url"],
                                     datetime.fromisoformat(d["fetched_at"])))

    def last_price(self, symbol: str) -> float | None:
        s = self.get(symbol)
        return s.bars[-1].close if s and s.bars else None

    def prices(self) -> dict[str, float]:
        return {k: v.bars[-1].close for k, v in self._d.items() if v.bars}

    def is_stale(self, symbol: str, max_age: timedelta) -> bool:
        s = self.get(symbol)
        return s is None or utcnow() - s.fetched_at > max_age

    def returns(self, symbol: str, n: int | None = None) -> np.ndarray:
        s = self.get(symbol)
        if not s or len(s.bars) < 3:
            return np.array([])
        c = np.array([b.close for b in s.bars], dtype=float)
        r = np.diff(np.log(c))
        return r[-n:] if n else r

    def vol_ratio(self, symbol: str, short: int = 16, long: int = 192) -> float | None:
        """Short-window realized vol / long-window realized vol. >1 = vol expanding."""
        r = self.returns(symbol)
        if len(r) < max(short * 2, 30):
            return None
        lv = r[-long:].std()
        return float(r[-short:].std() / lv) if lv > 0 else None

    def correlation(self, symbols: list[str], n: int) -> tuple[float | None, dict]:
        """Average pairwise correlation of returns over the last n bars."""
        series = {s: self.returns(s, n) for s in symbols}
        series = {k: v for k, v in series.items() if len(v) >= max(10, n // 2)}
        if len(series) < 2:
            return None, {}
        m = min(len(v) for v in series.values())
        keys = list(series)
        mat = np.corrcoef(np.vstack([series[k][-m:] for k in keys]))
        pairs = {f"{keys[i]}/{keys[j]}": round(float(mat[i, j]), 3)
                 for i in range(len(keys)) for j in range(i + 1, len(keys))}
        return float(np.mean(list(pairs.values()))), pairs
