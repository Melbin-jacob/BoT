"""Local dashboard: http://127.0.0.1:8080  (stdlib only, no extra dependencies).

Read-only views of every bot, plus the two actions you already have on the CLI:
prepare a ticket from an idea, and approve/reject staged tickets.
Risk overrides (override-freeze, clear-survival, rearm) are deliberately CLI-only.

Binds to 127.0.0.1 by default. If DESK_UI_TOKEN is set, every POST must send it
in the X-Desk-Token header (the page asks for it once and keeps it in localStorage).
"""
from __future__ import annotations

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .models import NewsItem, RiskAction, Signal, TicketStatus, TradeIdea

log = logging.getLogger("web")
PAGE = (Path(__file__).parent / "static" / "dashboard.html").read_text()


def snapshot(desk) -> dict:
    s, st = desk.store, desk.store
    desk.cache.hydrate(s, [a.symbol for a in desk.s.watchlist])
    prices = []
    for a in desk.s.watchlist:
        ser = desk.cache.get(a.symbol)
        if ser and len(ser.bars) > 1:
            last, first = ser.bars[-1], ser.bars[max(0, len(ser.bars) - 97)]
            prices.append({"symbol": a.symbol, "asset_class": a.asset_class, "price": last.close,
                           "chg_24h": (last.close / first.close - 1) * 100, "ts": last.ts.isoformat(),
                           "source": ser.source, "spark": [b.close for b in ser.bars[-96:]]})
        else:
            prices.append({"symbol": a.symbol, "asset_class": a.asset_class, "price": None})
    hb = {k: st.get(f"heartbeat:{k}") for k in
          ("market_scout", "risk_survival", "news_narrative", "research_analyst", "desk_lead", "watchdog")}
    hb["risk_engine"] = st.get("risk_heartbeat")
    return {
        "mode": desk.s.mode, "live_enabled": desk.s.execution.live_enabled, "llm": desk.llm.available,
        "risk": desk.risk.status(),
        "regime": {"name": st.get("scout_regime", "unknown"), "detail": st.get("scout_regime_detail", {})},
        "prices": prices,
        "signals": [{"id": rid, **sig.model_dump(mode="json")} for rid, sig in s.recent("signal", Signal, limit=40)],
        "news": [{"id": rid, **n.model_dump(mode="json")} for rid, n in s.recent("news", NewsItem, limit=30)],
        "ideas": [{"id": rid, **i.model_dump(mode="json")} for rid, i in s.recent("idea", TradeIdea, limit=20)],
        "tickets": [t.model_dump(mode="json") for t in s.tickets(limit=40)],
        "risk_actions": [a.model_dump(mode="json") for _, a in s.recent("risk_action", RiskAction, limit=40)],
        "edge": st.get("research_last_edge"),
        "brief": st.get("last_brief"),
        "heartbeats": hb,
    }


def make_handler(desk, token: str | None):
    lock = threading.Lock()

    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

        def _send(self, code: int, body, ctype="application/json"):
            data = body.encode() if isinstance(body, str) else json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                return self._send(200, PAGE, "text/html")
            if self.path == "/api/state":
                with lock:
                    return self._send(200, snapshot(desk))
            if self.path == "/healthz":
                return self._send(200, {"ok": True})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if token and self.headers.get("X-Desk-Token") != token:
                return self._send(401, {"error": "bad or missing X-Desk-Token"})
            parts = self.path.strip("/").split("/")   # api/<what>/<id>/<verb>
            try:
                with lock:
                    if parts == ["api", "brief"]:
                        return self._send(200, {"brief": desk.lead.brief()})
                    if len(parts) == 4 and parts[:2] == ["api", "ideas"] and parts[3] == "prepare":
                        t = desk.execution.prepare(parts[2])
                        return self._send(200, {"ok": t.status == TicketStatus.STAGED, "message": desk.execution.render(t)})
                    if len(parts) == 4 and parts[:2] == ["api", "tickets"] and parts[3] in ("approve", "reject"):
                        if parts[3] == "approve":
                            ok, msg = desk.execution.approve(parts[2], actor="owner(web)")
                        else:
                            ok, msg = True, desk.execution.reject(parts[2], "rejected in dashboard")
                        return self._send(200, {"ok": ok, "message": msg})
            except ValueError as e:
                return self._send(400, {"ok": False, "message": str(e)})
            except Exception as e:  # pragma: no cover
                log.exception("POST %s failed", self.path)
                return self._send(500, {"ok": False, "message": f"{type(e).__name__}: {e}"})
            self._send(404, {"error": "not found"})

    return H


def serve(desk, host: str = "127.0.0.1", port: int = 8080, background: bool = False) -> ThreadingHTTPServer:
    token = os.getenv("DESK_UI_TOKEN") or None
    if host not in ("127.0.0.1", "localhost", "::1") and not token:
        log.warning("Dashboard bound to %s without DESK_UI_TOKEN — anyone who can reach it can approve tickets.", host)
    srv = ThreadingHTTPServer((host, port), make_handler(desk, token))
    srv.daemon_threads = True
    desk.notifier.send("DESK", f"Dashboard on http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}", "info")
    if background:
        threading.Thread(target=srv.serve_forever, name="dashboard", daemon=True).start()
    else:
        srv.serve_forever()
    return srv
