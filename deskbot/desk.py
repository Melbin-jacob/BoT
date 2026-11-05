"""Wires the six bots together and runs them 24/7.

Roles let you split processes so a crash or hang in the LLM-driven bots can never
take down risk monitoring:
  core      Market Scout (data) + Risk & Survival + ticket expiry + watchdog
  research  News & Narrative + Research Analyst + Desk Lead (daily brief)
  all       everything in one process (fine for paper trading / dev)
"""
from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .bots.desk_lead import DeskLead
from .bots.execution import Execution
from .bots.market_scout import MarketScout
from .bots.news_narrative import NewsNarrative
from .bots.research_analyst import ResearchAnalyst
from .bots.risk_survival import RiskSurvival
from .brokers.paper import PaperBroker
from .config import Settings
from .data.cache import MarketCache
from .data.providers import build_providers
from .llm import LLM
from .models import utcnow
from .notify import Notifier
from .store import Store

log = logging.getLogger("desk")


class Desk:
    def __init__(self, settings: Settings, synthetic: bool = False):
        self.s = settings
        self.store = Store(settings.db_path)
        self.notifier = Notifier(settings.notify)
        self.cache = MarketCache()
        self.cache.hydrate(self.store, [a.symbol for a in settings.watchlist])
        self.llm = LLM(settings.llm, self.store)
        if settings.execution.broker != "paper":
            raise NotImplementedError(f"broker '{settings.execution.broker}' adapter not built yet — see OPEN_QUESTIONS.md")
        self.broker = PaperBroker(settings, self.store)
        self.risk = RiskSurvival(settings, self.store, self.broker, self.cache, self.notifier)
        self.scout = MarketScout(settings, self.store, build_providers(synthetic), self.cache, self.notifier)
        self.news = NewsNarrative(settings, self.store, self.llm, self.notifier)
        self.research = ResearchAnalyst(settings, self.store, self.llm, self.cache, self.risk, self.notifier)
        self.execution = Execution(settings, self.store, self.broker, self.cache, self.risk, self.notifier)
        self.lead = DeskLead(settings, self.store, self.llm, self.cache, self.risk)
        self._stop = asyncio.Event()

    # ── loops ──────────────────────────────────────────────────────────────
    async def _loop(self, name: str, interval: int, fn, *, first_delay: float = 0) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=first_delay or 0.001)
            return                             # stopped during the initial delay
        except asyncio.TimeoutError:
            pass
        failures = 0
        while not self._stop.is_set():
            t0 = utcnow()
            try:
                work = asyncio.ensure_future(asyncio.to_thread(fn))
                stop = asyncio.ensure_future(self._stop.wait())
                done, _ = await asyncio.wait({work, stop}, timeout=max(interval * 4, 60),
                                             return_when=asyncio.FIRST_COMPLETED)
                stop.cancel()
                if self._stop.is_set():
                    break                      # don't wait on a blocked network call during shutdown
                if work not in done:
                    raise TimeoutError(f"{name} exceeded {max(interval * 4, 60)}s")
                work.result()
                failures = 0
                self.store.set(f"heartbeat:{name}", t0.isoformat())
            except Exception as e:
                failures += 1
                log.exception("%s failed (%d in a row)", name, failures)
                if failures in (3, 10) or failures % 50 == 0:
                    self.notifier.send("DESK", f"{name} failing repeatedly ({failures}x): {type(e).__name__}: {e}",
                                       "critical" if name == "risk_survival" else "warn")
            elapsed = (utcnow() - t0).total_seconds()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=max(1.0, interval - elapsed))
            except asyncio.TimeoutError:
                pass

    def _watchdog(self) -> None:
        hb = self.store.get("risk_heartbeat")
        limit = timedelta(seconds=self.s.schedule.heartbeat_stale_after)
        if not hb or utcnow() - datetime.fromisoformat(hb) > limit:
            self.notifier.send("WATCHDOG", f"Risk & Survival heartbeat stale (last {hb}). Risk is NOT being monitored.",
                               "critical")

    def _brief_if_due(self) -> None:
        tz = ZoneInfo(self.s.timezone)
        now = utcnow().astimezone(tz)
        hh, mm = map(int, self.s.schedule.desk_lead_brief_time.split(":"))
        key = f"brief_sent:{now.date().isoformat()}"
        if (now.hour, now.minute) >= (hh, mm) and not self.store.get(key):
            text = self.lead.brief()
            self.store.set(key, True)
            self.notifier.send("DESK_LEAD", text, "info")

    async def run(self, role: str = "all") -> None:
        sc = self.s.schedule
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except NotImplementedError:  # pragma: no cover
                pass
        tasks = []
        if role in ("core", "all"):
            tasks += [self._loop("market_scout", sc.market_scout, self.scout.run_once),
                      self._loop("risk_survival", sc.risk_survival, self.risk.run_once, first_delay=2),
                      self._loop("ticket_expiry", 60, self.execution.expire_old, first_delay=5)]
        if role in ("research", "all"):
            syms = [a.symbol for a in self.s.watchlist]

            def fresh(fn):  # research-only process: reload prices written by the core process
                return fn if role == "all" else (lambda: (self.cache.hydrate(self.store, syms), fn())[1])
            tasks += [self._loop("news_narrative", sc.news_narrative, self.news.run_once, first_delay=3),
                      self._loop("research_analyst", sc.research_analyst, fresh(self.research.run_once), first_delay=90),
                      self._loop("desk_lead", 30, fresh(self._brief_if_due), first_delay=10)]
        # Every role watches the risk heartbeat, so a dead core process is noticed by the research process too.
        tasks.append(self._loop("watchdog", 30, self._watchdog, first_delay=max(sc.heartbeat_stale_after, 30)))
        self.notifier.send("DESK", f"Desk started (role={role}, mode={self.s.mode}, broker={self.broker.name}, "
                                   f"LLM={'on' if self.llm.available else 'off'})", "info")
        await asyncio.gather(*tasks)
        self.notifier.send("DESK", f"Desk stopped (role={role})", "warn")
