from datetime import timedelta

from deskbot.bots.risk_survival import Context
from deskbot.models import RiskMode, Ticket, TicketStatus, utcnow


def ctx(**kw):
    return Context(now=kw.pop("now", utcnow()), **kw)


def evaluate(env, **kw):
    pf = env.broker.portfolio(env.cache.prices())
    return env.risk.evaluate(pf, ctx(**kw))


def ticket(sym, qty, side="buy", stop=90.0, price=100.0, reduce_only=False):
    return Ticket(id="T1", symbol=sym, instrument=sym, side=side, quantity=qty, order_type="limit",
                  limit_price=price, stop_loss=stop, size_pct=0, sizing_calc="test", reduce_only=reduce_only)


# ── Rule 1: drawdown > 5% → flatten everything + lockdown ─────────────────────
def test_drawdown_flattens_and_locks(env):
    env.hold("GOLD", 70, 100.0)          # 7% position
    env.hold("SPX", 50, 100.0)           # 5%
    evaluate(env)                         # sets HWM at 100k
    env.set_price("GOLD", 30.0)          # −4,900
    env.set_price("SPX", 85.0)           # −750  → equity 94,350, DD 5.65%
    acts = evaluate(env)
    assert env.risk.mode == RiskMode.LOCKDOWN
    assert any(a.rule == "max_drawdown" and a.action == "flatten_all" for a in acts)
    assert env.broker.portfolio(env.cache.prices()).positions == []
    ok, why = env.risk.check_ticket(ticket("EURUSD", 1))
    assert not ok and "LOCKDOWN" in why


def test_drawdown_just_below_limit_does_nothing(env):
    env.hold("GOLD", 70, 100.0)
    evaluate(env)
    env.set_price("GOLD", 30.5)           # −4,865 → 4.865%
    evaluate(env)
    assert env.risk.mode != RiskMode.LOCKDOWN


def test_lockdown_needs_manual_rearm_and_survives_new_day(env):
    env.hold("GOLD", 70, 100.0)
    evaluate(env)
    env.set_price("GOLD", 20.0)
    evaluate(env)
    evaluate(env, now=utcnow() + timedelta(days=2))
    assert env.risk.mode == RiskMode.LOCKDOWN
    assert "Re-armed" in env.risk.rearm("reviewed")
    assert env.risk.mode == RiskMode.NORMAL


# ── Rule 2: daily loss > 2.5% → freeze new entries ───────────────────────────
def test_daily_loss_freezes_but_does_not_flatten(env):
    env.hold("GOLD", 75, 100.0)
    evaluate(env)
    env.set_price("GOLD", 65.0)           # −2,625 → 2.625% daily loss, DD same (< 5%)
    acts = evaluate(env)
    assert env.risk.mode == RiskMode.FROZEN
    assert any(a.action == "freeze" for a in acts)
    assert env.broker.portfolio(env.cache.prices()).positions  # still holding
    ok, _ = env.risk.check_ticket(ticket("SPX", 1))
    assert not ok
    ok, _ = env.risk.check_ticket(ticket("GOLD", 10, side="sell", reduce_only=True))
    assert ok                             # reducing is always allowed


def test_freeze_lifts_next_day_and_override_works(env):
    env.hold("GOLD", 75, 100.0)
    evaluate(env)
    env.set_price("GOLD", 65.0)
    evaluate(env)
    assert env.risk.mode == RiskMode.FROZEN
    env.risk.override_freeze("owner override")
    assert env.risk.mode == RiskMode.NORMAL
    env.set_price("GOLD", 64.0)                       # worse, same day: override holds for today
    evaluate(env)
    assert env.risk.mode == RiskMode.NORMAL


def test_freeze_lifts_on_next_day(env):
    env.hold("GOLD", 75, 100.0)
    evaluate(env)
    env.set_price("GOLD", 65.0)
    evaluate(env)
    assert env.risk.mode == RiskMode.FROZEN
    acts = evaluate(env, now=utcnow() + timedelta(days=1))
    assert env.risk.mode == RiskMode.NORMAL
    assert any(a.action == "unfreeze" for a in acts)


# ── Rule 3: single position > 8% → trim ──────────────────────────────────────
def test_oversized_position_trimmed_to_cap(env):
    env.hold("GOLD", 120, 100.0)          # 12%
    acts = evaluate(env)
    assert any(a.rule == "max_position" and a.action == "trim" for a in acts)
    pf = env.broker.portfolio(env.cache.prices())
    gold = next(p for p in pf.positions if p.symbol == "GOLD")
    assert abs(gold.market_value) / pf.equity * 100 <= 8.0 + 0.05


def test_short_positions_are_trimmed_too(env):
    env.hold("SPX", -120, 100.0)
    evaluate(env)
    pf = env.broker.portfolio(env.cache.prices())
    spx = next(p for p in pf.positions if p.symbol == "SPX")
    assert spx.quantity < 0 and abs(spx.market_value) / pf.equity * 100 <= 8.05


# ── Concentration / correlation ──────────────────────────────────────────────
def test_asset_class_concentration_reduced(env):
    for s in ("BTC", "ETH", "SOL", "XRP"):
        env.hold(s, 75, 100.0)            # 4 × 7.5% = 30% crypto > 25% cap
    acts = evaluate(env)
    assert any(a.rule == "concentration" for a in acts)
    pf = env.broker.portfolio(env.cache.prices())
    crypto = sum(abs(p.market_value) for p in pf.positions if p.asset_class == "crypto")
    assert crypto / pf.equity * 100 <= 25.05


def test_high_correlation_forces_reduction_once(env):
    for s in ("GOLD", "SILVER", "SPX"):
        env.hold(s, 75, 100.0)            # gross 22.5% > 20% threshold
    before = env.broker.portfolio(env.cache.prices()).gross_exposure
    evaluate(env, avg_corr=0.92)
    after = env.broker.portfolio(env.cache.prices()).gross_exposure
    assert after < before * 0.8
    evaluate(env, avg_corr=0.92)          # cooldown: no second cut immediately
    assert env.broker.portfolio(env.cache.prices()).gross_exposure == after


def test_new_trade_vetoed_if_it_breaches_caps(env):
    env.set_price("SPX", 100.0)
    ok, why = env.risk.check_ticket(ticket("SPX", 90))     # 9% > 8%
    assert not ok and "8.0%" in why
    ok, _ = env.risk.check_ticket(ticket("SPX", 70))
    assert ok
    ok, why = env.risk.check_ticket(ticket("SPX", 70, stop=None))
    assert not ok and "stop" in why


# ── Survival Mode ────────────────────────────────────────────────────────────
def test_vol_spike_enters_survival_and_cuts_70pct(env):
    env.hold("GOLD", 60, 100.0)
    env.hold("SPX", 60, 100.0)
    before = env.broker.portfolio(env.cache.prices()).gross_exposure
    acts = evaluate(env, vol_ratios={"SPX": 3.5})
    assert env.risk.mode == RiskMode.SURVIVAL
    assert any(a.action == "enter_survival" for a in acts)
    after = env.broker.portfolio(env.cache.prices()).gross_exposure
    assert after <= before * 0.30 + 1
    ok, _ = env.risk.check_ticket(ticket("EURUSD", 1))
    assert not ok


def test_news_shock_and_loss_streak_trigger_survival(env, tmp_path):
    evaluate(env, news_shocks=[("n1", "Exchange halts withdrawals after hack")])
    assert env.risk.mode == RiskMode.SURVIVAL

    from tests.conftest import Env
    e2 = Env(tmp_path / "b")
    e2.store.record_closed_trade("GOLD", -10); e2.store.record_closed_trade("SPX", -5); e2.store.record_closed_trade("BTC", -1)
    pf = e2.broker.portfolio({})
    e2.risk.evaluate(pf, Context(now=utcnow(), recent_closed_pnls=e2.store.recent_closed_pnls(3)))
    assert e2.risk.mode == RiskMode.SURVIVAL


def test_survival_cannot_be_cleared_early_and_freeze_override_refused(env):
    evaluate(env, vol_ratios={"SPX": 5.0})
    assert "Refused" in env.risk.clear_survival("too soon")
    assert "Refused" in env.risk.override_freeze("nope")
    assert env.risk.mode == RiskMode.SURVIVAL


def test_survival_never_increases_size(env):
    env.hold("GOLD", 60, 100.0)
    evaluate(env, vol_ratios={"SPX": 4.0})
    env.set_price("GOLD", 150.0)          # price rally pushes gross back above target
    evaluate(env)
    pf = env.broker.portfolio(env.cache.prices())
    st = env.risk.state()
    assert pf.gross_exposure <= st.survival_target_gross * 1.002


# ── Live-mode behaviour: recommend + block, don't auto-send ──────────────────
def test_live_without_auto_reduce_only_recommends(env):
    env.s.mode = "live"; env.s.execution.live_enabled = True   # broker is still paper in tests
    env.hold("GOLD", 120, 100.0)
    acts = evaluate(env)
    assert any(a.action == "recommend" for a in acts)
    assert next(p for p in env.broker.portfolio(env.cache.prices()).positions if p.symbol == "GOLD").quantity == 120
    staged = [t for t in env.store.tickets(TicketStatus.STAGED) if t.origin == "risk_survival"]
    assert len(staged) == 1 and staged[0].reduce_only
    evaluate(env)                          # no duplicate recommendation
    assert len([t for t in env.store.tickets(TicketStatus.STAGED) if t.origin == "risk_survival"]) == 1


def test_every_action_is_logged(env):
    env.hold("GOLD", 120, 100.0)
    evaluate(env)
    rows = list(env.store.iter_kind("risk_action"))
    assert rows and all("detail" in r[2] for r in rows)
