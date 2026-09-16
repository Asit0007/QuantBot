"""
Composes the Telegram message. Three sections, matching the "RSI as
post-screen entry timing" rule from ValueInvesting.md — nothing here lets
RSI decide what's fundamentally sound, only what's currently cheap to look at:

  1. VALUE + OVERSOLD  — cleared every automated Graham check AND is
     currently RSI-oversold. The headline section.
  2. VALUE QUALIFIED   — cleared the checks, not currently oversold. Context,
     not urgency.
  3. PRICE ONLY, OVERSOLD — ETFs/REITs/InvITs/SGBs/BTC: no fundamentals
     screen exists (or wasn't run) for these, so they NEVER appear in 1 or 2.
     Labelled plainly as price-only so it's never mistaken for a value call.

Every qualifying instrument in section 1/2 shows which checks it passed —
the message argues its case the way the ValueInvesting doctrine expects,
rather than asserting a ticker and asking for trust.
"""

from __future__ import annotations

import datetime as dt

from screener import config

TELEGRAM_MAX_CHARS = 4096
MANUAL_REVIEW_NOTE = (
    "⚠️ None of this checks management quality (Graham parameter 2) "
    "— that's not automatable. Read the annual report yourself before acting."
)


def _fmt_check(check) -> str:
    icon = "✅" if check.passed else ("⚪" if check.passed is None else "❌")
    return f"  {icon} {check.label}: {check.detail}"


def _instrument_block(symbol: str, rsi: float | None, result=None) -> str:
    lines = [f"<b>{symbol}</b>" + (f" (RSI {rsi:.0f})" if rsi is not None else "")]
    if result is not None:
        lines += [_fmt_check(c) for c in result.checks]
    return "\n".join(lines)


def build_digest(
    as_of: dt.date,
    value_oversold: list[tuple[str, float, object]],
    value_qualified: list[tuple[str, float | None, object]],
    price_only_oversold: dict[str, list[tuple[str, float, float]]],
) -> list[str]:
    """Returns a list of message chunks, each under Telegram's char limit."""
    sections: list[str] = []

    header = (
        f"\U0001f4c8 <b>Value + RSI digest</b> — {as_of:%d %b %Y}\n"
        f"G-Sec benchmark: {config.INDIA_10Y_GSEC_YIELD_PCT}% | "
        f"RSI oversold &lt; {config.RSI_OVERSOLD:.0f}"
    )
    sections.append(header)

    if value_oversold:
        block = ["\U0001f7e2 <b>VALUE + OVERSOLD</b> — qualified AND cheap right now"]
        block += [_instrument_block(sym, rsi, res) for sym, rsi, res in value_oversold]
        sections.append("\n\n".join(block))
    else:
        sections.append("\U0001f7e2 <b>VALUE + OVERSOLD</b>: none today.")

    if value_qualified:
        block = ["\U0001f535 <b>VALUE QUALIFIED</b> — not currently oversold"]
        block += [f"  {sym}" + (f" (RSI {rsi:.0f})" if rsi is not None else "") for sym, rsi, _ in value_qualified]
        sections.append("\n".join(block))

    any_price_only = any(v for v in price_only_oversold.values())
    if any_price_only:
        block = ["⚪ <b>PRICE ONLY, OVERSOLD</b> — no value screen applies (see below)"]
        for tier, items in price_only_oversold.items():
            if not items:
                continue
            block.append(f"<i>{tier}</i>")
            block += [f"  {sym}: {price:,.2f} (RSI {rsi:.0f})" for sym, price, rsi in items]
        sections.append("\n".join(block))

    sections.append(MANUAL_REVIEW_NOTE)

    return _chunk(sections)


def _chunk(sections: list[str]) -> list[str]:
    messages: list[str] = []
    current = ""
    for section in sections:
        candidate = f"{current}\n\n{section}" if current else section
        if len(candidate) > TELEGRAM_MAX_CHARS:
            if current:
                messages.append(current)
            current = section
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages
