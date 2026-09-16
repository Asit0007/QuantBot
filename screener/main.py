"""
Orchestrator: universe -> fundamentals -> value screen -> RSI overlay -> Telegram.

    python -m screener.main --dry-run --limit 20

Run from the repo root with screener/.venv active (or PYTHONPATH=. and that
venv's python directly) so the `screener.*` package imports resolve.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

from screener import config, digest, telegram_send, universe, value_screen
from screener.adapters import binance_rsi, screener_in
from screener.indicators import latest_rsi


def _rsi_for(symbol: str, price_panel) -> float | None:
    if symbol not in price_panel.index:
        return None
    return latest_rsi(price_panel.loc[symbol])


def run(top_n: int | None, limit: int | None, dry_run: bool, force_refresh: bool) -> None:
    uni = universe.build(top_n=top_n)
    as_of = uni["as_of"]
    price_panel = uni["price_panel"]

    equity_symbols = uni["equity"][:limit] if limit else uni["equity"]
    print(f"Universe as of {as_of}: {len(equity_symbols)} equities to screen "
          f"(of {len(uni['equity'])} in the top-N), {len(uni['etf'])} Gold ETFs, "
          f"{len(uni['reit'])} REITs, {len(uni['invit'])} InvITs, {len(uni['sgb'])} SGBs",
          file=sys.stderr)

    value_oversold: list[tuple[str, float, object]] = []
    value_qualified: list[tuple[str, float | None, object]] = []

    for i, symbol in enumerate(equity_symbols, 1):
        print(f"  [{i}/{len(equity_symbols)}] {symbol}", end="\r", file=sys.stderr)
        fundamentals = screener_in.fetch_fundamentals(symbol, force_refresh=force_refresh)
        if fundamentals is None:
            continue
        result = value_screen.score_instrument(fundamentals)
        if not result.qualifies:
            continue
        rsi = _rsi_for(symbol, price_panel)
        if rsi is not None and rsi < config.RSI_OVERSOLD:
            value_oversold.append((symbol, rsi, result))
        else:
            value_qualified.append((symbol, rsi, result))
    print(file=sys.stderr)

    price_only_oversold: dict[str, list[tuple[str, float, float]]] = {}
    for tier_label, symbols in (("ETF", uni["etf"]), ("REIT", uni["reit"]),
                                 ("InvIT", uni["invit"]), ("SGB", uni["sgb"])):
        items = []
        for symbol in symbols:
            rsi = _rsi_for(symbol, price_panel)
            if rsi is not None and rsi < config.RSI_OVERSOLD:
                price = price_panel.loc[symbol].dropna().iloc[-1]
                items.append((symbol, price, rsi))
        price_only_oversold[tier_label] = items

    def _crypto_progress(i: int, total: int, symbol: str) -> None:
        print(f"  [crypto {i}/{total}] {symbol}", end="\r", file=sys.stderr)

    print(f"Scanning top {config.CRYPTO_TOP_N} crypto by 24h USDT volume...", file=sys.stderr)
    crypto_snapshots = binance_rsi.fetch_top_n_snapshot(config.CRYPTO_TOP_N, progress=_crypto_progress)
    print(file=sys.stderr)
    crypto_oversold = [(s["symbol"], s["price"], s["rsi"]) for s in crypto_snapshots if s["rsi"] < config.RSI_OVERSOLD]
    if crypto_oversold:
        price_only_oversold["Crypto"] = crypto_oversold

    messages = digest.build_digest(as_of, value_oversold, value_qualified, price_only_oversold)

    if dry_run:
        for m in messages:
            print("\n" + "=" * 60)
            print(m)
    else:
        telegram_send.send(messages)
        print(f"Sent {len(messages)} message(s) to Telegram.", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top-n", type=int, default=None, help="override UNIVERSE_TOP_N")
    parser.add_argument("--limit", type=int, default=None, help="cap equities actually scraped (testing)")
    parser.add_argument("--dry-run", action="store_true", help="print instead of sending to Telegram")
    parser.add_argument("--force-refresh-fundamentals", action="store_true", help="bypass the 7-day fundamentals cache")
    args = parser.parse_args()
    run(args.top_n, args.limit, args.dry_run, args.force_refresh_fundamentals)


if __name__ == "__main__":
    main()
