# Trading Desk — multi-bot research desk with a hard risk layer

Six bots, one rule: **the bots research and brief; you make every decision.** The only bot that can
touch positions on its own is Risk & Survival, and it can only *reduce* risk.

| Bot | What it does | LLM? | Can trade? |
|---|---|---|---|
| **Market Scout** | 24/7 monitor: volume and price spikes, breakouts/breakdowns, funding extremes, correlation shifts, risk-on/off regime. Every signal is timestamped and sourced. | No (deterministic) | Never |
| **News & Narrative** | Primary-source RSS (Fed, ECB, BLS, SEC, EIA, CoinDesk) → What happened / Why it matters / Assets, for the next 1–48h | Triage (rule-based fallback) | Never |
| **Research Analyst** | Turns signals + news into structured ideas: thesis, direction, entry, stop, targets, size, horizon, risks. Drops ideas with weak edge. | Yes | Never |
| **Risk & Survival** | Hard rules with absolute veto (below). Logs every action. | **No, on purpose** | Reduce-only |
| **Execution** | Builds exact tickets with the sizing math shown; sends **only** after `desk approve <ID>`; re-checks risk at send time | No | Only with your approval |
| **Desk Lead** | Morning brief plus on-demand brief; ends with *"I research and brief. You make every final decision."* | Optional synthesis | Never |

## Risk & Survival rules (`config/settings.yaml → risk`)

| Trigger | Action | How it clears |
|---|---|---|
| Drawdown from high-water mark > **5%** | **LOCKDOWN**: flatten everything, freeze all new risk | Only `desk rearm --reason ...` |
| Daily loss > **2.5%** | **FROZEN**: no new entries | Next local day, or `desk override-freeze` |
| Single position > **8%** of equity | Trim back to 8% | Automatic |
| Asset-class concentration over its cap / gross > 100% | Pro-rata reduction | Automatic |
| Avg pairwise correlation of holdings > 0.8 (gross > 20%) | Cut all positions 25% (max once per 4h) | Automatic |
| Vol spike ≥3× normal, a critical news shock, or 3 losing trades in a row | **SURVIVAL**: cut gross exposure 70% (configurable up to 100%), no new risk, and no re-growth | Only you, and only after at least 24h |

Every new-risk order is also vetoed unless it has a stop, and unless it would stay within the position, class and gross caps after it fills.

**Paper mode**: reductions execute automatically. **Live mode** (your spec says "recommend"): reductions are staged as
`RSK-…` tickets plus a critical alert, and new risk is blocked meanwhile. Flip `risk.auto_reduce.live: true` to let
it execute reductions itself.

## Quick start

```bash
pip install -r requirements-dev.txt
pytest -q                                   # 23 tests, risk engine first
cp .env.example .env                        # add ANTHROPIC_API_KEY, Telegram (optional)
python -m deskbot.cli --synthetic scan      # offline dry run on fake data
python -m deskbot.cli run                   # everything, 24/7 (paper)
```

24/7 with auto-restart and health checks:
```bash
docker compose up -d --build
```

## Dashboard (localhost)

```bash
python -m deskbot.cli run --ui-port 8080      # desk + dashboard → http://127.0.0.1:8080
python -m deskbot.cli ui                      # dashboard only, against the shared database
```

It shows the risk mode, equity, drawdown and daily-loss meters, the watchlist with sparklines, every bot's feed,
the risk log, bot heartbeats and the brief. You can **prepare / approve / reject** tickets from it. Risk overrides
(`override-freeze`, `clear-survival`, `rearm`) are CLI-only on purpose. The dashboard binds to 127.0.0.1. Set
`DESK_UI_TOKEN` to require a token for the buttons; do that before exposing it beyond your own machine.
With Docker it's published only on the host's loopback: `127.0.0.1:8080`.

## Daily commands

```
python -m deskbot.cli status                     # risk mode, equity, DD, positions
python -m deskbot.cli brief                      # Desk Lead brief now
python -m deskbot.cli ideas                      # recent Research ideas
python -m deskbot.cli prepare IDEA-XXXXXX        # → staged ticket (risk-checked)
python -m deskbot.cli tickets                    # what's waiting for you
python -m deskbot.cli approve TKT-XXXXXX         # send (re-checked by Risk)
python -m deskbot.cli reject  TKT-XXXXXX --reason "..."
python -m deskbot.cli override-freeze --reason "..."   # asks you to type YES
python -m deskbot.cli clear-survival  --reason "..."
python -m deskbot.cli rearm           --reason "..."
python -m deskbot.cli audit                      # full risk action log
```

## Layout

```
deskbot/
  bots/risk_survival.py   ← the part to review first
  bots/market_scout.py  bots/news_narrative.py  bots/research_analyst.py
  bots/execution.py     bots/desk_lead.py
  brokers/paper.py      (live adapters implement brokers/base.py)
  data/providers.py     (Binance public, Yahoo, Synthetic)
  store.py              SQLite: message bus + permanent audit log
  desk.py               24/7 scheduler, watchdog, process roles
config/settings.yaml    every threshold; "# ASK" marks defaults to confirm
```

See `OPEN_QUESTIONS.md` for what's still needed before going live.
