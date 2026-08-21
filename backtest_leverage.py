"""
backtest_leverage.py — Config A across 5 Leverage / Stop-Width Tiers
════════════════════════════════════════════════════════════════════
Answers: "What happens if we trade the exact live signal at lower
leverage with wider stops instead of 20x?"

SIGNAL — IDENTICAL to the live bot.py (Config A), not a variant:
  Entry:  bull/bear armed (RSI div, DIV_MEMORY=3 candles)
          AND MACD cross (12/26/9) same direction
          AND volume > 2x 20-bar SMA
  Exit:   opposite signal (same 3-gate check) OR ATR stop hit
  Same exact process() sequencing as bot.py: DCA -> update armed
  -> exits -> entries (an exit and a fresh entry CAN occur on the
  same candle, exactly as in production).

WHY LEVERAGE ALONE CAN'T BE VARIED IN ISOLATION:
  The stop-distance-aware sizing fix (qty = dollar_risk /
  (stop_distance x leverage)) makes $ risk per trade EXACTLY
  10% of corpus regardless of leverage, AS LONG AS stop_distance
  stays fixed. So if we only changed LEVERAGE and kept the same
  ATR multiplier, every tier would produce IDENTICAL $ results —
  leverage would be provably irrelevant to backtest P&L. That
  would not be a useful test.

  Instead, per the liquidation-safety table already discussed,
  each lower-leverage tier gets a PROPORTIONALLY WIDER stop,
  preserving the same safety-buffer ratio to liquidation:

    Leverage   Liquidation dist   Safe-stop ceiling (60%)   ATR mult scale
    20x (base)      ~5%                  ~3.0%                  1.000x
    15x             ~6.7%                ~4.0%                  1.333x
    10x             ~10%                 ~6.0%                  2.000x
    7x              ~14.3%               ~8.6%                  2.857x
    5x              ~20%                 ~12.0%                 4.000x

  mult_scale = 20 / leverage_tier
  LONG_ATR_MULT_tier  = 2.0 * mult_scale   (base LONG  mult = 2.0)
  SHORT_ATR_MULT_tier = 1.5 * mult_scale   (base SHORT mult = 1.5)

WHAT THIS ACTUALLY TESTS:
  Wider stop -> smaller qty for the same 10% dollar risk (sizing
  auto-compensates) -> potentially fewer whipsaw stop-outs (better
  WR) BUT smaller position size means smaller $ profit on trades
  that exit via opposite-signal (not stop). Whether wider-stop/
  lower-leverage nets out better or worse is exactly what running
  the real 6.5-year BTC data through the real signal will show.

RUIN CHECK (added 2026-08-21) — READ THIS BEFORE COMPARING TO OLD OUTPUT:
  Earlier runs of this script had NO bankruptcy check. Position size is
  10% of CORPUS, and corpus only ratchets down after N CONSECUTIVE
  losses, so a fast losing stretch kept risking a corpus-derived amount
  larger than the balance actually left — driving `balance` negative and
  continuing to trade. That is why the old output reported a "max
  drawdown" above 100%: dd = (peak-eq)/peak only exceeds 100% when
  equity goes negative.

  Now: the moment balance hits <= 0 the account is declared RUINED,
  equity is floored at 0 (an exchange liquidates you at the liquidation
  price — you lose the margin posted, you do not end up owing money),
  and the simulation STOPS. No DCA, no further trades, no recovery.
  `ruin_overshoot` records how far below zero the unconstrained sim
  would have gone; `max_margin_ratio` records the worst
  required-margin / available-balance ratio reached (diagnostic only —
  it does NOT cap trade size).

  Consequence: terminal-equity and CAGR figures from before this change
  assumed infinite margin and are not comparable to the numbers below.

IDENTICAL FRAMEWORK ACROSS ALL TIERS:
  10% risk/trade, flat CB (5 losses -> 48h), DCA $10/mo +10%/yr,
  CorpusManager ratchet (10 trades) — only LEVERAGE and the two
  ATR multipliers change between tiers.
════════════════════════════════════════════════════════════════════
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

# ─── Fixed framework (identical across all tiers) ─────────────────
SYMBOL           = "BTC/USDT"
INITIAL_BALANCE  = 100.0
BASE_RISK        = 0.10
FEE_RATE         = 0.0005
DCA_BASE         = 10.0
DCA_GROWTH       = 0.10
DCA_DAY          = 10
START_YEAR       = 2019
CORPUS_REFRESH   = 10
FLAT_CB_TRIGGER  = 5
FLAT_CB_HOURS    = 48
CANDLE_MINUTES   = 15
CANDLES_PER_YEAR = int(365.25 * 24 * 60 / CANDLE_MINUTES)   # ≈ 35,064

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

# Base ATR multipliers — calibrated and backtest-validated at 20x
BASE_LONG_ATR_MULT  = 2.0
BASE_SHORT_ATR_MULT = 1.5

# ─── Leverage / stop-width tiers ──────────────────────────────────
# mult_scale = 20/leverage preserves the SAME ratio of realized stop
# distance to each tier's liquidation-safety ceiling (see docstring).
TIERS = [
    # (label, leverage, mult_scale)
    ("20x (current)", 20, 20/20),
    ("15x",            15, 20/15),
    ("10x",            10, 20/10),
    ("7x",              7, 20/7),
    ("5x",              5, 20/5),
]

BASELINE = {'final': 4699.0, 'annual': 45.0, 'dd': 28.5,
            'pf': 1.78, 'wr': 12.4, 'rdd': 1.58}


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


# ─── Indicators — EXACT replica of bot.py's build_indicators() ────
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


# ─── Simulation — mirrors bot.py's process() sequencing exactly ──
def simulate(df: pd.DataFrame, leverage: int,
             long_atr_mult: float, short_atr_mult: float) -> dict:

    cm = CorpusManager(
        initial_balance=INITIAL_BALANCE, base_monthly_dca=DCA_BASE,
        dca_annual_growth=DCA_GROWTH, ratchet_up_every=CORPUS_REFRESH,
        ratchet_down_after=CORPUS_REFRESH,
    )

    balance     = INITIAL_BALANCE
    total_fees  = 0.0
    total_inv   = INITIAL_BALANCE
    trades      = []
    eq_curve    = [(None, INITIAL_BALANCE)]
    last_dca    = None
    consec      = 0
    cb_until    = None
    cb_count    = 0
    pos         = None       # {'side','entry','stop','qty','fee_in','bar'}
    bull_armed  = 0
    bear_armed  = 0

    # Track realized stop distances (as % of price) for sanity check
    stop_dists_pct = []
    liq_dist_pct   = 100.0 / leverage

    # ── Ruin tracking ─────────────────────────────────────────────
    # Without this the sim can drive `balance` below zero and keep
    # trading: size is 10% of CORPUS, and corpus only ratchets down
    # after 10 CONSECUTIVE losses, so a fast losing stretch keeps
    # risking a corpus-derived amount larger than the balance that is
    # actually left. That is what produced a >100% "max drawdown" —
    # (peak-eq)/peak only exceeds 100% when eq goes negative.
    # A real exchange liquidates instead. Model that: the account dies.
    ruined         = False
    ruin_ts        = None
    ruin_bar       = None
    ruin_overshoot = 0.0    # how far below zero it WOULD have gone
    margin_ratios  = []     # required margin / balance at each entry

    rows = df.reset_index().to_dict('records')

    for i, row in enumerate(rows):
        if ruined:
            break          # account is dead — no DCA, no signals, no trades
        ts    = row.get('timestamp', row.get('index'))
        price = float(row['close'])
        atr   = float(row['atr']) if not (isinstance(row['atr'], float)
                                          and math.isnan(row['atr'])) else 0.0

        # ── 1. DCA ────────────────────────────────────────────────
        if hasattr(ts, 'day') and ts.day == DCA_DAY:
            key = (ts.year, ts.month)
            if key != last_dca:
                res        = cm.on_monthly_refresh(balance, ts.year, ts.month, START_YEAR)
                balance   += res['contribution']
                total_inv += res['contribution']
                eq_curve.append((ts, balance))
                last_dca   = key

        # ── 2. Update armed counters (identical to bot.py) ────────
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
            nonlocal ruined, ruin_ts, ruin_bar, ruin_overshoot
            ep      = price
            fee_out = ep * pos['qty'] * FEE_RATE
            if pos['side'] == 'long':
                raw = (ep - pos['entry']) * pos['qty'] * leverage
            else:
                raw = (pos['entry'] - ep) * pos['qty'] * leverage
            pnl = raw - pos['fee_in'] - fee_out
            total_fees += pos['fee_in'] + fee_out
            balance    += pnl
            if balance <= 0 and not ruined:
                # Account is gone. An exchange closes you out at the
                # liquidation price — you lose the margin posted, you do
                # not end up owing money — so floor equity at zero and
                # record how far past zero the unconstrained sim went.
                ruined         = True
                ruin_ts        = ts
                ruin_bar       = i
                ruin_overshoot = balance
                balance        = 0.0
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

        # ── 3. Exits (signal takes precedence over stop, as in bot.py) ─
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

        # ── 4. Circuit breaker gate ────────────────────────────────
        in_cb = cb_until is not None and i < cb_until

        # ── 5. Entries — only if flat (can re-enter same candle) ──
        if pos is None and not in_cb and atr > 0 and not ruined:
            side = stop = None
            if bull_armed > 0 and macd_bull and high_vol:
                side = 'long'
                stop = price - long_atr_mult * atr
                stop_dist = max(abs(price - stop), price * 0.0001)
            elif bear_armed > 0 and macd_bear and high_vol:
                side = 'short'
                stop = price + short_atr_mult * atr
                stop_dist = max(abs(stop - price), price * 0.0001)

            if side is not None:
                qty    = (corpus * BASE_RISK) / (stop_dist * leverage)
                fee_in = price * qty * FEE_RATE
                # Diagnostic only — does NOT cap the trade. Shows how far
                # the corpus-based size outruns the balance actually left.
                if balance > 0:
                    margin_ratios.append((qty * price / leverage) / balance)
                balance    -= fee_in
                total_fees += fee_in
                if balance <= 0:
                    ruined         = True
                    ruin_ts        = ts
                    ruin_bar       = i
                    ruin_overshoot = balance
                    balance        = 0.0
                    eq_curve.append((ts, balance))
                else:
                    pos = {'side': side, 'entry': price, 'stop': stop,
                           'qty': qty, 'fee_in': fee_in, 'bar': i}

    eq_curve.append((None, balance))
    avg_stop_pct = float(np.mean(stop_dists_pct)) if stop_dists_pct else 0.0
    return {
        'trades': trades, 'eq_curve': eq_curve, 'final': balance,
        'total_inv': total_inv, 'total_fees': total_fees,
        'cb_count': cb_count, 'avg_stop_pct': avg_stop_pct,
        'liq_dist_pct': liq_dist_pct,
        'ruined': ruined, 'ruin_ts': ruin_ts, 'ruin_bar': ruin_bar,
        'ruin_overshoot': ruin_overshoot,
        'max_margin_ratio': (float(max(margin_ratios)) if margin_ratios else 0.0),
    }


# ─── Metrics ──────────────────────────────────────────────────────
def metrics(result: dict, years: float) -> dict:
    T = result['trades']
    n = len(T)
    if n == 0:
        return {'n':0,'pf':0,'wr':0,'dd':0,'ret':0,'annual':0,
                'final':result['final'],'ml':0,'sharpe':0,
                'stop_pct':0,'signal_pct':0,'longs':0,'shorts':0,
                'avg_w':0,'avg_l':0,'cb_count':result['cb_count'],
                'net_profit':result['final']-result['total_inv'],
                'total_inv':result['total_inv'],
                'total_fees':result['total_fees'],
                'avg_stop_pct':result['avg_stop_pct'],
                'liq_dist_pct':result['liq_dist_pct'],
                'ruined':result['ruined'],'ruin_ts':result['ruin_ts'],
                'ruin_overshoot':result['ruin_overshoot'],
                'max_margin_ratio':result['max_margin_ratio']}
    pnls  = [t['pnl'] for t in T]
    wins  = [p for p in pnls if p > 0]
    loss  = [p for p in pnls if p <= 0]
    pf    = sum(wins)/abs(sum(loss)) if loss and sum(loss)!=0 else 999
    wr    = len(wins)/n*100
    ret   = (result['final']-result['total_inv'])/result['total_inv']*100
    ann   = ret/years if years>0 else 0
    eq    = np.array([e[1] for e in result['eq_curve']],dtype=float)
    peaks = np.maximum.accumulate(eq)
    dd    = float(((peaks-eq)/peaks*100).max())
    ra    = np.diff(eq)/np.where(eq[:-1]==0,1,eq[:-1])
    sh    = (float((ra.mean()/ra.std())*math.sqrt(CANDLES_PER_YEAR))
             if len(ra)>1 and ra.std()>0 else 0)
    ml=cl=0
    for p in pnls:
        if p>0: cl=0
        else: cl+=1; ml=max(ml,cl)
    stop_n   = sum(1 for t in T if t['reason']=='stop')
    signal_n = sum(1 for t in T if t['reason']=='signal')
    return {'n':n,'pf':pf,'wr':wr,'dd':dd,'ret':ret,'annual':ann,
            'final':result['final'],'ml':ml,'sharpe':sh,
            'stop_pct':stop_n/n*100,'signal_pct':signal_n/n*100,
            'longs':sum(1 for t in T if t['side']=='long'),
            'shorts':sum(1 for t in T if t['side']=='short'),
            'avg_w':float(np.mean(wins)) if wins else 0,
            'avg_l':float(abs(np.mean(loss))) if loss else 0,
            'cb_count':result['cb_count'],
            'net_profit':result['final']-result['total_inv'],
            'total_inv':result['total_inv'],
            'total_fees':result['total_fees'],
            'avg_stop_pct':result['avg_stop_pct'],
            'liq_dist_pct':result['liq_dist_pct'],
            'ruined':result['ruined'],'ruin_ts':result['ruin_ts'],
            'ruin_overshoot':result['ruin_overshoot'],
            'max_margin_ratio':result['max_margin_ratio']}


def pf_i(v):  return "🟢" if v>=1.5 else("🟡" if v>=1.0 else"🔴")
def dd_i(v):  return "🟢" if v<=15  else("🟡" if v<=30   else"🔴")
def rd_i(v):  return "🟢" if v>=2.0 else("🟡" if v>=1.0 else"🔴")
def safe_i(realized, ceiling):
    return "🟢" if realized <= ceiling*0.9 else ("🟡" if realized <= ceiling else "🔴")


def print_tier_detail(label, leverage, result, m, years):
    rdd = abs(m['annual'])/m['dd'] if m['dd']>0 else 0
    t22 = sum(t['pnl'] for t in result['trades']
              if hasattr(t['date'],'year') and t['date'].year==2022)

    print(f"\n{'█'*66}")
    print(f"  {label}  —  {leverage}x leverage")
    print(f"{'█'*66}")

    if m['n'] == 0:
        print(f"  ❌ No trades.")
        return

    if m['ruined']:
        rd = str(m['ruin_ts'])[:10] if m['ruin_ts'] is not None else "?"
        print(f"\n  {'💀'*22}")
        print(f"  ACCOUNT RUINED on {rd} — balance hit zero after "
              f"{m['n']} trades.")
        print(f"  Unconstrained sim would have gone to "
              f"${m['ruin_overshoot']:,.2f} and kept trading.")
        print(f"  Everything below is the run UP TO ruin. There is no "
              f"recovery — the run stops here.")
        print(f"  {'💀'*22}")

    print(f"\n  ╔{'═'*58}╗")
    print(f"  ║  {'Invested (inc DCA):':<26} ${m['total_inv']:>27,.2f}  ║")
    print(f"  ║  {'Final Balance:':<26} ${m['final']:>27,.2f}  ║")
    print(f"  ║  {'Net Profit:':<26} ${m['net_profit']:>+27,.2f}  ║")
    print(f"  ║  {'Annual ROI:':<26} {m['annual']:>+27.1f}%  ║")
    print(f"  ║  {'Total Fees:':<26} ${m['total_fees']:>27.2f}  ║")
    print(f"  ╠{'═'*58}╣")
    print(f"  ║  {'Trades (L / S):':<26} {m['n']:>3} ({m['longs']}L / {m['shorts']}S){'':<15}  ║")
    print(f"  ║  {'Win Rate:':<26} {m['wr']:>27.1f}%  ║")
    print(f"  ║  {'Stop-exit / Signal-exit:':<26} "
          f"{m['stop_pct']:>12.1f}% / {m['signal_pct']:>10.1f}%  ║")
    print(f"  ║  {'Profit Factor:':<26} {pf_i(m['pf'])}{m['pf']:>27.2f}  ║")
    print(f"  ║  {'Max Drawdown:':<26} {dd_i(m['dd'])}{m['dd']:>27.1f}%  ║")
    print(f"  ║  {'Return / DD:':<26} {rd_i(rdd)}{rdd:>26.2f}×  ║")
    print(f"  ║  {'Sharpe Ratio:':<26} {m['sharpe']:>27.1f}  ║")
    print(f"  ║  {'Avg Win / Avg Loss:':<26} ${m['avg_w']:>11.2f} / ${m['avg_l']:>11.2f}  ║")
    print(f"  ║  {'Max Loss Streak:':<26} {'▼'+str(m['ml'])+' consec':>28}  ║")
    print(f"  ║  {'CB fires:':<26} {m['cb_count']:>28}  ║")
    print(f"  ║  {'2022 P&L:':<26} ${t22:>+27.2f}  ║")
    print(f"  ╠{'═'*58}╣")
    print(f"  ║  {'Avg realized stop dist:':<26} {m['avg_stop_pct']:>27.2f}%  ║")
    print(f"  ║  {'Liquidation distance:':<26} {m['liq_dist_pct']:>27.2f}%  ║")
    mmr = m['max_margin_ratio']
    mmr_i = "🟢" if mmr <= 1.0 else ("🟡" if mmr <= 2.0 else "🔴")
    print(f"  ║  {'Peak margin / balance:':<26} {mmr_i}{mmr:>26.2f}×  ║")
    safety_ratio = m['avg_stop_pct']/m['liq_dist_pct']*100 if m['liq_dist_pct']>0 else 0
    print(f"  ║  {'Stop uses % of liq buffer:':<26} "
          f"{safe_i(m['avg_stop_pct'], m['liq_dist_pct'])}{safety_ratio:>26.1f}%  ║")
    print(f"  ╚{'═'*58}╝")

    yrs = sorted(set(t['date'].year for t in result['trades']
                     if hasattr(t['date'],'year')))
    running = INITIAL_BALANCE
    print(f"\n  {'Year':<6} {'N':>4} {'WR':>5} {'PF':>6} {'Ret%':>8} {'Balance':>11}  Note")
    print(f"  {'─'*56}")
    for yr in yrs:
        yt  = [t for t in result['trades']
               if hasattr(t['date'],'year') and t['date'].year==yr]
        if not yt: continue
        yp  = [t['pnl'] for t in yt]
        yw  = [p for p in yp if p>0]
        yl  = [p for p in yp if p<=0]
        ypf = sum(yw)/abs(sum(yl)) if yl and sum(yl)!=0 else 999
        ywr = len(yw)/len(yt)*100
        yret= sum(yp)/running*100 if running>0 else 0
        running += sum(yp)
        note = "⚠️ BEAR" if yr==2022 else("🚀" if yr in(2020,2021,2023,2024) else"")
        pfs = f"{ypf:.2f}" if ypf<900 else "∞"
        print(f"  {yr:<6}{len(yt):>4} {ywr:>4.0f}% {pf_i(ypf)}{pfs:>5} "
              f"{yret:>+7.1f}%  ${running:>10,.2f}  {note}")
    print(f"  {'─'*56}")
    print(f"  {'TOT':<6}{m['n']:>4} {m['wr']:>4.0f}% {pf_i(m['pf'])}{m['pf']:>5.2f} "
          f"{m['ret']:>+7.1f}%  ${m['final']:>10,.2f}")


# ─── Main ─────────────────────────────────────────────────────────
def main():
    exchange = ccxt.binanceusdm({
        'enableRateLimit': True, 'options': {'defaultType': 'future'}
    })
    print("📡 Connecting to Binance...")
    exchange.load_markets()
    sym = 'BTC/USDT' if 'BTC/USDT' in exchange.symbols else 'BTC/USDT:USDT'
    print(f"✅ {sym}\n")

    print("═"*66)
    print("  CONFIG A across 5 Leverage / Stop-Width Tiers")
    print("  (identical live signal — RSI Div + MACD Cross + Vol Spike)")
    print("═"*66)

    print(f"\n  📥 Fetching 15m {sym} data (fetched ONCE, reused for all tiers)...")
    df = fetch_ohlcv(exchange, sym, "15m", "2019-09-01")
    years = (df.index[-1] - df.index[0]).days / 365.25
    print(f"\n  ✅ {len(df):,} candles  [{years:.1f} years]")

    warmup = max(RSI_LEN, MACD_SLOW, VOL_SMA_PERIOD, ATR_PERIOD,
                DIV_WINDOW+DIV_SHIFT) + 5
    df = df.iloc[warmup:].copy()
    df = build_indicators(df)

    print(f"\n{'═'*66}")
    print("  Running 5 leverage tiers on identical 6.5-year dataset...")
    print("═"*66)

    all_results = {}
    for label, leverage, mult_scale in TIERS:
        long_mult  = BASE_LONG_ATR_MULT  * mult_scale
        short_mult = BASE_SHORT_ATR_MULT * mult_scale
        print(f"\n  ── {label}  "
              f"(LONG_ATR_MULT={long_mult:.2f}  SHORT_ATR_MULT={short_mult:.2f})")
        result = simulate(df.copy(), leverage, long_mult, short_mult)
        m      = metrics(result, years)
        rdd    = abs(m['annual'])/m['dd'] if m['dd']>0 else 0
        t22    = sum(t['pnl'] for t in result['trades']
                     if hasattr(t['date'],'year') and t['date'].year==2022)
        all_results[label] = (result, m, leverage)
        ruin_tag = (f"  💀 RUINED {str(m['ruin_ts'])[:10]}"
                    if m.get('ruined') else "")
        print(f"     ${m['final']:>9,.0f}  {m['annual']:>+6.1f}%/yr  "
              f"DD:{m['dd']:>5.1f}%  R/DD:{rdd:.2f}×  "
              f"N:{m['n']}  WR:{m['wr']:.1f}%  PF:{m['pf']:.2f}  "
              f"avg_stop:{m['avg_stop_pct']:.2f}%  2022:${t22:>+.0f}{ruin_tag}")

    # ── Full detail per tier ──────────────────────────────────────
    print(f"\n\n{'═'*66}")
    print("  FULL DETAIL — each leverage tier")
    print("═"*66)
    for label, leverage, mult_scale in TIERS:
        result, m, lev = all_results[label]
        print_tier_detail(label, lev, result, m, years)

    # ── Master comparison ─────────────────────────────────────────
    print(f"\n\n{'═'*100}")
    print("  📊 MASTER COMPARISON — leverage/stop-width tiers")
    print(f"{'═'*100}")
    print(f"  {'Tier':<15} {'Final':>9} {'Ann%':>7} {'PF':>6} {'DD':>6} "
          f"{'R/DD':>6} {'N':>5} {'WR':>5} {'Stop%':>6} {'AvgStopDist':>12} "
          f"{'2022':>8}  {'Ruin':<12}")
    print(f"  {'─'*112}")

    # A ruined tier cannot be "best" at anything — exclude them outright.
    survivors = {k: v for k, v in all_results.items()
                 if not v[1].get('ruined')} or all_results
    best_final = max(survivors.items(), key=lambda x: x[1][1]['final'])
    best_rdd   = max(survivors.items(),
                     key=lambda x: (abs(x[1][1]['annual'])/x[1][1]['dd']
                                    if x[1][1]['dd']>0 else 0))

    for label, leverage, mult_scale in TIERS:
        result, m, lev = all_results[label]
        rdd = abs(m['annual'])/m['dd'] if m['dd']>0 else 0
        t22 = sum(t['pnl'] for t in result['trades']
                  if hasattr(t['date'],'year') and t['date'].year==2022)
        mk = ""
        if m.get('ruined'):
            mk = "  💀"
        else:
            if label == best_final[0]: mk += " ← best $"
            if label == best_rdd[0]:   mk += " ← best R/DD"
        ruin_col = (str(m['ruin_ts'])[:10] if m.get('ruined') else "survived")
        print(f"  {label:<15} ${m['final']:>8,.0f} "
              f"{m['annual']:>+5.1f}% {pf_i(m['pf'])}{m['pf']:>4.2f} "
              f"{dd_i(m['dd'])}{m['dd']:>4.1f}% {rd_i(rdd)}{rdd:>4.2f}× "
              f"{m['n']:>5} {m['wr']:>4.1f}% {m['stop_pct']:>5.0f}% "
              f"{m['avg_stop_pct']:>10.2f}%  ${t22:>+7.0f}  {ruin_col:<12}{mk}")

    print(f"\n{'═'*100}")
    print(f"  📌 20x baseline for reference: $4,699  +45.0%/yr  DD 28.5%  "
          f"R/DD 1.58×  WR 12.4%  PF 1.78")
    print(f"{'═'*100}")

    # ── Verdict ────────────────────────────────────────────────────
    print(f"\n  Ranking by final balance (highest first):")
    ranked = sorted(all_results.items(),
                    key=lambda x: (x[1][1].get('ruined', False),
                                   -x[1][1]['final']))
    for i, (label, (result, m, lev)) in enumerate(ranked, 1):
        rdd = abs(m['annual'])/m['dd'] if m['dd']>0 else 0
        tag = (f"  💀 RUINED {str(m['ruin_ts'])[:10]}"
               if m.get('ruined') else "")
        print(f"    {i}. {label:<15} ${m['final']:>9,.0f}  "
              f"WR {m['wr']:.1f}%  R/DD {rdd:.2f}×{tag}")

    print(f"\n  ── PARAMETERS USED ─────────────────────────────────────────")
    for label, leverage, mult_scale in TIERS:
        lm = BASE_LONG_ATR_MULT*mult_scale
        sm = BASE_SHORT_ATR_MULT*mult_scale
        print(f"  {label:<15} leverage={leverage:<4} "
              f"LONG_ATR_MULT={lm:.2f}  SHORT_ATR_MULT={sm:.2f}")
    print(f"  Sizing: dollar_risk / (stop_dist × leverage) — 10% corpus/trade always")
    print(f"{'═'*100}\n")


if __name__ == "__main__":
    main()