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
| 1 | 2026-09-13 | BTC signal shape ported as-is: RSI div + MACD cross + volume>2×SMA20, ATR(3.0) stop, long only, 1×, 5 concurrent. Partial range 2015-01→2021-01 | 16 trades, WR 6.2%, −29.7%. **All five gates FAIL** | **REJECTED** |
| 1b | 2026-09-13 | Same config, **full range 2015-01→2026-09** (2,883 sessions, 224 symbols ever selected) | 27 trades, WR 22.2%, **+29.1%**. **All five gates still FAIL** | **REJECTED** |

### Config #1b — why a *positive* headline is still a rejection

The full range flipped the headline from −29.7% to **+29.1%**, and it changes
nothing. This is the clearest illustration in the project of why the headline
number is the least informative statistic available:

| gate | result | |
|---|---|---|
| 1 OOS | IS PF **0.69** (net −9,466) vs OOS PF **4.45** (net +38,578) | FAIL |
| 2 bootstrap | p5 **0.34**, P(PF≤1) **25.9%** | FAIL |
| 3 concentration | best trade = **49%** of gross, top 5 = **99%**; net without the best trade **−5,253** | FAIL |
| 4 per-year | **3/11** years positive | FAIL |
| 5 LOYO | worst year **2024** at the **2.0th** percentile of random drops | FAIL |

Every dollar of profit sits in the out-of-sample tail — the strategy lost money
for six years and made it back recently. Remove the single best trade and it is
net negative. Eight of eleven years lose.

**2024 again.** BTC's research log records that *"any trend gate on this data
leans on 2024."* A completely unrelated market, a completely different signal,
and gate 5 lands on 2024 once more. Worth noting, not yet worth a theory.

**Versus doing nothing:** +29.1% over 11.7 years is **2.2%/yr**. An equal-weight
buy-and-hold of the same starting universe returned **5.6%/yr** over the same
period — and that figure is itself understated (see corporate actions below).
The strategy underperforms buying the universe and never looking at it again,
while being in the market for 27 trades.

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

---

## ⚠️ DATA QUALITY — bhavcopy prices are NOT adjusted for corporate actions

Found 2026-09-13 while computing a benchmark. **This must be fixed before any
further NSE research.**

NSE bhavcopy publishes **raw traded prices**. Splits, bonuses and demergers
appear as enormous single-day crashes that never happened:

- `NIFTYBEES` 2019-12-19: **1292.54 → 130.20**, a ratio of 9.93 — a 1:10 split,
  not a −90% day. This alone made the index proxy print **−68% over 11.7 years**
  for a period in which the Nifty roughly tripled.
- Panel-wide: **1,340 single-day drops worse than −40% across 939 symbols.** NSE
  has a ~20% circuit limit on most stocks, so genuine −40% days are essentially
  impossible. The worst offenders are all ETF splits (GOLDBEES, SETFGOLD, …).

**How much did this affect config #1b? Almost nothing — and the first answer was
wrong.** "13 of 27 trades are in symbols that split at some point" was the wrong
test: a split *somewhere* in a symbol's history is irrelevant if it falls outside
the hold window. The trade carrying 49% of gross profit (TATAMOTORS, +34,365) ran
2023-01→2024-05, while that symbol's discontinuity is the 2025-10 demerger — over
a year after the trade closed, with zero >20% moves inside the hold.

Testing correctly — split **inside** the hold window — gives **2 of 27 trades**
(BPCL, HINDPETRO, both 2016), together **−3,939**. Splits made config #1b's
result slightly *worse*. **The gate failures are genuine.**

*Lesson: "this symbol has a data problem" and "this trade was affected by it" are
different claims. Check the second before reporting the first.*

**Still a blocker for what comes next**, because contamination scales with trade
count and holding period: a strategy that trades more, holds longer, or touches
ETFs will be hit hard. Fix options, in order of preference: fetch NSE's published
corporate-action feed and back-adjust; or detect clean split ratios (2, 5, 10)
beyond the circuit limit and repair heuristically; or exclude affected symbols
(cheapest, but reintroduces survivorship bias by dropping exactly the companies
that restructured).

---

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
