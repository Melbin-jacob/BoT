"""NEWS & NARRATIVE — breaking news, official releases, central banks, geopolitics, high-signal X posts.

Pipeline: fetch primary-source RSS (and X if configured) → dedupe by URL →
triage (Claude if available, else keyword rules) → keep catalysts for the next
1–48h → What happened / Why it matters / Assets affected → store + alert.
Items judged "critical" are visible to Risk & Survival as news-shock triggers.
Never trades.
"""
from __future__ import annotations

import hashlib
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Literal

import httpx
from pydantic import BaseModel

from ..config import Settings
from ..llm import LLM
from ..models import NewsItem
from ..notify import Notifier
from ..store import Store

log = logging.getLogger("news")
BOT = "NEWS_NARRATIVE"

KEYWORDS = {
    "BTC": ["bitcoin", "btc", "crypto", "etf flow", "spot etf"], "ETH": ["ethereum", "ether", " eth "],
    "SOL": ["solana"], "XRP": ["xrp", "ripple"], "GOLD": ["gold", "bullion"], "SILVER": ["silver"],
    "OIL": ["oil", "crude", "opec", "brent", "wti", "petroleum"],
    "EURUSD": ["ecb", "euro", "eurozone", "lagarde"], "GBPUSD": ["bank of england", "boe", "sterling", "pound"],
    "USDJPY": ["bank of japan", "boj", "yen", "ueda"],
    "SPX": ["fomc", "federal reserve", "cpi", "payroll", "jobs report", "inflation", "gdp", "s&p", "powell", "tariff"],
    "NDX": ["nasdaq", "nvidia", "semiconductor", "big tech", "ai chip"],
}


class Triage(BaseModel):
    keep: bool
    relevance: float
    severity: Literal["info", "warn", "critical"]
    what_happened: str
    why_it_matters: str
    assets: list[str]
    horizon_hours: int


SYSTEM = """You are the News & Narrative monitor on a multi-asset research desk (crypto, commodities, FX, equity indices).
You never give trade advice and never suggest orders.
For each item decide whether it is a catalyst likely to move the listed assets within 1–48 hours.
Discard noise, opinion pieces, hype, and stale/recycled news (keep=false, relevance<0.3).
severity=critical ONLY for genuine market shocks (emergency central-bank action, exchange hack/insolvency,
stablecoin depeg, war escalation, trading halts, sovereign default). Be terse and factual.
what_happened: 1 sentence. why_it_matters: 1–2 sentences on the transmission mechanism.
assets: only symbols from the watchlist."""


class NewsNarrative:
    def __init__(self, settings: Settings, store: Store, llm: LLM, notifier: Notifier, http: httpx.Client | None = None):
        self.s, self.store, self.llm, self.n = settings, store, llm, notifier
        self.http = http or httpx.Client(timeout=15, headers={"User-Agent": "trading-desk/0.1"}, follow_redirects=True)
        self.symbols = [a.symbol for a in settings.watchlist]

    def fetch_rss(self) -> list[dict]:
        items = []
        for feed in self.s.news.rss_feeds:
            try:
                r = self.http.get(feed.url)
                r.raise_for_status()
                root = ET.fromstring(r.content)
            except Exception as e:
                log.warning("feed %s failed: %s", feed.name, e)
                continue
            ns = {"a": "http://www.w3.org/2005/Atom"}
            entries = root.findall(".//item") or root.findall(".//a:entry", ns)
            for it in entries[:25]:
                title = (it.findtext("title") or it.findtext("a:title", namespaces=ns) or "").strip()
                link = it.findtext("link") or ""
                if not link:
                    le = it.find("a:link", ns)
                    link = le.get("href", "") if le is not None else ""
                desc = (it.findtext("description") or it.findtext("a:summary", namespaces=ns) or "")[:800]
                pub = it.findtext("pubDate") or it.findtext("a:updated", namespaces=ns)
                items.append({"source": feed.name, "title": title, "url": link.strip(), "desc": desc,
                              "published": _parse_dt(pub)})
        return items

    def run_once(self, raw: list[dict] | None = None) -> list[NewsItem]:
        raw = raw if raw is not None else self.fetch_rss()
        seen = set(self.store.get("news_seen", []))
        out = []
        for it in raw:
            key = hashlib.sha1((it.get("url") or it["title"]).encode()).hexdigest()[:16]
            if key in seen:
                continue
            seen.add(key)
            item = self._triage(it)
            if item is None or item.relevance < self.s.news.min_relevance:
                continue
            self.store.add("news", item)
            out.append(item)
            if item.severity != "info":
                self.n.send(BOT, f"{item.headline}\n  What: {item.what_happened}\n  Why: {item.why_it_matters}\n"
                                 f"  Assets: {', '.join(item.assets) or '-'} | src {item.source} {item.source_url or ''}",
                            item.severity)
        self.store.set("news_seen", list(seen)[-5000:])
        return out

    def _triage(self, it: dict) -> NewsItem | None:
        text = f"{it['title']} {it.get('desc', '')}".lower()
        if self.llm.available:
            t = self.llm.structured(
                SYSTEM,
                f"Watchlist: {', '.join(self.symbols)}\nSource: {it['source']}\nPublished: {it.get('published')}\n"
                f"Headline: {it['title']}\nSummary: {it.get('desc', '')}",
                Triage, max_tokens=2000)
            if t is not None:
                if not t.keep:
                    return None
                return NewsItem(headline=it["title"], what_happened=t.what_happened, why_it_matters=t.why_it_matters,
                                assets=[a for a in t.assets if a in self.symbols], horizon_hours=t.horizon_hours,
                                relevance=t.relevance, severity=t.severity, source=it["source"],
                                source_url=it.get("url"), published_at=it.get("published"))
        # Rule-based fallback
        assets = [s for s, kws in KEYWORDS.items() if s in self.symbols and any(k in f" {text} " for k in kws)]
        if not assets:
            return None
        # Keyword matching alone never marks an item "critical": that would let a stray word
        # ("default", "halt") push Risk & Survival into a 70% cut. Possible shocks are flagged for you.
        shock = any(k in text for k in self.s.news.shock_keywords)
        why = ("POSSIBLE SHOCK keyword — verify manually. " if shock else "") + \
              "Keyword match on watchlist assets (LLM triage unavailable)."
        return NewsItem(headline=it["title"], what_happened=it["title"], why_it_matters=why,
                        assets=assets, relevance=0.6, severity="warn" if shock or len(assets) > 1 else "info",
                        source=it["source"], source_url=it.get("url"), published_at=it.get("published"))


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return parsedate_to_datetime(s)
    except Exception:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
