<h1 align="center">⚡ QuantBot</h1>

<p align="center">
  <b>Production-grade algorithmic trading bot for BTC/USDT futures — from backtest to cloud deployment</b><br>
  <i>A full DevOps project — 20 backtests across 6.5 years, Docker multi-stage builds, Terraform IaC, GitHub Actions CI/CD, and live deployment on Oracle Cloud with Cloudflare Tunnel.</i>
  <br><br>
  <a href="https://github.com/Asit0007/QuantBot/actions/workflows/deploy.yml">
    <img src="https://github.com/Asit0007/QuantBot/actions/workflows/deploy.yml/badge.svg" alt="CI/CD Status" />
  </a>
  <a href="https://github.com/Asit0007/QuantBot/blob/main/LICENSE">
    <img src="https://img.shields.io/github/license/Asit0007/QuantBot?color=blue" alt="License" />
  </a>
  <a href="https://github.com/Asit0007/QuantBot">
    <img src="https://img.shields.io/github/last-commit/Asit0007/QuantBot" alt="Last Commit" />
  </a>
  <img src="https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/Terraform-1.3+-7B42BC?logo=terraform&logoColor=white" alt="Terraform" />
  <img src="https://img.shields.io/badge/Oracle_Cloud-Always_Free-F80000?logo=oracle&logoColor=white" alt="OCI" />
  <img src="https://img.shields.io/badge/Docker-Containerized-2496ED?logo=docker&logoColor=white" alt="Docker" />
  <img src="https://img.shields.io/badge/Cloudflare-Tunnel-F38020?logo=cloudflare&logoColor=white" alt="Cloudflare" />
</p>

---

## What This Project Demonstrates

QuantBot is an end-to-end algorithmic trading and DevOps project — built from scratch, broken, debugged, and shipped independently. Every component was designed with production readiness in mind: from rigorous backtesting to zero-downtime deployments.

| Domain                     | Tools & Practices                                                              |
| -------------------------- | ------------------------------------------------------------------------------ |
| **Infrastructure as Code** | Terraform (OCI VCN, subnets, security lists, ARM compute)                      |
| **Containerization**       | Docker multi-stage builds, 4-service Docker Compose stack                      |
| **CI/CD Pipeline**         | GitHub Actions (lint → selective deploy → health check)                        |
| **Cloud Deployment**       | Oracle Cloud Infrastructure Always Free ARM (VM.Standard.A1.Flex)              |
| **Networking & Security**  | Cloudflare Tunnel (zero-port-exposure HTTPS), iptables, OCI security lists     |
| **Observability**          | Real-time Plotly Dash dashboard, Telegram alerting, structured logging         |
| **Quantitative Finance**   | RSI divergence, MACD cross, volume spike signals — 20 backtests over 6.5 years |
| **Secrets Management**     | Environment variable isolation, `.gitignore` hardening, GitHub Secrets         |
| **Log Management**         | Docker json-file driver with rotation (max-size, max-file)                     |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    Oracle Cloud (ARM VM)                         │
│                                                                  │
│  ┌─────────────┐    shared volume     ┌──────────────────────┐  │
│  │   bot.py    │ ──bot_state.json──▶  │    notifier.py       │  │
│  │  (Trading)  │ ──trade_log.csv───▶  │  (Telegram Alerts)   │  │
│  │             │ ──rsi_history.json─▶ │                      │  │
│  └─────────────┘                      └──────────────────────┘  │
│         │               quantbot_data                           │
│         │               (Docker volume)                          │
│         ▼                      ▲                                 │
│  ┌─────────────┐               │         ┌──────────────────┐   │
│  │corpus_mgr   │               └─────────│   dashboard.py   │   │
│  │  (DCA &     │                         │  (Plotly Dash)   │   │
│  │  Ratchet)   │                         │  port 8050       │   │
│  └─────────────┘                         └────────┬─────────┘   │
│                                                    │             │
│                                          ┌─────────▼─────────┐  │
│                                          │      nginx         │  │
│                                          │   (port 8888)      │  │
│                                          └─────────┬─────────┘  │
└────────────────────────────────────────────────────┼────────────┘
                                                      │
                                          ┌───────────▼──────────┐
                                          │  Cloudflare Tunnel   │
                                          │  (HTTPS, no open     │
                                          │   ports required)    │
                                          └───────────┬──────────┘
                                                      │
                                          ┌───────────▼──────────┐
                                          │  quantbot.asitminz   │
                                          │  .com (HTTPS)        │
                                          └──────────────────────┘
```

### Key Design Decisions

- **ARM VM (A1.Flex)** chosen for Oracle's Always Free tier — 4 OCPUs / 24GB RAM available for free, ARM architecture runs Python workloads efficiently at near-zero cost.
- **Shared Docker volume** instead of a database or message queue — all four services communicate through JSON/CSV files on a named volume. This eliminates network overhead for a single-host deployment while keeping services completely decoupled.
- **Selective CI/CD restarts** — GitHub Actions detects which files changed (bot.py, notifier.py, dashboard.py, or infrastructure) and only rebuilds the affected container. A bot with an open position is never restarted mid-trade.
- **Cloudflare Tunnel** instead of open ports — the VM has no inbound ports exposed to the internet (except SSH from a specific IP). All dashboard traffic flows through Cloudflare's network, hiding the server IP and providing automatic HTTPS with no certificate management.
- **CorpusManager module** separates risk management from trading logic — handles DCA contributions, corpus ratcheting (scale up after 10 net-positive trades, scale down after 10 consecutive losses), and monthly growth calculations independently.

---

## Signal Logic

The trading signal requires **all three gates to align simultaneously** on a 15-minute BTC/USDT futures candle — this selectivity is by design and is what makes the strategy viable at 20× leverage.

```
Gate 1 — RSI Divergence (armed for DIV_MEMORY=3 candles)
  Bullish: price makes lower low, RSI makes higher low (momentum diverging)
  Bearish: price makes higher high, RSI makes lower high

Gate 2 — MACD Cross (timing confirmation)
  Bull entry: MACD line crosses above signal line
  Bear entry: MACD line crosses below signal line

Gate 3 — Volume Spike (institutional confirmation)
  Current volume > 2× 20-bar SMA volume
  Filters out ~70% of candles — only acts on significant moves

All three gates armed simultaneously → entry
ATR-based stop: Long = entry − (ATR × 2.0), Short = entry + (ATR × 1.5)
Exit: opposite signal OR stop hit
```

---

## Backtest Results

Validated across **20 backtests** over **6.5 years** (Sep 2019 → Mar 2026) covering multiple market regimes:

| Year      | Market      | P&L         | Note                        |
| --------- | ----------- | ----------- | --------------------------- |
| 2019      | Neutral     | Small loss  | Warmup period               |
| 2020      | Bull        | +206%       | COVID crash + recovery      |
| 2021      | Bull        | +137%       | BTC ATH cycle               |
| 2022      | Bear        | **-44%**    | Worst year — FTX collapse   |
| 2023      | Bull        | +325%       | Recovery + new accumulation |
| 2024      | Bull        | +126%       | ETF approval cycle          |
| **TOTAL** | **6.5 yrs** | **+45%/yr** | **$100 → $4,699**           |

**Config A (locked parameters — do not change without re-backtesting):**

| Parameter        | Value          | Rationale                         |
| ---------------- | -------------- | --------------------------------- |
| Symbol           | BTC/USDT       | Highest liquidity futures pair    |
| Timeframe        | 15m            | Signal quality vs. noise tradeoff |
| Leverage         | 20×            | Confirmed safe with ATR stops     |
| Risk per trade   | 10% corpus     | Validated over 6.5 years          |
| ATR stop (long)  | 2.0×           | Avoids premature stop-outs        |
| ATR stop (short) | 1.5×           | Tighter on shorts — regime aware  |
| Circuit breaker  | 5 losses → 48h | Flat pause, not tiered            |
| Win rate         | 12.4%          | High R:R, not high frequency      |
| Profit factor    | 1.78           | Gross profit / gross loss         |

---

## Live Dashboard

The dashboard auto-refreshes every 15 seconds and reads directly from the shared Docker volume:

**Overview Tab:**

- Real-time balance, return %, corpus
- Equity curve, drawdown chart
- Monthly P&L bars, rolling win rate
- Open position with unrealised P&L
- Full trade history table with filtering

**RSI Radar Tab:**

- Live RSI gauge cards for BTC, ETH, SOL, BNB, XRP, SUI
- Colour-coded: green border (oversold < 20), red border (overbought > 80)
- Historical extreme events table (all readings outside thresholds)
- RSI over time line chart with threshold bands

```
https://quantbot.asitminz.com   ← live dashboard (paper trading mode)
```

---

## Infrastructure

All cloud resources are managed as Terraform code — no manual Console clicks after initial provisioning.

```hcl
# Oracle Cloud ARM VM — Always Free
resource "oci_core_instance" "quantbot_vm" {
  shape = "VM.Standard.A1.Flex"
  shape_config {
    ocpus         = 1
    memory_in_gbs = 6
  }
  # cloud-init: installs Docker, clones repo, starts services
}
```

**Resources provisioned:**

| Resource         | Type                             | Free Tier      |
| ---------------- | -------------------------------- | -------------- |
| VCN              | Virtual Cloud Network            | ✅ Always Free |
| Internet Gateway | IGW                              | ✅ Always Free |
| Route Table      | Public routing                   | ✅ Always Free |
| Security List    | Firewall rules (SSH + port 8888) | ✅ Always Free |
| Subnet           | Public subnet                    | ✅ Always Free |
| Compute          | VM.Standard.A1.Flex 1 OCPU / 6GB | ✅ Always Free |
| Boot Volume      | 50GB                             | ✅ Always Free |
| **Monthly cost** |                                  | **$0**         |

---

## CI/CD Pipeline

```
Push to main branch
        │
        ▼
  Lint & Syntax Check
  ├── flake8 (E9, F63, F7, F82 — real errors only)
  └── py_compile on all 4 Python files
        │
        ▼ (only if lint passes)
  Deploy to Oracle Cloud
  ├── Detect changed services (git diff HEAD~1)
  │   ├── bot.py / corpus_manager.py → BOT=true
  │   ├── notifier.py               → NOTIFIER=true
  │   ├── dashboard.py              → DASHBOARD=true
  │   └── Dockerfile/requirements   → INFRA=true
  │
  ├── Safety gate (if bot.py changed)
  │   └── Check bot_state.json for open position
  │       └── If open → SKIP bot restart
  │
  ├── Copy files to server (scp — explicit file list, no .git)
  │
  ├── Selective restart
  │   ├── INFRA=true  → docker compose build && up -d (full rebuild)
  │   ├── DASHBOARD   → docker compose up -d --no-deps --build dashboard
  │   ├── NOTIFIER    → docker compose up -d --no-deps --build notifier
  │   └── BOT         → docker compose up -d --no-deps --build bot
  │
  └── Health check
      └── docker inspect each container → exit 1 if any not "running"
```

**GitHub Secrets required:**

| Secret           | Description                                          |
| ---------------- | ---------------------------------------------------- |
| `ORACLE_HOST`    | VM public IP address                                 |
| `ORACLE_USER`    | `ubuntu`                                             |
| `ORACLE_SSH_KEY` | Full contents of `~/.ssh/quantbot_rsa` (private key) |

---

## Project Structure

```
quantbot/
├── bot.py                    # Trading engine — signal detection, order management
├── corpus_manager.py         # Risk management — DCA, ratchet, corpus tracking
├── dashboard.py              # Plotly Dash web dashboard (Overview + RSI Radar)
├── notifier.py               # Telegram bot — alerts, heartbeat, RSI scanner
├── backtest.py               # Backtest engine — 20 configs tested over 6.5 years
├── backtest_pa.py            # Price Action backtest (CHoCH + BOS + FVG)
├── backtest_combo.py         # Combination signal backtest (4 hybrid configs)
├── requirements.txt          # Python dependencies
├── Dockerfile                # Multi-stage build (base → bot / notifier / dashboard)
├── docker-compose.yml        # 4-service stack with shared named volume
├── .env.example              # All environment variables documented
├── .gitignore                # Secrets, state files, logs excluded
├── nginx/
│   └── nginx.conf            # Reverse proxy — port 8888, Cloudflare-compatible
├── terraform/
│   ├── main.tf               # OCI compute, VCN, security, cloud-init
│   ├── variables.tf          # All input variables
│   ├── outputs.tf            # VM IP, SSH command, dashboard URL
│   └── terraform.tfvars.example  # Template — never commit .tfvars
└── .github/
    └── workflows/
        └── deploy.yml        # CI/CD: lint → deploy → health check
```

---

## Getting Started

### Prerequisites

- Python 3.11+
- Docker + Docker Compose v2
- Terraform 1.3+
- Oracle Cloud account (Always Free tier)
- Binance account (for live trading only — paper mode requires no keys)
- Telegram bot token (from @BotFather)

### Local Development

```bash
# 1. Clone
git clone https://github.com/Asit0007/QuantBot.git
cd QuantBot

# 2. Create virtual environment
python3 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env — set TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
# Leave BINANCE_API_KEY blank for paper trading
# Set DATA_DIR=. for local development

# 5. Run all three services in separate terminals
python bot.py          # Terminal 1 — starts in paper mode by default
python notifier.py     # Terminal 2 — Telegram alerts
python dashboard.py    # Terminal 3 — http://localhost:8050
```

### Paper Trading Verification

Before going live, run at least 20 paper trades and compare to backtest benchmarks:

```bash
# Check current status
python bot.py --status

# Expected output shows:
# Balance, trades, WR, net P&L
# Open position if any
# Corpus and DCA totals
```

Benchmarks to match (within 20%):

- Win rate: **12.4%** (backtest)
- Profit factor: **1.78** (backtest)

### Going Live

Only after 20+ paper trades match the backtest benchmarks:

```bash
# Edit .env
PAPER_TRADE=false
BINANCE_API_KEY=your_key
BINANCE_API_SECRET=your_secret

# Restart bot
docker compose up -d --no-deps --build bot
```

---

## Cloud Deployment

### 1. Provision Infrastructure (one time)

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
# Fill in: tenancy_ocid, user_ocid, fingerprint, ssh_public_key,
#          my_ip_cidr, repo_url, vm_image_ocid

terraform init
terraform plan    # Review — should show 6 resources, all free tier
terraform apply   # ~3 minutes
# Outputs: vm_public_ip, ssh_command, dashboard_url
```

### 2. Configure Server

```bash
ssh -i ~/.ssh/quantbot_rsa ubuntu@YOUR_VM_IP

# Create .env with production values
nano ~/quantbot/.env
# Set DATA_DIR=/app/data, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

chmod 600 ~/quantbot/.env
```

### 3. Deploy via CI/CD

```bash
# Add GitHub Secrets: ORACLE_HOST, ORACLE_USER, ORACLE_SSH_KEY
# Then push to trigger deployment:
git push origin main

# Watch: GitHub → Actions → Deploy to Oracle Cloud
# All steps should turn green within ~3 minutes
```

### 4. Verify Deployment

```bash
ssh -i ~/.ssh/quantbot_rsa ubuntu@YOUR_VM_IP

sudo docker ps
# Expected:
# quantbot_bot        Up X minutes (healthy)
# quantbot_notifier   Up X minutes
# quantbot_dashboard  Up X minutes
# quantbot_nginx      Up X minutes

sudo docker logs quantbot_bot --tail=20
# Should show: QuantBot PAPER BTC/USDT 15m 20× 10% risk
#              Connected — BTC/USDT:USDT (PAPER)
#              Next candle in Xm Xs ...
```

---

## Cloudflare Tunnel Setup

Zero-port-exposure HTTPS — no certificates to manage, server IP fully hidden:

```bash
# On the server
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 \
  -o /usr/local/bin/cloudflared
chmod +x /usr/local/bin/cloudflared

cloudflared tunnel login            # Opens browser for Cloudflare auth
cloudflared tunnel create quantbot  # Creates tunnel, saves credentials JSON
cloudflared tunnel route dns quantbot quantbot.yourdomain.com

# Create config
cat > ~/.cloudflared/config.yml << EOF
tunnel: YOUR_TUNNEL_ID
credentials-file: /home/ubuntu/.cloudflared/YOUR_TUNNEL_ID.json
ingress:
  - hostname: quantbot.yourdomain.com
    service: http://localhost:8888
  - service: http_status:404
EOF

# Install as systemd service (survives reboots)
sudo mkdir -p /etc/cloudflared
sudo cp ~/.cloudflared/config.yml /etc/cloudflared/
sudo cp ~/.cloudflared/*.json /etc/cloudflared/
sudo cloudflared service install
sudo systemctl enable --now cloudflared
```

Dashboard available at `https://quantbot.yourdomain.com` with automatic HTTPS, no certificate renewal required.

---

## Environment Variables

All configuration is environment-driven — nothing is hardcoded.

```bash
# ── Trading Mode ─────────────────────────────────────────
PAPER_TRADE=true          # true = simulate, false = live orders
START_BALANCE=100.0       # Starting balance for fresh state

# ── Position Sizing (locked from backtest) ───────────────
LEVERAGE=20               # 20× isolated margin
RISK_PER_TRADE=0.10       # 10% of corpus per trade as margin

# ── DCA ──────────────────────────────────────────────────
DCA_MONTHLY_USD=10.0      # Monthly contribution amount
DCA_DAY=10                # Day of month for contribution
DCA_ANNUAL_GROWTH=0.10    # 10% annual step-up on DCA amount

# ── Binance API (live trading only) ──────────────────────
BINANCE_API_KEY=          # Leave blank for paper trading
BINANCE_API_SECRET=

# ── Telegram ─────────────────────────────────────────────
TELEGRAM_TOKEN=           # From @BotFather
TELEGRAM_CHAT_ID=         # From @userinfobot

# ── Infrastructure ───────────────────────────────────────
DATA_DIR=/app/data        # Docker: /app/data, Local: .
DASHBOARD_PORT=8050
DASHBOARD_REFRESH_MS=15000

# ── Strategy (commented — defaults are backtest-validated) ─
# SYMBOL=BTC/USDT
# TIMEFRAME=15m
# CB_TRIGGER=5
# CB_HOURS=48
# (changing any of these invalidates the 6.5-year backtest)
```

---

## Monitoring & Alerting

Telegram alerts are sent for every significant event:

| Alert               | Trigger                                   |
| ------------------- | ----------------------------------------- |
| ✅ Notifier started | Service boot                              |
| 📈 Trade opened     | Long/short entry with price, stop, margin |
| 📉 Trade closed     | Exit with P&L, reason, hold time          |
| 🛑 Circuit breaker  | 5 consecutive losses → 48h pause          |
| 🚨 Bot crash/stall  | No state update for > 30 minutes          |
| 📊 Daily summary    | Midnight UTC — balance, trades, P&L       |
| 💰 DCA contribution | Monthly on DCA_DAY                        |
| 🔵 RSI oversold     | Any coin < 20 on monthly/weekly           |
| 🔴 RSI overbought   | Any coin > 80 on monthly/weekly           |

RSI is scanned every 4 hours across 6 coins: BTC, ETH, SOL, BNB, XRP, SUI.

---

## Log Management

Docker log rotation is configured in `docker-compose.yml` — logs never grow unbounded:

| Container | Max file size | Max files | Max total |
| --------- | ------------- | --------- | --------- |
| bot       | 10MB          | 5         | 50MB      |
| notifier  | 5MB           | 3         | 15MB      |
| dashboard | 5MB           | 3         | 15MB      |
| nginx     | 2MB           | 2         | 4MB       |
| **Total** |               |           | **~84MB** |

```bash
# View live logs
sudo docker logs quantbot_bot -f
sudo docker logs quantbot_notifier -f

# All containers at once
sudo docker compose -f ~/quantbot/docker-compose.yml logs -f
```

---

## Useful Commands

```bash
# Check bot status (local)
python bot.py --status

# Reset all state and start fresh
python bot.py --reset

# Check live state on server
sudo docker exec quantbot_bot python bot.py --status

# Manually trigger full rebuild
cd ~/quantbot && sudo docker compose up -d --build

# Check Cloudflare tunnel
sudo systemctl status cloudflared
cloudflared tunnel info quantbot

# View trade history
cat ~/quantbot/data/trade_log.csv  # (on server, inside Docker volume)
sudo docker exec quantbot_bot cat /app/data/trade_log.csv
```

---

## What I Learned / Challenges Solved

- **OCI ARM capacity constraints**: The Hyderabad free tier ARM pool was exhausted. Solved by writing an automated retry script (5-minute intervals, cycling availability domains), filing a support ticket (escalated to Sev 2), and ultimately upgrading to PAYG — which grants access to the paid capacity pool while remaining within Always Free resource limits.

- **Docker Compose v1 vs v2**: Ubuntu 22.04 ships Docker Compose v2 (`docker compose` with a space). The legacy `docker-compose` binary is not installed by default. All CI/CD scripts updated accordingly — a subtle but deploy-breaking difference.

- **GitHub Actions scp-action and `.git` permissions**: Copying the entire repo with `source: "."` included `.git/objects` which has mode 444 files — causing `permission denied` on tar extraction. Fixed by listing only the files actually needed by the server, excluding version control internals entirely.

- **Cloudflare Tunnel vs open ports**: Traditional HTTPS (certbot + nginx) requires open port 443, certificate renewal every 90 days, and exposes the server IP. Cloudflare Tunnel eliminates all of this — the VM makes an outbound connection to Cloudflare's network, requiring zero inbound ports and providing automatic HTTPS forever. Server IP is completely hidden.

- **Lookahead bias in backtesting**: An early implementation of swing high/low detection marked swings at bar `i` using data from bars `i+1` through `i+5` — data that doesn't exist in real time. This produced a $2.4 billion backtest result. Fixed by marking swings at bar `i+SWING_LOOKBACK` (the first bar where confirmation is actually complete), which produced realistic results.

- **Heartbeat false positives**: The notifier's heartbeat was comparing `now` against `last_candle_ts` — the candle's own timestamp (e.g. 16:00). Every candle processed at 16:30 would immediately appear "30 minutes stale" and fire a crash alert. Fixed by adding `last_updated_at` (wall-clock time of processing) to `bot_state.json` and comparing against that instead.

- **Terraform image data source**: OCI's images API returns `null` when filtering by both `shape` and `operating_system_version` in certain regions. Removed the shape filter and switched to passing the image OCID directly as a variable — more explicit, region-agnostic, and avoids the API quirk entirely.

- **Progressive scaling vs flat circuit breaker**: Backtesting across 5 configurations (flat CB, standard scaling, aggressive scaling, conservative scaling, combined) confirmed that flat 48-hour pauses outperform progressive position scaling on this signal. The key insight: loss streaks cluster just before big reversals — scaling down means missing the recovery.

---

## Security

- **`.env` is gitignored** — never committed, created manually on the server
- **Terraform state** (`terraform.tfstate`, `terraform.tfvars`) is gitignored — contains resource IDs
- **SSH access locked to specific IP** via OCI security list (`my_ip_cidr/32`)
- **No Binance withdrawal permissions** — API keys created with Futures trading only
- **Cloudflare Tunnel** hides server IP — no direct exposure to the internet
- **Docker volume** isolates state files inside the container network
- **GitHub Secrets** for all CI/CD credentials — never in workflow YAML

If you find a security issue, please email [asitminz007@gmail.com](mailto:asitminz007@gmail.com).

---

## Future Improvements

- [ ] Implement `bot_paused.flag` check in bot.py entry guard (notifier `/pause` command already creates the flag)
- [ ] Paper trade 20+ trades → compare WR/PF to backtest benchmarks → go live at $100
- [ ] Investigate C3 signal (RSI Div + CHoCH + FVG) — backtest showed +64.8%/yr with same DD as Config A
- [ ] Terraform remote state (OCI Object Storage) for team/multi-environment support
- [ ] Grafana integration for metrics beyond the Dash dashboard
- [ ] Multi-symbol support (ETH/USDT, SOL/USDT) with separate corpus per symbol
- [ ] Alertmanager integration for PagerDuty/OpsGenie escalation

---

## Backtest Methodology

Three separate backtest files cover different hypothesis tests:

| File                | Purpose                                           | Result                     |
| ------------------- | ------------------------------------------------- | -------------------------- |
| `backtest.py`       | Config A–E: flat CB, progressive scaling variants | Config A wins              |
| `backtest_pa.py`    | Pure Price Action: CHoCH + BOS + FVG              | $1,759 — loses to Config A |
| `backtest_combo.py` | 4 hybrid combos: RSI Div + PA signals             | C3 ($6,362) shows promise  |

All backtests use identical risk parameters (20× leverage, 10% risk, flat CB, CorpusManager DCA) so results are directly comparable.

---

## License

MIT — see [LICENSE](LICENSE) for details.
Built for learning, portfolio demonstration, and live deployment. Review risk parameters before any real capital deployment.

---

<p align="center">
  <b>QuantBot &copy; 2026 | Built by <a href="https://github.com/Asit0007">Asit Minz</a></b><br>
  <i>Trained on caffeine. Powered by backtest. Not financial advice — just vibes and RSI divergence.</i>
</p>
