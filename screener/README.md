# Value + RSI screener

A daily Telegram digest of digital investment instruments buyable in India,
screened for fundamental quality (Graham/Buffett/Munger, per
`~/.claude/skills/ValueInvesting/ValueInvesting.md`) and flagged for current
RSI-oversold entries. Standalone tool — it never trades, never touches
`quant_bot`'s trading state, and is never deployed to the VM that runs the
live BTC bot (see "Deploy safety" below).

## Why RSI and value investing at all

`ValueInvesting.md` §7 explicitly bans RSI ("tools of the speculator, not
the investor"). This tool is the one place that's allowed to break that
rule, and only in a scoped way — added as an explicit exception to the
doctrine file itself (2026-09-16), not silently ignored:

- RSI never decides what qualifies. The value screen runs first, on
  fundamentals alone.
- RSI only ever runs on names that *already* cleared the value screen, to
  flag which of them are currently price-depressed and worth a closer look
  sooner.
- Anything with no fundamentals to screen (ETFs, REITs, InvITs, SGBs, BTC)
  never gets a "value" label — it's reported as PRICE ONLY, plainly marked.

## Asset coverage

| Tier | Source | Value screen? | RSI? |
|---|---|---|---|
| NSE equities | bhavcopy (price) + Screener.in (fundamentals) | Yes — full 5-parameter test | Yes, on qualifiers |
| ETFs | bhavcopy | No (a basket has no moat/ROIC to test) | Yes |
| REITs / InvITs | bhavcopy | **Not yet** — see Limitations | Yes |
| Sovereign Gold Bonds | bhavcopy | No (no earnings at all) | Yes |
| BTC | Binance via ccxt | No | Yes |
| US equities (S&P 500) | — | — | **Deferred**, see below |

NSE bhavcopy carries clean series codes for the first five rows (verified
live 2026-09-16): `EQ` = equities + ETFs (split by an "ETF" name heuristic —
NSE doesn't give ETFs their own series code), `RR` = REITs, `IV` = InvITs,
`GB` = Sovereign Gold Bonds. SME board, trade-for-trade/surveillance names
(`SM`, `BE`, `BZ`, `ST`, ...) and government/state bonds (`GS`, `SG`, `TB`,
...) are excluded outright — see `adapters/nse_bhavcopy.py`'s
`TIER_BY_SERIES`.

**US S&P 500 is deferred, not built.** No verified working free data source
exists as of 2026-09-16 (per the `market-data-source-findings` memory:
Alpaca needs a key, Yahoo Finance 429s, yfinance untested). Revisit once
there's a real source — don't guess at one.

## Universe sizing

The equity tier is bounded to the top `UNIVERSE_TOP_N` (default 300) NSE
names by average daily turnover over `TURNOVER_LOOKBACK_DAYS` (default 40
trading days) — a reproducible, index-committee-independent cut, the same
approach the workspace already settled on for NSE research (see the
`nse-bhavcopy-is-the-data-source` memory). This also bounds how many pages
get scraped from Screener.in per run.

## The value screen, exactly

`value_screen.py` operationalizes Graham's five-parameter capitalization
factor plus the earnings-yield gate, against what Screener.in actually
publishes (verified against RELIANCE/HDFCBANK/TCS 2026-09-16):

1. **Earnings yield** — `100/PE >= 2 * INDIA_10Y_GSEC_YIELD_PCT`. This is a
   strict, literal reading of Graham's rule. At today's Indian large-cap
   valuations it is a *hard* bar — in testing, none of RELIANCE, HDFCBANK or
   TCS cleared it (P/E 14–22 implies a 4.5–7.1% earnings yield against a
   13.8% requirement at a 6.9% G-Sec placeholder). Expect the qualified list
   to be short, sometimes empty, on any given day for large-caps — that is
   the doctrine working as designed, not a bug to tune away.
2. **Quality of management** — **not automated.** Nothing about a scrape can
   assess management integrity. Every digest carries a standing note saying
   so; this is never silently assumed.
3. **Financial strength** — Debt/Equity = Borrowings / (Equity Capital +
   Reserves), latest reported year, from Screener's Balance Sheet table.
4. **Dividend record** — fraction of years with Dividend Payout % > 0, out
   of years Screener actually reports (usually ~12).
5. **Current dividend rate** — today's Dividend Yield.

Long-term growth (Sales CAGR, 10Y preferred, falls back to 5Y/3Y) and ROIC
(ROCE as the proxy) are evaluated alongside these. **A name must clear every
evaluated check to qualify — missing data fails closed, it never gets a
free pass.**

**Known limitation: ROCE isn't a meaningful quality signal for banks/NBFCs/
insurers** (leverage is their business model) — confirmed in testing
(HDFCBANK ROCE 7.0% reads as a fail, which isn't really comparable to a
non-financial company's ROCE). A sector-aware rule (ROE instead of ROCE for
financials) is a reasonable Phase 2, not built yet.

**Known limitation: REITs/InvITs get no value screen yet.** Their
distribution mechanics don't map onto "Dividend Payout %"/P&L the way an
operating company's do, and building that properly means parsing a
different Screener.in template. They're PRICE ONLY for now, same as ETFs —
flagged plainly, not silently underserved.

## Data sources — what's verified

- **NSE bhavcopy** (`nsearchives.nseindia.com`) — free, no key, HTTP 200,
  browser UA + `Referer` header required. UDiFF format only (works 2024-01
  onward); this tool never looks back further than ~2 months so the legacy
  pre-2024 format used elsewhere in this workspace isn't needed here.
- **Screener.in** — no official API; this scrapes the same HTML a browser
  gets, unauthenticated. Verified reachable and parseable 2026-09-16.
  Polite by design: 1.5s between live requests, 7-day fundamentals cache
  (`screener/data/fundamentals/`) so a daily run only re-fetches what's
  actually stale. **This can break if Screener.in changes their markup** —
  `fetch_fundamentals` fails closed (returns `None`) on any parse failure
  rather than scoring on a half-filled record.
- **Binance (ccxt)** — same client pattern as `notifier.py`'s RSI radar.
  **Verified 2026-09-16 from a normal terminal**: `fetch_btc_snapshot()`
  returned a live price and RSI. The earlier same-day connection refusal to
  `api.binance.com` was confirmed to be that sandboxed session's network
  egress, not a real block. The code still fails closed (BTC just doesn't
  appear) if Binance is unreachable on a given run.
- **India 10Y G-Sec yield** — **no free live source found.** `config.py`
  requires `INDIA_10Y_GSEC_YIELD_PCT` in `.env` with no default; the tool
  refuses to run without it rather than guess. Look it up from
  [rbi.org.in](https://www.rbi.org.in) or a broker terminal and refresh it
  every month or so — it doesn't need to be exact to the day.

## Setup

```bash
cd screener
python3.11 -m venv .venv
.venv/bin/pip install -r requirements-screener.txt
cp env.example .env   # fill in INDIA_10Y_GSEC_YIELD_PCT at minimum
```

```bash
# from the repo root, with the venv above
PYTHONPATH=. screener/.venv/bin/python -m screener.main --dry-run --limit 20
```

`--limit` caps how many equities actually get scraped from Screener.in —
use it for testing. A full run against the default 300-name universe takes
~10–15 minutes (300 requests x 1.5s politeness delay, plus network latency)
on a cold cache; a same-week rerun is much faster since fundamentals are
cached for 7 days. Drop `--dry-run` to actually send to Telegram once
`SCREENER_TELEGRAM_BOT_TOKEN` / `SCREENER_TELEGRAM_CHAT_ID` (or the shared
`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` fallback) are set in `.env`.

**One-way only.** This is push, not request-response — it is not wired into
`notifier.py`'s `CommandHandler` (`/status /balance /pos /pause /resume
/help`, all about the live BTC bot), so there is no Telegram command that
triggers a run. The only way a digest reaches Telegram is the daily
`launchd` job (see `deploy/` and "Roadmap / open items" #5 below) or a
manual non-`--dry-run` invocation from a terminal.

## Deploy safety

This lives in the same repo as the live BTC bot, whose VM reconciles `main`
every 5 minutes and rebuilds containers based on which files changed (see
the root `CLAUDE.md` §4.9). `screener/` and `requirements-screener.txt`
match none of the deployer's four classification regexes (`^(bot|
corpus_manager)\.py$`, `^notifier\.py$`, `^dashboard\.py$|^assets/`,
`^Dockerfile$|^\.dockerignore$|^requirements\.txt$|^docker-compose\.yml$|
^nginx/`) — verified against `deploy/quantbot-pull-deploy.sh` 2026-09-16.
A commit touching only this directory is inert on the VM even if it ever
reached `main`. It shouldn't: this branch (`value-rsi-screener`) follows the
same rule the `multi-market` branch already established — research/tooling
branches stay unmerged, `main` stays exactly what's running in production.

## Roadmap / open items

1. **Verify BTC connectivity** from a normal (non-sandboxed) terminal.
2. **Sector-aware quality metric** for financials (ROE instead of ROCE).
3. **REIT/InvIT value screen** (distribution yield + gearing), once their
   Screener.in template is mapped the way the equity one is here.
4. **US S&P 500** — pick a real data source first (a free Alpaca key is the
   likeliest unblock per existing memory); don't build on an unverified one.
5. ~~**Scheduling**~~ — **done 2026-09-16.** `deploy/` has the full launchd
   setup: a signed launcher app (JobPipe's `~/Applications/*.app` pattern,
   since a plain LaunchAgent gets TCC-denied under `~/Documents`) driving
   `run-daily.sh` at 19:00 IST. It runs from a **dedicated worktree**,
   `../quant_bot-screener` (branch `screener-scheduled`), not this checkout —
   `value-rsi-screener` is a research branch that stays unmerged, and this
   checkout gets switched to `main` for live-bot work, which would silently
   break a job pointed here. The worktree hard-resets to
   `origin/value-rsi-screener` on every run, so **a local commit on this
   branch has no effect on the scheduled digest until it's pushed.** One
   manual, one-time step remains and can't be scripted: grant the launcher
   app Full Disk Access in System Settings > Privacy & Security (see
   `deploy/build-launcher.sh`'s output for the exact path) — until that's
   done, `launchctl kickstart` fires but the job exits without reading the
   repo.
