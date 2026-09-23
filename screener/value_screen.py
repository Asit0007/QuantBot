"""
The Graham-Buffett-Munger five-parameter test, operationalized against what
Screener.in actually publishes. Doctrine lives in ValueInvesting.md; this is
its mechanical implementation for NSE equities/REITs/InvITs — see that file
before changing a threshold's meaning, not just its value.

Mapping (ValueInvesting.md §2's five-parameter capitalization factor):
  1. Long-term growth      -> Compounded Sales Growth, longest period Screener
                               has (10Y preferred, falls back to 5Y/3Y).
  2. Quality of management -> NOT automated. No scrape gives you integrity.
                               Every result carries manual_review_required=True
                               and the digest says so — never silently assumed.
  3. Financial strength    -> Debt/Equity = Borrowings / (Equity Capital + Reserves),
                               latest reported year.
  4. Dividend record       -> fraction of years with Dividend Payout % > 0,
                               out of years where the row actually has data.
  5. Current dividend rate -> today's Dividend Yield.
Plus the mandatory earnings-yield gate (§2): 1/PE >= 2x the bond rate.
Plus ROIC (§4): ROCE as the proxy. Not meaningful for banks/NBFCs/insurers
(leverage is their business) — treat a ROCE fail on a financial-sector name
as inconclusive, not disqualifying, until this gets a sector-aware rule.

A name must clear ALL of the automated checks to "qualify" (the defensive-
investor posture in §3: safety first, no partial credit).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from screener import config


@dataclass
class Check:
    label: str
    passed: bool | None  # None = data unavailable, not evaluated
    detail: str


@dataclass
class ScreenResult:
    symbol: str
    qualifies: bool
    checks: list[Check] = field(default_factory=list)
    manual_review_required: bool = True  # quality-of-management, always


def _dividend_consistency(dividend_payout_by_year: dict[str, float | None]) -> tuple[float, int]:
    years = [v for v in dividend_payout_by_year.values() if v is not None]
    if not years:
        return 0.0, 0
    paid = sum(1 for v in years if v > 0)
    return paid / len(years), len(years)


def _best_sales_cagr(sales_cagr: dict[str, float | None]) -> tuple[float | None, str]:
    for period in ("10 Years", "5 Years", "3 Years"):
        value = sales_cagr.get(period)
        if value is not None:
            return value, period
    return None, "n/a"


def score_instrument(fundamentals: dict) -> ScreenResult:
    symbol = fundamentals["symbol"]
    checks: list[Check] = []

    pe = fundamentals.get("pe")
    if pe and pe > 0:
        earnings_yield = 100.0 / pe
        min_required = 2 * config.INDIA_10Y_GSEC_YIELD_PCT
        passed = earnings_yield >= min_required
        checks.append(Check(
            "earnings_yield",
            passed,
            f"{earnings_yield:.1f}% vs {min_required:.1f}% required (2x {config.INDIA_10Y_GSEC_YIELD_PCT}% G-Sec)",
        ))
    else:
        checks.append(Check("earnings_yield", False, "no P/E (loss-making or unavailable)"))

    roce = fundamentals.get("roce_pct")
    if roce is not None:
        checks.append(Check("roic_roce", roce >= config.MIN_ROCE_PCT, f"{roce:.1f}% vs {config.MIN_ROCE_PCT:.1f}% min"))
    else:
        checks.append(Check("roic_roce", None, "ROCE unavailable"))

    borrowings = fundamentals.get("borrowings_cr")
    equity_capital = fundamentals.get("equity_capital_cr")
    reserves = fundamentals.get("reserves_cr")
    if borrowings is not None and equity_capital is not None and reserves is not None and (equity_capital + reserves) > 0:
        debt_equity = borrowings / (equity_capital + reserves)
        checks.append(Check(
            "financial_strength",
            debt_equity <= config.MAX_DEBT_EQUITY,
            f"D/E {debt_equity:.2f} vs {config.MAX_DEBT_EQUITY:.2f} max",
        ))
    else:
        checks.append(Check("financial_strength", None, "balance sheet data unavailable"))

    consistency, n_years = _dividend_consistency(fundamentals.get("dividend_payout_by_year", {}))
    if n_years > 0:
        checks.append(Check(
            "dividend_record",
            consistency >= config.MIN_DIVIDEND_CONSISTENCY,
            f"paid in {consistency:.0%} of {n_years} reported years vs {config.MIN_DIVIDEND_CONSISTENCY:.0%} min",
        ))
    else:
        checks.append(Check("dividend_record", None, "no dividend history available"))

    div_yield = fundamentals.get("dividend_yield_pct")
    if div_yield is not None:
        checks.append(Check(
            "current_dividend_rate",
            div_yield >= config.MIN_DIVIDEND_YIELD_PCT,
            f"{div_yield:.2f}% vs {config.MIN_DIVIDEND_YIELD_PCT:.2f}% min",
        ))
    else:
        checks.append(Check("current_dividend_rate", None, "dividend yield unavailable"))

    cagr, period = _best_sales_cagr(fundamentals.get("sales_cagr", {}))
    if cagr is not None:
        checks.append(Check(
            "long_term_growth",
            cagr >= config.MIN_SALES_CAGR_PCT,
            f"sales CAGR ({period}) {cagr:.1f}% vs {config.MIN_SALES_CAGR_PCT:.1f}% min",
        ))
    else:
        checks.append(Check("long_term_growth", None, "no growth history available"))

    # Qualifies only if every evaluated check passed AND nothing was
    # left unevaluated — missing data is a fail-closed, not a free pass.
    qualifies = all(c.passed for c in checks)

    return ScreenResult(symbol=symbol, qualifies=qualifies, checks=checks)
