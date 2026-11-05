"""Market data adapters. Each returns bars with exact timestamps + a citable source string.

- BinanceProvider: public spot klines + USDT-M perp funding (no API key needed).
- YahooProvider:   futures / FX / indices via Yahoo's chart endpoint (no key; delayed for some symbols).
- SyntheticProvider: deterministic random-walk data for tests and offline dry runs.

Swap in paid feeds (Polygon, Databento, Kaiko, broker feeds) by implementing `MarketData`.
"""
from __future__ import annotations

import math
import random
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone

import httpx

from ..config import Asset
from ..models import Bar

_INTERVAL_MIN = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}


class MarketData(ABC):
    name: str

    @abstractmethod
    def bars(self, asset: Asset, interval: str, limit: int) -> tuple[list[Bar], str, str | None]:
        """Return (bars oldest→newest, source description, source url)."""

    def funding_rate(self, asset: Asset) -> tuple[float, datetime, str, str | None] | None:
        return None


class BinanceProvider(MarketData):
    name = "binance"
    SPOT = "https://api.binance.com/api/v3/klines"
    FUND = "https://fapi.binance.com/fapi/v1/premiumIndex"

    def __init__(self, client: httpx.Client | None = None):
        self.http = client or httpx.Client(timeout=10)

    def bars(self, asset, interval, limit):
        params = {"symbol": asset.ref, "interval": interval, "limit": limit}
        r = self.http.get(self.SPOT, params=params)
        r.raise_for_status()
        bars = [Bar(ts=datetime.fromtimestamp(k[0] / 1000, timezone.utc), open=float(k[1]), high=float(k[2]),
                    low=float(k[3]), close=float(k[4]), volume=float(k[5])) for k in r.json()]
        return bars, f"binance:spot_klines {asset.ref} {interval}", str(r.url)

    def funding_rate(self, asset):
        if not asset.perp:
            return None
        r = self.http.get(self.FUND, params={"symbol": asset.ref})
        r.raise_for_status()
        d = r.json()
        return (float(d["lastFundingRate"]), datetime.fromtimestamp(d["time"] / 1000, timezone.utc),
                f"binance:usdm_premiumIndex {asset.ref}", str(r.url))


class YahooProvider(MarketData):
    name = "yahoo"
    URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ref}"

    def __init__(self, client: httpx.Client | None = None):
        self.http = client or httpx.Client(timeout=10, headers={"User-Agent": "trading-desk/0.1"})

    def bars(self, asset, interval, limit):
        minutes = _INTERVAL_MIN[interval]
        rng = "60d" if minutes < 1440 else "2y"
        r = self.http.get(self.URL.format(ref=asset.ref), params={"interval": interval, "range": rng})
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        bars = []
        for i, t in enumerate(res.get("timestamp") or []):
            c = q["close"][i]
            if c is None:
                continue
            bars.append(Bar(ts=datetime.fromtimestamp(t, timezone.utc), open=q["open"][i] or c,
                            high=q["high"][i] or c, low=q["low"][i] or c, close=c, volume=q["volume"][i] or 0.0))
        return bars[-limit:], f"yahoo:chart {asset.ref} {interval} (may be delayed)", str(r.url)


class SyntheticProvider(MarketData):
    """Deterministic fake data. `shocks` lets tests inject a spike on the last bar."""
    name = "synthetic"

    def __init__(self, seed: int = 7, shocks: dict[str, dict] | None = None):
        self.seed = seed
        self.shocks = shocks or {}

    def bars(self, asset, interval, limit):
        rnd = random.Random(f"{self.seed}-{asset.symbol}")
        step = timedelta(minutes=_INTERVAL_MIN.get(interval, 15))
        end = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
        price, out = 100.0 + rnd.random() * 50, []
        for i in range(limit):
            ret = rnd.gauss(0, 0.004)
            o, price = price, price * math.exp(ret)
            vol = abs(rnd.gauss(1000, 150))
            out.append(Bar(ts=end - step * (limit - 1 - i), open=o, high=max(o, price) * 1.001,
                           low=min(o, price) * 0.999, close=price, volume=vol))
        shock = self.shocks.get(asset.symbol)
        if shock:
            last = out[-1]
            close = last.open * (1 + shock.get("ret", 0))
            out[-1] = Bar(ts=last.ts, open=last.open, high=max(last.open, close) * 1.001,
                          low=min(last.open, close) * 0.999, close=close, volume=last.volume * shock.get("vol_mult", 1))
        return out, f"synthetic:{asset.symbol} {interval}", None

    def funding_rate(self, asset):
        if not asset.perp:
            return None
        rate = self.shocks.get(asset.symbol, {}).get("funding", 0.0001)
        return rate, datetime(2026, 11, 5, 8, tzinfo=timezone.utc), f"synthetic:funding {asset.symbol}", None


def build_providers(use_synthetic: bool = False) -> dict[str, MarketData]:
    if use_synthetic:
        s = SyntheticProvider()
        return {"binance": s, "yahoo": s, "synthetic": s}
    return {"binance": BinanceProvider(), "yahoo": YahooProvider(), "synthetic": SyntheticProvider()}
