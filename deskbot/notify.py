"""Alerts to console, an append-only file, and Telegram (if TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID are set)."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .config import NotifyCfg

log = logging.getLogger("notify")
ICON = {"info": "ℹ️", "warn": "⚠️", "critical": "🚨"}


class Notifier:
    def __init__(self, cfg: NotifyCfg):
        self.cfg = cfg
        self.tg_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.tg_chat = os.getenv("TELEGRAM_CHAT_ID")
        self.sent: list[str] = []      # kept for tests / status

    def send(self, bot: str, text: str, level: str = "info") -> None:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        line = f"{ICON.get(level, '')} [{ts}] [{bot}] {text}"
        self.sent.append(line)
        if self.cfg.console:
            print(line, flush=True)
        if self.cfg.file:
            Path(self.cfg.file).parent.mkdir(parents=True, exist_ok=True)
            with open(self.cfg.file, "a") as f:
                f.write(line + "\n")
        if self.cfg.telegram and self.tg_token and self.tg_chat:
            try:
                httpx.post(f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                           json={"chat_id": self.tg_chat, "text": line[:4000],
                                 "disable_notification": level == "info"}, timeout=10)
            except httpx.HTTPError as e:
                log.error("telegram send failed: %s", e)
