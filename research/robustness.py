"""
╔══════════════════════════════════════════════════════════════════════╗
║  research/robustness.py — the five gates, for any market             ║
╠══════════════════════════════════════════════════════════════════════╣
║  Generalised from backtest/backtest_robustness.py, which was written  ║
║  for BTC. Gates 2-5 there already operated on plain P&L lists; only   ║
║  the runner was crypto-specific. This module takes a list of trades   ║
║  — dicts with `date` and `pnl` — and nothing else, so NSE, US and     ║
║  BTC results are scored by identical arithmetic.                      ║
║                                                                      ║
║  WHY GATES AT ALL. QuantBot has ~100 configurations tested against    ║
║  one 6.9-year BTC sample. At that point the multiple-comparison       ║
║  burden dominates and test #101 is LESS trustworthy than test #1:     ║
║  with enough attempts something always looks good. These gates exist  ║
║  to make "looks good" expensive.                                      ║
║                                                                      ║
║  GATE 5 IS THE ONE THAT CATCHES THINGS. On BTC it rejected every      ║
║  trend gate, sentiment filter and regime model that had already       ║
║  passed gates 1-4 — all of them turned out to be leaning on 2024.     ║
║  Its control is the entire test: dropping a year removes ~15% of a    ║
║  small sample, which alone pushes the 5th-percentile PF under 1       ║
║  regardless of WHICH trades leave, so a naive "is p5 still > 1?"      ║
║  fails ~35% of the time by chance. Each year is therefore scored      ║
║  against 400 same-size RANDOM drops instead.                          ║
║                                                                      ║
║  EXPECT FAILURES, AND RECORD THEM. A gate that never rejects is       ║
║  decoration. Anything that reduces trade count should be assumed to   ║
║  make gate 5 worse until it says otherwise.                           ║
╚══════════════════════════════════════════════════════════════════════╝
"""
from __future__ import annotations

import numpy as np
import pandas as pd

N_BOOT = 10_000
N_CONTROL = 400
RNG = np.random.default_rng(20260913)     # deterministic: a failure must reproduce


def profit_factor(pnls) -> float:
    w = sum(p for p in pnls if p > 0)
    lo = abs(sum(p for p in pnls if p <= 0))
    return (w / lo) if lo > 0 else float("inf")


def bootstrap_pf(pnls, n: int = N_BOOT):
    """Resample trades with replacement -> (p5, p50, p95, P(PF<=1)).

    The point is the 5th percentile, not the median. A strategy whose
    realised PF is 1.6 but whose p5 is 0.9 is one whose worst plausible
    ordering loses money — and the realised ordering was one draw.
    """
    arr = np.asarray(pnls, dtype=float)
    if len(arr) == 0:
        return (0.0, 0.0, 0.0, 1.0)
    idx = RNG.integers(0, len(arr), size=(n, len(arr)))
    samp = arr[idx]
    wins = np.where(samp > 0, samp, 0).sum(axis=1)
    loss = np.abs(np.where(samp <= 0, samp, 0).sum(axis=1))
    with np.errstate(divide="ignore", invalid="ignore"):
        pf = np.where(loss > 0, wins / np.maximum(loss, 1e-12), np.inf)
    return (float(np.percentile(pf, 5)), float(np.percentile(pf, 50)),
            float(np.percentile(pf, 95)), float((pf <= 1.0).mean()))


def concentration(pnls):
    """Share of gross profit from the best 1 and best 5 trades.

    The stop model failed here: its entire 6.9-year net profit was one
    trade (+$1,674 of +$1,540 — remove it and the strategy is net
    negative). A result carried by one trade is a result you cannot
    expect to repeat.
    """
    wins = sorted([p for p in pnls if p > 0], reverse=True)
    gross, net = sum(wins), sum(pnls)
    if gross <= 0:
        return 0.0, 0.0, net
    return wins[0] / gross, sum(wins[:5]) / gross, net - wins[0]


def _date(t):
    """Trade date, accepting either key.

    The engine emits `datetime`; backtest_nostop.py emits `date`. Normalise
    here rather than making every caller remember which.
    """
    return pd.Timestamp(t.get("date", t.get("datetime")))


def _year(t):
    return _date(t).year


def leave_one_year_out(trades, n_control: int = N_CONTROL):
    """Drop each year in turn, scored against same-size random drops.

    The year-level analogue of concentration(): that asks "does one TRADE
    carry this?", this asks "does one REGIME carry this?" — the failure a
    decade-scale sample with few trades per year is most exposed to.
    """
    by_year: dict[int, list[float]] = {}
    for t in trades:
        by_year.setdefault(_year(t), []).append(t["pnl"])

    allp = np.array([t["pnl"] for t in trades], dtype=float)
    rows = []
    for y in sorted(by_year):
        kept = [p for yy, ps in by_year.items() if yy != y for p in ps]
        if not kept:
            continue
        k = len(by_year[y])
        p5, _, _, _ = bootstrap_pf(kept)
        ctrl = np.empty(n_control)
        for i in range(n_control):
            idx = RNG.choice(len(allp), size=len(allp) - k, replace=False)
            ctrl[i], _, _, _ = bootstrap_pf(allp[idx], n=1500)
        rows.append({
            "year": y, "n_dropped": k, "pnl_dropped": sum(by_year[y]),
            "n_kept": len(kept), "pf_kept": profit_factor(kept), "p5_kept": p5,
            "control_mean": float(ctrl.mean()),
            "percentile": float((ctrl < p5).mean() * 100.0),
        })
    return rows


def run_gates(trades, *, label: str = "", oos_frac: float = 0.30,
              gate5_min_pct: float = 5.0, verbose: bool = True) -> dict:
    """All five gates. Returns a dict of results plus an overall verdict."""
    if not trades:
        if verbose:
            print(f"  {label}: NO TRADES — nothing to score")
        return {"label": label, "n": 0, "passed": False, "gates": {}}

    trades = sorted(trades, key=_date)
    pnls = [t["pnl"] for t in trades]
    out, g = {"label": label, "n": len(trades)}, {}

    # ── 1. in-sample / out-of-sample ─────────────────────────────────
    cut = int(len(trades) * (1 - oos_frac))
    is_p, oos_p = pnls[:cut], pnls[cut:]
    g["1_oos"] = {
        "is_pf": profit_factor(is_p), "oos_pf": profit_factor(oos_p),
        "is_net": sum(is_p), "oos_net": sum(oos_p),
        # Both halves must make money. A strategy profitable only in-sample
        # is a curve fit; one profitable only out-of-sample is luck.
        "pass": sum(is_p) > 0 and sum(oos_p) > 0,
    }

    # ── 2. bootstrap ─────────────────────────────────────────────────
    p5, p50, p95, p_le1 = bootstrap_pf(pnls)
    g["2_bootstrap"] = {"p5": p5, "p50": p50, "p95": p95, "p_pf_le_1": p_le1,
                        "pass": p5 > 1.0}

    # ── 3. concentration ─────────────────────────────────────────────
    top1, top5, without_best = concentration(pnls)
    g["3_concentration"] = {"top1_share": top1, "top5_share": top5,
                            "net_without_best": without_best,
                            "pass": without_best > 0}

    # ── 4. per-year ──────────────────────────────────────────────────
    by_year: dict[int, list[float]] = {}
    for t in trades:
        by_year.setdefault(_year(t), []).append(t["pnl"])
    pos_years = sum(1 for y in by_year if sum(by_year[y]) > 0)
    g["4_per_year"] = {
        "years": len(by_year), "positive": pos_years,
        "detail": {y: round(sum(p), 2) for y, p in sorted(by_year.items())},
        "pass": pos_years >= 0.6 * len(by_year),
    }

    # ── 5. leave-one-year-out ────────────────────────────────────────
    rows = leave_one_year_out(trades)
    worst = min(rows, key=lambda r: r["percentile"]) if rows else None
    g["5_loyo"] = {
        "rows": rows, "worst_year": worst["year"] if worst else None,
        "worst_pct": worst["percentile"] if worst else 100.0,
        # A year sitting below the 5th percentile of random same-size drops
        # carried genuinely more than an average handful of trades.
        "pass": bool(worst and worst["percentile"] >= gate5_min_pct),
    }

    out["gates"] = g
    out["passed"] = all(v["pass"] for v in g.values())

    if verbose:
        print(f"\n  ── {label} — {len(trades)} trades ──")
        print(f"  1 OOS         IS PF {g['1_oos']['is_pf']:.2f} net {g['1_oos']['is_net']:+,.0f} | "
              f"OOS PF {g['1_oos']['oos_pf']:.2f} net {g['1_oos']['oos_net']:+,.0f}"
              f"   {'PASS' if g['1_oos']['pass'] else 'FAIL'}")
        print(f"  2 bootstrap   p5 {p5:.2f}  p50 {p50:.2f}  p95 {p95:.2f}  "
              f"P(PF<=1) {p_le1:.1%}   {'PASS' if g['2_bootstrap']['pass'] else 'FAIL'}")
        print(f"  3 concentr.   best trade {top1:.0%} of gross, top5 {top5:.0%}, "
              f"net without best {without_best:+,.0f}"
              f"   {'PASS' if g['3_concentration']['pass'] else 'FAIL'}")
        print(f"  4 per-year    {pos_years}/{len(by_year)} years positive"
              f"   {'PASS' if g['4_per_year']['pass'] else 'FAIL'}")
        if worst:
            print(f"  5 LOYO        worst year {worst['year']} at {worst['percentile']:.1f}th "
                  f"pct of random drops (p5 {worst['p5_kept']:.2f} vs control "
                  f"{worst['control_mean']:.2f})   {'PASS' if g['5_loyo']['pass'] else 'FAIL'}")
        print(f"  VERDICT: {'PASS all five' if out['passed'] else 'FAILED'}")
    return out
