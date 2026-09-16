"""Minimal Telegram sender — same raw-requests pattern as notifier.py, no
webhook, no bot framework. Read-only from the market's perspective: this
tool never places an order, it only pushes text."""

from __future__ import annotations

import requests

from screener import config

API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def send(messages: list[str]) -> None:
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (or SCREENER_* variants) "
            "not set — see screener/env.example."
        )
    url = API_URL.format(token=config.TELEGRAM_BOT_TOKEN)
    for msg in messages:
        resp = requests.post(
            url,
            json={"chat_id": config.TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=15,
        )
        resp.raise_for_status()
