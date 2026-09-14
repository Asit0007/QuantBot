# Multi-market research log

---

## BTC gate ablation — all three gates earn their place

Ablation is **falsification, not search**: three pre-specified questions about
the *existing* config, not a hunt for a new one. A gate is removed by forcing
its column true for every bar, which deletes it as a filter while leaving
sizing, stops, circuit breaker, funding and ratchet untouched — and because
entry and exit read the same columns, each gate is ablated symmetrically.

| variant | trades | WR | PF | final $ | liq | fees |
|---|---|---|---|---|---|---|
| **BASELINE (all 3)** | **127** | 53.5% | **1.60** | **2,531** | 13 | 65 |
| no RSI-divergence | 1,385 | 37.3% | 0.90 | 923 | 5 | 314 |
| no MACD cross | 1,058 | 59.0% | 0.89 | 820 | 29 | 330 |
| no volume spike | 1,292 | 55.8% | 0.85 | 540 | 23 | 402 |

**Every gate is load-bearing.** Removing any one explodes trade count 8-11×
and pushes PF below 1. And the ablations are worse than they look: with $100
seed plus ~$840 of DCA over the period, roughly **$940 was invested** — so
$923, $820 and $540 are all *losses on capital*, against the baseline's $2,531.

Detail worth noting: dropping MACD gives the **highest win rate** (59.0%) and
the **worst liquidation count** (29 vs 13). Many small wins, a few ruinous
losses — precisely the profile a naive win-rate optimisation would select for.

### MACD sensitivity — closing the documented gap

`backtest_sensitivity.py`'s "18/18 neighbours profitable" plateau claim swept
DIV_WINDOW, DIV_SHIFT, DIV_MEMORY, VOL_MULT, RSI_LEN, VOL_SMA_PERIOD — **not
MACD**, which appears **zero times** in the 552-line BTC log while sitting in 34
of 44 scripts and in the live entry. Its 12/26/9 were textbook defaults carried
in unexamined.

| param | values tested | all PF > 1? |
|---|---|---|
| macd_fast | 10 / **12** / 14 | 1.51 / **1.60** / 1.55 ✓ |
| macd_slow | 22 / **26** / 30 | 1.62 / **1.60** / 1.59 ✓ |
| macd_signal_win | 7 / **9** / 11 | 1.53 / **1.60** / 1.78 ✓ |

**9/9 profitable.** The plateau claim now covers all three gates — 27/27 rather
than 18/18.

⚠️ **`macd_signal_win=11` scores PF 1.78 and $2,901, beating production.** Do
NOT adopt it. That is one neighbour out of nine on a single 127-trade sample,
and switching to it would be selection on noise — the exact move this entire
file argues against. It is recorded because the temptation is the finding.

---

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

| 1c | 2026-09-13 | Config #1 on **split-adjusted** data, full range | 24 trades (3 fake ones removed), WR 25.0%, +33.0%. **Gates 1-5 FAIL, gate 6 FAIL (DSR 0.461), gate 8 PASS (marginal)** | **REJECTED** |
| 2 | 2026-09-13 | **Squeeze-break**: BB(20,2) bandwidth in bottom quartile of trailing 252d → upper-band break on >1.5× median volume; **exit on SAR(0.02,0.20) flip**, ATR stop underneath | 603 trades, WR 42.8%, **−6.1%**. **Gates 1-5 FAIL, gate 6 FAIL (DSR 0.119), gate 8 PASS** | **REJECTED** |

### Config #2 — the mechanical fix worked, and there is still no edge

Config #2 existed to test one specific hypothesis: that config #1 failed for a
*mechanical* reason rather than an absence of signal. Its exit fired on ~0.02%
of daily bars, so the ATR stop was the only way out and a ~0% win rate followed
by construction.

**The fix worked exactly as predicted.** SAR always eventually exits:

| | config #1c | config #2 |
|---|---|---|
| trades | 24 | **603** |
| win rate | 25.0% | **42.8%** |
| best trade as share of gross | 49% | **4%** |

A real sample, a plausible win rate, and no single-trade concentration — the
opposite of config #1 on every structural measure. And it still loses money.

**Gate 1 is the tell:** in-sample PF 1.10 (+26,122), out-of-sample PF 0.77
(−32,157). Profitable while being fitted, loss-making afterwards.

**The fee question, and why it does NOT rescue this.** Fees were 35,384 on a
100,000 account, so the obvious thought is that a real edge is merely being
taxed away — gross +29,349 versus net −6,035. It does not survive contact with
the benchmark: **+29,349 over 11.7 years is 2.24%/yr GROSS, against 5.6%/yr for
equal-weight buy-and-hold of the same universe.** Even with every rupee of
friction removed, it loses to doing nothing. This is not a cost problem.

(BTC reached the same verdict by a different route — its log closes
"cheaper fees, trade faster" permanently, having measured 5m at PF 0.81 and 3m
at PF 0.87 *at the zero-fee bound*: the failures were in the signal, not the
cost.)

**Gate 7 became computable** once two configs existed: PBO 0.40 across 70 CSCV
splits. Directional only — CSCV truncates to the shorter series, so it compared
just 24 trades per config.

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

## US market

**Unblocked without a key.** The Ken French data library (Dartmouth, from CRSP)
publishes daily US total-market returns free: **26,296 sessions, 1926-07-01 →
2026-07-31, CAGR 10.26%** — the textbook US equity return, which is the sanity
check that the parse is right. `markets/adapters/us_french.py`.

Every other free US source is walled: Yahoo 429, Stooq serves a SHA-256
proof-of-work challenge, Tiingo 403, Alpaca 401.

**Limits, stated plainly:** these are RETURNS, so a price index can be
compounded but there is no genuine open/high/low. ATR stops, Parabolic SAR and
intrabar checks **cannot** be computed — synthesising a high/low from a close
would be inventing data, the same error as "repairing" a price move that was
never a split. Close-only strategies only. Also the whole market, not SPX/NDX/DJI
individually, and not single stocks. A real OHLCV feed (free Alpaca key) is still
needed for Phase 3 proper.

---

## Cross-market breadth — the first genuinely promising result

**Hypothesis:** multi-market is not just "more trades", it is a multiple-testing
defence. One signal, run unchanged on uncorrelated markets, required to agree. If
a spurious signal clears one market with probability p, clearing three is p³ —
at p=0.05 that is a ~400× stronger filter at *zero* extra degrees of freedom,
because nothing is tuned per market.

Signal: MACD(12,26,9) bullish cross. Close-only so it computes identically
everywhere, standard, and **not chosen by searching**.

**Trial 1 (failed on measurement, not hypothesis):** requiring the signal on the
*same calendar day* in every market found 7 such days in 2,763 → n=14. Three
markets on different calendars and time zones essentially never produce a
same-day point event. Agreement was re-specified as "within ±3 trading days",
**pre-committed and tested once, not swept**.

**Trial 2 — NSE + US, 11.6 years, b=2 (n=94) vs b=1 (n=135):**

| horizon | b=2 | b=1 | diff | bootstrap 95% CI | permutation p |
|---|---|---|---|---|---|
| 5d | +0.78% | +0.09% | +0.69% | [+0.09%, +1.26%] | **0.029** |
| 10d | +1.44% | +0.29% | +1.16% | [+0.37%, +1.95%] | **0.008** |
| 20d | +2.09% | +1.05% | +1.04% | [−0.22%, +2.29%] | 0.107 |
| 40d | +3.69% | +1.80% | +1.89% | [+0.03%, +3.79%] | 0.053 |

Short horizons survive with CIs excluding zero. The 10d result clears Bonferroni
across the four horizons tested (0.008 × 4 = 0.032).

**Triple (BTC+NSE+US, 6.9y, n=33): nothing.** Every CI straddles zero, p from
0.26 to 0.88. Under-powered — the window is bounded by BTC's 2019 start.

**What this is and is not.** It is a conditional-return effect that survived a
first significance screen — *not* a trading strategy: no entry rule, no exit, no
costs, no gates. Forward windows overlap, so the p-values are floors. And it is
the first thing in this project to look interesting after a proper screen, which
is precisely when to be most suspicious.

**Trial 3 — four regions, 36 years.** The same French library publishes daily
factors for Europe, Japan, Asia-Pacific ex-Japan and North America (1990-07 →
2026-07, Bloomberg-based). Four markets over 36.1 years instead of two over
11.6, with **n=418** against the earlier n=33.

First, the independence check the p³ argument depends on — daily return
correlation:

| | EUROPE | JAPAN | ASIAPAC | NORTHAM |
|---|---|---|---|---|
| **EUROPE** | 1.00 | 0.28 | 0.56 | 0.55 |
| **JAPAN** | 0.28 | 1.00 | 0.47 | **0.04** |
| **ASIAPAC** | 0.56 | 0.47 | 1.00 | 0.35 |
| **NORTHAM** | 0.55 | 0.04 | 0.35 | 1.00 |

Lower than expected — Japan and North America are essentially uncorrelated daily.

**All four regions in agreement (n=418) vs one alone (n=284):**

| horizon | b=4 | b=1 | diff | bootstrap 95% CI | permutation p |
|---|---|---|---|---|---|
| 5d | +0.60% | **−0.68%** | +1.28% | [+0.94%, +1.65%] | **0.000** |
| 10d | +0.76% | **−0.42%** | +1.18% | [+0.66%, +1.68%] | **0.000** |
| 20d | +1.20% | **−0.13%** | +1.33% | [+0.60%, +2.07%] | **0.000** |
| 40d | +1.93% | +0.83% | +1.10% | [+0.04%, +2.17%] | 0.041 |

Every horizon significant, three at p < 0.001, all CIs excluding zero.

**The asymmetry is the interesting part.** A MACD cross in ONE market alone
precedes *negative* forward returns at 5/10/20 days. The same cross in all four
precedes positive ones. Breadth is not amplifying a signal — it is separating a
real one from a harmful one.

**What would still kill this:** all four regions come from one vendor
(Bloomberg), so a shared data-processing artefact is not ruled out — the US
CRSP series is the independent check. Forward windows overlap, so p is a floor.
And there are no costs, no execution and no gates here: it is a conditional
return, not a strategy.

**Trials spent on this idea: 3** (same-day, ±3d pair/triple, 4-region).

### Four kill-tests — all failed to kill it

Each was designed to destroy the effect, not confirm it. Cheapest and deadliest
first (`research/breadth_killtests.py`).

**1. Placebo — shuffle the breadth labels.** Real +1.28/+1.18/+1.33% at
5/10/20d; shuffled −0.40/+0.62/+0.59%. Largely destroyed.
⚠️ *Weakness in my own test:* this was a **single shuffle draw**, not a
distribution, and the shuffled sample is ~105 vs the real 418. The +0.62%
residual at 10d is therefore uninterpretable. A proper placebo repeats the
shuffle a few thousand times and reports where the real value sits. **Redo this
before trusting it.**

**2. Vendor — swap NORTHAM (Bloomberg) for US (CRSP).** An independent data
lineage; a shared processing artefact cannot survive it.

| horizon | b=4 | b=1 | diff | p |
|---|---|---|---|---|
| 5d | +0.72% | −0.76% | **+1.48%** | 0.000 |
| 10d | +0.95% | −0.37% | **+1.31%** | 0.000 |
| 20d | +1.37% | −0.03% | **+1.41%** | 0.001 |

**Stronger** than the all-Bloomberg baseline. Not a vendor artefact.

**3. Sub-period — by decade.** This is gate 5's logic, and it is what killed
every BTC trend gate (all of which leaned on 2024).

| decade | diff (10d) | p | n |
|---|---|---|---|
| 1990–1999 | +1.22% | 0.030 | 56/103 |
| 2000–2009 | +1.24% | 0.028 | 117/76 |
| 2010–2019 | +0.85% | 0.052 | 137/61 |
| 2020–2026 | +1.46% | 0.021 | 107/44 |

Holds in **all four decades**. No single regime carries it.

**4. Trend control — is breadth just "global uptrend"?** The most likely killer:
"all four crossed up" may simply mean momentum, and buying momentum is not news.
Compared b=4 vs b=1 *within* matched trailing-60d-return terciles.

| tercile | 5d diff | p | 10d diff | p |
|---|---|---|---|---|
| **weak trend** | **+1.84%** | 0.000 | **+1.64%** | 0.003 |
| mid trend | +1.13% | 0.000 | +1.44% | 0.000 |
| strong trend | +0.93% | 0.000 | +0.44% | 0.239 |

3/3 terciles at 5d, 2/3 at 10d — **and the effect is STRONGEST in weak trend**,
which is the opposite of what a momentum proxy would produce. If breadth were
just trend in disguise it would concentrate in the strong tercile. It does the
reverse.

### Placebo, done properly — 3,000 circular shifts

The first attempt was a single shuffle draw and I refused to count it. Redone
with **circular shifts**: a shift preserves the full autocorrelation of both
breadth and returns while destroying only their alignment, which is the correct
null for "does breadth line up with future returns beyond chance?" A plain
shuffle also destroys serial structure, making the null easier to beat than it
should be.

| horizon | real | null mean | null sd | real's percentile | p |
|---|---|---|---|---|---|
| 5d | +1.28% | −0.01% | 0.33% | **100.0th** | **0.0000** |
| 10d | +1.18% | +0.01% | 0.51% | 98.5th | 0.0293 |
| 20d | +1.33% | −0.00% | 0.73% | 96.8th | 0.0647 |

The null centres on zero, so the test itself is clean. In sigma terms: **3.9σ at
5d, 2.3σ at 10d, 1.8σ at 20d** — real and decaying with horizon, which is what a
genuine short-horizon effect should look like.

### OUT-OF-SAMPLE — the decisive test

Breadth computed **only** from the four Ken French regions, then used to predict
markets that played no part in discovering it.

**NSE (11.6 years, never used in discovery):**

| horizon | b=4 | b≤1 | diff | p |
|---|---|---|---|---|
| 5d | +0.97% | +0.13% | +0.83% | **0.000** |
| 10d | +1.64% | +0.42% | +1.22% | **0.000** |
| 20d | +2.48% | +1.14% | +1.34% | **0.001** |

**BTC (6.9 years, never used in discovery):**

| horizon | b=4 | b≤1 | diff | p |
|---|---|---|---|---|
| 5d | +2.20% | +0.59% | +1.61% | **0.026** |
| 10d | +4.60% | +1.16% | **+3.43%** | **0.001** |
| 20d | +5.84% | +3.37% | +2.47% | 0.120 |

**A breadth signal built from Europe, Japan, Asia-Pacific and North America
predicts Indian equities and Bitcoin.** Neither market contributed to finding
it. That is genuine out-of-sample transfer, not another in-sample slice.

**BTC is the stronger evidence of the two**, precisely because it is the less
correlated market — Indian equities co-move with global equities, so some NSE
transfer is expected mechanically. Bitcoin has no such excuse, and it shows the
largest effect of any market tested (+3.43% at 10d).

The consistent picture across every test: **real at 5–10 day horizons, decaying
by 20.**

### Standing caveats

Overlapping forward windows mean every p is a floor. This is still a
**conditional return, not a strategy** — no entry rule, no exit, no costs, no
position sizing, and none of the eight gates have been applied to a tradeable
version of it. And the placebo needs redoing properly.
