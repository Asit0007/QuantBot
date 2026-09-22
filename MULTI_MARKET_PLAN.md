# QuantBot — Multi-Market Expansion Plan

*Drafted 2026-09-13. All technical claims verified against the repo and live endpoints; see
"Verification record" at the end.*

---

## Context

QuantBot today is one bot, one symbol, one market: BTC/USDT perpetuals, 15m, `nostop` at 5×.
It is not live (blocked on funding — $100 cannot clear the 0.001 BTC lot step), and its research
programme has hit a wall the repo's own log already names:

> "At ~100 configs against one 6.9-year sample the multiple-comparison burden dominates — test
> #101 is *less* trustworthy than test #1. **The binding constraint is data, not ideas.**"
> — `backtest/README.md`

The instinct behind this request is therefore correct and corroborated by the project's own
findings. But the framing needs sharpening: **the shortage is not candles, it is trades.**
6.9 years of 15m BTC data produced **126 trades**. No further parameter search on that sample
can be trusted. The fix is *independent samples from uncorrelated markets* — which is also
exactly why `backtest_symbols.py` failed: ETH/SOL/XRP/BNB added correlated bets, not information
(portfolio PF 1.03, 82.6% DD).

This plan adds two new market engines — **NSE India (cross-sectional over Nifty 50)** and
**US indices (SPX/NDX/DJI)** — plus a **news layer** producing scored, falsifiable predictions.
Everything is **research and paper only**: no broker accounts, no real orders, no SEBI algo
registration, no LRS remittance. Development and running happen **locally on the Mac**; cloud
deployment is a later, separate decision.

**Hard constraint: `quantbot.asitminz.com` must not be affected.** This is sharper than it looks
— the VM reconciles `main` every 5 minutes. See Guardrails.

### Decisions taken

| Question | Decision |
|---|---|
| Goal | Research + paper only, ~6 months |
| Separation | One repo, isolated at runtime — no long-lived per-bot branches |
| News layer | LLM briefing with a **scored prediction log** + a backtestable event-risk gate |
| Where it runs | Local Mac |
| US scope | All three indices (SPX, NDX, DJI) |
| India scope | Cross-sectional across Nifty 50 constituents |

Two carry caveats worth recording now.

**Three US indices are ~0.9 correlated.** Fine for what you chose them for — trade samples and
the portfolio story — but they must never be counted as three independent risk exposures.
Mitigation: the three share **one corpus** with a `MAX_CONCURRENT_POSITIONS` cap. That is not a
workaround; it is precisely the experiment `backtest/README.md` says was never run ("the shape to
test is 4 symbols at LOWER leverage with a cap on concurrent positions").

**Cross-sectional Nifty 50 is the strongest answer to the data problem**, and it changes the
architecture for everyone: the engine becomes **universe-based rather than single-symbol** from
day one. BTC is simply a universe of one. This unifies all three markets on one code path.

---

## What must NOT be ported from the BTC bot

The framework transfers. **The strategy does not.** Five findings, worst-first:

1. **`nostop` is unsafe on any market that gaps.** Its entire safety premise — "the margin IS the
   maximum loss, enforced by isolated-margin liquidation" — depends on price moving *continuously*
   to the liquidation level. NSE is shut 15:30→09:15 plus weekends and holidays; US likewise. An
   overnight gap opens *through* the liquidation price, so the loss ceiling silently stops
   existing. **Equities require real stops or defined-risk structures.** `size_position_nostop()`
   cannot be reused for NSE/US.
2. **`charge_funding()` has no analogue.** Perpetual funding at 00/08/16 UTC is crypto-perp-only.
   Equities have dividends, borrow and roll costs instead — different sign, different cadence.
3. **The volume gate fires on the clock, not on information.** `volume > 2× 20-bar SMA` on a
   session market triggers mechanically at the open and close every day, because intraday volume
   is U-shaped. Needs session-relative normalisation (same time-of-day bucket) or it is a
   calendar artefact.
4. **`secs_to_next_candle()` is a pure UTC minute-modulo** with no session concept — verified at
   `bot.py:1359`: `elapsed = (now.minute % CANDLE_MINUTES)*60 + now.second`. Biggest structural
   change in the whole plan.
5. **`BENCH_WR` / `BENCH_PF` (0.540 / 1.63) are BTC's numbers.** Each market earns its own from
   its own validated backtest, or the paper-vs-backtest gate is meaningless.

Inherited and non-negotiable: **every new config passes robustness gates 1–5**, including gate 5
(leave-one-year-out scored against `N_CONTROL = 400` same-size random drops).

---

## Phase 0 findings — data availability, tested live today

I tested the candidate sources rather than assuming. **This materially changes the plan.**

| Source | Result | Usable? |
|---|---|---|
| Binance (ccxt) | HTTP 200 | ✅ works — the existing path |
| **NSE official API** (`/api/allIndices`, browser UA + Referer) | HTTP 200, returned live Nifty 50 | ✅ works |
| FRED (St. Louis Fed) | HTTP 200 | ✅ works — US macro + release calendar |
| alternative.me Fear & Greed | HTTP 200 | ✅ works |
| **Yahoo Finance** (`query1.../v8/finance/chart`) | **HTTP 429, persistent across 3 retries** | ❌ not dependable |
| **Stooq** | HTTP 200 but body is a **JavaScript bot-challenge page**, not CSV | ❌ not usable unauthenticated |
| **GDELT** doc API | **HTTP 429** | ❌ not dependable unauthenticated |
| Alpaca market data | HTTP 401 | ⚠️ needs a free API key |

**Conclusion, and it is the most important operational finding here: free *unauthenticated
scraping* is not a foundation to build on.** Two unrelated services returning 429 from the same
egress IP indicates shared-IP rate limiting, and Stooq is actively bot-challenged. Plan for
**keyed APIs from the start** — a free Alpaca key for US, and an Indian broker free tier
(Upstox / Angel One SmartAPI / Dhan) for NSE historical intraday.

Caveat held honestly: a 429 from this machine is not proof Yahoo is unusable everywhere, and the
`yfinance` library does a crumb/cookie handshake that raw `curl` does not. It may work where this
test failed. **Do not build on it either way** — an undocumented scraper that can 429 mid-backtest
is not a dependency a trading system should carry.

---

## Architecture

Universe-based. One process per *market*, iterating its symbol universe each bar.

```
quant_bot/
  bot.py  notifier.py  dashboard.py  corpus_manager.py  ohlcv_cache.py   ← UNTOUCHED
  docker-compose.yml  Dockerfile  nginx/  deploy/  requirements.txt      ← UNTOUCHED

  markets/                        ← NEW — the expansion lives here
    registry.py                   ← ALL_MARKETS dict (JobPipe sources/__init__.py pattern)
    base.py                       ← DataAdapter protocol: the 11 methods of bot.py's Exchange
    calendar.py                   ← session/holiday logic — THE key new abstraction
    universe.py                   ← symbol list + per-symbol instrument spec
    state.py                      ← multi-position state schema + atomic writes
    engine.py                     ← universe-aware trading loop
    adapters/{crypto_binance,nse_india,us_indices}.py
  news/
    sources.py                    ← keyed news APIs; GDELT only for backfill, with a key
    events.py                     ← economic calendar → event-risk gate
    brief.py                      ← LLM briefing (ports JobPipe llm.py)
    predictions.py                ← prediction log + scorer
  research/                       ← new backtests  ⚠️ see gitignore decision below
  requirements-research.txt       ← NEW deps land HERE, never in requirements.txt
```

### The calendar is the crux

`markets/calendar.py` replaces `secs_to_next_candle()` with a session-aware equivalent:

```python
class MarketCalendar:
    def is_open(self, ts) -> bool
    def next_candle_close(self, ts, candle_minutes) -> datetime
    def session_bounds(self, date) -> tuple | None
    def bar_index_in_session(self, ts) -> int      # for session-relative volume
    def gap_is_expected(self, t0, t1) -> bool      # so ohlcv_cache stops flagging nights
```

Use `exchange_calendars` (verified on PyPI at **4.13.2**, ships `XNSE` and `XNYS`) rather than
hand-rolling holiday tables. `AlwaysOpenCalendar` serves crypto and **must reproduce
`secs_to_next_candle()` exactly** — that equality is a unit test, and it is what proves the
abstraction did not change BTC behaviour.

### Reuse — verified, not assumed

| Reuse | Verified finding |
|---|---|
| `corpus_manager.py` | `save_state(filepath=…)` / `load_state(filepath=…)` at lines 153/178 — **already multi-instance capable.** Zero market coupling |
| **Robustness gates 2–5** | `bootstrap_pf(pnls)`, `concentration(pnls)`, `leave_one_year_out(trades, n_control)` **already take plain P&L/trade lists.** Only `run(df, model, lev, funding)` is BTC-specific — generalising the harness is a small job, not a rewrite |
| `ohlcv_cache.py` | Duck-typed: needs only `.id` and `fetch_ohlcv(sym, tf, since_ms, limit)`. Set `OHLCV_CACHE_DIR` per market. Never call `load_funding` |
| `JobPipe/src/jobpipe/llm.py` | Clean public surface — `generate()`, `generate_json()`, `budget_remaining()`, `redact()`, `QuotaExhausted`. Only 2 lines mention job/resume/facts, so it is **essentially domain-agnostic and ports nearly as-is.** Brings cross-process flock rate limiting, daily budget with correct Pacific quota-day rollover, PII redaction, JSON coercion |
| JobPipe `sources/base.py` + `ALL_SOURCES` | Proven plugin-registry shape already in this codebase |
| bot↔notifier↔dashboard contract | Files on disk, not imports — new engines emit the same shapes without importing anything |

### New logic the BTC bot never needed

A 50-symbol universe on a small corpus will fire more signals than it can fund. Requires
`MAX_CONCURRENT_POSITIONS` plus a **deterministic, pre-committed ranking rule** for which signals
to take when more fire than there is margin for. Rank on a backtestable key (signal strength, or
alphabetical as the honest null) — never on anything computed after the bar closes.

---

## Guardrails — how the live site stays untouched

The VM runs `deploy/quantbot-pull-deploy.sh` on a 5-minute systemd timer. **Anything merged to
`main` is on the production box within five minutes.**

1. **Never modify `requirements.txt`.** Verified at line 98 of the deployer, the INFRA regex is
   `^Dockerfile$|^\.dockerignore$|^requirements\.txt$|^docker-compose\.yml$|^nginx/` — a match
   triggers a **full rebuild of every container, including the live BTC bot.** New dependencies
   go in `requirements-research.txt`, installed only into the local venv.
2. **`markets/`, `news/`, `research/` match none of the four classification regexes** (lines
   95–98, verified). They are pulled to the box and sit inert. Safe.
3. **Work on a feature branch (`multi-market`), merge to `main` in reviewed, inert chunks.** A
   normal feature branch that merges — not the long-lived per-bot branch pattern, which would
   have cost 4× on every bugfix and never shipped (the deployer only tracks `main`).
4. **Add new modules to `.github/workflows/deploy.yml`.** Verified: it lints exactly four files
   (`bot.py corpus_manager.py dashboard.py notifier.py`) — `ohlcv_cache.py` is tracked but
   **unlinted and uncompiled today.** `.github/` matches no rebuild regex, so extending it is
   safe. Neither flake8 `--select=E9,F63,F7,F82` nor `py_compile` imports, so uninstalled
   research deps will not break CI.
5. **Never write into the live `DATA_DIR`.** Each market gets its own (`./data/nse`, `./data/us`).
   Filenames inside are flat and fixed (`bot_state.json`, `trade_log.csv`), so a shared directory
   means silent mutual overwrite.
6. **Do not touch** `bot.py`, `notifier.py`, `dashboard.py`, `docker-compose.yml`, `nginx/`, or
   `terraform/` in Phases 0–6.
7. **`.env.nse` / `.env.us` are already safe** — `.gitignore` carries `.env.*` with a single
   `!env.example` negation. No change needed. (Correcting an earlier assumption of mine.)
8. **Decision required: `research/` is NOT gitignored.** `.gitignore` covers `backtest_*.py` and
   `backtest/`, but a new `research/` directory would be **committed**. Either add it to
   `.gitignore` (matches existing convention) or commit it deliberately. **Recommend committing
   it** — the existing convention caused the incident where an IDE silently broke all ten
   research scripts for nine days precisely because "nothing in `backtest/` runs in CI."
9. Delete the stale `hyderabad-migration` branch (0 ahead, 4 behind `main`).

---

## Phases

### Phase 0 — Finish the data spike ⚠️ before any strategy code

Partly done above. Remaining: obtain a free Alpaca key and an Indian broker free-tier key, then
prove you can pull **N years of bars at the chosen granularity** for one NSE constituent and one
US index. Deliverable: a written table of source / depth / granularity / cost / rate limits /
terms, and a **go/no-go on timeframe per market**.

Pre-empting the likely outcome: if only daily bars are available at depth for NSE, **the Indian
bot becomes a daily/swing strategy, and that is fine.** `CLAUDE.md` already describes the BTC
edge as "a swing strategy that happens to trigger on 15m candles" — ~18 trades/year at ~18-day
holds. A daily-bar equity strategy matches that edge shape well. 15m was never validated as a
requirement.

**Exit:** data table + chosen timeframe per market.

### Phase 1 — Market abstraction + BTC equivalence proof

Build `markets/` (calendar, adapter protocol, universe, state, engine) and the crypto adapter.

**Acceptance test, non-negotiable:** replay 6.9 years through the *new* engine with the crypto
adapter and reproduce `backtest_nostop.py` exactly — **126 trades, 54.0% WR, PF 1.63, $2,566
final balance** (verified against `backtest/README.md:523`). If the new engine cannot reproduce
the old result, the abstraction changed behaviour and is not ready for a market where you cannot
check the answer.

**Exit:** replay matches; `AlwaysOpenCalendar` unit-tests equal to `secs_to_next_candle()`.

### Phase 2 — NSE cross-sectional research

Universe = Nifty 50 constituents. **Handle index reconstitution** — using today's members over
history is survivorship bias and the classic way to manufacture a fake edge here.

Re-derive the signal; do not port it. Establish a baseline, then search deliberately and **count
every config tested**. Run gates 1–5. Real stops, not `nostop`. Session-relative volume
normalisation.

**Exit:** a config clearing all five gates — or a written, honest "no edge found," which is a
perfectly good outcome and a better build-log post than a fitted one.

### Phase 3 — US indices research

SPX/NDX/DJI, one shared corpus, `MAX_CONCURRENT_POSITIONS` cap, same gates. Explicitly measure
and record cross-correlation of the three symbols' trade P&L so the concentration is documented
rather than assumed away.

### Phase 4 — Paper runners, locally

NSE and US engines in paper on live data. Separate `DATA_DIR` and `.env.<market>` per market, one
venv. Reuse the notifier's Telegram file contract for alerts.

⚠️ **Laptop scheduling is unreliable and you already have the scar.** JobPipe's launchd job sat
at `runs = 0` and first fired only on **2026-09-13** — today — because launchd replays a calendar
interval missed while *asleep* but not while *powered off*. A 09:00 IST pre-open briefing on a
Mac will silently miss days. Use JobPipe's signed-launcher-app pattern
(`JobPipe/deploy/build-launcher.sh`, its `CLAUDE.md` §7.56) for the TCC grant, and **log and
surface missed runs** rather than assuming the schedule held — `runs = 0` looks exactly like a
broken install.

### Phase 5 — News briefing + prediction log

Pre-open and post-close LLM digest per market. **The prediction log is the entire point** — the
same discipline as "20 paper trades before live," applied to the model.

Each briefing writes a structured, falsifiable record to `predictions.csv` **before** the session:
`{market, date, session, direction, confidence, horizon, evidence_quotes, rationale}`. A scorer
runs after the close and appends the realised outcome. Track accuracy, Brier score, calibration.

**Nothing wires to order logic until that log has ≥100 scored predictions and beats a coin flip
with statistical significance.** Until then it is an information product — and a genuinely good
build-log subject either way.

Prompt design (per the `prompting` skill):
- Articles at the **top**, instruction at the **bottom** (worth up to ~30% on multi-document
  inputs); XML tags per content type.
- **Quote-grounding**: extract `<evidence>` quotes *first*, then reason from them. Makes every
  prediction auditable against a specific headline and cuts fabrication.
- 3–5 diverse few-shot examples of the JSON contract, **including at least one abstention**, or
  the model will always produce a direction.
- State the *why* in the prompt: the call is scored against the realised close, and a confident
  wrong call is penalised more than an abstention. Make `"direction": "none"` first-class and
  explicitly encouraged.
- Use `generate_json()` from the ported JobPipe client — budget and rate limiting already solved.

⚠️ **Lookahead bias is the failure mode, and this repo has the scar tissue:** a swing detector
reading `i+1..i+5` once produced a **$2.4 billion** backtest. Live news search returns
hindsight-ranked, re-dated, re-edited articles. Any *backtested* use of news must come from a
point-in-time archive with original timestamps — never a present-day search. Note GDELT returned
429 unauthenticated today, so budget for a keyed tier or an alternative archive.

### Phase 6 — Event-risk gate

Block new entries within ±N bars of scheduled high-impact events (FOMC, RBI MPC, US CPI/NFP,
Union Budget, earnings windows). Release *schedules* are announced in advance, so a hand-built CSV
of historical release datetimes is genuinely point-in-time correct and cheap. FRED is reachable
(verified) and covers the US side.

**Expect this to fail gate 5, and treat that as the base case.** `backtest/README.md` is explicit:
"any trend gate on this data leans on 2024," and "assume anything that reduces trade count makes
this worse until gate 5 says otherwise." An event gate reduces trade count. If it fails, record it
in the rejected table and move on — **do not tune it until it passes**, which is how the
multiple-comparison problem got this bad in the first place.

### Phase 7 — Multi-market dashboard + deploy decision

Only after the above. `dashboard.py` reads a CSV column literally named `quantity_btc`, assumes
one account, and duplicates `BENCH_WR`/`BENCH_PF`/`LEVERAGE` from `bot.py`. Build a **separate**
multi-market dashboard on a different port rather than retrofitting it. nginx can serve it
additively via a second `listen` block (leaving `location /` byte-identical); path-prefix routing
would require `Dash(routes_pathname_prefix=…)`, which `dashboard.py:421` does not set.

The VM is 1 OCPU / 6 GB and `CLAUDE.md` calls ~3 containers + nginx the ceiling. Before any cloud
deploy: consolidate to one shared notifier and one multi-market dashboard, and resize to
2 OCPU / 12 GB (still within OCI always-free, already flagged in `terraform/main.tf`).

---

## Verification

| Phase | How it's verified |
|---|---|
| 0 | Written data table; a script that pulls N years of bars for one NSE symbol and one US index and prints coverage + gap stats |
| 1 | **Replay reproduces 126 trades, PF 1.63, $2,566**; `AlwaysOpenCalendar` == `secs_to_next_candle()` unit test |
| 2–3 | Gates 1–5 per market, with the count of configs tested recorded beside the result |
| 4 | Paper engines run a full week; state files well-formed; Telegram alerts arrive; missed-schedule days logged |
| 5 | `predictions.csv` written before each session and scored after; accuracy + Brier + calibration reported |
| 6 | Gate-5 result recorded whether it passes or fails |
| Site | `curl -sI https://quantbot.asitminz.com` → 200 after every merge; `tail ~/quantbot-deploy.log` shows no rebuild triggered by these commits |

Run the existing lint gate before every merge:

```bash
flake8 bot.py corpus_manager.py dashboard.py notifier.py --select=E9,F63,F7,F82
```

---

## Open items to resolve during execution

- **Survivorship bias in the Nifty 50 universe** — historical constituents are required. Resolve
  in Phase 2 before any result is quoted.
- **Instrument spec per market** — lot sizes, tick sizes, minimum notionals. This is the exact
  class of constraint that blocked BTC go-live at $100 (0.001 BTC step = ~$79.85 vs $50 of
  requested notional). Encode in `universe.py` from the start.
- **Cost model** — Indian STT / stamp duty / exchange charges and US commissions are structurally
  different from a flat 0.05% taker fee. The phantom-leverage post-mortem showed fee errors scale
  with trade count and silently subsidise high-frequency ideas. Model explicitly.
- **`research/` gitignore decision** (Guardrail 8) — recommend committing.
- Whether the three US indices share one corpus or three (plan assumes one + concurrency cap).

---

## Verification record

Checked directly rather than assumed, 2026-09-13:

- Deployer classification regexes read at `deploy/quantbot-pull-deploy.sh:95–98`; confirmed
  `markets/`, `news/`, `research/` match none, and `requirements.txt` matches INFRA.
- `.gitignore` read in full — `.env.*` present (so `.env.nse` is covered); `research/` absent.
- `secs_to_next_candle()` read at `bot.py:1359` — confirmed pure UTC modulo.
- `corpus_manager.py:153,178` — `save_state`/`load_state` both take `filepath`.
- `backtest/backtest_robustness.py` — five gates confirmed (IS/OOS, bootstrap, concentration,
  per-year, leave-one-year-out); `N_CONTROL = 400`; gates 2–5 take plain lists.
- `backtest/README.md:523` — BTC row: $2,566, +13.7%/yr, PF 1.63, DD 25.5%, 126 trades, WR 54.0%.
- `.github/workflows/deploy.yml:27,32–35` — four files linted; `ohlcv_cache.py` not among them.
- `JobPipe/src/jobpipe/llm.py` — public surface enumerated; 2 domain-specific references only.
- Live endpoint tests (HTTP codes in the Phase 0 table above).
- `venv` is Python 3.11.4 with ccxt 4.5.40, pandas 2.3.3, numpy 2.4.2, dash 4.0.0, scipy 1.17.1,
  ta 0.11.0, tzdata. **No `yfinance`, no `exchange_calendars`** — both need installing into the
  research requirements.
- `exchange_calendars` confirmed on PyPI at 4.13.2.
