<h1 align="center">⚡ QuantBot</h1>

<p align="center">
  <b>An automated BTC/USDT futures trading bot — and the production infrastructure that keeps it alive.</b><br>
  <i>One quantitatively-validated signal, ~20 backtests over 6.5 years, three containerised services,<br>
  Terraform-provisioned cloud, a CI/CD pipeline that refuses to restart the bot mid-trade,<br>
  and a public HTTPS dashboard behind zero open ports.</i>
</p>

<p align="center">
  <a href="https://github.com/Asit0007/QuantBot/actions/workflows/deploy.yml">
    <img src="https://github.com/Asit0007/QuantBot/actions/workflows/deploy.yml/badge.svg" alt="CI/CD Status" />
  </a>
  <a href="https://quantbot.asitminz.com">
    <img src="https://img.shields.io/badge/dashboard-live-3fb950?logo=plotly&logoColor=white" alt="Live Dashboard" />
  </a>
  <a href="https://github.com/Asit0007/QuantBot/blob/main/LICENSE">
    <img src="https://img.shields.io/github/license/Asit0007/QuantBot?color=blue" alt="License" />
  </a>
  <a href="https://github.com/Asit0007/QuantBot">
    <img src="https://img.shields.io/github/last-commit/Asit0007/QuantBot" alt="Last Commit" />
  </a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/Docker-multi--stage-2496ED?logo=docker&logoColor=white" alt="Docker" />
  <img src="https://img.shields.io/badge/Terraform-~%3E5.0_OCI-7B42BC?logo=terraform&logoColor=white" alt="Terraform" />
  <img src="https://img.shields.io/badge/Oracle_Cloud-Always_Free_ARM-F80000?logo=oracle&logoColor=white" alt="OCI" />
  <img src="https://img.shields.io/badge/Cloudflare-Tunnel-F38020?logo=cloudflare&logoColor=white" alt="Cloudflare" />
  <img src="https://img.shields.io/badge/Telegram-control_plane-26A5E4?logo=telegram&logoColor=white" alt="Telegram" />
</p>

---

## 📍 Status

> [!NOTE]
> **Paper trading in production.** The full stack is deployed, healthy, and processing every
> closed 15-minute candle on Oracle Cloud. **No real capital is at risk yet.**
> The go-live gate is 20+ paper trades landing within ±20% of the backtested
> **36.7% win rate / 1.60 profit factor** — see [Going Live](#-going-live).

| | |
| --- | --- |
| **Mode** | `PAPER_TRADE=true` — simulated fills, no exchange orders |
| **Live config** | BTC/USDT perp · 15m candles · **5× isolated** · 10% corpus risk/trade |
| **Backtest (6.5 yr)** | PF **1.60** · WR **36.7%** · $100 → **$9,347** · Sep 2019 → Mar 2026 — [read the drawdown caveat](#the-decisive-result-5-beats-20) |
| **Dashboard** | [quantbot.asitminz.com](https://quantbot.asitminz.com) — public, read-only, no auth (deliberate) |
| **Infrastructure** | OCI `VM.Standard.A1.Flex` (1 OCPU / 6 GB ARM) · **$0/month** |
| **Deploys** | Push to `main` → lint → selective container rebuild → health check |

---

## 📑 Contents

- [What This Project Demonstrates](#-what-this-project-demonstrates)
- [Architecture](#-architecture)
- [The Strategy](#-the-strategy)
- [Backtest Results](#-backtest-results)
- [Research Log — What Was Tried and Rejected](#-research-log--what-was-tried-and-rejected)
- [Live Dashboard](#-live-dashboard)
- [Telegram Control Plane](#-telegram-control-plane)
- [Infrastructure](#-infrastructure)
- [CI/CD Pipeline](#-cicd-pipeline)
- [Repository Layout](#-repository-layout)
- [Getting Started](#-getting-started)
- [Configuration Reference](#-configuration-reference)
- [Cloud Deployment](#-cloud-deployment)
- [Cloudflare Tunnel](#-cloudflare-tunnel)
- [Operations Runbook](#-operations-runbook)
- [Going Live](#-going-live)
- [Security Posture](#-security-posture)
- [Known Limitations](#-known-limitations)
- [Roadmap](#-roadmap)
- [Engineering Log — Bugs Fought, Lessons Kept](#-engineering-log--bugs-fought-lessons-kept)
- [License & Disclaimer](#-license--disclaimer)

---

## 🎯 What This Project Demonstrates

The trading logic is roughly 1,050 lines of Python. **The surrounding apparatus is the point.**
This is deliberately a DevOps/SRE portfolio piece wearing a quant hat: everything below was
built, broken, debugged, and shipped solo.

| Domain | What's actually here |
| --- | --- |
| **Infrastructure as Code** | Terraform `~> 5.0` OCI provider — VCN, IGW, route table, security list, public subnet, ARM compute, cloud-init bootstrap. Six resources, one `apply`. |
| **Containerisation** | Multi-stage `Dockerfile` — one `base` layer, three runtime targets (`bot` / `notifier` / `dashboard`). Compose v2 stack of four services on one named volume. |
| **CI/CD** | GitHub Actions: lint gate → change detection → **open-position safety gate** → explicit-file scp → per-service selective rebuild → container health check. |
| **Cloud deployment** | Oracle Cloud Always-Free ARM. Zero recurring cost, real 24/7 uptime. |
| **Network security** | Cloudflare Tunnel = **zero inbound ports**. Dashboard bound to `127.0.0.1` inside the host, reached only via nginx. SSH + `:8888` restricted to a single CIDR. |
| **Observability** | Live Plotly Dash dashboard (20 KPIs, 7 charts), Telegram alerting with a 30-minute heartbeat crash detector, rotating structured logs. |
| **Quantitative finance** | RSI divergence + MACD cross + volume spike. Stop-distance-aware position sizing that makes leverage provably neutral. ~20 backtests over 6.5 years across bull, bear, and chop. |
| **Secrets management** | `.env` never committed, never in an image layer, never overwritten by CI. GitHub Secrets for deploy credentials. Hardened `.gitignore` / `.dockerignore`. |
| **Operational discipline** | Exact-pinned dependencies, log rotation caps, a documented runbook, and an honest [limitations](#-known-limitations) list rather than a marketing one. |

---

## 🏗 Architecture

### Runtime topology

Three Python processes, one shared Docker volume, **files as the only IPC**.

```
                              Binance  (ccxt)
                    ┌───────────────┴───────────────┐
                    │                               │
              binanceusdm                      binance spot
             (futures — trading)             (RSI macro radar)
                    │                               │
   ┌────────────────▼─────────────┐   ┌─────────────▼──────────────────┐
   │  bot.py                      │   │  notifier.py                   │
   │  ── trading engine           │   │  ── alerts + remote control    │
   │  the ONLY writer of          │   │  the ONLY process that talks   │
   │  trade state                 │   │  to Telegram                   │
   │                              │   │                                │
   │  imports corpus_manager.py   │   │  TradeWatcher · Heartbeat      │
   │  (sizing ratchet + DCA)      │   │  RSIScanner · DailySummary     │
   │                              │   │  CommandHandler                │
   └──────────────┬───────────────┘   └───────────────┬────────────────┘
                  │ writes                     reads all │ writes RSI + pause flag
                  ▼                                      ▼
        ┌──────────────────────────────────────────────────────────┐
        │   DATA_DIR   —   docker named volume  quantbot_data      │
        │                                                          │
        │   bot_state.json     corpus_state.json   trade_log.csv   │
        │   rsi_history.json   rsi_alert_state.json                │
        │   bot_paused.flag    bot.log             notifier.log    │
        └──────────────────────────┬───────────────────────────────┘
                                   │ reads only — never writes
                                   ▼
                        ┌─────────────────────────┐
                        │  dashboard.py           │
                        │  Dash + Plotly :8050    │
                        │  bound to 127.0.0.1     │
                        └───────────┬─────────────┘
                                    ▼
                        ┌─────────────────────────┐
                        │  nginx:alpine  :8888    │
                        │  themed 401/50x pages   │
                        │  server_tokens off      │
                        └───────────┬─────────────┘
                                    ▼
                        ┌─────────────────────────┐
                        │  cloudflared            │
                        │  outbound tunnel only   │
                        │  ── no inbound ports ── │
                        └───────────┬─────────────┘
                                    ▼
                      https://quantbot.asitminz.com
```

| Process | Role | Writes | Reads |
| --- | --- | --- | --- |
| `bot.py` | Trading engine — **the only writer of trade state** | `bot_state.json`, `corpus_state.json`, `trade_log.csv`, `bot.log` | Binance via ccxt |
| `notifier.py` | Telegram alerts, remote commands, RSI radar | `rsi_alert_state.json`, `rsi_history.json`, `bot_paused.flag`, `notifier.log` | every bot state file |
| `dashboard.py` | Dash/Plotly web UI, 15s auto-refresh | **nothing** | every state file |
| `corpus_manager.py` | Library imported by `bot.py` (also runnable standalone) — position-sizing ratchet + monthly DCA | `corpus_state.json` | — |

### Why files instead of a database

One host, three processes, exactly one writer per file, and state you can `cat` over SSH at
3 a.m. A Postgres or Redis dependency would add an operational failure mode and buy nothing
at this scale. The costs of that choice are real and are listed honestly under
[Known Limitations](#-known-limitations) — non-atomic writes and no file locking are the
two that matter.

### How the processes actually couple

This is the non-obvious part, and it's deliberately dumb-simple:

- **A closed trade** is detected by `notifier.py` watching the **row count** of `trade_log.csv`.
- **An opened position** is detected by watching `position.entry_time` change in `bot_state.json`.
- **A crashed or wedged bot** is detected by watching the wall-clock `last_updated_at` field —
  30+ minutes of silence fires a Telegram crash alert. That field exists *specifically* because
  comparing `now` against the candle's own timestamp produced a false positive on every single
  candle (see [Engineering Log](#-engineering-log--bugs-fought-lessons-kept)).
- **Telegram `/pause`** works by touching `bot_paused.flag`. `bot.py` checks for it before
  opening a position — open positions still exit normally. A pause must never trap a live trade.

### Per-candle order of operations

`QuantBot.process()` runs once per **closed** candle. The loop wakes 6 seconds after each 15m
boundary, discards the still-forming candle (`df.iloc[:-1]`), and dedupes on `last_candle_ts`
so a retry can never double-process:

```
1. DCA check              — monthly contribution on DCA_DAY
2. Update armed counters  — bull_armed / bear_armed set to DIV_MEMORY on a fresh
                            divergence, otherwise decay by 1
3. Exits                  — signal exit takes priority over stop exit
4. Entries                — only if flat, not circuit-breaker-paused, not manually paused
```

An exit and a fresh entry **can** occur on the same candle. The backtests replicate this exact
sequencing — **reordering these steps silently invalidates every backtest number in this
README.**

### Traffic path and network posture

```
Dash 127.0.0.1:8050  →  nginx :8888  →  cloudflared (outbound)  →  https://quantbot.asitminz.com
```

Compose publishes the dashboard on `127.0.0.1` only, so it is unreachable from outside the host
even if a firewall rule were wrong. The OCI security list restricts SSH and direct `:8888` to a
single CIDR. The Cloudflare Tunnel makes the dashboard internet-reachable **without opening a
single inbound port** and keeps the VM's public IP out of DNS.

---

## 📐 The Strategy

> [!IMPORTANT]
> **The strategy is LOCKED.** Signal, timeframe, and risk parameters are the validated output
> of ~20 backtests. They live in `.env`, not in code — but changing any of them invalidates
> every number this README cites. Re-run the backtests, or don't touch them.

### Three gates, one candle

An entry requires **all three gates to fire on the same closed 15-minute candle**. This
selectivity is the whole edge — it is what makes a 36.7% win rate profitable.

```
Gate 1 — RSI(14) Divergence          armed for DIV_MEMORY = 3 candles
    Bullish:  price makes a lower low   +  RSI makes a higher low
    Bearish:  price makes a higher high +  RSI makes a lower high
    Detected over a DIV_WINDOW = 5 rolling window shifted by DIV_SHIFT = 5
    (rolling min/max — deliberately NOT pivot-based swing detection; see
     the lookahead-bias story in the Engineering Log for why)

Gate 2 — MACD(12/26/9) Cross         timing confirmation
    Long:   MACD line crosses above its signal line
    Short:  MACD line crosses below its signal line

Gate 3 — Volume Spike                participation confirmation
    Current candle volume > 2.0 × its 20-bar SMA
    The binding constraint — most candles die here

        ↓ all three aligned ↓

ENTRY   BTC/USDT perp, isolated margin, 5× leverage
STOP    Long  = entry − (ATR(14) × 8.0)
        Short = entry + (ATR(14) × 6.0)
        Fallback ±5% if ATR is NaN
EXIT    Opposite three-gate signal (priority)  OR  ATR stop hit
```

### Position sizing — the one formula not to break

`size_position()` in `bot.py` is **stop-distance-aware**, not notional-based:

```python
dollar_risk   = corpus × RISK_PER_TRADE          # 10% of corpus
stop_distance = abs(entry_price − stop_price)
qty           = dollar_risk / (stop_distance × LEVERAGE)

# P&L at stop = stop_distance × qty × LEVERAGE = dollar_risk   — always
```

This guarantees that a stop-out loses **exactly** `RISK_PER_TRADE × corpus`, *independent of
leverage*. That independence is precisely why the leverage backtest was meaningful: since
leverage alone is provably irrelevant under this sizing, the 5× tier's win came from **stop
width**, not from leverage. A floor of 0.01% of price guards against a degenerate near-zero
stop distance.

<details>
<summary><b>The bug this replaced (worth reading)</b></summary>

The original sizing was `qty = (corpus × RISK × LEVERAGE) / price`, and the P&L calculation
*also* multiplied by leverage — **squaring it**. A 0.47% ATR produced an 18.8% loss instead of
10%. A 2.5% ATR would have wiped the account in a single trade. Fixed in commit `40595b5`.
Any change to this function needs the P&L-at-stop identity re-derived, not merely tested.
</details>

### Risk controls

| Control | Behaviour | Why |
| --- | --- | --- |
| **Risk per trade** | 10% of *corpus* (not balance) as the loss-at-stop | Corpus is the ratcheted, deliberately-lagging sizing base |
| **Circuit breaker** | 5 consecutive losses → **48h flat pause** on new entries | Backtested against 5 alternatives; flat beats progressive scaling because loss streaks cluster *immediately before* big reversals — scaling down means missing the recovery |
| **Corpus ratchet ↑** | After 10 completed trades with a net gain → `corpus = balance` | Locks in profit before sizing up |
| **Corpus ratchet ↓** | After 10 consecutive losses → `corpus = balance` | Stops throwing good money after bad |
| **Ratchet reset** | 10 trades with a net loss → cycle counter resets only | Neither scale up nor down on chop |
| **DCA** | $10/month on the 10th, +10%/yr step-up from `START_YEAR` | Compounds a small account with new capital, not just returns |
| **Manual pause** | Telegram `/pause` blocks new entries; open positions still exit | An operator kill-switch must never trap a live trade |
| **Paper default** | `PAPER_TRADE=true` unless *both* `.env` and `--live` say otherwise | Fail-safe in the direction of not trading |

---

## 📊 Backtest Results

Validated across **~20 backtests** spanning **6.5 years (Sep 2019 → Mar 2026)** — a COVID
crash, two bull cycles, the FTX bear market, and the ETF cycle.

### The decisive result: 5× beats 20×

Both tiers run the **identical signal**. Only leverage and stop width change — ATR multipliers
are scaled by `20/leverage` so risk-to-liquidation stays constant across tiers.

| Tier | ATR stops (L/S) | Terminal equity | CAGR | Profit factor | Win rate | Verdict |
| --- | --- | --- | --- | --- | --- | --- |
| 20× | 2.0× / 1.5× | $4,699 | +45%/yr | 1.78 | 12.4% | superseded |
| **5×** | **8.0× / 6.0×** | **$9,347** | **+90.7%/yr** | **1.60** | **36.7%** | ✅ **in production** |

Wider stops trade a *lower* profit factor for a *far* higher win rate and roughly double the
terminal equity — fewer trades die to whipsaw noise before the thesis plays out.

> [!WARNING]
> **Read this before believing the headline number.**
>
> The 5× run reports a **max drawdown of 184.2%**. A drawdown above 100% is not a formatting
> quirk — the backtest computes `(peak − equity) / peak`, so exceeding 100% means **simulated
> equity went negative at some point on the path.**
>
> The cause is structural: the backtest has **no bankruptcy or margin-call check**. Position size
> is 10% of *corpus*, and corpus only ratchets down after 10 *consecutive* losses — so during a
> sharp losing stretch the sim can keep risking a corpus-derived amount larger than the balance
> that is actually left, drive the balance below zero, and carry on trading. A real exchange
> would have liquidated the account and the run would have ended there.
>
> **What this means in practice:** `$100 → $9,347` is the return of a path that includes a
> stretch no real account would have survived. Treat the win rate, profit factor, and per-trade
> distribution as the trustworthy outputs of these backtests, and treat the terminal-equity and
> CAGR figures as an upper bound that assumes infinite margin. Adding a ruin check and re-running
> both sweeps is [on the roadmap](#-roadmap) and is a gate on deploying real capital — it is the
> reason the live account starts at $100 rather than at a size that would hurt to lose.

### Per-year regime breakdown

> ⚠️ This table is from the original **20× signal-calibration** run. It is kept because the
> per-regime shape is what matters — the 5× tier supersedes it for totals.

| Year | Regime | P&L | Note |
| --- | --- | --- | --- |
| 2019 | Neutral | small loss | Warm-up period only |
| 2020 | Bull | **+206%** | COVID crash and recovery |
| 2021 | Bull | **+137%** | BTC all-time-high cycle |
| 2022 | Bear | **−44%** | Worst year — FTX collapse |
| 2023 | Bull | **+325%** | Recovery and accumulation |
| 2024 | Bull | **+126%** | ETF approval cycle |
| **Total** | **6.5 yrs** | **+45%/yr** | $100 → $4,699 *(20× run)* |

### Locked production configuration

| Parameter | Value | Rationale |
| --- | --- | --- |
| Symbol | `BTC/USDT` perp | Deepest liquidity, tightest spreads |
| Timeframe | `15m` | Signal quality vs. noise; one decision per candle is the design ceiling |
| Leverage | **5×** isolated | Best terminal equity of 5 tiers tested |
| Risk per trade | 10% of corpus | Validated across the full 6.5 years |
| ATR stop — long | `8.0×` ATR(14) | Scaled 4× from the 2.0× base of the 20× tier |
| ATR stop — short | `6.0×` ATR(14) | Scaled 4× from the 1.5× base; asymmetric because downside moves are faster |
| Circuit breaker | 5 losses → 48h | Flat pause; beat all 4 progressive-scaling variants |
| Ratchet | 10 up / 10 down | Beat 5/5, 2/2, and 2/10 asymmetric at the 5× tier |
| Fee model | 0.05% per side (taker) | Binance futures taker rate, charged on entry and exit |
| Win rate | **36.7%** | Benchmark for the go-live gate |
| Profit factor | **1.60** | Gross profit ÷ gross loss |
| Total return | **$100 → $9,347** (+90.7%/yr) | 6.5 yr, `backtest_ratchet.py`, ratchet 10/10 |

`BENCH_WR = 0.367` and `BENCH_PF = 1.60` are hardcoded in **both** `bot.py` and `dashboard.py`.
If the config ever changes, both must change together. In paper mode the bot prints a live-vs-
benchmark comparison every 5 trades once 20 trades exist.

### Methodology

| Script | Question it isolates | Result |
| --- | --- | --- |
| [`backtest_leverage.py`](backtest_leverage.py) | Same signal at 5 leverage/stop-width tiers (20× / 15× / 10× / 7× / 5×), ATR multipliers scaled by `20/lev` so risk-to-liquidation is constant | **5× wins** — $9,347 vs $4,699. Switched production from 20× to 5×. |
| [`backtest_ratchet.py`](backtest_ratchet.py) | Corpus ratchet frequency at the winning 5× tier: 10/10, 5/5, 2/2, 2/10 asymmetric | **10/10 baseline kept** — what production runs today |

Every backtest shares one framework — 10% risk, stop-distance sizing, flat circuit breaker,
DCA, and the `CorpusManager` ratchet — so any new idea is *directly* comparable to the live
config. Backtests print to stdout, write no files, and are imported by nothing.

---

## 🔬 Research Log — What Was Tried and Rejected

Recorded so it is never accidentally re-explored. These scripts are local-only research and are
not committed (`.gitignore` excludes `backtest_*.py`; the two winners above are force-added).

| Idea | Why it was rejected |
| --- | --- |
| **Donchian-20d breakout** + EMA100 + chandelier trail (1d) | Loosening the signal buys more trades at the cost of quality — worse PF and drawdown |
| **RSI(3) mean reversion** + Bollinger(20,2) + EMA200 (4h) | Same trade-off; negative Sharpe at the extremes |
| **Both daily tiers run simultaneously** on one shared balance/corpus/CB | Max drawdown **80.4%** — worse than either alone. Their bad stretches *overlap* instead of offsetting |
| **ADX(14) regime gate** (breakout only when ADX ≥ 25, mean-rev only when ADX ≤ 20, hysteresis between) + volatility-targeted sizing | Drawdown improved to 42.7%, but trade count collapsed 242 → 59. Not enough samples to trust |
| **Volatility-scaled sizing in isolation** | Established that the *regime gate*, not the sizing change, carried the risk reduction |
| **15m/10m/5m mean-rev vs pullback-momentum showdown** | 15m mean-rev won the 18-month window ($100 → $953, PF 1.42, WR 69.8%); **5m went bankrupt** (559% drawdown) |
| **15m mean-rev over the full 6.5 years** | $100 → $8,205 at PF **1.21** — lower profit factor *and* lower terminal equity than the live config, with the same >100% drawdown artefact (a handful of oversized losses from candles closing far past the stop). Beaten on quality, not just risk |
| **Progressive position scaling** instead of a flat circuit breaker (5 configs) | Flat 48h pause wins. Loss streaks cluster just before big reversals — scaling down means missing the recovery |

---

## 📈 Live Dashboard

**[quantbot.asitminz.com](https://quantbot.asitminz.com)** — auto-refreshing every 15 seconds via
`dcc.Interval`, reading straight off the shared Docker volume. Strictly **read-only**: there is
no write action anywhere in the UI. GitHub-dark theme, custom favicon.

<table>
<tr><td width="50%" valign="top">

**📈 Overview tab**

- **8 headline KPIs** — balance, net profit, total return, corpus, trades, win rate vs benchmark, fees paid, consecutive losses
- **12 quality metrics** — profit factor, Sharpe, Sortino, max drawdown, Calmar, avg win/loss, R:R, expectancy, avg hold, best/worst streaks
- Equity curve · drawdown · P&L distribution
- Monthly P&L bars · long-vs-short split
- Rolling 10-trade win rate with the 36.7% benchmark line
- Cumulative P&L
- Open-position card — side, entry, stop (with % distance), qty, margin, age
- Full trade table with derived **`Invested $`** and **`SL %`** columns

</td><td width="50%" valign="top">

**🔭 RSI Radar tab**

- Per-coin RSI gauge grid for **BTC, ETH, SOL, BNB, XRP, SUI**
- Colour-coded borders — green ≤ 20 (oversold), red ≥ 80 (overbought)
- **AVG RSI** aggregate card with zone badge
- Per-coin small-multiples history grid
- Single aggregate average-RSI line chart
- Extremes table — every reading outside the 20/80 thresholds

</td></tr>
</table>

**A rigor detail worth calling out:** annualised return is **deliberately suppressed** below 30
days or 5 trades. A lucky first week should not render "+40,000% / yr" on a public dashboard.

---

## 💬 Telegram Control Plane

`notifier.py` runs a single 60-second loop with five independent components. Every incoming
command has its `chat_id` verified against `TELEGRAM_CHAT_ID` — unauthorized senders are logged
and dropped.

### Alerts

| Alert | Trigger |
| --- | --- |
| ✅ Notifier started | Service boot |
| 📈 Trade opened | Long/short entry — price, stop, margin |
| 📉 Trade closed | Exit — P&L, reason, hold time |
| 🛑 Circuit breaker | 5 consecutive losses → 48h pause |
| 🚨 Bot crash / stall | No `last_updated_at` movement for > 30 minutes |
| 📊 Daily summary | 00:00 UTC — balance, return on invested, today/total P&L, WR, CB status, DCA total |
| 💰 DCA contribution | Monthly on `DCA_DAY` |
| 🔵 RSI oversold | Any scanned coin < 20 on monthly (or weekly) |
| 🔴 RSI overbought | Any scanned coin > 80 |

### Commands

| Command | Aliases | Effect |
| --- | --- | --- |
| `/status` | `/s` | Mode, balance, corpus, trades, WR, position, CB state, consecutive losses |
| `/balance` | `/bal`, `/b` | Balance, corpus, return on total invested (start + DCA) |
| `/pos` | `/position`, `/p` | Open position — side, entry, stop, qty |
| `/pause` | — | Touches `bot_paused.flag` → **blocks new entries**; open positions still exit |
| `/resume` | — | Removes the flag |
| `/help` | `/h`, `/start` | Command list |

### Component cadences

| Component | Cadence | Behaviour |
| --- | --- | --- |
| `TradeWatcher` | 60s | New `trade_log.csv` rows → close alerts; `position.entry_time` change → open alert |
| `HeartbeatMonitor` | 60s poll, 30 min timeout | **One** crash alert per stall — re-arms only when `last_updated_at` moves again |
| `RSIScanner` | every 4h | Monthly RSI(14) on 6 spot pairs (falls back to weekly if < 100 monthly candles); per-coin + per-timeframe spam suppression; appends to `rsi_history.json`, capped at 2000 entries |
| `DailySummary` | 00:00 UTC | Full day roll-up |
| `CommandHandler` | 60s | Long-poll `getUpdates` — no webhook, so no inbound port needed |

---

## ☁️ Infrastructure

Every cloud resource is Terraform code. No Console clicks after the initial tenancy setup.

```hcl
resource "oci_core_instance" "quantbot_vm" {
  shape = "VM.Standard.A1.Flex"

  shape_config {
    ocpus         = 1    # 1 of 4 free OCPUs — better availability than 2
    memory_in_gbs = 6    # 6 of 24 free GB — proportional to 1 OCPU
  }

  source_details {
    boot_volume_size_in_gbs = 50
    boot_volume_vpus_per_gb = 10   # CRITICAL: 10 = Balanced = Always Free. 20 bills you.
  }
  # cloud-init: installs Docker + Compose v2 + git, clones the repo, writes .env, brings the stack up
}
```

| Resource | Purpose | Free tier |
| --- | --- | --- |
| VCN `10.0.0.0/16` | Virtual cloud network | ✅ Always Free |
| Internet Gateway | Egress + tunnel establishment | ✅ Always Free |
| Route Table | Public routing | ✅ Always Free |
| Security List | SSH `:22` and dashboard `:8888`, both restricted to `my_ip_cidr` | ✅ Always Free |
| Public Subnet `10.0.1.0/24` | Instance placement | ✅ Always Free |
| Compute `VM.Standard.A1.Flex` | 1 OCPU / 6 GB ARM | ✅ Always Free |
| Boot Volume | 50 GB @ 10 VPUs (Balanced) | ✅ Always Free |
| **Monthly cost** | | **$0** |

> **Note:** the security list also carries a legacy `:80` ingress rule from the pre-tunnel
> Let's Encrypt design. `nginx.conf` has no `listen 80` block any more, so that port is open
> and answers nothing — [flagged for removal](#-known-limitations).

The shape is deliberately 1 OCPU / 6 GB rather than the full 4 / 24: the Always-Free ARM pool
is capacity-constrained, and a smaller shape schedules far more reliably. Scale to 2 / 12 by
recreating the instance when capacity allows — pandas on 200 candles is nowhere near the limit.

---

## 🚀 CI/CD Pipeline

Push to `main` triggers `.github/workflows/deploy.yml`. The design goal is simple: **a code
push must never be able to close a live position.**

```
   Push to main
        │
        ▼
┌───────────────────────────────────────────────────────────┐
│  1. Lint & Syntax Check                                   │
│     flake8 --select=E9,F63,F7,F82   (real errors only)    │
│     py_compile × bot / corpus_manager / dashboard / notifier │
└───────────────────────┬───────────────────────────────────┘
                        │ deploy does not run if this fails
                        ▼
┌───────────────────────────────────────────────────────────┐
│  2. Detect changed services   (git diff HEAD~1 HEAD)      │
│     bot.py | corpus_manager.py        → BOT=true          │
│     notifier.py                       → NOTIFIER=true     │
│     dashboard.py | assets/            → DASHBOARD=true    │
│     Dockerfile | .dockerignore |                          │
│     requirements.txt | docker-compose.yml | nginx/        │
│                                       → INFRA=true        │
└───────────────────────┬───────────────────────────────────┘
                        ▼
┌───────────────────────────────────────────────────────────┐
│  3. 🔒 Open-position safety gate   (only if BOT=true)     │
│     ssh → read bot_state.json → position open?            │
│         open  → bot container is NOT restarted            │
│                 (even during a full infra rebuild —       │
│                  everything else rebuilds around it)      │
│         none  → restart is safe                           │
└───────────────────────┬───────────────────────────────────┘
                        ▼
┌───────────────────────────────────────────────────────────┐
│  4. Copy files   (scp — explicit allow-list, never ".")   │
│     4 Python modules · requirements · Dockerfile ·        │
│     .dockerignore · docker-compose.yml · nginx.conf ·     │
│     error pages · favicon                                 │
│     .env on the server is NEVER overwritten;              │
│     a missing .env fails the deploy loudly                │
└───────────────────────┬───────────────────────────────────┘
                        ▼
┌───────────────────────────────────────────────────────────┐
│  5. Selective restart                                     │
│     INFRA     → compose build && up -d  (full rebuild)    │
│                 + always --force-recreate nginx  ←── see  │
│                   note below, this one cost real hours    │
│     DASHBOARD → up -d --no-deps --build dashboard         │
│     NOTIFIER  → up -d --no-deps --build notifier          │
│     BOT       → up -d --no-deps --build bot   (gated)     │
│     First deploy → nothing running? start everything      │
└───────────────────────┬───────────────────────────────────┘
                        ▼
┌───────────────────────────────────────────────────────────┐
│  6. Health check                                          │
│     docker inspect each container → exit 1 if not         │
│     "running", and dump the last 30 log lines on failure  │
└───────────────────────────────────────────────────────────┘
```

> **Why nginx is always force-recreated:** `nginx.conf`, `htpasswd`, and the error pages are
> **bind-mounted from disk**, not baked into an image. `docker compose up` only recreates a
> container when the *service definition* changes — so an `nginx.conf`-only edit was being
> scp'd to the server and then never read by the running process. Config changes silently did
> nothing. Fixed in `259157d`.

**Required GitHub Secrets:**

| Secret | Value |
| --- | --- |
| `ORACLE_HOST` | VM public IP |
| `ORACLE_USER` | `ubuntu` |
| `ORACLE_SSH_KEY` | Full contents of the private key (e.g. `~/.ssh/quantbot_rsa`) |

---

## 📂 Repository Layout

```
quantbot/
├── bot.py                      # Trading engine — signal, sizing, orders, state  (~1,050 lines)
├── corpus_manager.py           # Risk module — DCA, corpus ratchet, monthly refresh
├── dashboard.py                # Plotly Dash UI — Overview + RSI Radar tabs
├── notifier.py                 # Telegram — alerts, heartbeat, RSI radar, commands
│
├── backtest_leverage.py        # Committed: leverage/stop-width sweep (20× → 5×), 6.5 yr
├── backtest_ratchet.py         # Committed: ratchet-frequency sweep at the 5× tier
│                               # (other backtest_*.py are local research, gitignored)
│
├── requirements.txt            # Exact-pinned deps — deliberate "==", never ">="
├── Dockerfile                  # Multi-stage: base → bot / notifier / dashboard
├── docker-compose.yml          # 4 services, named volume, per-service log rotation
├── env.example                 # ⚠️ no leading dot — authoritative config template
├── .dockerignore               # Keeps .env, state, and logs out of image layers
├── .gitignore                  # Secrets, state, tfvars, tfstate, local research
├── LICENSE                     # MIT
│
├── assets/
│   └── favicon.ico             # Auto-served by Dash from assets/
│
├── nginx/
│   ├── nginx.conf              # Reverse proxy :8888, server_tokens off, auth_basic ready
│   ├── htpasswd                # Empty placeholder — real one generated on the server
│   └── error_pages/
│       ├── 401.html            # Themed auth-required page
│       └── 50x.html            # Themed upstream-down page
│
├── terraform/
│   ├── main.tf                 # VCN, IGW, route table, security list, subnet, ARM instance, cloud-init
│   ├── variables.tf            # All inputs — no committed .tfvars example (secrets)
│   └── outputs.tf              # vm_public_ip, ssh_command, dashboard_url
│
└── .github/workflows/
    └── deploy.yml              # lint → detect → safety gate → scp → selective restart → health check
```

---

## 🧰 Getting Started

### Prerequisites

| Requirement | Notes |
| --- | --- |
| **Python 3.11** | Match the container. 3.12+ is untested against the pinned numpy/pandas |
| **Docker + Compose v2** | `docker compose` with a space — *not* the legacy `docker-compose` |
| Telegram bot token | [@BotFather](https://t.me/BotFather) → `/newbot` |
| Telegram chat ID | [@userinfobot](https://t.me/userinfobot) |
| Binance futures API key | **Live mode only.** Paper mode needs no keys at all |
| Terraform ≥ 1.3 + OCI CLI | Only if you're provisioning infrastructure |

### Local development

```bash
git clone https://github.com/Asit0007/QuantBot.git quant_bot
cd quant_bot

python3.11 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp env.example .env          # note: env.example, NOT .env.example
```

Then edit `.env`:

```bash
DATA_DIR=.                   # ← IMPORTANT locally. /app/data is the container path
TELEGRAM_BOT_TOKEN=…         # ← BOT_TOKEN, not TOKEN
TELEGRAM_CHAT_ID=…
PAPER_TRADE=true             # leave it
BINANCE_API_KEY=             # leave blank for paper trading
BINANCE_API_SECRET=
```

Run the three services in three terminals:

```bash
python bot.py                # paper mode by default
python notifier.py           # Telegram alerts
python dashboard.py          # http://127.0.0.1:8050
```

> [!NOTE]
> `bot.py` **hard-fails at import** with an explicit list of missing variables if `.env` is
> incomplete (`_REQUIRED_ENV_VARS`). That check is intentional — do not paper over it with
> `os.getenv(..., default)`. A silently-defaulted risk parameter is how accounts die.

### Full stack via Docker

```bash
docker compose up -d                               # bot + notifier + dashboard + nginx
docker compose logs -f bot
docker compose up -d --no-deps --build dashboard   # rebuild one service only
docker compose down                                # the quantbot_data volume survives
```

Locally, `nginx/htpasswd` must exist for the bind mount to resolve — an empty placeholder is
committed for exactly this reason.

### Lint exactly what CI runs

```bash
flake8 bot.py corpus_manager.py dashboard.py notifier.py --select=E9,F63,F7,F82
```

---

## ⚙️ Configuration Reference

All configuration is environment-driven. **Nothing is hardcoded, no secret is ever committed.**
[`env.example`](env.example) is the authoritative template.

```bash
# ── Trading mode ─────────────────────────────────────────────────
PAPER_TRADE=true          # true = simulate. false + --live = real orders
START_BALANCE=100.0       # only used on the very first run
START_YEAR=2026           # anchors the DCA annual step-up

# ── Position sizing ──────────────────────────────────────────────
RISK_PER_TRADE=0.10       # 10% of corpus as the loss-at-stop

# ── DCA ──────────────────────────────────────────────────────────
DCA_DAY=10
DCA_MONTHLY_USD=10.0
DCA_ANNUAL_GROWTH=0.10    # +10% per year

# ── Binance (live trading only) ──────────────────────────────────
BINANCE_API_KEY=          # leave blank for paper
BINANCE_API_SECRET=

# ── Telegram ─────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN=       # from @BotFather   ← BOT_TOKEN, not TOKEN
TELEGRAM_CHAT_ID=         # from @userinfobot — doubles as the authz allowlist

# ── Paths & dashboard ────────────────────────────────────────────
DATA_DIR=/app/data        # Docker: /app/data   ·   Local: .
DASHBOARD_PORT=8050
DASHBOARD_HOST=0.0.0.0
DASHBOARD_REFRESH_MS=15000

# ── LOCKED strategy parameters — changing any invalidates the backtests ──
SYMBOL=BTC/USDT           LEVERAGE=5              CB_TRIGGER=5
TIMEFRAME=15m             LONG_ATR_MULT=8.0       CB_HOURS=48
CANDLE_MINUTES=15         SHORT_ATR_MULT=6.0      FEE_RATE=0.0005
RSI_LEN=14                VOL_MULT=2.0            DIV_WINDOW=5
MACD_FAST=12              VOL_SMA_PERIOD=20       DIV_SHIFT=5
MACD_SLOW=26              ATR_PERIOD=14           DIV_MEMORY=3
MACD_SIGNAL_WIN=9         CANDLES_NEEDED=200      WARMUP=50
```

> `ORACLE_HOST`, `ORACLE_USER`, and `ORACLE_SSH_KEY` are **CI-only** and live in GitHub Secrets.
> They must never appear in `.env`.

New parameters go in `env.example` too — and in `_REQUIRED_ENV_VARS` if the bot cannot run
without them.

### Pinned dependencies

Exact `==` pins are deliberate: a rebuild months from now must not silently pull a breaking
change into a bot trading real money. Bump intentionally, then re-run paper trading before
redeploying.

| Package | Version | | Package | Version |
| --- | --- | --- | --- | --- |
| `ccxt` | 4.5.40 | | `dash` | 4.0.0 |
| `pandas` | 2.3.3 | | `plotly` | 6.6.0 |
| `numpy` | 2.4.2 | | `requests` | 2.32.5 |
| `ta` | 0.11.0 | | `scipy` | 1.17.1 |
| `python-dotenv` | 1.2.2 | | | |

---

## 🌍 Cloud Deployment

### 1 — Provision infrastructure (one time)

```bash
cd terraform

# There is NO committed terraform.tfvars.example — the file is gitignored
# because it holds OCI credentials. Create terraform.tfvars by hand.
# Required (see variables.tf):
#   tenancy_ocid, user_ocid, fingerprint, compartment_ocid,
#   ssh_public_key, my_ip_cidr, repo_url, vm_image_ocid
# Optional (have defaults):
#   private_key_path, region, env_file_contents

terraform init
terraform plan     # should show 6 resources, all free tier
terraform apply    # ~3 minutes
# Outputs: vm_public_ip · ssh_command · dashboard_url
```

> `vm_image_ocid` is passed **explicitly** rather than looked up. OCI's images API returns
> `null` in some regions when filtering by both shape and OS version — an explicit OCID is
> region-agnostic and avoids the quirk entirely.

### 2 — Configure the server

```bash
ssh -i ~/.ssh/quantbot_rsa ubuntu@YOUR_VM_IP

nano ~/quantbot/.env
#   DATA_DIR=/app/data
#   TELEGRAM_BOT_TOKEN=…
#   TELEGRAM_CHAT_ID=…
#   PAPER_TRADE=true

chmod 600 ~/quantbot/.env
```

> [!WARNING]
> Leave the Terraform `env_file_contents` variable **empty** and provision `.env` by hand.
> cloud-init writes `user_data` into instance metadata, which is readable from inside the VM
> and stored in Terraform state — not where exchange keys belong.

### 3 — Deploy via CI/CD

```bash
# Add ORACLE_HOST / ORACLE_USER / ORACLE_SSH_KEY to GitHub repo secrets, then:
git push origin main

# Watch: GitHub → Actions → QuantBot CI/CD
# All steps green in ~3 minutes.
```

### 4 — Verify

```bash
ssh -i ~/.ssh/quantbot_rsa ubuntu@YOUR_VM_IP

sudo docker ps
# quantbot_bot        Up X minutes (healthy)
# quantbot_notifier   Up X minutes
# quantbot_dashboard  Up X minutes
# quantbot_nginx      Up X minutes

sudo docker logs quantbot_bot --tail=20
# ──────────────────────────────────────────────────────────
#   QuantBot  PAPER  BTC/USDT 15m 5×  10% risk
#   CB: 5 losses → 48h  |  DCA: $10.0/mo on 10th
# ──────────────────────────────────────────────────────────
#   Connected — BTC/USDT:USDT (PAPER)
#   Next candle in Xm Xs …
```

---

## 🔐 Cloudflare Tunnel

Public HTTPS with **zero inbound ports**, no certificates to renew, and the server IP fully
hidden. Configured manually on the VM as a systemd service — deliberately *not* in Terraform,
since it needs an interactive browser login.

```bash
# On the server
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 \
  -o /usr/local/bin/cloudflared
chmod +x /usr/local/bin/cloudflared

cloudflared tunnel login             # opens a browser for Cloudflare auth
cloudflared tunnel create quantbot   # creates the tunnel, saves credentials JSON
cloudflared tunnel route dns quantbot quantbot.yourdomain.com

cat > ~/.cloudflared/config.yml << EOF
tunnel: YOUR_TUNNEL_ID
credentials-file: /home/ubuntu/.cloudflared/YOUR_TUNNEL_ID.json
ingress:
  - hostname: quantbot.yourdomain.com
    service: http://localhost:8888
  - service: http_status:404
EOF

# Install as a systemd service so it survives reboots
sudo mkdir -p /etc/cloudflared
sudo cp ~/.cloudflared/config.yml /etc/cloudflared/
sudo cp ~/.cloudflared/*.json     /etc/cloudflared/
sudo cloudflared service install
sudo systemctl enable --now cloudflared
```

**Why this over certbot + open 443:** traditional HTTPS needs an inbound port, a 90-day renewal
cron, and it publishes your server IP in DNS. The tunnel makes a single *outbound* connection to
Cloudflare's edge — no inbound ports, automatic HTTPS forever, origin IP invisible.

---

## 🛠 Operations Runbook

### CLI reference

```bash
python bot.py                 # paper trade (safe default)
python bot.py --live          # live — requires PAPER_TRADE=false in .env AND API keys
python bot.py --status        # print state and exit
python bot.py --reset         # wipe ALL state files (incl. the notifier's) — irreversible

python notifier.py            # Telegram notifier
python dashboard.py           # dashboard on :8050
python dashboard.py --port 8080 --host 0.0.0.0

python corpus_manager.py --balance 1250.50 --year 2026          # dry-run corpus refresh
python corpus_manager.py --balance 1250.50 --load --save        # persist it
```

> [!CAUTION]
> `--reset` has **no confirmation prompt and no backup.** It deletes every state file the
> stack owns.

### Server operations

```bash
sudo docker ps
sudo docker exec quantbot_bot python bot.py --status
sudo docker exec quantbot_bot cat /app/data/trade_log.csv
sudo docker compose -f ~/quantbot/docker-compose.yml logs bot --tail=50
sudo systemctl status cloudflared
cloudflared tunnel info quantbot

# Manual full rebuild
cd ~/quantbot && sudo docker compose up -d --build
```

> State files live in the **named volume** `quantbot_data`, not in `~/quantbot/data`. Read them
> through `docker exec` — the host path is a Docker-internal directory, not a bind mount.

### State file reference

| File | Owner | Contents |
| --- | --- | --- |
| `bot_state.json` | `bot.py` | Balance, open position, armed counters, CB timer, `last_updated_at` heartbeat, mode, `last_candle_ts`, `last_dca_month` |
| `corpus_state.json` | `corpus_manager.py` | Corpus, peak, ratchet counters, `net_since_ratchet`, DCA totals |
| `trade_log.csv` | `bot.py` | Append-only: `datetime, side, entry_price, exit_price, stop_price, quantity_btc, pnl_usd, fees_usd, balance, reason, hold_candles, mode` |
| `rsi_history.json` | `notifier.py` | RSI radar scan log for 6 coins — read by the dashboard, capped at 2000 entries |
| `rsi_alert_state.json` | `notifier.py` | Last alerted zone per coin + timeframe (spam suppression) |
| `bot_paused.flag` | `notifier.py` | Presence = manual pause |
| `bot.log` / `notifier.log` | respective process | In-volume application logs |

All are gitignored **and** dockerignored — they contain live balances and are machine-local.

### Log rotation

Configured per service in `docker-compose.yml` so logs can never fill the 50 GB boot volume:

| Container | Max file | Max files | Ceiling |
| --- | --- | --- | --- |
| `quantbot_bot` | 10 MB | 5 | 50 MB |
| `quantbot_notifier` | 5 MB | 3 | 15 MB |
| `quantbot_dashboard` | 5 MB | 3 | 15 MB |
| `quantbot_nginx` | 2 MB | 2 | 4 MB |
| **Total** | | | **~84 MB** |

---

## 🚦 Going Live

**Do not skip step 4.** It is the single least-obvious step in the entire project.

**1. Confirm the edge survived contact with the exchange.**
20+ paper trades with win rate and profit factor within **±20%** of **36.7% / 1.60**.
The bot prints this comparison automatically every 5 trades once 20 trades exist:

```bash
python bot.py --status
```

**2. Create Binance API keys.**
Futures trading permission **only**. Withdrawals **disabled**. IP-restricted to the VM.

**3. Set the mode in the server's `.env`.**

```bash
PAPER_TRADE=false
BINANCE_API_KEY=your_key
BINANCE_API_SECRET=your_secret
```

**4. ⚠️ Change how the bot container starts.**
`Dockerfile` sets `CMD ["python", "bot.py"]` — **with no `--live` flag** — and
`docker-compose.yml` overrides no command. So **the containerised bot can never trade live as
currently configured, whatever `.env` says.** Add the command override to the `bot` service:

```yaml
  bot:
    build:
      context: .
      target: bot
    command: ["python", "bot.py", "--live"]   # ← required to actually go live
```

**5. Restart the bot while flat, then verify on Binance directly.**
Leverage is 5×, margin is isolated, and a `STOP_MARKET` reduce-only order appears within
seconds of the first entry.

```bash
sudo docker compose up -d --no-deps --build bot
```

### The safety gates protecting you

| Gate | Behaviour |
| --- | --- |
| **Double-key live mode** | `--live` requires `PAPER_TRADE=false` **and** a present `BINANCE_API_KEY`. Either mismatch exits non-zero |
| **Fail-safe direction** | `PAPER_TRADE=false` *without* `--live` still runs in paper mode |
| **Startup reconciliation** | On live start, `bot.py` compares state against the real exchange position and **warns** rather than silently trading; a stale state-side position is cleared |
| **Protective stop** | Live entries place a `STOP_MARKET` reduce-only order with 3 retries. All 3 failing logs CRITICAL and falls back to the per-candle software stop |
| **Circuit breaker** | 5 consecutive losses → 48h pause on new entries |
| **Remote kill-switch** | Telegram `/pause` blocks new entries; open positions still exit normally |
| **CI open-position gate** | A deploy will not restart the bot container while a position is open |
| **Key scope** | Binance keys are created with futures permission only — no withdrawals |

---

## 🛡 Security Posture

### What's handled

- **`.env` is gitignored *and* dockerignored** — never committed, never baked into an image
  layer, never overwritten by CI. A missing `.env` fails the deploy loudly rather than starting
  with defaults.
- **Telegram commands are sender-verified** — `notifier.py` checks the incoming `chat_id`
  against `TELEGRAM_CHAT_ID` before acting. Unauthorized senders are logged and dropped, so only
  the configured chat can query balance or send `/pause`.
- **Binance keys are scoped to futures trading only** — withdrawals disabled at key creation.
- **Zero inbound ports** — the Cloudflare Tunnel dials out; nothing dials in.
- **Dashboard bound to `127.0.0.1`** inside the host — unreachable except through nginx.
- **SSH and `:8888` restricted to a single CIDR** via the OCI security list.
- **`server_tokens off`** — nginx version is not leaked in headers or error pages.
- **GitHub Secrets for all CI credentials** — never in workflow YAML.
- **`nginx/htpasswd` is gitignored** — generated on the server with `htpasswd -c -B`, never
  synced by CI. Only an empty placeholder is committed, to satisfy the bind mount.
- **Terraform state and `.tfvars` are gitignored** — they carry OCI credentials and topology.
- **Exact-pinned dependencies** — no silent transitive upgrades into a bot handling money.

### Deliberate trade-offs

**The dashboard is public and unauthenticated.** `auth_basic` is fully wired into `nginx.conf`
but commented out for the building-in-public phase (`9d8ef2a`). `dashboard.py` renders balance,
corpus, trades, and open positions — **never** API keys or secrets — so this is a disclosure
choice, not a credentials leak. It is still a real-time feed of open positions and an
unauthenticated compute surface, and it will be closed before real capital is deployed.

To lock it back down:

```bash
ssh -i ~/.ssh/quantbot_rsa ubuntu@YOUR_VM_IP
cd ~/quantbot

sudo apt install apache2-utils -y
htpasswd -c -B nginx/htpasswd admin     # -c only on the first run — it overwrites the file
chmod 600 nginx/htpasswd

# Uncomment the two auth_basic lines in nginx/nginx.conf, then:
sudo docker compose up -d --force-recreate --no-deps nginx
```

Found a security issue? Please email **[asitminz007@gmail.com](mailto:asitminz007@gmail.com)**.

---

## ⚖️ Known Limitations

Published deliberately. A README that lists only strengths is a marketing document, not
engineering. Ranked by what would actually hurt.

### Before real capital goes in

| # | Issue | Impact |
| --- | --- | --- |
| 1 | **The backtest has no bankruptcy check.** Simulated `balance` can go negative and keep trading, because sizing is 10% of *corpus* and corpus only ratchets down after 10 consecutive losses. This is why the 5× run reports a 184.2% max drawdown | The headline terminal equity and CAGR assume infinite margin. Add a ruin check (`if balance <= 0: break`) and re-run both sweeps before sizing up beyond the initial $100 |
| 2 | **Wick stop-outs can desync live state.** The bot's own stop check runs on the *candle close*, but the exchange `STOP_MARKET` order fires *intrabar*. A wick through the stop that recovers by the close closes the real position on Binance while `bot_state.json` still shows it open — and reconciliation currently runs only at startup | The highest-value fix: a per-candle `get_exchange_position()` reconciliation |
| 3 | **The CI safety gate can fail open.** The open-position probe runs as a bare `docker exec` with errors swallowed to `\|\| echo "none"`. If the `ubuntu` user ever lost docker-group membership, the probe would error and report "no position" | A safety gate must fail *closed*: use `sudo docker`, and treat any non-zero exit as "open" |
| 4 | **State writes are not atomic.** `save_state` writes directly over `bot_state.json`; RSI history does a full read-modify-write | A crash mid-write corrupts the file with no backup. Write to `.tmp` + `os.replace` — a five-line fix |
| 5 | **Stop-order placement failure has no Telegram alert.** Three retries, then a CRITICAL log line and reliance on the software stop | Reaches `bot.log` only. If the process then dies, the position is naked |
| 6 | **`.env` is passed to every container.** `notifier` and `dashboard` get the same `env_file` as `bot`, so exchange keys sit in the environment of two processes that never trade — one of which serves internet traffic | Split into `.env.bot` / `.env.shared` |
| 7 | **Containers run as root**, and `gcc` + `curl` remain in the final runtime layers | Add a non-root `USER`; drop the build toolchain from runtime stages |
| 8 | **Base image is not digest-pinned.** `python:3.11-slim` floats, contradicting the strict `==` pinning of Python deps | Pin by digest, bump deliberately |

### Accounting quirks (known, documented, intentionally not "fixed")

- **The entry fee is charged twice to `balance`** — `open_*` does `balance -= fee_in`, then
  `close_position` computes `pnl = raw_pnl − fee_in − fee_out`. **The backtest does exactly the
  same thing**, so live and backtest agree with each other, but both understate returns by one
  entry fee per trade (~0.05% of notional) and `balance ≠ start + total_pnl + dca` will never
  reconcile. Fixing it without re-running the backtests would break comparability.
- **Paper stop exits are optimistic vs the backtest** — `close_position` prices a stop exit *at
  the stop*, while the backtest prices it at the candle close (by definition past the stop).
  Paper results therefore look slightly better than the backtest would have on the same candles.
  Relevant, because the go-live decision is a ±20% comparison against exactly those benchmarks.
- **Paper entries fill at the live ticker, exits at the candle close** — asymmetric, and neither
  matches the backtest's "fill at close".
- **The corpus ratchet can silently skip a trade** — a loss that closes while an older circuit-
  breaker pause is still running is never seen by `CorpusManager`, desyncing its counters from
  `bot_state.json`. The backtest calls it unconditionally, so this is a genuine live-vs-backtest
  divergence in ratchet timing.
- **`hold_candles` is wrong across restarts** — the candle counter resets to 0 on process start.
- **A missed DCA day is never caught up** — if the bot is down for all of the 10th, that month's
  contribution is skipped permanently.

### Operational

- **No unit tests.** CI lints only `E9,F63,F7,F82` — syntax and undefined names, nothing
  semantic. This is a conscious trade-off (validation is paper trading against backtest
  benchmarks), not an oversight. `size_position`, `cb_on_loss`, and the `CorpusManager` ratchet
  are pure functions with obvious invariants and are the natural first three test targets.
- **The healthcheck can't detect a hung bot** — it only verifies `bot_state.json` parses, so a
  wedged process with a valid old state file still reads "healthy". The real liveness signal is
  the notifier's 30-minute heartbeat, which lives in a *different* container.
- **Exceptions are swallowed by a bare 60s retry loop** — a persistent bug produces an infinite
  error loop with no Telegram escalation, and the bot never exits non-zero, so
  `restart: unless-stopped` has nothing to act on.
- **Benchmarks are duplicated** in `bot.py` and `dashboard.py`; `LEVERAGE` is read independently
  by `dashboard.py` to compute `Invested $`. Three places to keep in sync by hand.
- **Dead firewall rule** — OCI opens `:80` to `0.0.0.0/0` and compose publishes `80:80`, but
  `nginx.conf` has no `listen 80` block. The port is open and answers nothing.
- **Stale Telegram command replay** — `CommandHandler` starts with `offset=0`, so `getUpdates`
  returns anything queued for up to 24h. A `/pause` sent while the notifier was down executes on
  startup.
- **Terraform state is local and unencrypted** — one laptop, no backend, no locking, no
  versioning. It holds no exchange or Telegram secrets today (`env_file_contents` is left
  empty), but it does hold the full network topology.

### Design ceilings

| Constraint | Ceiling | What breaks first |
| --- | --- | --- |
| Single host, single writer | 1 symbol | `bot_state.json` has one `position` field — two bots on one volume would corrupt it |
| Flat-file IPC | ~seconds of latency | The notifier polls at 60s; a 1m-candle strategy would need real IPC |
| `trade_log.csv` read in full | ~10⁵ rows | The dashboard re-reads and re-derives every 15s — fine for years at ~40 trades/yr |
| `rsi_history.json` | 2000 entries (~1 yr) | Hard-capped, truncates silently |
| 1 OCPU / 6 GB ARM | ~3 containers + nginx | The limit is OCI free-tier capacity, not compute — pandas on 200 candles is trivial |
| One Telegram chat | 1 operator | The chat ID *is* the authorization model |

---

## 🗺 Roadmap

In priority order:

- [ ] **Accumulate 20+ paper trades** → compare WR/PF to 36.7% / 1.60 (±20%) → go live at $100
- [ ] **Add a ruin check to both backtests** and re-run the 6.5-year sweeps — the current terminal-equity figures assume infinite margin
- [ ] **Fix the three pre-live code items** — CI fail-open gate, per-candle exchange reconciliation, atomic state writes
- [ ] **Investigate the C3 signal** — RSI divergence + CHoCH + FVG, backtested elsewhere at +64.8%/yr with comparable drawdown
- [ ] **Terraform remote state** — OCI Object Storage backend with locking
- [ ] **Non-root containers** + digest-pinned base image
- [ ] **Unit tests** for `size_position`, `cb_on_loss`, and the `CorpusManager` ratchet
- [ ] **Prometheus + Grafana** for metrics beyond the Dash dashboard
- [ ] **Alertmanager escalation** beyond Telegram (PagerDuty / OpsGenie)
- [ ] **Multi-symbol** (ETH/USDT, SOL/USDT) — requires per-symbol state files and a separate corpus each

---

## 🐛 Engineering Log — Bugs Fought, Lessons Kept

The war stories. Each fix below is load-bearing.

<details open>
<summary><b>Quant & correctness</b></summary>

- **Lookahead bias produced a $2.4 billion backtest.** Swing high/low detection originally
  marked a swing at bar `i` using bars `i+1` through `i+5` — data that does not exist in real
  time. Fixed by marking swings at `i + SWING_LOOKBACK`, the first bar where confirmation
  actually exists. This is *why* the live divergence detector uses a rolling min/max window
  rather than pivot-based swings.

- **The leverage-squared sizing bug.** Sizing by `notional/price` and then multiplying P&L by
  leverage squared the leverage: a 0.47% ATR gave an 18.8% loss instead of 10%, and a 2.5% ATR
  would have wiped the account in one trade. Fixed by making sizing stop-distance-aware
  (`40595b5`). The docstring in `size_position()` preserves the full derivation.

- **Heartbeat false positives on every candle.** The notifier compared `now` against
  `last_candle_ts` — the candle's *own* timestamp. A candle stamped 16:00 processed at 16:30
  looked instantly "30 minutes stale" and fired a crash alert every single time. Fixed by adding
  a wall-clock `last_updated_at` field to `bot_state.json`. That field exists for exactly this
  reason.

- **Binance timeframe quirk.** Spot weekly klines are `"1w"`; `"1W"` is futures-only syntax.
  One character, one silently empty RSI radar.

</details>

<details open>
<summary><b>Infrastructure & deployment</b></summary>

- **nginx bind-mount config was never reloaded.** `nginx.conf`, `htpasswd`, and the error pages
  are bind-mounted, and `docker compose up` only recreates a container when the *service
  definition* changes. Config edits were being scp'd to the server and then completely ignored by
  the running process. CI now always `--force-recreate`s nginx on infra changes (`259157d`).

- **OCI ARM capacity exhaustion.** The Hyderabad free-tier ARM pool was empty. Solved with a
  retry script cycling availability domains at 5-minute intervals, a support ticket escalated to
  Sev-2, and finally an upgrade to PAYG — which grants the paid capacity pool while staying
  inside Always-Free resource limits. The VM runs 1 OCPU / 6 GB rather than 2 / 12 purely for
  scheduling reliability.

- **Boot volume cost trap.** `boot_volume_vpus_per_gb = 10` (Balanced) is the Always-Free tier.
  Setting 20 silently starts billing. Fixed in `171df33`; a separate block volume was removed as
  unnecessary.

- **Terraform OCI image API quirk.** Filtering images by both shape *and* OS version returns
  `null` in some regions. The Ubuntu ARM image OCID is now passed explicitly via `vm_image_ocid`
  — more verbose, but region-agnostic.

- **cloud-init docker-group race.** cloud-init runs `sudo -u ubuntu docker-compose up` in the
  same boot as `usermod -aG docker ubuntu` — but group membership doesn't apply to the existing
  session, so a first boot can fail under `set -e`. CI's `sudo docker` path is what actually
  keeps deploys working.

- **Docker Compose v1 vs v2.** Ubuntu 22.04 ships only `docker compose` (with a space). Every
  script uses v2 syntax; cloud-init symlinks a legacy `docker-compose` for compatibility.

- **scp-action and `.git` permissions.** Copying `source: "."` broke on mode-444
  `.git/objects` files during tar extraction. The workflow now scp's an explicit allow-list —
  which turned out to be better practice anyway.

- **The nginx/ports evolution.** 443 → 8888 → IP-only mode (so a first deploy works with no SSL
  at all, HTTPS server blocks kept commented in `nginx.conf`) → Cloudflare Tunnel for real HTTPS
  with zero open ports. Basic auth was added once the tunnel made the dashboard internet-
  reachable, then deliberately disabled again for building in public.

- **Env validation that never validated.** The original check was a `try/except KeyError` around
  `os.getenv` — which returns `None` rather than raising, so it caught nothing and a missing
  variable surfaced as a raw `TypeError` from `float(None)` deep in the config block. Replaced
  with the explicit `_REQUIRED_ENV_VARS` list (`51e864c`).

</details>

---

## 📄 License & Disclaimer

**MIT** — see [LICENSE](LICENSE).

> **This is not financial advice.** QuantBot is a portfolio and learning project that happens to
> be capable of moving real money. Leveraged futures trading can lose more than your initial
> deposit. Backtested performance is not a prediction of future results — it is a measurement of
> how a set of rules would have behaved on data that already happened. Read the
> [Known Limitations](#-known-limitations) section in full before pointing this at an account
> you care about, and never deploy capital you cannot afford to lose.

---

<p align="center">
  <b>QuantBot</b> &copy; 2026 &nbsp;·&nbsp; built by <a href="https://github.com/Asit0007">Asit Minz</a><br>
  <sub><i>Trained on caffeine. Powered by backtest. Not financial advice — just vibes and RSI divergence.</i></sub>
</p>
