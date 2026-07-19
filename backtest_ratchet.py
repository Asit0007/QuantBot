"""
backtest_ratchet.py — Corpus Ratchet Frequency Comparison at 5x Leverage
════════════════════════════════════════════════════════════════════════════
Signal is IDENTICAL to bot.py / backtest_leverage.py.
Leverage is FIXED at 5x with the 5x-validated ATR multipliers.

WHAT THIS TESTS:
  How often should the corpus be rebalanced (ratcheted)?

  Ratchet UP every N trades (if net PnL positive over the cycle)
    → locks in profits into a larger position size going forward
  Ratchet DOWN after N *consecutive* losses
    → shrinks position size when things go wrong

  Faster ratchet = profits compound sooner, but losses also shrink
  the corpus sooner.  Slower ratchet = smoother sizing, but slower
  to capture growth.

VARIANTS TESTED:
  Label          ratchet_up  ratchet_down  Notes
  ─────────────────────────────────────────────────────────────────
  10/10          10          10            backtest_leverage.py baseline
  5/5             5           5            intermediate
  2/2 (proposed)  2           2            proposed improvement
  2/10 (asymm)    2          10            grow fast, shrink slowly

USAGE:
  python backtest_ratchet.py

REFERENCE (from backtest_leverage.py, 5x, ratchet 10/10):
  $9,347  +90.7%/yr  PF 1.60  DD 184.2%  WR 36.7%  2022: +$387
════════════════════════════════════════════════════════════════════════════
"""

import ccxt
import pandas as pd
import numpy as np
from ta.momentum import RSIIndicator
from ta.trend import MACD as MACDIndicator
from ta.volatility import AverageTrueRange
import sys, math, time
from datetime import datetime, timezone, timedelta
from corpus_manager import CorpusManager

# ─── Fixed framework (identical across all variants) ──────────────
SYMBOL           = "BTC/USDT"
INITIAL_BALANCE  = 100.0
BASE_RISK        = 0.10
FEE_RATE         = 0.0005
DCA_BASE         = 10.0
DCA_GROWTH       = 0.10
DCA_DAY          = 10
START_YEAR       = 2019
FLAT_CB_TRIGGER  = 5
FLAT_CB_HOURS    = 48
CANDLE_MINUTES   = 15
CANDLES_PER_YEAR = int(365.25 * 24 * 60 / CANDLE_MINUTES)

# ─── Fixed at 5x (validated winner from backtest_leverage.py) ─────
LEVERAGE       = 5
LONG_ATR_MULT  = 8.0    # 2.0 * (20/5)
SHORT_ATR_MULT = 6.0    # 1.5 * (20/5)

# ─── Signal parameters — IDENTICAL to bot.py, never vary these ───
RSI_LEN         = 14
MACD_FAST       = 12
MACD_SLOW       = 26
MACD_SIGNAL_WIN = 9
VOL_SMA_PERIOD  = 20
VOL_MULT        = 2.0
ATR_PERIOD      = 14
DIV_WINDOW      = 5
DIV_SHIFT       = 5
DIV_MEMORY      = 3

# ─── Ratchet variants ─────────────────────────────────────────────
RATCHET_TIERS = [
    # (label,              ratchet_up, ratchet_down)
    ("10/10 (baseline)",   10,         10),
    ("5/5",                 5,          5),
    ("2/2 (proposed)",      2,          2),
    ("2/10 (asymm)",        2,         10),
]

# Reference from backtest_leverage.py at 5x with ratchet 10/10
REFERENCE = {
    'final': 9347.0, 'annual': 90.7, 'dd': 184.2,
    'pf': 1.60, 'wr': 36.7, 'rdd': 0.49,
}


# ─── Data fetching ────────────────────────────────────────────────
def fetch_ohlcv(exchange, symbol: str, tf: str = "15m",
                since_str: str = "2019-09-01") -> pd.DataFrame:
    since     = exchange.parse8601(f'{since_str}T00:00:00Z')
    all_ohlcv = []
    reqs      = 0
    while True:
        try:
            chunk = exchange.fetch_ohlcv(symbol, tf, since=since, limit=1000)
            if not chunk:
                break
            all_ohlcv.extend(chunk)
            since = chunk[-1][0] + 1
            reqs += 1
            sys.stdout.write(
                f"\r    {len(all_ohlcv):>9,} candles "
                f"({datetime.fromtimestamp(chunk[-1][0]/1000).strftime('%Y-%m')}) "
                f"[{reqs} req]  "
            )
            sys.stdout.flush()
            if len(chunk) < 1000:
                break
            time.sleep(0.05)
        except Exception as e:
            print(f"\n    ⚠️  {e}")
            time.sleep(2)
            break

    df = pd.DataFrame(all_ohlcv,
                      columns=['timestamp','open','high','low','close','volume'])
    df.drop_duplicates(subset='timestamp', inplace=True)
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
    df.set_index('timestamp', inplace=True)
    return df.astype(float)


# ─── Indicators — EXACT replica of bot.py's compute_indicators() ──
def build_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["rsi"] = RSIIndicator(close=df["close"], window=RSI_LEN).rsi()

    macd = MACDIndicator(close=df["close"], window_fast=MACD_FAST,
                         window_slow=MACD_SLOW, window_sign=MACD_SIGNAL_WIN)
    df["macd_line"]   = macd.macd()
    df["signal_line"] = macd.macd_signal()

    df["avg_vol"]  = df["volume"].rolling(VOL_SMA_PERIOD).mean()
    df["high_vol"] = df["volume"] > (VOL_MULT * df["avg_vol"])

    df["atr"] = AverageTrueRange(
        high=df["high"], low=df["low"], close=df["close"], window=ATR_PERIOD
    ).average_true_range()

    df["low_close"]  = df["close"].rolling(DIV_WINDOW).min()
    df["low_rsi"]    = df["rsi"].rolling(DIV_WINDOW).min()
    df["high_close"] = df["close"].rolling(DIV_WINDOW).max()
    df["high_rsi"]   = df["rsi"].rolling(DIV_WINDOW).max()

    df["bull_div"] = (
        (df["low_close"] < df["low_close"].shift(DIV_SHIFT)) &
        (df["low_rsi"]   > df["low_rsi"].shift(DIV_SHIFT))
    )
    df["bear_div"] = (
        (df["high_close"] > df["high_close"].shift(DIV_SHIFT)) &
        (df["high_rsi"]   < df["high_rsi"].shift(DIV_SHIFT))
    )

    pm = df["macd_line"].shift(1)
    ps = df["signal_line"].shift(1)
    df["macd_bull_cross"] = (df["macd_line"] > df["signal_line"]) & (pm <= ps)
    df["macd_bear_cross"] = (df["macd_line"] < df["signal_line"]) & (pm >= ps)

    return df


# ─── Simulation — mirrors bot.py's process() sequencing exactly ───
def simulate(df: pd.DataFrame, ratchet_up: int, ratchet_down: int) -> dict:

    cm = CorpusManager(
        initial_balance    = INITIAL_BALANCE,
        base_monthly_dca   = DCA_BASE,
        dca_annual_growth  = DCA_GROWTH,
        ratchet_up_every   = ratchet_up,
        ratchet_down_after = ratchet_down,
    )

    balance    = INITIAL_BALANCE
    total_fees = 0.0
    total_inv  = INITIAL_BALANCE
    trades     = []
    eq_curve   = [(None, INITIAL_BALANCE)]
    last_dca   = None
    consec     = 0
    cb_until   = None
    cb_count   = 0
    pos        = None
    bull_armed = 0
    bear_armed = 0

    stop_dists_pct = []

    rows = df.reset_index().to_dict('records')

    for i, row in enumerate(rows):
        ts    = row.get('timestamp', row.get('index'))
        price = float(row['close'])
        atr   = float(row['atr']) if not (isinstance(row['atr'], float)
                                          and math.isnan(row['atr'])) else 0.0

        # 1. DCA
        if hasattr(ts, 'day') and ts.day == DCA_DAY:
            key = (ts.year, ts.month)
            if key != last_dca:
                res        = cm.on_monthly_refresh(balance, ts.year, ts.month, START_YEAR)
                balance   += res['contribution']
                total_inv += res['contribution']
                eq_curve.append((ts, balance))
                last_dca   = key

        # 2. Update armed counters (identical to bot.py)
        if row.get('bull_div', False):
            bull_armed = DIV_MEMORY
        elif bull_armed > 0:
            bull_armed -= 1

        if row.get('bear_div', False):
            bear_armed = DIV_MEMORY
        elif bear_armed > 0:
            bear_armed -= 1

        macd_bull = bool(row.get('macd_bull_cross', False))
        macd_bear = bool(row.get('macd_bear_cross', False))
        high_vol  = bool(row.get('high_vol', False))

        corpus = cm.corpus

        def close_pos(reason: str):
            nonlocal balance, total_fees, consec, cb_until, cb_count, pos
            ep      = price
            fee_out = ep * pos['qty'] * FEE_RATE
            if pos['side'] == 'long':
                raw = (ep - pos['entry']) * pos['qty'] * LEVERAGE
            else:
                raw = (pos['entry'] - ep) * pos['qty'] * LEVERAGE
            pnl = raw - pos['fee_in'] - fee_out
            total_fees += pos['fee_in'] + fee_out
            balance    += pnl
            eq_curve.append((ts, balance))
            trades.append({
                'date': ts, 'side': pos['side'], 'pnl': pnl,
                'entry': pos['entry'], 'exit': ep, 'stop': pos['stop'],
                'reason': reason, 'hold': i - pos['bar'], 'balance': balance,
            })
            cm.on_trade_complete(pnl, balance)
            if pnl <= 0:
                consec += 1
                if consec % FLAT_CB_TRIGGER == 0:
                    secs_per_candle = 60 * CANDLE_MINUTES
                    cb_candles      = int(FLAT_CB_HOURS * 3600 / secs_per_candle)
                    cb_until        = i + max(cb_candles, 1)
                    cb_count       += 1
            else:
                consec = 0
            pos = None

        # 3. Exits — signal takes precedence over stop (same as bot.py)
        if pos is not None:
            if pos['side'] == 'long':
                signal_exit = bear_armed > 0 and macd_bear and high_vol
                stop_exit   = atr > 0 and price <= pos['stop']
            else:
                signal_exit = bull_armed > 0 and macd_bull and high_vol
                stop_exit   = atr > 0 and price >= pos['stop']

            if signal_exit:
                close_pos('signal')
            elif stop_exit:
                stop_dists_pct.append(abs(pos['entry']-pos['stop'])/pos['entry']*100)
                close_pos('stop')

        # 4. Circuit breaker gate
        in_cb = cb_until is not None and i < cb_until

        # 5. Entries — only when flat; can re-enter same candle after exit
        if pos is None and not in_cb and atr > 0:
            if bull_armed > 0 and macd_bull and high_vol:
                stop      = price - LONG_ATR_MULT * atr
                stop_dist = max(abs(price - stop), price * 0.0001)
                qty       = (corpus * BASE_RISK) / (stop_dist * LEVERAGE)
                fee_in    = price * qty * FEE_RATE
                balance  -= fee_in
                total_fees += fee_in
                pos = {'side': 'long', 'entry': price, 'stop': stop,
                       'qty': qty, 'fee_in': fee_in, 'bar': i}

            elif bear_armed > 0 and macd_bear and high_vol:
                stop      = price + SHORT_ATR_MULT * atr
                stop_dist = max(abs(stop - price), price * 0.0001)
                qty       = (corpus * BASE_RISK) / (stop_dist * LEVERAGE)
                fee_in    = price * qty * FEE_RATE
                balance  -= fee_in
                total_fees += fee_in
                pos = {'side': 'short', 'entry': price, 'stop': stop,
                       'qty': qty, 'fee_in': fee_in, 'bar': i}

    eq_curve.append((None, balance))
    avg_stop_pct = float(np.mean(stop_dists_pct)) if stop_dists_pct else 0.0
    return {
        'trades': trades, 'eq_curve': eq_curve, 'final': balance,
        'total_inv': total_inv, 'total_fees': total_fees,
        'cb_count': cb_count, 'avg_stop_pct': avg_stop_pct,
    }


# ─── Metrics ──────────────────────────────────────────────────────
def metrics(result: dict, years: float) -> dict:
    T = result['trades']
    n = len(T)
    if n == 0:
        return {k: 0 for k in ['n','pf','wr','dd','ret','annual','final',
                                'ml','sharpe','stop_pct','signal_pct',
                                'longs','shorts','avg_w','avg_l','cb_count',
                                'net_profit','total_inv','total_fees','avg_stop_pct']}

    pnls   = [t['pnl'] for t in T]
    wins   = [p for p in pnls if p > 0]
    loss   = [p for p in pnls if p <= 0]
    pf     = sum(wins) / abs(sum(loss)) if loss and sum(loss) != 0 else 999.0
    wr     = len(wins) / n * 100
    ret    = (result['final'] - result['total_inv']) / result['total_inv'] * 100
    ann    = ret / years if years > 0 else 0

    eq     = np.array([e[1] for e in result['eq_curve']], dtype=float)
    peaks  = np.maximum.accumulate(eq)
    dd     = float(((peaks - eq) / np.where(peaks == 0, 1, peaks) * 100).max())

    ra     = np.diff(eq) / np.where(eq[:-1] == 0, 1, eq[:-1])
    sh     = (float((ra.mean() / ra.std()) * math.sqrt(CANDLES_PER_YEAR))
              if len(ra) > 1 and ra.std() > 0 else 0.0)

    ml = cl = 0
    for p in pnls:
        if p > 0:
            cl = 0
        else:
            cl += 1
            ml  = max(ml, cl)

    stop_n   = sum(1 for t in T if t['reason'] == 'stop')
    signal_n = sum(1 for t in T if t['reason'] == 'signal')

    return {
        'n': n, 'pf': pf, 'wr': wr, 'dd': dd, 'ret': ret, 'annual': ann,
        'final': result['final'], 'ml': ml, 'sharpe': sh,
        'stop_pct': stop_n / n * 100, 'signal_pct': signal_n / n * 100,
        'longs':  sum(1 for t in T if t['side'] == 'long'),
        'shorts': sum(1 for t in T if t['side'] == 'short'),
        'avg_w':  float(np.mean(wins)) if wins else 0.0,
        'avg_l':  float(abs(np.mean(loss))) if loss else 0.0,
        'cb_count':    result['cb_count'],
        'net_profit':  result['final'] - result['total_inv'],
        'total_inv':   result['total_inv'],
        'total_fees':  result['total_fees'],
        'avg_stop_pct': result['avg_stop_pct'],
    }


# ─── Indicator helpers ────────────────────────────────────────────
def pf_i(v):  return "🟢" if v >= 1.5 else ("🟡" if v >= 1.0 else "🔴")
def dd_i(v):  return "🟢" if v <= 15  else ("🟡" if v <= 30   else "🔴")
def rd_i(v):  return "🟢" if v >= 2.0 else ("🟡" if v >= 1.0  else "🔴")
def vs_ref(val, ref, higher_is_better=True):
    """Arrow showing direction vs reference baseline."""
    if higher_is_better:
        return "▲" if val > ref * 1.02 else ("▼" if val < ref * 0.98 else "═")
    else:
        return "▲" if val < ref * 0.98 else ("▼" if val > ref * 1.02 else "═")


# ─── Print full detail for one variant ───────────────────────────
def print_variant_detail(label: str, ratchet_up: int, ratchet_down: int,
                         result: dict, m: dict, years: float):
    rdd = abs(m['annual']) / m['dd'] if m['dd'] > 0 else 0.0
    t22 = sum(t['pnl'] for t in result['trades']
              if hasattr(t['date'], 'year') and t['date'].year == 2022)

    print(f"\n{'█'*70}")
    print(f"  {label}  —  ratchet_up={ratchet_up}  ratchet_down={ratchet_down}  (5x leverage)")
    print(f"{'█'*70}")

    if m['n'] == 0:
        print("  ❌ No trades.")
        return

    print(f"\n  ╔{'═'*62}╗")
    print(f"  ║  {'Invested (inc DCA):':<28} ${m['total_inv']:>29,.2f}  ║")
    print(f"  ║  {'Final Balance:':<28} ${m['final']:>29,.2f}  ║")
    print(f"  ║  {'Net Profit:':<28} ${m['net_profit']:>+29,.2f}  ║")
    print(f"  ║  {'Annual ROI:':<28} {m['annual']:>+29.1f}%  ║")
    print(f"  ║  {'Total Fees:':<28} ${m['total_fees']:>29.2f}  ║")
    print(f"  ╠{'═'*62}╣")
    print(f"  ║  {'Trades (L / S):':<28} {m['n']:>4} ({m['longs']}L / {m['shorts']}S){'':<18}  ║")
    print(f"  ║  {'Win Rate:':<28} {m['wr']:>29.1f}%  ║")
    print(f"  ║  {'Stop-exit / Signal-exit:':<28} "
          f"{m['stop_pct']:>13.1f}% / {m['signal_pct']:>10.1f}%  ║")
    print(f"  ║  {'Profit Factor:':<28} {pf_i(m['pf'])}{m['pf']:>29.2f}  ║")
    print(f"  ║  {'Max Drawdown:':<28} {dd_i(m['dd'])}{m['dd']:>29.1f}%  ║")
    print(f"  ║  {'Return / DD:':<28} {rd_i(rdd)}{rdd:>28.2f}×  ║")
    print(f"  ║  {'Sharpe Ratio:':<28} {m['sharpe']:>29.1f}  ║")
    print(f"  ║  {'Avg Win / Avg Loss:':<28} ${m['avg_w']:>13.2f} / ${m['avg_l']:>11.2f}  ║")
    print(f"  ║  {'Max Loss Streak:':<28} {'▼'+str(m['ml'])+' consec':>31}  ║")
    print(f"  ║  {'CB fires:':<28} {m['cb_count']:>31}  ║")
    print(f"  ║  {'2022 P&L:':<28} ${t22:>+29.2f}  ║")
    print(f"  ╠{'═'*62}╣")
    print(f"  ║  {'Avg realized stop dist:':<28} {m['avg_stop_pct']:>29.2f}%  ║")
    print(f"  ║  {'Liquidation distance (5x):':<28} {'20.00':>29}%  ║")
    print(f"  ╚{'═'*62}╝")

    yrs     = sorted(set(t['date'].year for t in result['trades']
                         if hasattr(t['date'], 'year')))
    running = INITIAL_BALANCE
    print(f"\n  {'Year':<6} {'N':>4} {'WR':>5} {'PF':>6} {'Ret%':>8} {'Balance':>12}  Note")
    print(f"  {'─'*60}")
    for yr in yrs:
        yt  = [t for t in result['trades']
               if hasattr(t['date'], 'year') and t['date'].year == yr]
        if not yt:
            continue
        yp  = [t['pnl'] for t in yt]
        yw  = [p for p in yp if p > 0]
        yl  = [p for p in yp if p <= 0]
        ypf = sum(yw) / abs(sum(yl)) if yl and sum(yl) != 0 else 999.0
        ywr = len(yw) / len(yt) * 100
        yret = sum(yp) / running * 100 if running > 0 else 0
        running += sum(yp)
        note = "⚠️ BEAR" if yr == 2022 else ("🚀" if yr in (2020, 2021, 2023, 2024) else "")
        pfs  = f"{ypf:.2f}" if ypf < 900 else "∞"
        print(f"  {yr:<6}{len(yt):>4} {ywr:>4.0f}% {pf_i(ypf)}{pfs:>5} "
              f"{yret:>+7.1f}%  ${running:>11,.2f}  {note}")
    print(f"  {'─'*60}")
    print(f"  {'TOT':<6}{m['n']:>4} {m['wr']:>4.0f}% {pf_i(m['pf'])}{m['pf']:>5.2f} "
          f"{m['ret']:>+7.1f}%  ${m['final']:>11,.2f}")


# ─── Main ─────────────────────────────────────────────────────────
def main():
    exchange = ccxt.binanceusdm({
        'enableRateLimit': True, 'options': {'defaultType': 'future'}
    })
    print("📡 Connecting to Binance...")
    exchange.load_markets()
    sym = 'BTC/USDT' if 'BTC/USDT' in exchange.symbols else 'BTC/USDT:USDT'
    print(f"✅ {sym}\n")

    print("═" * 70)
    print("  Corpus Ratchet Frequency — 4 variants at 5x leverage")
    print("  (identical 6.5-year signal — RSI Div + MACD Cross + Vol Spike)")
    print("═" * 70)

    print(f"\n  📥 Fetching 15m {sym} data (fetched ONCE, reused for all variants)...")
    df    = fetch_ohlcv(exchange, sym, "15m", "2019-09-01")
    years = (df.index[-1] - df.index[0]).days / 365.25
    print(f"\n  ✅ {len(df):,} candles  [{years:.1f} years]")

    warmup = max(RSI_LEN, MACD_SLOW, VOL_SMA_PERIOD, ATR_PERIOD,
                 DIV_WINDOW + DIV_SHIFT) + 5
    df = df.iloc[warmup:].copy()
    df = build_indicators(df)

    print(f"\n{'═' * 70}")
    print(f"  Running {len(RATCHET_TIERS)} ratchet variants on identical dataset...")
    print(f"  Leverage fixed at {LEVERAGE}x  "
          f"LONG_ATR={LONG_ATR_MULT:.1f}  SHORT_ATR={SHORT_ATR_MULT:.1f}")
    print("═" * 70)

    all_results = {}
    for label, ratchet_up, ratchet_down in RATCHET_TIERS:
        print(f"\n  ── {label:<20}  up={ratchet_up}  down={ratchet_down}")
        result = simulate(df.copy(), ratchet_up, ratchet_down)
        m      = metrics(result, years)
        rdd    = abs(m['annual']) / m['dd'] if m['dd'] > 0 else 0.0
        t22    = sum(t['pnl'] for t in result['trades']
                     if hasattr(t['date'], 'year') and t['date'].year == 2022)
        all_results[label] = (result, m, ratchet_up, ratchet_down)

        ref_arrow_final  = vs_ref(m['final'],   REFERENCE['final'])
        ref_arrow_pf     = vs_ref(m['pf'],      REFERENCE['pf'])
        ref_arrow_dd     = vs_ref(m['dd'],       REFERENCE['dd'], higher_is_better=False)

        print(f"     ${m['final']:>9,.0f} {ref_arrow_final}  "
              f"{m['annual']:>+6.1f}%/yr  "
              f"DD:{m['dd']:>6.1f}% {ref_arrow_dd}  "
              f"PF:{m['pf']:.2f} {ref_arrow_pf}  "
              f"WR:{m['wr']:.1f}%  "
              f"N:{m['n']}  "
              f"2022:${t22:>+.0f}")

    # ── Full detail per variant ────────────────────────────────────
    print(f"\n\n{'═'*70}")
    print("  FULL DETAIL — each ratchet variant")
    print("═"*70)
    for label, ratchet_up, ratchet_down in RATCHET_TIERS:
        result, m, ru, rd = all_results[label]
        print_variant_detail(label, ru, rd, result, m, years)

    # ── Master comparison table ────────────────────────────────────
    W = 110
    print(f"\n\n{'═'*W}")
    print("  📊 MASTER COMPARISON — ratchet frequency at 5x")
    print(f"{'═'*W}")
    print(f"  {'Variant':<22} {'Up':>3} {'Dn':>3} {'Final':>9} {'Ann%':>7} "
          f"{'PF':>6} {'DD':>7} {'R/DD':>6} {'N':>5} {'WR':>6} "
          f"{'StopEx':>7} {'SigEx':>6} {'2022':>9}  {'AvgStop':>8}")
    print(f"  {'─'*(W-2)}")

    best_final = max(all_results.items(), key=lambda x: x[1][1]['final'])
    best_pf    = max(all_results.items(), key=lambda x: x[1][1]['pf'])
    best_rdd   = max(all_results.items(),
                     key=lambda x: (abs(x[1][1]['annual']) / x[1][1]['dd']
                                    if x[1][1]['dd'] > 0 else 0))

    for label, ratchet_up, ratchet_down in RATCHET_TIERS:
        result, m, ru, rd = all_results[label]
        rdd  = abs(m['annual']) / m['dd'] if m['dd'] > 0 else 0.0
        t22  = sum(t['pnl'] for t in result['trades']
                   if hasattr(t['date'], 'year') and t['date'].year == 2022)
        tags = []
        if label == best_final[0]: tags.append("best $")
        if label == best_pf[0]:    tags.append("best PF")
        if label == best_rdd[0]:   tags.append("best R/DD")
        tag = " ← " + " + ".join(tags) if tags else ""

        print(f"  {label:<22} {ru:>3} {rd:>3} "
              f"${m['final']:>8,.0f} "
              f"{m['annual']:>+6.1f}% "
              f"{pf_i(m['pf'])}{m['pf']:>4.2f} "
              f"{dd_i(m['dd'])}{m['dd']:>5.1f}% "
              f"{rd_i(rdd)}{rdd:>4.2f}× "
              f"{m['n']:>5} {m['wr']:>5.1f}% "
              f"{m['stop_pct']:>6.0f}% "
              f"{m['signal_pct']:>5.0f}% "
              f"${t22:>+8,.0f}  "
              f"{m['avg_stop_pct']:>6.2f}%"
              f"{tag}")

    print(f"\n{'═'*W}")
    print(f"  📌 Reference (backtest_leverage.py, 5x, ratchet 10/10): "
          f"${REFERENCE['final']:,.0f}  "
          f"{REFERENCE['annual']:+.1f}%/yr  "
          f"PF {REFERENCE['pf']:.2f}  "
          f"DD {REFERENCE['dd']:.1f}%  "
          f"WR {REFERENCE['wr']:.1f}%")
    print(f"  ▲ = better than reference  ▼ = worse  ═ = within 2%")
    print(f"{'═'*W}")

    # ── Verdict ───────────────────────────────────────────────────
    print(f"\n  Ranking by final balance:")
    ranked = sorted(all_results.items(), key=lambda x: -x[1][1]['final'])
    for i, (label, (result, m, ru, rd)) in enumerate(ranked, 1):
        rdd = abs(m['annual']) / m['dd'] if m['dd'] > 0 else 0.0
        t22 = sum(t['pnl'] for t in result['trades']
                  if hasattr(t['date'], 'year') and t['date'].year == 2022)
        print(f"    {i}. {label:<22}  ${m['final']:>9,.0f}  "
              f"PF {m['pf']:.2f}  WR {m['wr']:.1f}%  "
              f"R/DD {rdd:.2f}×  2022: ${t22:>+,.0f}")

    print(f"\n  ── PARAMETERS ────────────────────────────────────────────────")
    print(f"  Leverage: {LEVERAGE}x  LONG_ATR: {LONG_ATR_MULT}  SHORT_ATR: {SHORT_ATR_MULT}")
    print(f"  Risk/trade: {BASE_RISK*100:.0f}%  CB: {FLAT_CB_TRIGGER} losses → {FLAT_CB_HOURS}h  "
          f"DCA: ${DCA_BASE}/mo +{DCA_GROWTH*100:.0f}%/yr")
    print(f"{'═'*W}\n")


if __name__ == "__main__":
    main()
