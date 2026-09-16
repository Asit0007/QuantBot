"""All tunables via .env, no hardcoded thresholds in the logic modules —
same convention as bot.py (CLAUDE.md §7.5: "Config via .env only").

INDIA_10Y_GSEC_YIELD_PCT has no default on purpose. It is Graham's earnings-
yield benchmark (§2 of ValueInvesting.md: earnings yield >= 2x the bond
rate) and there is no verified free live source for it (checked 2026-09-16 —
FRED's usual OECD long-term-rate series doesn't cover India). Guessing a
number here would silently decide which stocks qualify. Look it up from RBI
(https://www.rbi.org.in) or a broker terminal and set it by hand; refresh
every month or so — it does not need to be exact to the day.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

_REQUIRED = ["INDIA_10Y_GSEC_YIELD_PCT"]
missing = [k for k in _REQUIRED if not os.getenv(k)]
if missing:
    raise RuntimeError(
        f"Missing required screener .env vars: {missing}. "
        "See screener/env.example."
    )

INDIA_10Y_GSEC_YIELD_PCT = float(os.getenv("INDIA_10Y_GSEC_YIELD_PCT"))

# Value-screen thresholds (defensive-investor defaults; tune deliberately).
MIN_ROCE_PCT = float(os.getenv("MIN_ROCE_PCT", "12"))
MAX_DEBT_EQUITY = float(os.getenv("MAX_DEBT_EQUITY", "1.0"))
MIN_DIVIDEND_CONSISTENCY = float(os.getenv("MIN_DIVIDEND_CONSISTENCY", "0.7"))
MIN_DIVIDEND_YIELD_PCT = float(os.getenv("MIN_DIVIDEND_YIELD_PCT", "0.0"))
MIN_SALES_CAGR_PCT = float(os.getenv("MIN_SALES_CAGR_PCT", "0.0"))

# RSI overlay (entry timing only — see ValueInvesting.md's scoped exception).
RSI_OVERSOLD = float(os.getenv("RSI_OVERSOLD", "30"))

# Universe.
UNIVERSE_TOP_N = int(os.getenv("UNIVERSE_TOP_N", "400"))
TURNOVER_LOOKBACK_DAYS = int(os.getenv("TURNOVER_LOOKBACK_DAYS", "40"))
# ETF tier is filtered to this name substring (case-insensitive, matched
# against the bhavcopy FinInstrmNm) rather than reported in full — narrowed
# to Gold ETFs only on 2026-09-16. Empty string disables the filter.
ETF_NAME_FILTER = os.getenv("ETF_NAME_FILTER", "GOLD")

# Crypto — top N Binance USDT pairs by 24h quote volume (same "reproducible,
# no index committee" reasoning as the NSE turnover cut), replacing the old
# BTC-only check.
CRYPTO_TOP_N = int(os.getenv("CRYPTO_TOP_N", "200"))

# Telegram — separate vars from the trading bot's, so this can point at a
# different bot/chat; falls back to the bot's own vars if unset.
TELEGRAM_BOT_TOKEN = os.getenv("SCREENER_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("SCREENER_TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")
