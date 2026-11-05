"""MARKET SCOUT — continuous multi-asset monitor. Observations only: never advice, never orders.

Detectors (all deterministic, thresholds in settings.scout):
  volume_spike      last-bar volume z-score vs rolling window
  price_spike       last-bar log-return z-score
  breakout/down     close beyond the prior N-bar high/low (the "key level")
  funding_extreme   perp funding rate beyond ±threshold
  correlation_shift change in rolling correlation between watchlist pairs
  regime_shift      risk-on → risk-off composite (equity index down + vol up + safe havens bid)
Every signal carries the data timestamp, the observation time, and the exact source.
"""
from __future__ import annotations

import logging
from datetime import timedelta

import numpy as np

from ..config import Settings
from ..data.cache import MarketCache, Series
from ..data.providers import MarketData
from ..models import Signal, utcnow
from ..notify import Notifier
from ..store import Store

log = logging.getLogger("market_scout")
BOT = "MARKET_SCOUT"
CORR_PAIRS = [("BTC", "SPX"), ("BTC", "NDX"), ("GOLD", "SPX"), ("BTC", "GOLD"), ("EURUSD", "GOLD"), ("OIL", "SPX")]


def _z(x: np.ndarray, v: float) -> float:
    sd = x.std()
    return float((v - x.mean()) / sd) if sd > 0 else 0.0


class MarketScout:
    def __init__(self, settings: Settings, store: Store, providers: dict[str, MarketData],
                 cache: MarketCache, notifier: Notifier):
        self.s, self.store, self.providers, self.cache, self.n = settings, store, providers, cache, notifier
        self.cfg = settings.scout

    def run_once(self) -> list[Signal]:
        signals: list[Signal] = []
        for a in self.s.watchlist:
            prov = self.providers.get(a.provider)
            if not prov:
                continue
            try:
                bars, src, url = prov.bars(a, self.cfg.bar_interval, self.cfg.lookback_bars)
            except Exception as e:
                log.warning("data fetch failed %s: %s", a.symbol, e)
                prev = self.cache.get(a.symbol)
                if prev is None or utcnow() - prev.fetched_at > timedelta(minutes=10):
                    signals.append(Signal(kind="data_gap", symbol=a.symbol, severity="warn",
                                          summary=f"No data for {a.symbol} from {a.provider}: {type(e).__name__}",
                                          source=f"{a.provider}:{a.ref}", data_ts=utcnow()))
                continue
            if len(bars) < 30:
                continue
            self.cache.put(a.symbol, Series(bars, src, url))
            self.cache.save(self.store, a.symbol)
            signals += self._per_asset(a.symbol, bars, src, url)
            try:
                fr = prov.funding_rate(a)
            except Exception as e:
                log.debug("funding fetch failed %s: %s", a.symbol, e)
                fr = None
            if fr and abs(fr[0]) >= self.cfg.funding_extreme_abs:
                rate, ts, fsrc, furl = fr
                side = "longs paying shorts (crowded long)" if rate > 0 else "shorts paying longs (crowded short)"
                signals.append(Signal(kind="funding_extreme", symbol=a.symbol, severity="warn",
                                      summary=f"{a.symbol} perp funding {rate*100:.4f}%/8h — {side}",
                                      metrics={"funding_rate": rate}, source=fsrc, source_url=furl, data_ts=ts))
        signals += self._correlation()
        signals += self._regime()
        return self._publish(signals)

    def _per_asset(self, sym, bars, src, url) -> list[Signal]:
        out = []
        c = np.array([b.close for b in bars])
        v = np.array([b.volume for b in bars])
        r = np.diff(np.log(c))
        last, ts = bars[-1], bars[-1].ts
        hist_r, hist_v = r[-self.cfg.lookback_bars:-1], v[:-1]
        if v[-1] > 0 and hist_v.std() > 0:
            vz = _z(hist_v[-96:], v[-1])
            if vz >= self.cfg.volume_z:
                out.append(Signal(kind="volume_spike", symbol=sym, severity="critical" if vz > 2 * self.cfg.volume_z else "warn",
                                  summary=f"{sym} volume {v[-1]:,.0f} is {vz:.1f}σ above recent average",
                                  metrics={"volume": v[-1], "z": round(vz, 2)}, source=src, source_url=url, data_ts=ts))
        rz = _z(hist_r, r[-1])
        if abs(rz) >= self.cfg.return_z:
            out.append(Signal(kind="price_spike", symbol=sym, severity="critical" if abs(rz) > 2 * self.cfg.return_z else "warn",
                              summary=f"{sym} moved {r[-1]*100:+.2f}% in one {self.cfg.bar_interval} bar ({rz:+.1f}σ)",
                              metrics={"return_pct": round(r[-1] * 100, 3), "z": round(rz, 2), "price": last.close},
                              source=src, source_url=url, data_ts=ts))
        n = self.cfg.breakout_lookback
        if len(bars) > n + 1:
            hi = max(b.high for b in bars[-n - 1:-1])
            lo = min(b.low for b in bars[-n - 1:-1])
            if last.close > hi:
                out.append(Signal(kind="breakout", symbol=sym, severity="warn",
                                  summary=f"{sym} closed {last.close:.4f} above {n}-bar high {hi:.4f}",
                                  metrics={"level": hi, "price": last.close}, source=src, source_url=url, data_ts=ts))
            elif last.close < lo:
                out.append(Signal(kind="breakdown", symbol=sym, severity="warn",
                                  summary=f"{sym} closed {last.close:.4f} below {n}-bar low {lo:.4f}",
                                  metrics={"level": lo, "price": last.close}, source=src, source_url=url, data_ts=ts))
        return out

    def _correlation(self) -> list[Signal]:
        out, w = [], self.cfg.corr_window
        for a, b in CORR_PAIRS:
            ra, rb = self.cache.returns(a), self.cache.returns(b)
            m = min(len(ra), len(rb))
            if m < 2 * w:
                continue
            ra, rb = ra[-m:], rb[-m:]
            now = float(np.corrcoef(ra[-w:], rb[-w:])[0, 1])
            prev = float(np.corrcoef(ra[-2 * w:-w], rb[-2 * w:-w])[0, 1])
            if abs(now - prev) >= self.cfg.corr_shift:
                sa, sb = self.cache.get(a), self.cache.get(b)
                out.append(Signal(kind="correlation_shift", symbol=f"{a}/{b}", severity="warn",
                                  summary=f"{a}/{b} {w}-bar correlation moved {prev:+.2f} → {now:+.2f}",
                                  metrics={"prev": round(prev, 3), "now": round(now, 3)},
                                  source=f"derived from [{sa.source}] & [{sb.source}]", data_ts=sa.bars[-1].ts))
        return out

    def _regime(self) -> list[Signal]:
        """Composite risk-on/off score. Each leg: +1 risk-off, −1 risk-on."""
        legs, detail = [], {}
        def ret(sym, n=16):
            r = self.cache.returns(sym, n)
            return float(r.sum()) if len(r) else None
        spx, vr = ret("SPX"), self.cache.vol_ratio("SPX")
        if spx is not None:
            legs.append(1 if spx < -0.005 else -1 if spx > 0.005 else 0); detail["SPX_ret"] = round(spx * 100, 2)
        if vr is not None:
            legs.append(1 if vr > self.cfg.regime_vol_mult else -1 if vr < 0.8 else 0); detail["SPX_vol_ratio"] = round(vr, 2)
        g = ret("GOLD")
        if g is not None:
            legs.append(1 if g > 0.005 else 0); detail["GOLD_ret"] = round(g * 100, 2)
        jpy = ret("USDJPY")
        if jpy is not None:
            legs.append(1 if jpy < -0.004 else -1 if jpy > 0.004 else 0); detail["USDJPY_ret"] = round(jpy * 100, 2)
        btc = ret("BTC")
        if btc is not None:
            legs.append(1 if btc < -0.02 else -1 if btc > 0.02 else 0); detail["BTC_ret"] = round(btc * 100, 2)
        if len(legs) < 3:
            return []
        score = sum(legs) / len(legs)
        regime = "risk_off" if score >= 0.5 else "risk_on" if score <= -0.5 else "neutral"
        prev = self.store.get("scout_regime", "neutral")
        self.store.set("scout_regime", regime)
        self.store.set("scout_regime_detail", {"score": round(score, 2), **detail})
        if regime != prev:
            spx_s = self.cache.get("SPX")
            return [Signal(kind="regime_shift", symbol="MACRO", severity="critical" if regime == "risk_off" else "warn",
                           summary=f"Regime shift {prev} → {regime} (score {score:+.2f})", metrics=detail,
                           source="derived: SPX, GOLD, USDJPY, BTC returns + SPX vol ratio",
                           data_ts=spx_s.bars[-1].ts if spx_s else utcnow())]
        return []

    def _publish(self, signals: list[Signal]) -> list[Signal]:
        out = []
        cutoff = utcnow() - timedelta(minutes=self.cfg.dedupe_minutes)
        for s in signals:
            last = self.store.last_record_ts("signal", s.symbol, f'"kind":"{s.kind}"')
            if last and last > cutoff:
                continue
            self.store.add("signal", s, symbol=s.symbol)
            if s.severity != "info":
                self.n.send(BOT, f"{s.summary} | data {s.data_ts:%Y-%m-%d %H:%MZ} | src {s.source}", s.severity)
            out.append(s)
        return out
