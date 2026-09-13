"""
╔══════════════════════════════════════════════════════════════════════╗
║  corporate_actions.py — repair split/bonus discontinuities            ║
╠══════════════════════════════════════════════════════════════════════╣
║  NSE bhavcopy publishes RAW traded prices. A 1:10 split appears as a  ║
║  −90% day that never happened. Left alone this poisons everything:    ║
║  returns, indicators (a "crash" fires RSI divergence and a volume     ║
║  spike simultaneously), and P&L for any position held through it.     ║
║                                                                      ║
║  WHY A HEURISTIC AT ALL — I checked for the honest answer first.      ║
║  bhavcopy carries a PREVCLOSE column, and if NSE adjusted it on a     ║
║  split day we would get the exact factor from the exchange and need   ║
║  no guessing. It does not: on NIFTYBEES 2019-12-19, PREVCLOSE reads   ║
║  1292.54, exactly the prior day's raw close. So detection it is.      ║
║                                                                      ║
║  USE THE OPEN, NOT THE CLOSE. Same day: PREVCLOSE/OPEN = 1292.54 /    ║
║  129.20 = 10.004, while PREVCLOSE/CLOSE = 9.927. The open is the      ║
║  first print after the adjustment and carries almost none of the      ║
║  day's real trading; the close carries all of it. The open therefore  ║
║  gives a far cleaner ratio to snap to.                                ║
║                                                                      ║
║  TWO GUARDS AGAINST "REPAIRING" A REAL CRASH:                         ║
║  1. The move must exceed the exchange circuit limit (~20%; we use     ║
║     25%). A price cannot legally move further than that in a session, ║
║     so anything beyond it is an adjustment, a circuit revision, or    ║
║     bad data — never ordinary trading.                                ║
║  2. The ratio must sit within tolerance of a SIMPLE rational (2/1,    ║
║     5/1, 10/1, 3/2, 11/10 …). Splits and bonuses produce exact        ║
║     fractions by construction; a genuine collapse does not land on    ║
║     10.000 by coincidence.                                            ║
║                                                                      ║
║  Both must hold. A −60% move that is not near a clean fraction is     ║
║  left ALONE and flagged — being wrong in that direction merely keeps  ║
║  a real loss, while wrongly "repairing" one would invent a gain.      ║
╚══════════════════════════════════════════════════════════════════════╝
"""
from __future__ import annotations

from fractions import Fraction

import numpy as np
import pandas as pd

CIRCUIT = 0.25          # beyond any NSE circuit band
TOL = 0.01              # 1%. NIFTYBEES's true 10:1 matched to 0.04%.
MAX_DENOM = 10          # see _clean_ratio

# Plausible corporate-action ratios. A bonus of a:b (a new shares per b held)
# divides the price by (a+b)/b; a split divides it by the split factor.
#
# FIRST VERSION OF THIS WAS TOO LOOSE AND THAT MATTERED. With denominators up
# to 20 and 2% tolerance, the audit "explained" 4,326 events and flagged ZERO
# as unexplained — which is not reassurance, it is the tell. Ratios like 1.474
# (28/19) and 1.389 (25/18) are not corporate actions, they are real price
# moves being snapped onto meaningless fractions. Adjusting those would INVENT
# price history, which is strictly worse than leaving a split unrepaired: an
# unrepaired split is a visible outlier, a fabricated adjustment is invisible.
#
# So: an explicit whitelist, built from (a+b)/b for small a,b plus the face-value
# split factors NSE actually uses. Anything else is reported, never adjusted.
_BONUS = {(a + b) / b for b in range(1, 11) for a in range(1, 21)}
_SPLITS = {2, 2.5, 3, 4, 5, 10, 20, 25, 50, 100}
PLAUSIBLE = sorted({round(x, 6) for x in (_BONUS | _SPLITS) if 1.05 <= x <= 100})


def _clean_ratio(r: float, tol: float = TOL, max_denom: int = MAX_DENOM):
    """Nearest PLAUSIBLE corporate-action ratio to r, or None.

    Matches against the explicit whitelist rather than "any fraction with a
    small denominator" — see the note above PLAUSIBLE for why that
    distinction is load-bearing.
    """
    if not np.isfinite(r) or r <= 1.0:
        return None
    best = min(PLAUSIBLE, key=lambda p: abs(p - r))
    return best if abs(best - r) / r <= tol else None


def detect(df: pd.DataFrame) -> list[dict]:
    """Find adjustment events in one symbol's OHLCV frame (date-indexed).

    Returns dicts of {date, ratio, kind}. `ratio` > 1 means the price was
    divided (split/bonus); < 1 means multiplied (reverse split).
    """
    if len(df) < 2:
        return []
    prev_close = df["close"].shift(1)
    # Ratio from the OPEN — see the module docstring.
    ratio = prev_close / df["open"].replace(0, np.nan)
    move = df["open"] / prev_close - 1.0

    out = []
    for d, r, m in zip(df.index, ratio, move):
        if not np.isfinite(r) or abs(m) < CIRCUIT:
            continue
        clean = _clean_ratio(r if r > 1 else 1 / r)
        if clean is None:
            out.append({"date": d, "ratio": float(r), "kind": "unexplained"})
            continue
        out.append({"date": d,
                    "ratio": clean if r > 1 else 1 / clean,
                    "kind": "split" if r > 1 else "reverse_split"})
    return out


def adjust(df: pd.DataFrame, events: list[dict] | None = None) -> pd.DataFrame:
    """Back-adjust prices and volume for detected splits.

    Convention: the MOST RECENT price is left untouched and history is
    scaled to it. That is the only choice that keeps the series usable
    live — adjusting forwards would silently change today's price, so a
    stop or a position size computed from the series would not match the
    order actually sent to the exchange.

    `unexplained` events are deliberately NOT adjusted. They are the ones
    that need a human.
    """
    events = detect(df) if events is None else events
    real = [e for e in events if e["kind"] != "unexplained"]
    if not real:
        return df.copy()

    out = df.copy()
    # Walk backwards, compounding. Each event scales everything BEFORE it.
    for e in sorted(real, key=lambda x: x["date"], reverse=True):
        mask = out.index < e["date"]
        for c in ("open", "high", "low", "close"):
            if c in out.columns:
                out.loc[mask, c] = out.loc[mask, c] / e["ratio"]
        if "volume" in out.columns:
            out.loc[mask, "volume"] = out.loc[mask, "volume"] * e["ratio"]
    return out


def audit(panel: pd.DataFrame) -> pd.DataFrame:
    """Scan a long-form panel; one row per detected event."""
    rows = []
    for sym, g in panel.groupby("symbol", sort=False):
        g = g.set_index("date").sort_index()
        for e in detect(g):
            rows.append({"symbol": sym, **e})
    return pd.DataFrame(rows)
