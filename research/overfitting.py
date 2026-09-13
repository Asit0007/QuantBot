"""
╔══════════════════════════════════════════════════════════════════════╗
║  research/overfitting.py — gates 6-8: how much did SEARCHING cost?   ║
╠══════════════════════════════════════════════════════════════════════╣
║  Gates 1-5 ask "is this result real?" of ONE config in isolation.     ║
║  They cannot see how many configs you tried to get there, and that    ║
║  is the dominant error in this project: ~100 configs against one      ║
║  6.9-year BTC sample, after which test #101 is LESS trustworthy than  ║
║  test #1. These three gates price that in.                            ║
║                                                                      ║
║  This is the disciplined version of "run permutations and             ║
║  combinations". Combinations are used to VALIDATE, never to SEARCH:   ║
║  a combinatorial sweep over parameters adds trials to a sample that   ║
║  already cannot support the ones it has, and will always find         ║
║  something. CPCV uses the same combinatorics to produce a             ║
║  DISTRIBUTION of out-of-sample results instead of one lucky path.     ║
║                                                                      ║
║  6. DEFLATED SHARPE (Bailey & Lopez de Prado 2014) — discounts the    ║
║     observed Sharpe by the number of trials it took to find it, and   ║
║     by skew/kurtosis. Non-normal returns inflate naive Sharpe, and a  ║
║     trading strategy's returns are never normal.                      ║
║  7. PBO via CSCV — the probability that the config which looked best  ║
║     in-sample ranks below median out-of-sample. Needs a SET of        ║
║     configs; it is meaningless for one, and says so rather than       ║
║     returning a number.                                               ║
║  8. CPCV — combinatorial purged cross-validation. Every combination   ║
║     of held-out blocks, with PURGING (drop training trades whose      ║
║     holding period overlaps the test window) and an EMBARGO after it. ║
║     Without purging, a position open across the boundary leaks its    ║
║     outcome into training and every number is optimistic.             ║
╚══════════════════════════════════════════════════════════════════════╝
"""
from __future__ import annotations

from itertools import combinations

import numpy as np
import pandas as pd

# stdlib, not scipy. scipy.stats imports in 0.75s of CPU but ~61s of WALL
# CLOCK on this machine at 1% CPU — macOS scanning its compiled extensions
# under ~/Documents, the same TCC-guarded path that already forces JobPipe's
# scheduled job through a signed launcher app. NormalDist gives the exact
# same cdf/ppf with zero import cost, and these gates need nothing else from
# scipy.
from statistics import NormalDist

_N = NormalDist()
EULER = 0.5772156649015329


def _pf(p) -> float:
    p = np.asarray(p, dtype=float)
    w = p[p > 0].sum()
    lo = abs(p[p <= 0].sum())
    return float(w / lo) if lo > 0 else float("inf")


def _dates(trades):
    return pd.to_datetime([t.get("date", t.get("datetime")) for t in trades])


# ── Gate 6 ───────────────────────────────────────────────────────────
def deflated_sharpe(pnls, n_trials: int, sr_variance: float | None = None) -> dict:
    """Probability the true Sharpe is > 0, after paying for the search.

    n_trials is the honest count of configurations tried on this data —
    including the ones you discarded, and including other people's. Under-
    reporting it is the whole game; it is the one input nobody wants to
    fill in truthfully.
    """
    r = np.asarray(pnls, dtype=float)
    n = len(r)
    if n < 3 or r.std(ddof=1) == 0:
        return {"sr": 0.0, "sr0": 0.0, "dsr": 0.0, "n": n, "trials": n_trials,
                "pass": False, "note": "too few trades"}

    sr = r.mean() / r.std(ddof=1)
    g3 = float(pd.Series(r).skew())
    g4 = float(pd.Series(r).kurtosis()) + 3.0          # pandas gives excess

    # Expected MAXIMUM Sharpe under the null across n_trials independent
    # trials. Even with zero real edge, the best of many trials looks good —
    # this is the bar that "good" has to clear.
    v = sr_variance if sr_variance is not None else (1.0 / max(n - 1, 1))
    k = max(n_trials, 2)
    sr0 = np.sqrt(v) * ((1 - EULER) * _N.inv_cdf(1 - 1.0 / k)
                        + EULER * _N.inv_cdf(1 - 1.0 / (k * np.e)))

    denom = np.sqrt(max(1 - g3 * sr + ((g4 - 1) / 4.0) * sr ** 2, 1e-12))
    dsr = float(_N.cdf((sr - sr0) * np.sqrt(n - 1) / denom))
    return {"sr": float(sr), "sr0": float(sr0), "dsr": dsr, "n": n,
            "trials": k, "skew": g3, "kurt": g4, "pass": dsr > 0.95}


# ── Gate 7 ───────────────────────────────────────────────────────────
def pbo(config_pnls: dict[str, list], n_splits: int = 10) -> dict:
    """Probability of Backtest Overfitting, via CSCV.

    Requires >= 2 configurations: it measures whether picking the
    in-sample winner generalises, and with one candidate there is no
    choice to get wrong. Returns a refusal rather than a number.
    """
    names = [k for k, v in config_pnls.items() if len(v) >= n_splits]
    if len(names) < 2:
        return {"pass": None, "pbo": None,
                "note": f"needs >=2 configs with >={n_splits} trades; "
                        f"got {len(names)}"}

    m = min(len(config_pnls[k]) for k in names)
    M = np.column_stack([np.asarray(config_pnls[k][:m], float) for k in names])
    blocks = np.array_split(np.arange(m), n_splits)

    below = tot = 0
    for combo in combinations(range(n_splits), n_splits // 2):
        tr = np.concatenate([blocks[i] for i in combo])
        te = np.concatenate([blocks[i] for i in range(n_splits) if i not in combo])
        best = int(np.argmax([_pf(M[tr, j]) for j in range(len(names))]))
        oos = np.array([_pf(M[te, j]) for j in range(len(names))])
        rank = (oos < oos[best]).sum() / max(len(names) - 1, 1)
        below += int(rank < 0.5)
        tot += 1
    p = below / tot
    return {"pbo": p, "n_configs": len(names), "n_combos": tot,
            "pass": p < 0.5}


# ── Gate 8 ───────────────────────────────────────────────────────────
def cpcv(trades, n_blocks: int = 8, k_test: int = 2,
         embargo_days: int = 5) -> dict:
    """Combinatorial purged CV — distribution of out-of-sample PF.

    PURGING is the part that matters. A trade held across a block boundary
    knows the test window's outcome, so leaving it in training leaks the
    answer. Any trade whose [entry, exit] overlaps a test block — plus an
    embargo after it — is dropped from training.
    """
    if len(trades) < n_blocks * 2:
        return {"pass": None, "note": f"needs >= {n_blocks*2} trades, "
                                      f"got {len(trades)}"}
    tr = sorted(trades, key=lambda t: pd.Timestamp(t.get("date", t.get("datetime"))))
    ex = _dates(tr)
    hold = np.array([t.get("hold_candles", 0) for t in tr])
    en = ex - pd.to_timedelta(hold, unit="D")

    blocks = np.array_split(np.arange(len(tr)), n_blocks)
    pnl = np.array([t["pnl"] for t in tr], float)

    oos, ins = [], []
    for combo in combinations(range(n_blocks), k_test):
        te = np.concatenate([blocks[i] for i in combo])
        t0, t1 = en[te].min(), ex[te].max() + pd.Timedelta(days=embargo_days)
        keep = [i for i in range(len(tr))
                if i not in set(te) and not (en[i] <= t1 and ex[i] >= t0)]
        if len(keep) < 5 or len(te) < 3:
            continue
        ins.append(_pf(pnl[keep]))
        oos.append(_pf(pnl[te]))
    if not oos:
        return {"pass": None, "note": "no usable splits after purging"}
    oos = np.array([x for x in oos if np.isfinite(x)])
    return {"n_splits": len(oos), "oos_pf_median": float(np.median(oos)),
            "oos_pf_p25": float(np.percentile(oos, 25)),
            "frac_oos_above_1": float((oos > 1).mean()),
            "is_pf_median": float(np.median(ins)),
            # Majority of purged out-of-sample folds must make money.
            "pass": float((oos > 1).mean()) > 0.5}


def run_extended(trades, *, n_trials: int, config_pnls: dict | None = None,
                 label: str = "", verbose: bool = True) -> dict:
    pnls = [t["pnl"] for t in trades]
    g6 = deflated_sharpe(pnls, n_trials)
    g7 = pbo(config_pnls or {}, ) if config_pnls else {"pass": None,
             "note": "needs >=2 configs; only one supplied"}
    g8 = cpcv(trades)
    if verbose:
        print(f"\n  ── {label}: gates 6-8 (search cost) ──")
        print(f"  6 deflated SR  SR {g6['sr']:.3f} vs null-max SR0 {g6['sr0']:.3f} "
              f"over {g6['trials']} trials -> DSR {g6['dsr']:.3f}"
              f"   {'PASS' if g6['pass'] else 'FAIL'}")
        if g7.get("pbo") is None:
            print(f"  7 PBO          {g7.get('note')}   n/a")
        else:
            print(f"  7 PBO          {g7['pbo']:.1%} of splits pick a winner that "
                  f"lands below median OOS   {'PASS' if g7['pass'] else 'FAIL'}")
        if g8.get("pass") is None:
            print(f"  8 CPCV         {g8.get('note')}   n/a")
        else:
            print(f"  8 CPCV         {g8['n_splits']} purged splits, median OOS PF "
                  f"{g8['oos_pf_median']:.2f}, {g8['frac_oos_above_1']:.0%} above 1"
                  f"   {'PASS' if g8['pass'] else 'FAIL'}")
    return {"6_dsr": g6, "7_pbo": g7, "8_cpcv": g8}
