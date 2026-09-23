"""
Screener.in adapter — the fundamentals the value screen runs on.

Screener.in has no official public API; this parses the same HTML a browser
gets, unauthenticated (verified reachable 2026-09-16, HTTP 200, no login
wall for the data used here). That means:

- It can break if Screener.in changes their markup. Nothing here should be
  trusted blindly — `fetch_fundamentals` returns None rather than a
  half-filled dict on any parse failure, so a broken page fails closed
  (excluded from the screen) instead of silently scoring wrong.
- It must be scraped politely: SLEEP_SECONDS between live requests, and
  results are cached to disk for CACHE_TTL_DAYS so a daily run only re-fetches
  what's actually stale. Fundamentals change on a quarterly cadence; there is
  no reason to hit the site once per symbol per day.
- Some companies (mostly banks/NBFCs/insurers) have no consolidated
  financials — fetch_fundamentals falls back to the standalone page on 404.

Page structure (verified against RELIANCE 2026-09-16):
- `#top-ratios li` — Market Cap, Current Price, Stock P/E, Book Value,
  Dividend Yield, ROCE, ROE, Face Value as name/value pairs.
- `.ranges-table` (four of them) — Compounded Sales Growth, Compounded
  Profit Growth, Stock Price CAGR, Return on Equity, each as a small
  10Y/5Y/3Y/TTM (or Last Year) table.
- `#profit-loss table.data-table` — annual P&L rows incl. "Dividend Payout %"
  with one column per year, headers carrying `data-date-key="YYYY-MM-DD"`.
- `#balance-sheet table.data-table` — annual rows incl. "Borrowings",
  "Equity Capital", "Reserves"; last column is the most recent year.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "fundamentals"
CACHE_TTL_DAYS = 7
SLEEP_SECONDS = 1.5  # politeness delay between *live* requests only

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}
URL_CONSOLIDATED = "https://www.screener.in/company/{symbol}/consolidated/"
URL_STANDALONE = "https://www.screener.in/company/{symbol}/"


def _num(text: str | None) -> float | None:
    """'1,23,456.7%' / '₹ 82.3' / '-' / '' -> float or None."""
    if text is None:
        return None
    cleaned = re.sub(r"[,₹%\s]", "", text)
    if cleaned in ("", "-"):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_top_ratios(soup: BeautifulSoup) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    ul = soup.find("ul", id="top-ratios")
    if ul is None:
        return out
    for li in ul.find_all("li"):
        name_el = li.find("span", class_="name")
        value_el = li.find("span", class_="number")
        if name_el is None or value_el is None:
            continue
        out[name_el.get_text(strip=True)] = _num(value_el.get_text(strip=True))
    return out


def _parse_ranges_tables(soup: BeautifulSoup) -> dict[str, dict[str, float | None]]:
    out: dict[str, dict[str, float | None]] = {}
    for table in soup.find_all("table", class_="ranges-table"):
        title_el = table.find("th")
        if title_el is None:
            continue
        title = title_el.get_text(strip=True)
        periods: dict[str, float | None] = {}
        for row in table.find_all("tr")[1:]:
            cells = row.find_all("td")
            if len(cells) != 2:
                continue
            label = cells[0].get_text(strip=True).rstrip(":")
            periods[label] = _num(cells[1].get_text(strip=True))
        out[title] = periods
    return out


def _year_headers(table) -> list[str]:
    years = []
    for th in table.find_all("th"):
        key = th.get("data-date-key")
        if key:
            years.append(key[:4])
    return years


def _find_row(table, label_contains: str) -> list[str] | None:
    for row in table.find_all("tr"):
        label_cell = row.find("td", class_="text")
        if label_cell is None:
            continue
        label = label_cell.get_text(strip=True)
        if label_contains.lower() in label.lower():
            return [c.get_text(strip=True) for c in row.find_all("td")[1:]]
    return None


def _parse_section_table(soup: BeautifulSoup, section_id: str):
    section = soup.find("section", id=section_id)
    if section is None:
        return None
    return section.find("table", class_="data-table")


def _parse_page(symbol: str, source_url: str, html: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    top = _parse_top_ratios(soup)
    if not top.get("Stock P/E") and not top.get("Market Cap"):
        # Page loaded but the ratios grid is empty/absent — treat as a parse
        # failure rather than scoring an instrument on nothing.
        return None

    ranges = _parse_ranges_tables(soup)

    dividend_payout_by_year: dict[str, float | None] = {}
    pl_table = _parse_section_table(soup, "profit-loss")
    if pl_table is not None:
        years = _year_headers(pl_table)
        row = _find_row(pl_table, "Dividend Payout")
        if row:
            dividend_payout_by_year = dict(zip(years, (_num(v) for v in row)))

    borrowings_cr = equity_capital_cr = reserves_cr = None
    bs_table = _parse_section_table(soup, "balance-sheet")
    if bs_table is not None:
        b_row = _find_row(bs_table, "Borrowings")
        e_row = _find_row(bs_table, "Equity Capital")
        r_row = _find_row(bs_table, "Reserves")
        borrowings_cr = next((v for v in reversed([_num(x) for x in (b_row or [])]) if v is not None), None)
        equity_capital_cr = next((v for v in reversed([_num(x) for x in (e_row or [])]) if v is not None), None)
        reserves_cr = next((v for v in reversed([_num(x) for x in (r_row or [])]) if v is not None), None)

    return {
        "symbol": symbol,
        "source_url": source_url,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "market_cap_cr": top.get("Market Cap"),
        "current_price": top.get("Current Price"),
        "pe": top.get("Stock P/E"),
        "book_value": top.get("Book Value"),
        "dividend_yield_pct": top.get("Dividend Yield"),
        "roce_pct": top.get("ROCE"),
        "roe_pct": top.get("ROE"),
        "face_value": top.get("Face Value"),
        "sales_cagr": ranges.get("Compounded Sales Growth", {}),
        "profit_cagr": ranges.get("Compounded Profit Growth", {}),
        "roe_multi_year": ranges.get("Return on Equity", {}),
        "dividend_payout_by_year": dividend_payout_by_year,
        "borrowings_cr": borrowings_cr,
        "equity_capital_cr": equity_capital_cr,
        "reserves_cr": reserves_cr,
    }


def _cache_path(symbol: str) -> Path:
    return CACHE_DIR / f"{symbol}.json"


def _cached(symbol: str) -> dict | None:
    path = _cache_path(symbol)
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    fetched_at = datetime.fromisoformat(data["fetched_at"])
    if datetime.now(timezone.utc) - fetched_at > timedelta(days=CACHE_TTL_DAYS):
        return None
    return data


def fetch_fundamentals(symbol: str, force_refresh: bool = False) -> dict | None:
    """Fundamentals for one NSE symbol, consolidated first then standalone.

    Returns None if Screener.in has no page for the symbol, or the page
    couldn't be parsed — never a partially-filled dict.
    """
    if not force_refresh:
        cached = _cached(symbol)
        if cached is not None:
            return cached

    for url_template in (URL_CONSOLIDATED, URL_STANDALONE):
        url = url_template.format(symbol=symbol)
        resp = requests.get(url, headers=HEADERS, timeout=20)
        time.sleep(SLEEP_SECONDS)
        if resp.status_code != 200:
            continue
        parsed = _parse_page(symbol, url, resp.text)
        if parsed is not None:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            _cache_path(symbol).write_text(json.dumps(parsed, indent=2))
            return parsed

    return None
