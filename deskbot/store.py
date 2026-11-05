"""SQLite store = durable message bus + audit log.

Every bot writes its output here; other bots read from it. Nothing is ever
deleted, so the full decision trail survives restarts and can be audited.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, TypeVar

from pydantic import BaseModel

from .models import Ticket, TicketStatus, utcnow

T = TypeVar("T", bound=BaseModel)

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,          -- signal | news | idea | risk_action | brief
    ts TEXT NOT NULL,
    symbol TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_records_kind_ts ON records(kind, ts);
CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ticket_events (
    ticket_id TEXT, ts TEXT, status TEXT, actor TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS equity (
    ts TEXT PRIMARY KEY,
    equity REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS closed_trades (
    ts TEXT, symbol TEXT, pnl REAL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._lock = threading.RLock()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    # ── generic records ────────────────────────────────────────────────────
    def add(self, kind: str, obj: BaseModel, symbol: str | None = None, rid: str | None = None) -> str:
        rid = rid or f"{kind[:3]}_{uuid.uuid4().hex[:10]}"
        self._exec("INSERT INTO records VALUES (?,?,?,?,?)",
                   (rid, kind, _iso(utcnow()), symbol, obj.model_dump_json()))
        return rid

    def recent(self, kind: str, model: type[T], since: datetime | None = None,
               limit: int = 200) -> list[tuple[str, T]]:
        sql, params = "SELECT id, payload FROM records WHERE kind=?", [kind]
        if since:
            sql += " AND ts>=?"
            params.append(_iso(since))
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        rows = self._exec(sql, tuple(params)).fetchall()
        return [(r[0], model.model_validate_json(r[1])) for r in rows]

    def last_record_ts(self, kind: str, symbol: str, match: str) -> datetime | None:
        row = self._exec("SELECT ts FROM records WHERE kind=? AND symbol=? AND payload LIKE ? "
                         "ORDER BY ts DESC LIMIT 1", (kind, symbol, f"%{match}%")).fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    # ── tickets ────────────────────────────────────────────────────────────
    def save_ticket(self, t: Ticket, actor: str = "system", note: str = "") -> None:
        t.updated_at = utcnow()
        self._exec("INSERT OR REPLACE INTO tickets VALUES (?,?,?,?,?)",
                   (t.id, t.status.value, _iso(t.created_at), _iso(t.updated_at), t.model_dump_json()))
        self._exec("INSERT INTO ticket_events VALUES (?,?,?,?,?)",
                   (t.id, _iso(utcnow()), t.status.value, actor, note or t.note))

    def get_ticket(self, tid: str) -> Ticket | None:
        row = self._exec("SELECT payload FROM tickets WHERE id=?", (tid,)).fetchone()
        return Ticket.model_validate_json(row[0]) if row else None

    def tickets(self, status: TicketStatus | None = None, limit: int = 100) -> list[Ticket]:
        if status:
            rows = self._exec("SELECT payload FROM tickets WHERE status=? ORDER BY created_at DESC LIMIT ?",
                              (status.value, limit)).fetchall()
        else:
            rows = self._exec("SELECT payload FROM tickets ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [Ticket.model_validate_json(r[0]) for r in rows]

    # ── equity & trades ────────────────────────────────────────────────────
    def record_equity(self, equity: float, ts: datetime | None = None) -> None:
        self._exec("INSERT OR REPLACE INTO equity VALUES (?,?)", (_iso(ts or utcnow()), equity))

    def equity_since(self, since: datetime) -> list[tuple[datetime, float]]:
        rows = self._exec("SELECT ts, equity FROM equity WHERE ts>=? ORDER BY ts", (_iso(since),)).fetchall()
        return [(datetime.fromisoformat(r[0]), r[1]) for r in rows]

    def first_equity_since(self, since: datetime) -> float | None:
        row = self._exec("SELECT equity FROM equity WHERE ts>=? ORDER BY ts LIMIT 1", (_iso(since),)).fetchone()
        return row[0] if row else None

    def record_closed_trade(self, symbol: str, pnl: float) -> None:
        self._exec("INSERT INTO closed_trades VALUES (?,?,?)", (_iso(utcnow()), symbol, pnl))

    def recent_closed_pnls(self, n: int) -> list[float]:
        rows = self._exec("SELECT pnl FROM closed_trades ORDER BY ts DESC LIMIT ?", (n,)).fetchall()
        return [r[0] for r in rows]

    # ── key/value state ────────────────────────────────────────────────────
    def get(self, key: str, default: Any = None) -> Any:
        row = self._exec("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value: Any) -> None:
        self._exec("INSERT OR REPLACE INTO kv VALUES (?,?,?)", (key, json.dumps(value, default=str), _iso(utcnow())))

    def kv_updated_at(self, key: str) -> datetime | None:
        row = self._exec("SELECT updated_at FROM kv WHERE key=?", (key,)).fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    def iter_kind(self, kind: str) -> Iterator[tuple[str, str, dict]]:
        for rid, ts, payload in self._exec("SELECT id, ts, payload FROM records WHERE kind=? ORDER BY ts", (kind,)):
            yield rid, ts, json.loads(payload)
