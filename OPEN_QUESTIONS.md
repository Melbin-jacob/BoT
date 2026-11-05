# Open questions before live trading

Defaults are already set in `config/settings.yaml` (marked `# ASK`). The system runs in paper mode
without these answers; going live needs the **Blocking** ones.

## Blocking for live
1. **Brokers/exchanges + jurisdiction.** Which accounts (IBKR, OANDA, Binance, Coinbase, Kraken…) and which country/province? Only a paper broker exists today; each live venue needs an adapter implementing `brokers/base.py`.
2. **Instruments.** Spot only, or futures/perps/margin/options? Leverage changes the exposure math (notional vs margin).
3. **Polymarket.** Your first message listed it, but the new watchlist doesn't. Still in scope? (Jurisdiction-dependent.)
4. **Live risk behavior.** You wrote "*recommend* full flatten". Paper mode auto-executes reductions; live mode currently stages `RSK-` tickets for your approval and blocks new risk. Do you want live drawdown flattening to be **automatic** (`risk.auto_reduce.live: true`)? I recommend yes for the drawdown rule at minimum, because an alert at 3 a.m. that waits for you isn't capital preservation.

## Risk parameters to confirm
5. Risk per trade (drives sizing): default **0.5%** of equity at the stop.
6. Asset-class caps: crypto 25%, commodity 30%, FX 40%, index 40%. Gross exposure cap 100% (no leverage).
7. Survival Mode: cut **70%** or **100%**? Consecutive-loss trigger = **3**? Minimum duration **24h**, then only you can clear it?
8. "Daily" boundary timezone: **America/Toronto** midnight?
9. Correlation rule: avg pairwise corr > **0.8** → cut 25%. OK?

## Data and operations
10. Data feeds. Binance public (crypto) and Yahoo (delayed for some futures) are free but not institutional. Do you have Polygon, Databento, a broker feed, Glassnode, etc.?
11. X/Twitter: paid API access? Which accounts count as "high-signal"?
12. Notifications: Telegram is wired up. Do you also want SMS or a phone call for LOCKDOWN/SURVIVAL?
13. Morning brief time: default **07:30 America/Toronto**.
14. Hosting: which cloud/VPS? `docker compose up -d` runs it 24/7 with restart and a heartbeat health check.
15. LLM budget: default soft cap **$200/month**. Model `claude-opus-5-5` at `medium` effort.
16. Git remote: which GitHub repo should this be pushed to?
