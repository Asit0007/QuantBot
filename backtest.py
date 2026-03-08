"""
backtest.py — QuantBot FINAL Backtest v20.0

WHY WE'RE HERE:
  The tiered CB hurt performance because it locks us out of the market
  during exactly the windows when our reversal signal is most valuable.
  Loss streaks cluster just before big reversals — holding out for 8-12
  days after 20+ consecutive losses means MISSING the recovery wins.

  Flat CB wins because 48h is short enough to still catch the reversal.

THE UNTESTED IDEA — Progressive Position Scaling:
  Instead of binary (trade / don't trade), stay in the market at all
  times but SHRINK the position during losing streaks, grow back on wins.

  Scaling levels (10% base risk):
    Level 0  →  100%  → 10.0% corpus  (normal)
    Level 1  →   70%  →  7.0% corpus  (after 3 losses)
    Level 2  →   50%  →  5.0% corpus  (after 6 losses)
    Level 3  →   30%  →  3.0% corpus  (after 9 losses, minimum)

  Scale DOWN: every 3 consecutive losses → drop one level
  Scale UP:   every win → return to Level 0 immediately
              (wins reset fully — we trust the signal when it fires)

  Why this should help:
    2022: 33 losses means spending most of the year at Level 3 (3%)
          instead of 10%. Dramatically cuts 2022 damage.
    2020/2021/2023/2024: streaks are short (3-5 losses max before a win).
          You spend 95%+ of the time at Level 0. Barely any cost.

CONFIGS (all use 10%/20× base, flat CB confirmed best):
  A — Pure flat CB 48h (confirmed v18/v19 winner — our baseline)
  B — Progressive scaling only (no CB)
  C — Progressive scaling + flat CB (belt-and-suspenders)
  D — Aggressive scaling: tighter levels (every 2 losses = step down)
  E — Conservative scaling: only 2 levels (10% → 5% after 5 losses)

After this run: BUILD BOT.PY. No more backtests.
"""

import ccxt
import pandas as pd
import numpy as np
from ta.momentum import RSIIndicator
from ta.trend import MACD as MACDIndicator
from ta.volatility import AverageTrueRange
import sys
import math
import requests
from datetime import datetime, timezone, timedelta
from corpus_manager import CorpusManager

# ─── Fixed parameters ─────────────────────────────────────────────────────────
SYMBOL           = "BTC/USDT"
TIMEFRAME        = '15m'
INITIAL_BALANCE  = 100.0
RSI_LEN          = 14
MACD_FAST        = 12
MACD_SLOW        = 26
MACD_SIGNAL_WIN  = 9
VOL_MULT         = 2.0
VOL_SMA_PERIOD   = 20
DIV_WINDOW       = 5
DIV_SHIFT        = 5
DIV_MEMORY       = 3
LONG_ATR_MULT    = 2.0
SHORT_ATR_MULT   = 1.5
ATR_PERIOD       = 14
FEE_RATE         = 0.0005
DCA_BASE         = 10.0
DCA_GROWTH       = 0.10
DCA_DAY          = 10
START_YEAR       = 2019
BASE_RISK        = 0.10          # 10% corpus base
LEVERAGE         = 20
CORPUS_REFRESH   = 10
CANDLES_PER_YEAR = 365 * 24 * 4

# Flat CB
FLAT_CB_TRIGGER  = 5
FLAT_CB_HOURS    = 48

# Progressive scaling configs
SCALE_CONFIGS = {
    # name: (losses_per_step, levels=[1.0, 0.7, 0.5, 0.3])
    'standard':     (3, [1.00, 0.70, 0.50, 0.30]),
    'aggressive':   (2, [1.00, 0.65, 0.40, 0.20]),
    'conservative': (5, [1.00, 0.50]),
}


# ─── Indicators ───────────────────────────────────────────────────────────────
def build_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df['rsi'] = RSIIndicator(close=df['close'], window=RSI_LEN).rsi()
    macd             = MACDIndicator(close=df['close'],
                                     window_fast=MACD_FAST,
                                     window_slow=MACD_SLOW,
                                     window_sign=MACD_SIGNAL_WIN)
    df['macdLine']   = macd.macd()
    df['signalLine'] = macd.macd_signal()
    df['avgVol']     = df['volume'].rolling(VOL_SMA_PERIOD).mean()
    df['low_close']  = df['close'].rolling(DIV_WINDOW).min()
    df['low_rsi']    = df['rsi'].rolling(DIV_WINDOW).min()
    df['high_close'] = df['close'].rolling(DIV_WINDOW).max()
    df['high_rsi']   = df['rsi'].rolling(DIV_WINDOW).max()
    df['rsiBullDiv'] = (
        (df['low_close'] < df['low_close'].shift(DIV_SHIFT)) &
        (df['low_rsi']   > df['low_rsi'].shift(DIV_SHIFT))
    )
    df['rsiBearDiv'] = (
        (df['high_close'] > df['high_close'].shift(DIV_SHIFT)) &
        (df['high_rsi']   < df['high_rsi'].shift(DIV_SHIFT))
    )
    pm = df['macdLine'].shift(1)
    ps = df['signalLine'].shift(1)
    df['macdBull'] = (df['macdLine'] > df['signalLine']) & (pm <= ps)
    df['macdBear'] = (df['macdLine'] < df['signalLine']) & (pm >= ps)
    df['highVol']  = df['volume'] > (VOL_MULT * df['avgVol'])
    df['atr']      = AverageTrueRange(
        high=df['high'], low=df['low'], close=df['close'], window=ATR_PERIOD
    ).average_true_range()
    return df


def fetch_fear_greed() -> pd.DataFrame:
    try:
        resp = requests.get(
            "https://api.alternative.me/fng/?limit=2500&format=json", timeout=15)
        records = [{'date': pd.Timestamp(datetime.fromtimestamp(
                        int(e['timestamp']), tz=timezone.utc).date()),
                    'fg': int(e['value'])}
                   for e in resp.json().get('data', [])]
        return pd.DataFrame(records).set_index('date').sort_index()
    except:
        return pd.DataFrame()


def merge_fg(df, fg_df):
    df = df.copy()
    df['date'] = df.index.normalize()
    if not fg_df.empty:
        df = df.merge(fg_df[['fg']], left_on='date', right_index=True, how='left')
        df['fg'] = df['fg'].ffill().fillna(50)
    else:
        df['fg'] = 50
    df.drop(columns=['date'], inplace=True)
    return df


# ─── Simulation ───────────────────────────────────────────────────────────────
def simulate(df:             pd.DataFrame,
             scale_config:   str   = None,
             use_flat_cb:    bool  = True) -> dict:
    """
    scale_config: None | 'standard' | 'aggressive' | 'conservative'
    use_flat_cb:  True → pause 48h every 5 consecutive losses
    """
    cm = CorpusManager(
        initial_balance    = INITIAL_BALANCE,
        base_monthly_dca   = DCA_BASE,
        dca_annual_growth  = DCA_GROWTH,
        ratchet_up_every   = CORPUS_REFRESH,
        ratchet_down_after = CORPUS_REFRESH,
    )

    # Scaling setup
    if scale_config and scale_config in SCALE_CONFIGS:
        losses_per_step, scale_levels = SCALE_CONFIGS[scale_config]
    else:
        losses_per_step, scale_levels = 3, [1.0]   # no scaling

    scale_idx      = 0
    scale_losses   = 0    # consecutive losses since last level change

    # CB setup
    consec_losses  = 0
    cb_pause_until = None
    cb_count       = 0

    trades         = []
    eq_curve       = [(None, INITIAL_BALANCE)]
    balance        = INITIAL_BALANCE
    total_fees     = 0.0
    total_inv      = INITIAL_BALANCE
    long_pos       = None
    short_pos      = None
    bull_armed     = 0
    bear_armed     = 0
    last_dca       = None

    # Track time spent at each scale level
    level_trades   = [0] * len(scale_levels)

    rows = df.reset_index().to_dict('records')

    for i, row in enumerate(rows):
        ts    = row.get('timestamp', row.get('index'))
        price = float(row['close'])
        atr   = float(row['atr']) if not pd.isna(row['atr']) else 0

        # Monthly DCA
        if hasattr(ts, 'day') and ts.day == DCA_DAY:
            dca_key = (ts.year, ts.month)
            if dca_key != last_dca:
                res        = cm.on_monthly_refresh(balance, ts.year, ts.month, START_YEAR)
                balance   += res['contribution']
                total_inv += res['contribution']
                eq_curve.append((ts, balance))
                last_dca   = dca_key

        if row['rsiBullDiv']:  bull_armed = DIV_MEMORY
        elif bull_armed > 0:   bull_armed -= 1
        if row['rsiBearDiv']:  bear_armed = DIV_MEMORY
        elif bear_armed > 0:   bear_armed -= 1

        corpus      = cm.corpus
        multiplier  = scale_levels[scale_idx]
        risk        = BASE_RISK * multiplier

        def close_pos(pos, side, exit_price, reason):
            nonlocal balance, total_fees, consec_losses, cb_pause_until, cb_count
            nonlocal scale_idx, scale_losses

            exit_fee = exit_price * pos['size'] * FEE_RATE
            raw      = ((exit_price - pos['entry']) if side == 'long'
                        else (pos['entry'] - exit_price)) * pos['size'] * LEVERAGE
            pnl         = raw - pos['entry_fee'] - exit_fee
            total_fees += pos['entry_fee'] + exit_fee
            balance    += pnl
            eq_curve.append((ts, balance))
            risk_amt    = abs(pos['entry'] - pos['sl']) * pos['size'] * LEVERAGE
            r           = pnl / risk_amt if risk_amt > 0 else 0

            trades.append({'date': ts, 'side': side, 'pnl': pnl, 'r': r,
                           'entry': pos['entry'], 'exit': exit_price,
                           'reason': reason, 'hold': i - pos['bar'],
                           'balance': balance, 'scale': pos['scale_idx']})
            cm.on_trade_complete(pnl, balance)

            if pnl <= 0:
                # Flat CB
                consec_losses += 1
                if use_flat_cb and consec_losses % FLAT_CB_TRIGGER == 0:
                    if hasattr(ts, '__add__'):
                        cb_pause_until = ts + timedelta(hours=FLAT_CB_HOURS)
                    cb_count += 1

                # Progressive scaling
                scale_losses += 1
                if scale_config and scale_losses >= losses_per_step:
                    scale_idx   = min(len(scale_levels) - 1, scale_idx + 1)
                    scale_losses = 0
            else:
                consec_losses = 0
                # Win → snap back to full size immediately
                scale_idx    = 0
                scale_losses = 0

        # Exits
        if long_pos is not None:
            pine = bear_armed > 0 and row['macdBear'] and row['highVol']
            sl   = atr > 0 and price <= long_pos['sl']
            if pine or sl:
                ep = price if pine else long_pos['sl']
                close_pos(long_pos, 'long', ep, 'Signal' if pine else 'ATR Stop')
                long_pos = None

        if short_pos is not None:
            pine = bull_armed > 0 and row['macdBull'] and row['highVol']
            sl   = atr > 0 and price >= short_pos['sl']
            if pine or sl:
                ep = price if pine else short_pos['sl']
                close_pos(short_pos, 'short', ep, 'Signal' if pine else 'ATR Stop')
                short_pos = None

        in_cb = (use_flat_cb and cb_pause_until is not None and
                 hasattr(ts, '__lt__') and ts < cb_pause_until)

        # Entries
        if long_pos is None and bull_armed > 0 and row['macdBull'] and row['highVol']:
            if not in_cb:
                sl        = price - (LONG_ATR_MULT * atr) if atr > 0 else price * 0.95
                size      = (corpus * risk) / price
                entry_fee = price * size * FEE_RATE
                balance  -= entry_fee
                total_fees += entry_fee
                long_pos  = {'entry': price, 'sl': sl, 'size': size,
                             'bar': i, 'entry_fee': entry_fee, 'scale_idx': scale_idx}
                level_trades[scale_idx] += 1

        if short_pos is None and bear_armed > 0 and row['macdBear'] and row['highVol']:
            if not in_cb:
                sl        = price + (SHORT_ATR_MULT * atr) if atr > 0 else price * 1.05
                size      = (corpus * risk) / price
                entry_fee = price * size * FEE_RATE
                balance  -= entry_fee
                total_fees += entry_fee
                short_pos = {'entry': price, 'sl': sl, 'size': size,
                             'bar': i, 'entry_fee': entry_fee, 'scale_idx': scale_idx}
                level_trades[scale_idx] += 1

    eq_curve.append((None, balance))
    return {'trades': trades, 'eq_curve': eq_curve,
            'final': balance, 'total_inv': total_inv,
            'total_fees': total_fees, 'cb_count': cb_count,
            'level_trades': level_trades,
            'total_dca': cm.total_dca_added}


# ─── Metrics ──────────────────────────────────────────────────────────────────
def calc_metrics(result: dict, years: float = 6.5) -> dict:
    trades = result['trades']
    n      = len(trades)
    if n == 0:
        return {'n': 0, 'pf': 0, 'wr': 0, 'dd': 0, 'ret': 0, 'annual': 0,
                'final': result['final'], 'ml': 0,
                'net_profit': result['final'] - result['total_inv'],
                'total_inv': result['total_inv'],
                'total_fees': result['total_fees'], 'cb_count': result['cb_count']}

    pnls   = [t['pnl'] for t in trades]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    pf     = sum(wins) / abs(sum(losses)) if losses and sum(losses) != 0 else 999
    wr     = len(wins) / n * 100
    ret    = (result['final'] - result['total_inv']) / result['total_inv'] * 100
    annual = ret / years
    eq     = np.array([e[1] for e in result['eq_curve']], dtype=float)
    peaks  = np.maximum.accumulate(eq)
    max_dd = float(((peaks - eq) / peaks * 100).max())
    ret_arr= np.diff(eq) / np.where(eq[:-1] == 0, 1, eq[:-1])
    sharpe = float((ret_arr.mean() / ret_arr.std()) * math.sqrt(CANDLES_PER_YEAR)) \
             if len(ret_arr) > 1 and ret_arr.std() > 0 else 0
    ml = cl = 0
    for p in pnls:
        if p > 0: cl = 0
        else:     cl += 1; ml = max(ml, cl)
    avg_w = float(np.mean(wins))        if wins   else 0
    avg_l = float(abs(np.mean(losses))) if losses else 0
    return {'n': n, 'pf': pf, 'wr': wr, 'dd': max_dd,
            'ret': ret, 'annual': annual, 'final': result['final'],
            'total_inv': result['total_inv'], 'ml': ml, 'sharpe': sharpe,
            'net_profit': result['final'] - result['total_inv'],
            'total_fees': result['total_fees'], 'avg_w': avg_w, 'avg_l': avg_l,
            'longs': sum(1 for t in trades if t['side']=='long'),
            'shorts': sum(1 for t in trades if t['side']=='short'),
            'cb_count': result['cb_count'],
            'level_trades': result['level_trades']}


def pf_icon(v):  return "🟢" if v >= 1.5 else ("🟡" if v >= 1.0 else "🔴")
def dd_icon(v):  return "🟢" if v <= 8   else ("🟡" if v <= 15   else "🔴")
def rdd_icon(v): return "🟢" if v >= 2.0 else ("🟡" if v >= 1.0 else "🔴")


def print_full(result: dict, m: dict, label: str, sc: str, use_cb: bool):
    trades = result['trades']
    rdd    = abs(m['annual']) / m['dd'] if m['dd'] > 0 else 0

    print(f"\n{'█'*68}")
    print(f"  {label}")
    print(f"  Scale: {sc or 'none'}  |  Flat CB: {'yes' if use_cb else 'no'}")
    print(f"{'█'*68}")
    if m['n'] == 0: print("  ❌ No trades"); return

    inv_s  = f"${m['total_inv']:,.2f}"
    fin_s  = f"${m['final']:,.2f}"
    net_s  = f"${m['net_profit']:+,.2f}"
    ann_s  = f"{m['annual']:+.1f}% per year"
    fee_s  = f"${m['total_fees']:.2f}"
    tr_s   = f"{m['n']} ({m['longs']}L / {m['shorts']}S)"
    wr_s   = f"{m['wr']:.1f}%"
    pf_s   = f"{pf_icon(m['pf'])}{m['pf']:.2f}"
    dd_s   = f"{dd_icon(m['dd'])}{m['dd']:.1f}%"
    rdd_s  = f"{rdd_icon(rdd)}{rdd:.2f}× (higher=better)"
    sh_s   = f"{m['sharpe']:.1f}"
    wl_s   = f"${m['avg_w']:.2f} / ${m['avg_l']:.2f}"
    ml_s   = f"▼{m['ml']} consecutive"
    cb_s   = f"{m['cb_count']} CB pauses fired"

    print(f"\n  ╔{'═'*60}╗")
    print(f"  ║  {'Total Invested (DCA):':<28} {inv_s:>28}  ║")
    print(f"  ║  {'Final Balance:':<28} {fin_s:>28}  ║")
    print(f"  ║  {'Net Profit:':<28} {net_s:>28}  ║")
    print(f"  ║  {'Annual ROI:':<28} {ann_s:>28}  ║")
    print(f"  ║  {'Total Fees Paid:':<28} {fee_s:>28}  ║")
    print(f"  ╠{'═'*60}╣")
    print(f"  ║  {'Trades (L/S):':<28} {tr_s:>28}  ║")
    print(f"  ║  {'Win Rate:':<28} {wr_s:>28}  ║")
    print(f"  ║  {'Profit Factor:':<28} {pf_s:>28}  ║")
    print(f"  ║  {'Max Drawdown:':<28} {dd_s:>28}  ║")
    print(f"  ║  {'Return / DD ratio:':<28} {rdd_s:>28}  ║")
    print(f"  ║  {'Sharpe Ratio:':<28} {sh_s:>28}  ║")
    print(f"  ║  {'Avg Win / Avg Loss:':<28} {wl_s:>28}  ║")
    print(f"  ║  {'Max Loss Streak:':<28} {ml_s:>28}  ║")
    print(f"  ║  {'Circuit Breaker:':<28} {cb_s:>28}  ║")
    print(f"  ╚{'═'*60}╝")

    # Scale level distribution
    lt = result['level_trades']
    if sc and len(lt) > 1:
        _, levels = SCALE_CONFIGS[sc]
        total_lts = sum(lt) or 1
        print(f"\n  📊 Trade distribution by scale level:")
        for lvl, (mult, n_trades) in enumerate(zip(levels, lt)):
            bar  = '█' * int(n_trades / total_lts * 30)
            risk = BASE_RISK * mult * 100
            print(f"     Level {lvl} ({risk:.1f}% risk  ×{mult:.2f}): "
                  f"{n_trades:>4} trades  {bar}")

    # Year by year
    years_list = sorted(set(t['date'].year for t in trades if hasattr(t['date'],'year')))
    running    = INITIAL_BALANCE
    print(f"\n  {'Year':<6} {'N':>4} {'WR':>5} {'PF':>6} "
          f"{'Ret%':>8} {'Balance':>11}  Note")
    print(f"  {'─'*60}")
    for yr in years_list:
        yt   = [t for t in trades if hasattr(t['date'],'year') and t['date'].year == yr]
        if not yt: continue
        yp   = [t['pnl'] for t in yt]
        yw   = [p for p in yp if p > 0]
        yl   = [p for p in yp if p <= 0]
        ypf  = sum(yw)/abs(sum(yl)) if yl and sum(yl) != 0 else 999
        ywr  = len(yw)/len(yt)*100
        yret = sum(yp)/running*100 if running > 0 else 0
        running += sum(yp)
        note = ("⚠️  BEAR" if yr == 2022 else
                "🚀 BULL" if yr in (2020,2021,2023,2024) else "")
        pfstr = f"{ypf:.2f}" if ypf < 900 else "∞"
        print(f"  {yr:<6} {len(yt):>4} "
              f"{ywr:>4.0f}% {pf_icon(ypf)}{pfstr:>5} "
              f"{yret:>+7.1f}%  ${running:>10,.2f}  {note}")
    print(f"  {'─'*60}")
    print(f"  {'TOTAL':<6} {m['n']:>4} "
          f"{m['wr']:>4.0f}% {pf_icon(m['pf'])}{m['pf']:>5.2f} "
          f"{m['ret']:>+7.1f}%  ${m['final']:>10,.2f}")


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    exchange = ccxt.binanceusdm({'enableRateLimit': True,
                                  'options': {'defaultType': 'future'}})
    print("📡 Connecting to Binance...")
    exchange.load_markets()
    symbol = SYMBOL if SYMBOL in exchange.symbols else f"{SYMBOL}:USDT"
    print(f"✅ {symbol}\n")

    print("📥 Fetching 15m BTC futures history...")
    since, all_ohlcv = exchange.parse8601('2019-09-01T00:00:00Z'), []
    while True:
        try:
            chunk = exchange.fetch_ohlcv(symbol, TIMEFRAME, since=since, limit=1000)
            if not chunk: break
            all_ohlcv.extend(chunk)
            since = chunk[-1][0] + 1
            sys.stdout.write(f"\r  {len(all_ohlcv):>7,} candles "
                f"({datetime.fromtimestamp(chunk[-1][0]/1000).strftime('%Y-%m-%d')})")
            sys.stdout.flush()
            if len(chunk) < 1000: break
        except Exception as e:
            print(f"\n  ⚠️ {e}"); break

    df = pd.DataFrame(all_ohlcv,
                      columns=['timestamp','open','high','low','close','volume'])
    df.drop_duplicates(subset='timestamp', inplace=True)
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)
    years_covered = (df.index[-1] - df.index[0]).days / 365.25
    print(f"\n  ✅ {len(df):,} candles  [{years_covered:.1f} years]\n")

    warmup = MACD_SLOW + MACD_SIGNAL_WIN + DIV_WINDOW + DIV_SHIFT + 5
    df = df.iloc[warmup:].copy()
    df = build_indicators(df)
    fg_df = fetch_fear_greed()
    df    = merge_fg(df, fg_df)
    print("  ✅ Indicators ready\n")

    # ── Print scaling schedules ────────────────────────────────────────────────
    print(f"{'═'*68}")
    print(f"  📐 PROGRESSIVE SCALING SCHEDULES  (base: {BASE_RISK*100:.0f}% corpus)")
    print(f"{'═'*68}")
    for name, (step, levels) in SCALE_CONFIGS.items():
        print(f"\n  [{name}]  step down every {step} losses, "
              f"snap to full on any win:")
        for lvl, mult in enumerate(levels):
            trig = f"after {lvl*step}+ losses" if lvl > 0 else "default"
            print(f"    Level {lvl}: {trig:<22} → "
                  f"{BASE_RISK*mult*100:.1f}% risk  "
                  f"({BASE_RISK*mult*LEVERAGE*100:.0f}% notional)")
    print(f"\n{'═'*68}\n")

    # ── Configs ────────────────────────────────────────────────────────────────
    configs = [
        ("A — Flat CB only (confirmed winner)",
         dict(scale_config=None,           use_flat_cb=True)),
        ("B — Standard scaling, no CB",
         dict(scale_config='standard',     use_flat_cb=False)),
        ("C — Standard scaling + flat CB",
         dict(scale_config='standard',     use_flat_cb=True)),
        ("D — Aggressive scaling, no CB",
         dict(scale_config='aggressive',   use_flat_cb=False)),
        ("E — Conservative scaling, no CB",
         dict(scale_config='conservative', use_flat_cb=False)),
    ]

    all_results = []
    for label, kwargs in configs:
        sc  = kwargs['scale_config']
        cb  = kwargs['use_flat_cb']
        print(f"  ▶ {label}")
        r = simulate(df.copy(), **kwargs)
        m = calc_metrics(r, years_covered)
        all_results.append((label, sc, cb, r, m))
        rdd  = abs(m['annual'])/m['dd'] if m['dd'] > 0 else 0
        t22  = sum(t['pnl'] for t in r['trades']
                   if hasattr(t['date'],'year') and t['date'].year==2022)
        print(f"    → ${m['final']:>8,.0f}  {m['annual']:>+6.1f}%/yr  "
              f"DD:{m['dd']:>5.1f}%  R/DD:{rdd:.2f}×  "
              f"2022:${t22:+.0f}")

    # Full detail per config
    for label, sc, cb, r, m in all_results:
        print_full(r, m, label, sc, cb)

    # ── Master comparison ──────────────────────────────────────────────────────
    print(f"\n\n{'═'*80}")
    print(f"  📊 FINAL MASTER COMPARISON — $100 + DCA  ({years_covered:.1f} years)")
    print(f"{'═'*80}")
    print(f"  {'Config':<38} {'Final':>8} {'Ann%':>6} "
          f"{'PF':>6} {'DD':>6} {'R/DD':>6} {'Sharpe':>7} {'2022':>8}")
    print(f"  {'─'*78}")
    for label, sc, cb, r, m in all_results:
        rdd = abs(m['annual'])/m['dd'] if m['dd'] > 0 else 0
        t22 = sum(t['pnl'] for t in r['trades']
                  if hasattr(t['date'],'year') and t['date'].year==2022)
        print(
            f"  {label[:38]:<38} "
            f"${m['final']:>7,.0f}  "
            f"{m['annual']:>+5.1f}%  "
            f"{pf_icon(m['pf'])}{m['pf']:>5.2f}  "
            f"{dd_icon(m['dd'])}{m['dd']:>5.1f}%  "
            f"{rdd_icon(rdd)}{rdd:>4.2f}×  "
            f"{m['sharpe']:>6.1f}  "
            f"${t22:>+7.0f}"
        )

    # ── Final verdict ──────────────────────────────────────────────────────────
    print(f"\n{'═'*80}")
    print(f"  🔒 FINAL VERDICT — PARAMETERS LOCKED FOR bot.py")
    print(f"{'═'*80}")

    best_abs   = max(all_results, key=lambda x: x[4]['final'] if x[4]['n']>=20 else 0)
    best_rdd   = max(all_results,
                     key=lambda x: abs(x[4]['annual'])/x[4]['dd']
                     if x[4]['dd'] > 0 and x[4]['n'] >= 20 else 0)

    bal, bsc, bcb, br, bm = best_abs
    rral, rsc, rcb, rr, rm = best_rdd
    brdd = abs(bm['annual'])/bm['dd'] if bm['dd'] > 0 else 0
    rrdd = abs(rm['annual'])/rm['dd'] if rm['dd'] > 0 else 0

    print(f"\n  💰 Best absolute return:   {bal}")
    print(f"     ${bm['final']:,.2f} | {bm['annual']:+.1f}%/yr | "
          f"DD {bm['dd']:.1f}% | R/DD {brdd:.2f}×")

    print(f"\n  🏆 Best risk-adjusted:     {rral}")
    print(f"     ${rm['final']:,.2f} | {rm['annual']:+.1f}%/yr | "
          f"DD {rm['dd']:.1f}% | R/DD {rrdd:.2f}×")

    # Determine the confirmed winner
    winner_label = "A — Flat CB only" if best_abs[0].startswith("A") else best_abs[0]

    print(f"""
  ── FINAL CONCLUSION ────────────────────────────────────────────
  After 19 backtests across every angle:
    Signal quality:    RSI Div + MACD Cross + Volume — proven edge
    Filters:          ALL hurt performance — pure signal wins
    Leverage:         20× safe with our ATR stop (confirmed)
    Risk per trade:   10% corpus (confirmed optimal)
    Circuit breaker:  Flat 48h every 5 losses (confirmed best)
    Progressive scale: see results above — final determination

  ── LOCKED PARAMETERS FOR bot.py ───────────────────────────────

  SYMBOL           = BTC/USDT  (isolated margin futures)
  TIMEFRAME        = 15m
  LEVERAGE         = 20
  RISK_PER_TRADE   = 10%  [adjusted if scaling wins]
  LONG_ATR_MULT    = 2.0
  SHORT_ATR_MULT   = 1.5
  VOL_MULT         = 2.0
  DIV_MEMORY       = 3-bar
  CIRCUIT_BREAKER  = 5 consec losses → 48h pause (flat)
  SCALING          = [see results — standard or none]
  DCA              = $10/mo on 10th, +10%/yr  (corpus_manager.py)
  CORPUS_REFRESH   = 10 trades  (ratchet up/down)
  FEES             = 0.05% per side

  📌 BACKTESTING COMPLETE.  Next: build bot.py
    """)
    print(f"{'═'*80}\n")


if __name__ == "__main__":
    main()