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
| Gold ETFs | bhavcopy | No (a basket has no moat/ROIC to test) | Yes |
| REITs / InvITs | bhavcopy | **Not yet** — see Limitations | Yes |
| Sovereign Gold Bonds | bhavcopy | No (no earnings at all) | Yes |
| Crypto (top 50 by volume) | Binance via ccxt | No | Yes |
| US equities (top 100 S&P 500 by $ volume) | Alpaca market data | No (no fundamentals source wired up — see below) | Yes |

NSE bhavcopy carries clean series codes for the first five rows (verified
live 2026-09-16): `EQ` = equities + ETFs (split by an "ETF" name heuristic —
NSE doesn't give ETFs their own series code), `RR` = REITs, `IV` = InvITs,
`GB` = Sovereign Gold Bonds. SME board, trade-for-trade/surveillance names
(`SM`, `BE`, `BZ`, `ST`, ...) and government/state bonds (`GS`, `SG`, `TB`,
...) are excluded outright — see `adapters/nse_bhavcopy.py`'s
`TIER_BY_SERIES`.

**ETF tier narrowed to Gold ETFs only (2026-09-16), by request.** Of NSE's
~99 ETFs, `config.ETF_NAME_FILTER` (default `GOLD`) keeps only the ~11 whose
bhavcopy instrument name contains it (`GOLDBEES`, `HDFCGOLD`, `SETFGOLD`,
etc.) — same name-based match the ETF/equity split itself uses. Set it to
an empty string to report every ETF again, as before.

**Crypto expanded from BTC-only to the top `CRYPTO_TOP_N` (default 50, was
200 until 2026-09-17) Binance USDT pairs by 24h quote volume (2026-09-16)**
— see `adapters/binance_rsi.fetch_top_n_snapshot`. Same "reproducible, no
index committee" reasoning as the NSE turnover cut; no market-cap field
exists on Binance, so volume is the ranking key. Two exclusion lists keep
this honest: stablecoins (`USDT`/`USDC`/`RLUSD`/`USDE`/... — RSI on a
$1-pegged asset is meaningless) and Binance's tokenized-stock products
(`GOOGLB`, `TSLAB`, `NVDAB`, `COINB`, ... — these are equities, not crypto,
and would be a real mislabeling if they slipped into a "Crypto, oversold"
line). Both lists are explicit and observed-live rather than pattern-
guessed — a bare "ends in B" rule would incorrectly exclude `SHIB`, which is
real crypto. Re-verify against a live top-N run if Binance adds new
stablecoins or tokenized products; see the lists' comments in
`binance_rsi.py`.

**US S&P 500 built 2026-09-17**, once Asit got a free Alpaca market-data
key (superseding the earlier "don't get one" note — see the
`session-deliverables` memory; that note was scoped to a retracted, unrelated
breadth-strategy experiment). See `adapters/alpaca_sp500.py`. PRICE ONLY —
Alpaca has no fundamentals endpoint, so there's no Graham screen for this
tier; building one needs a separate verified fundamentals source, not
attempted. Two pieces, both verified live:
- **Constituent list**: GitHub's maintained `datasets/s-and-p-500-companies`
  CSV, cached locally with a 30-day TTL (membership changes rarely).
- **Price + ranking**: Alpaca's multi-symbol `/v2/stocks/bars` endpoint,
  ranked by average dollar volume (close × volume) — Alpaca's free tier has
  no market-cap field either, same situation as Binance. This endpoint
  **paginates** once the combined bar count crosses ~1500-2000 rows — all
  503 constituents × ~69 daily bars needs 4 pages, ~16s total; the adapter
  loops on `next_page_token` rather than assuming one page covers everything.
  One quirk worth remembering: multi-class tickers use **dot** notation
  (`BRK.B`), not dash — `BRK-B` 400s.

Fails closed like the crypto leg: if `ALPACA_API_KEY_ID` /
`ALPACA_API_SECRET_KEY` are unset, this tier is silently skipped, not fatal
to the rest of the run.

## Universe sizing

The equity tier is bounded to the top `UNIVERSE_TOP_N` (default 100, was 400
until 2026-09-17) NSE names by average daily turnover over
`TURNOVER_LOOKBACK_DAYS` (default 40 trading days) — a reproducible, index-
committee-independent cut, the same approach the workspace already settled
on for NSE research (see the `nse-bhavcopy-is-the-data-source` memory). This
also bounds how many pages get scraped from Screener.in per run.
`UNIVERSE_TOP_N` is shared across every NSE tier (equity/ETF/REIT/InvIT/SGB)
and the S&P 500 leg — it's only ever a ceiling, so a tier smaller than it
(all four non-equity NSE tiers, currently) is unaffected. Crypto is the one
deliberate exception, on its own `CRYPTO_TOP_N` (default 50) — see "Asset
coverage" above.

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
use it for testing. A full run against the default 400-name equity universe
takes ~13–20 minutes (400 requests x 1.5s politeness delay, plus network
latency) on a cold cache, plus another 1–3 minutes for the 200-coin crypto
RSI scan (no politeness delay there, just ccxt's own rate limiter); a
same-week equity rerun is much faster since fundamentals are cached for 7
days. Drop `--dry-run` to actually send to Telegram once
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
5. ~~**Scheduling**~~ — **done 2026-09-16, verified end-to-end 2026-09-17.**
   `deploy/` has the full launchd setup: a signed launcher app (JobPipe's
   `~/Applications/*.app` pattern — a code identity for TCC to hang a grant
   on, if one's ever needed) driving `run-daily.sh` at 19:00 IST. It runs
   from a **dedicated worktree**, `../quant_bot-screener` (branch
   `screener-scheduled`), not this checkout — `value-rsi-screener` is a
   research branch that stays unmerged, and this checkout gets switched to
   `main` for live-bot work, which would silently break a job pointed here.
   The worktree hard-resets to `origin/value-rsi-screener` on every run, so
   **a local commit on this branch has no effect on the scheduled digest
   until it's pushed.**
   **No manual TCC grant was actually needed.** JobPipe's own docs describe
   a required System Settings > Privacy & Security > Full Disk Access step;
   `launchctl kickstart` on this launcher ran clean on the first try —
   `sqlite3`'ing `TCC.db` afterward showed zero grant row for it at all. See
   `deploy/build-launcher.sh`'s header for the live evidence and what to
   check if a *future* run ever does hit exit 126 (the actual TCC service
   involved is `kTCCServiceSystemPolicyDocumentsFolder`, under Files and
   Folders — not the Full Disk Access pane).
