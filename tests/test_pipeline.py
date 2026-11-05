from deskbot.bots.market_scout import MarketScout
from deskbot.bots.news_narrative import NewsNarrative
from deskbot.bots.research_analyst import IdeaDraft, size_from_risk, validate_levels
from deskbot.data.providers import SyntheticProvider
from deskbot.llm import LLM
from deskbot.models import TicketStatus, TradeIdea


def test_sizing_from_risk_rules():
    size, calc = size_from_risk(100, 98, risk_pct=0.5, max_pos_pct=8)   # 2% stop → 25% raw → capped 8%
    assert size == 8 and "capped" in calc
    size, _ = size_from_risk(100, 90, risk_pct=0.5, max_pos_pct=8)      # 10% stop → 5%
    assert size == 5


def test_idea_level_validation():
    good = IdeaDraft(symbol="GOLD", thesis="t", direction="long", entry_low=99, entry_high=100, stop=95, targets=[110],
                     horizon="1-3d", key_risks=["x"], conviction=0.8, based_on=[])
    assert validate_levels(good) is None
    bad = good.model_copy(update={"stop": 101})
    assert validate_levels(bad)


def test_scout_flags_spikes_and_funding(env):
    prov = SyntheticProvider(shocks={"BTC": {"ret": 0.06, "vol_mult": 8, "funding": 0.002}})
    scout = MarketScout(env.s, env.store, {"binance": prov, "yahoo": prov}, env.cache, env.n)
    sigs = scout.run_once()
    kinds = {(s.kind, s.symbol) for s in sigs}
    assert ("price_spike", "BTC") in kinds
    assert ("volume_spike", "BTC") in kinds
    assert ("funding_extreme", "BTC") in kinds
    assert all(s.source and s.data_ts for s in sigs)
    assert not any(k == ("price_spike", "ETH") for k in kinds)
    assert scout.run_once() == [] or all(s.kind not in ("price_spike", "volume_spike") for s in scout.run_once())  # dedupe


def test_news_rule_fallback_never_critical(env):
    llm = LLM(env.s.llm, env.store)
    llm.client = None
    news = NewsNarrative(env.s, env.store, llm, env.n)
    items = news.run_once([
        {"source": "Fed", "title": "Federal Reserve issues FOMC statement", "url": "u1", "desc": "inflation"},
        {"source": "X", "title": "Celebrity launches meme NFT", "url": "u2", "desc": ""},
        {"source": "CoinDesk", "title": "Bitcoin exchange halts withdrawals after hack", "url": "u3", "desc": ""},
    ])
    heads = [i.headline for i in items]
    assert "Celebrity launches meme NFT" not in heads
    assert all(i.severity != "critical" for i in items)
    assert news.run_once([{"source": "Fed", "title": "Federal Reserve issues FOMC statement", "url": "u1"}]) == []


def test_execution_requires_approval_and_rechecks_risk(env):
    env.set_price("GOLD", 100.0)
    idea = TradeIdea(id="IDEA-1", symbol="GOLD", thesis="t", direction="long", entry_low=99.5, entry_high=100,
                     stop=95, targets=[110], size_pct=5.0, horizon="2d", key_risks=["x"], conviction=0.8)
    env.store.add("idea", idea, symbol="GOLD", rid="IDEA-1")
    t = env.exe.prepare("IDEA-1")
    assert t.status == TicketStatus.STAGED
    assert env.broker.portfolio(env.cache.prices()).positions == []        # nothing sent on prepare
    # risk state changes between staging and approval → veto at send time
    st = env.risk.state(); st.frozen = True; env.risk._save(st)
    ok, why = env.exe.approve(t.id)
    assert not ok and "FROZEN" in why
    st.frozen = False; env.risk._save(st)
    t2 = env.exe.prepare("IDEA-1")
    ok, msg = env.exe.approve(t2.id)
    assert ok, msg
    pos = env.broker.portfolio(env.cache.prices()).positions
    assert len(pos) == 1 and pos[0].symbol == "GOLD"
    ok, _ = env.exe.approve(t2.id)                                       # can't double-send
    assert not ok


def test_live_flag_required(env):
    env.set_price("GOLD", 100.0)
    env.s.mode = "live"
    idea = TradeIdea(id="IDEA-2", symbol="GOLD", thesis="t", direction="long", entry_low=99.5, entry_high=100,
                     stop=95, targets=[110], size_pct=5.0, horizon="2d", key_risks=["x"], conviction=0.8)
    env.store.add("idea", idea, symbol="GOLD", rid="IDEA-2")
    t = env.exe.prepare("IDEA-2")
    ok, why = env.exe.approve(t.id)
    assert not ok and "live_enabled" in why
