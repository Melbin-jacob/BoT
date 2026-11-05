"""DESK LEAD — one clean, decision-oriented brief (daily + on demand).

The brief body is assembled deterministically from the store (so numbers are never
hallucinated). If Claude is available it adds a short "Desk view" synthesis on top,
constrained to the facts provided.
"""
from __future__ import annotations

from datetime import timedelta

from pydantic import BaseModel

from ..config import Settings
from ..data.cache import MarketCache
from ..llm import LLM
from ..models import NewsItem, RiskAction, Signal, TicketStatus, TradeIdea, utcnow
from ..store import Store
from .risk_survival import RiskSurvival

SIGN_OFF = "I research and brief. You make every final decision."


class DeskView(BaseModel):
    summary: str
    recommended_action: str


SYSTEM = """You are the Desk Lead of a research desk. Write a 3–5 sentence synthesis of the brief you are given
and one recommended action (e.g. 'Stand by', 'Review IDEA-XXXX', 'Approve risk ticket RSK-XXXX').
Use only facts in the brief. No new numbers. If the risk mode is not NORMAL, the recommended action must respect it.
Never claim to have placed trades."""


class DeskLead:
    def __init__(self, settings: Settings, store: Store, llm: LLM, cache: MarketCache, risk: RiskSurvival):
        self.s, self.store, self.llm, self.cache, self.risk = settings, store, llm, cache, risk

    def brief(self, hours: int = 16) -> str:
        now = utcnow()
        since = now - timedelta(hours=hours)
        L: list[str] = [f"# Desk Brief — {now:%Y-%m-%d %H:%M} UTC ({self.s.mode.upper()} mode)", ""]

        # Overnight moves
        L.append("## Overnight market moves")
        for a in self.s.watchlist:
            ser = self.cache.get(a.symbol)
            if not ser or len(ser.bars) < 2:
                L.append(f"- {a.symbol}: no data")
                continue
            past = [b for b in ser.bars if b.ts <= ser.bars[-1].ts - timedelta(hours=hours)]
            ref = past[-1].close if past else ser.bars[0].close
            chg = (ser.bars[-1].close / ref - 1) * 100
            L.append(f"- {a.symbol}: {ser.bars[-1].close:,.4f} ({chg:+.2f}% / {hours}h) — {ser.source}, "
                     f"as of {ser.bars[-1].ts:%m-%d %H:%MZ}")
        L.append(f"- Regime: **{self.store.get('scout_regime', 'unknown')}** {self.store.get('scout_regime_detail', {})}")

        sigs = self.store.recent("signal", Signal, since=since, limit=50)
        L += ["", "## Scout flags"] + ([f"- {s.data_ts:%m-%d %H:%MZ} [{s.severity}] {s.summary} (src {s.source})"
                                       for _, s in sigs if s.severity != "info"][:12] or ["- none"])

        news = self.store.recent("news", NewsItem, since=since, limit=30)
        L += ["", "## Key news / catalysts"] + ([f"- [{n.severity}] {n.headline} — {n.why_it_matters} "
                                                f"({', '.join(n.assets)}) <{n.source_url or n.source}>"
                                                for _, n in news if n.severity != "info"][:10] or ["- none flagged"])

        ideas = self.store.recent("idea", TradeIdea, since=since, limit=10)
        L += ["", "## Top ideas (Research)"]
        if ideas:
            for rid, i in ideas[:5]:
                L.append(f"- **{rid}** {i.direction.upper()} {i.symbol} | entry {i.entry_low}–{i.entry_high} | stop {i.stop} "
                         f"| tgt {i.targets} | size {i.size_pct}% | {i.horizon} | conv {i.conviction:.2f}\n"
                         f"  Thesis: {i.thesis}\n  Risks: {'; '.join(i.key_risks)}\n  {i.risk_verdict}")
        else:
            L.append(f"- none. Edge assessment: {self.store.get('research_last_edge', 'n/a')}")

        st = self.risk.status()
        L += ["", "## Risk status (Risk & Survival)",
              f"- Mode: **{st['mode'].upper()}**" + (f" — {st['survival_reason']}" if st['survival_reason'] else ""),
              f"- Equity {st['equity']:,.2f} | HWM {st['high_water_mark']:,.2f} | DD {st['drawdown_pct']}% "
              f"(limit {st['limits']['max_drawdown_pct']}%) | today {st['daily_pnl_pct']:+.2f}% "
              f"(limit −{st['limits']['max_daily_loss_pct']}%) | gross {st['gross_exposure_pct']}%"]
        acts = self.store.recent("risk_action", RiskAction, since=since, limit=10)
        L += [f"- {a.ts:%m-%d %H:%MZ} {a.rule}: {a.action} — {a.detail}" for _, a in acts[:6]]

        L += ["", "## Open positions"] + ([f"- {p['symbol']}: qty {p['qty']:g}, value {p['value']:,.2f} "
                                          f"({p['pct_equity']}% eq), uPnL {p['unrealized']:+,.2f}"
                                          for p in st["positions"]] or ["- flat"])
        staged = self.store.tickets(TicketStatus.STAGED)
        L += ["", "## Awaiting your approval"] + ([f"- {t.id} {t.side} {t.quantity:g} {t.symbol} ({t.origin}: {t.note})"
                                                  for t in staged] or ["- nothing"])

        body = "\n".join(L)
        view = self.llm.structured(SYSTEM, body, DeskView, max_tokens=3000) if self.llm.available else None
        if view:
            head = f"## Desk view\n{view.summary}\n\n**Recommended action:** {view.recommended_action}\n"
        else:
            action = ("Stand by — risk mode " + st["mode"].upper()) if st["mode"] != "normal" else \
                     ("Review staged tickets" if staged else "Review ideas" if ideas else "Stand by")
            head = f"## Recommended action\n{action}\n"
        out = body.replace("\n\n## Overnight", f"\n\n{head}\n## Overnight", 1) + f"\n\n---\n_{SIGN_OFF}_\n"
        self.store.set("last_brief", out)
        return out
