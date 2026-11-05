"""Command line: `python -m deskbot.cli <command>` (or `desk <command>` once installed)."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta

from .config import load_settings
from .models import TicketStatus, TradeIdea, utcnow


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="desk", description="Multi-bot research desk")
    ap.add_argument("--config", default="config/settings.yaml")
    ap.add_argument("--synthetic", action="store_true", help="use synthetic market data (offline dry run)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run 24/7");  r.add_argument("--role", choices=["all", "core", "research"], default="all")
    r.add_argument("--ui-port", type=int, default=0, help="also serve the dashboard on this port (0 = off)")
    r.add_argument("--ui-host", default="127.0.0.1")
    u = sub.add_parser("ui", help="serve the dashboard only (reads the shared database)")
    u.add_argument("--port", type=int, default=8080); u.add_argument("--host", default="127.0.0.1")
    sub.add_parser("status", help="risk status + positions")
    sub.add_parser("brief", help="produce the Desk Lead brief now")
    sub.add_parser("scan", help="run Market Scout + Risk once")
    sub.add_parser("ideas", help="list recent ideas")
    p = sub.add_parser("prepare", help="turn an idea into a staged ticket"); p.add_argument("idea_id")
    sub.add_parser("tickets", help="list staged tickets")
    a = sub.add_parser("approve", help="approve + send a staged ticket"); a.add_argument("ticket_id")
    j = sub.add_parser("reject"); j.add_argument("ticket_id"); j.add_argument("--reason", default="")
    o = sub.add_parser("override-freeze", help="lift today's daily-loss freeze"); o.add_argument("--reason", required=True)
    c = sub.add_parser("clear-survival", help="exit Survival Mode (after min duration)"); c.add_argument("--reason", required=True)
    m = sub.add_parser("rearm", help="exit LOCKDOWN after a drawdown flatten"); m.add_argument("--reason", required=True)
    sub.add_parser("health", help="exit 1 if the risk heartbeat is stale (for Docker/systemd)")
    sub.add_parser("audit", help="print the risk action log")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings(args.config)

    if args.cmd == "health":
        from .store import Store
        hb = Store(settings.db_path).get("risk_heartbeat")
        ok = bool(hb) and utcnow() - datetime.fromisoformat(hb) < timedelta(seconds=settings.schedule.heartbeat_stale_after)
        print("ok" if ok else f"STALE (last {hb})")
        return 0 if ok else 1

    from .desk import Desk
    desk = Desk(settings, synthetic=args.synthetic)
    if args.cmd in ("override-freeze", "clear-survival", "rearm"):
        if not _confirm(f"Type YES to {args.cmd} (reason: {args.reason}): "):
            print("aborted"); return 1
    match args.cmd:
        case "ui":
            from .web import serve
            serve(desk, args.host, args.port)
        case "run":
            if args.ui_port:
                from .web import serve
                serve(desk, args.ui_host, args.ui_port, background=True)
            loop = asyncio.new_event_loop()
            loop.run_until_complete(desk.run(args.role))
            logging.shutdown()
            os._exit(0)   # all writes are already committed; don't block on stuck I/O threads
        case "status":
            print(json.dumps({"risk": desk.risk.status(), "regime": desk.store.get("scout_regime")}, indent=2))
        case "brief":
            print(desk.lead.brief())
        case "scan":
            desk.scout.run_once(); desk.risk.run_once()
            print(json.dumps(desk.risk.status(), indent=2))
        case "ideas":
            for rid, i in desk.store.recent("idea", TradeIdea, limit=20):
                print(f"{rid} {i.direction} {i.symbol} {i.entry_low}-{i.entry_high} stop {i.stop} size {i.size_pct}% — {i.thesis}")
        case "prepare":
            print(desk.execution.render(desk.execution.prepare(args.idea_id)))
        case "tickets":
            for t in desk.store.tickets(TicketStatus.STAGED):
                print(desk.execution.render(t), "\n")
        case "approve":
            ok, msg = desk.execution.approve(args.ticket_id)
            print(("OK: " if ok else "NOT SENT: ") + msg); return 0 if ok else 1
        case "reject":
            print(desk.execution.reject(args.ticket_id, args.reason))
        case "override-freeze":
            print(desk.risk.override_freeze(args.reason))
        case "clear-survival":
            print(desk.risk.clear_survival(args.reason))
        case "rearm":
            print(desk.risk.rearm(args.reason))
        case "audit":
            for rid, ts, a in desk.store.iter_kind("risk_action"):
                print(f"{ts} {a['rule']:<18} {a['action']:<15} executed={a['executed']!s:<5} {a['detail']}")
    return 0


def _confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        return False
    return input(prompt).strip() == "YES"


if __name__ == "__main__":
    sys.exit(main())
