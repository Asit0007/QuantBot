# Multi-market research log

Every configuration tested, and why it was kept or rejected. Mirrors the role
`backtest/README.md` plays for BTC — with one deliberate difference: this folder
is **committed and linted in CI**. The gitignored `backtest/` folder had all ten
of its scripts silently broken by an IDE import rewrite for nine days, precisely
because nothing exercised it automatically.

**Count every configuration.** QuantBot already has ~100 tested against one
6.9-year BTC sample, which is why its own log concludes test #101 is *less*
trustworthy than test #1. The number in the left column is the whole point.

---

## NSE — cross-sectional daily equities

Universe: point-in-time top-50 by trailing median turnover, rebalanced ~monthly
(`markets/adapters/nse_bhavcopy.build_universe`). Data: NSE bhavcopy, free.

| # | Date | Config | Result | Verdict |
|---|---|---|---|---|
| 1 | 2026-09-13 | BTC signal shape ported as-is: RSI div + MACD cross + volume>2×SMA20, ATR(3.0) stop, long only, 1×, 5 concurrent, 2015-01→2021-01 | 16 trades, WR 6.2%, −29.7%. **All five gates FAIL** (PF 0.03, p5 0.00, best trade = 100% of gross, 0/6 years positive, LOYO 0.0th pct) | **REJECTED** |

### What config #1 established

**The BTC parameters do not port, and the reason is structural.** The strategy's
lookbacks are expressed in *bars*, and a bar means something entirely different
at 15m and 1d — `div_memory=3` is 45 minutes on BTC and 3 days on NSE.

Two symptoms, one root cause:

- **Entry** fires on 0.02% of bars — the *same* rate as BTC. Not the problem.
- **Exit** is the problem. The opposite 3-gate signal is equally rare, so at
  daily frequency it almost never fires and the ATR stop becomes the *only*
  exit. A strategy where essentially every trade exits at its stop has a win
  rate near zero by construction. Hence 6.2%.

### The entry-quality screen — why this is "no edge", not "wrong exit"

Measured forward returns after the entry signal, independent of any exit rule:

| universe | n | 40d edge over baseline | bootstrap 95% CI |
|---|---|---|---|
| 50 symbols | 30 | **+8.81%** (63% win rate) | — |
| 132 symbols | 98 | **+3.34%** | **[−0.82%, +9.04%]** |

Widening the sample **collapsed** the edge, and every CI straddles zero with
t-stats under 1.4 — themselves optimistic, because forward windows overlap in
time and across correlated symbols. The apparent edge at n=30 was noise. This is
the failure the gates exist to catch, caught before the gates even ran.

**Lesson worth keeping:** a promising result on a small cross-sectional sample
should be re-measured on a wider one *before* any exit logic is designed around
it. Separating entry quality from exit design cost one diagnostic and saved a
tuning spiral.

### Venue constraints now encoded (not strategy choices)

- `allow_short=False` — an NSE **cash** short must be squared off the same day.
  Holding one overnight needs F&O or stock lending (SLB). Roughly half the raw
  signals are shorts, so booking them would have been untradeable fiction.
- `gaps_overnight=True` — forces a real stop and makes `nostop` raise. The
  no-stop loss ceiling assumes price moves *continuously* to the liquidation
  level; a market shut 18h a day gaps straight through it.
- `long_atr_mult` / `short_atr_mult` are config, not constants. bot.py hardcodes
  8.0/6.0 — BTC's validated values, meaningless elsewhere.

### Open, not yet tested

- Full 2015→2026 range (config #1 ran to 2021-01 only; the later data was still
  downloading).
- Whether *any* daily-frequency signal works on this universe — config #1 tested
  one ported signal, which is not the same question.
- ETFs carry `SctySrs=EQ` and enter a liquidity-ranked universe (SILVERBEES
  appeared in a 2026 top-50). Left unfiltered deliberately; whether they belong
  is a research decision.

---

## US indices

Not started. Blocked on a free Alpaca key.
