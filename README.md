# 📈 DailyMomentumStockBreakout

Automated NSE stock analysis toolkit that generates a daily **RSI Multi-Timeframe HTML report** and **Momentum Breakout Excel scanner** for Indian equity markets — with bulk email delivery to unlimited subscribers.

---

## 🔗 Live Reports — All URLs

All reports are auto-generated every **weekday at 5:00 AM IST** and available via two hosting platforms:

### 📡 GitHub Pages (always-on, no login needed)

| Report | URL |
|--------|-----|
| 📈 RSI Multi-Timeframe Breakout | https://dipenshah2006.github.io/DailyMomentumStockBreakout/ |
| 🏆 ATH Breakout | https://dipenshah2006.github.io/DailyMomentumStockBreakout/ath.html |
| 🚀 Rocket Scanner | https://dipenshah2006.github.io/DailyMomentumStockBreakout/rocket.html |
| 💎 Multibagger | https://dipenshah2006.github.io/DailyMomentumStockBreakout/multibagger.html |

### 🖥️ Replit Web Dashboard (live dashboard with Run Now button)

| Report | URL |
|--------|-----|
| 📈 RSI Multi-Timeframe Breakout | https://daily-momentum-stock-breakout--dipenshaah.replit.app/ |
| 🏆 ATH Breakout | https://daily-momentum-stock-breakout--dipenshaah.replit.app/ath |
| 🚀 Rocket Scanner | https://daily-momentum-stock-breakout--dipenshaah.replit.app/rocket |
| 💎 Multibagger | https://daily-momentum-stock-breakout--dipenshaah.replit.app/multibagger |

> **Tip:** GitHub Pages is the fastest way to share reports with subscribers — no login required, bookmark-friendly, and always shows the latest run.

---

## 📁 Repository Structure

```
DailyMomentumStockBreakout/
│
├── rsi_mtf_report_nse.py          # NSE RSI Multi-Timeframe Report (main script)
├── rsi_mtf_report_bse2.py         # BSE RSI Multi-Timeframe Report
├── momentum_breakout_scanner.py   # NSE Momentum Breakout Scanner
├── generate_summary.py            # Generates email_summary.html from the full report
├── send_report_email.py           # Bulk emailer — BCC batches or individual sends
├── email_recipients.txt           # Mailing list — one address per line
├── main.py                        # Flask web dashboard (Replit hosting)
├── requirements.txt               # Python dependencies
│
├── india/                         # Local stock universe data (tracked in git)
│   └── NSE/
│       ├── NSECash/
│       │   └── EQUITY_L.csv               # NSE EQ-series master list (~2,138 stocks)
│       ├── NSESME/
│       │   └── MW-SME-05-May-2026.csv     # NSE SME stocks (ST + SM series)
│       └── NIFTY_Indices_Master.xlsx      # Nifty index constituents + sector/industry map
│
├── charts/                        # Generated PNG charts (served via GitHub raw URLs)
│
└── .github/
    └── workflows/
        └── generate-report.yml    # GitHub Actions — auto-generate, deploy & email report
```

---

## 🛠️ Scripts Overview

### 1. `rsi_mtf_report_nse.py` — RSI MTF Report (NSE)

Scans the full NSE EQ + SME universe across **Daily / Weekly / Monthly** timeframes.

**Indicators calculated:**
| Indicator | Period | Timeframes |
|-----------|--------|------------|
| RSI | 14 | D / W / M |
| RSI SMA | 34 | D / W / M |
| MACD | 12, 26, 9 | D / W / M |
| CCI | 20 | D / W / M |
| ATR | 14 | Daily |
| Donchian | 20 | D / W / M |
| Bollinger Bands | 20 | Daily |
| MFI | 14 | Daily |

**Signals generated:**
- Phase detection: Uptrend / Sideways / Bearish
- RSI/SMA crossover: Strong Buy 🚀 / Buy / Watch / Sell
- All-Time High (ATH) proximity tagging
- Explosive breakout scoring (volume surge + BB breakout + MACD acceleration)
- Fibonacci extension targets (127.2% and 161.8%)
- Ranking vs Nifty50 (percentile)
- Ranking vs all NSE stocks (universe percentile)
- 52-week high/low % distance

**Outputs:**
- `rsi_mtf_report_NSE_YYYYMMDD_HHMM.html` — interactive sortable/filterable report
- `charts/TICKER.png` — per-stock HD charts (loaded from GitHub raw URLs, no browser hang)
- `error_log_YYYYMMDD_HHMM.txt` — exception log per ticker

**Key config (top of script):**
```python
DATA_PERIOD      = "max"     # full history for true ATH
MIN_CANDLES      = 1         # include all stocks
MAX_CHART_STOCKS = 0         # 0 = charts for all stocks
CHART_DPI        = 120       # chart image resolution
CHART_BARS       = 90        # bars shown per chart
RSI_P            = 14
RSI_SMA_P        = 34
SCORE_STRONG_BUY = 16
SCORE_BUY        = 12
SCORE_WATCH      = 8
GITHUB_CHARTS_BASE = "https://raw.githubusercontent.com/dipenshah2006/DailyMomentumStockBreakout/main/charts"
```

---

### 2. `generate_summary.py` — Daily Email Summary Generator

Reads the full HTML report, extracts the `STOCKS` JSON, and builds a rich
`email_summary.html` containing **all** qualifying stocks and sectors.

**Report sections:**
| Section | Contents |
|---------|----------|
| Market Summary | Scanned · Uptrend · Strong Buy · Buy · Watch · Fresh · Explosive |
| 🔥 Fresh Daily Breakouts | New Strong Buy crossovers today |
| 🚀 Strong Buy Table | **All** stocks with score ≥ 16 (no cap) |
| ✅ Buy Table | **All** stocks with score 12–15 (no cap) |
| 💥 Explosive Breakouts | Volume surge + BB breakout + MFI setups |
| 🗂️ Sector Breakdown | **All** sectors via NIFTY Indices Master (no cap) |

> The email contains no "View Full Report" or "Workflow Logs" links —
> it is a fully self-contained report readable in any email client.

---

### 3. `send_report_email.py` — Bulk Emailer

Sends the daily email to any number of subscribers using Gmail SMTP.
Designed to handle **1,000+ recipients** without hitting YAML or SMTP limits.

**Two delivery modes:**

| Mode | Behaviour | Best for |
|------|-----------|----------|
| `bcc` (default) | Groups into batches of `EMAIL_BCC_BATCH` (default 100) per SMTP call | Large lists — fast |
| `individual` | One SMTP call per recipient, 0.3 s delay | Personalised delivery |

**Environment variables:**

| Variable | Required | Description |
|----------|----------|-------------|
| `GMAIL_USERNAME` | ✅ | Sender Gmail address |
| `GMAIL_APP_PASSWORD` | ✅ | Gmail App Password (16-char, not login password) |
| `EMAIL_SEND_MODE` | optional | `bcc` or `individual` (default: `bcc`) |
| `EMAIL_BCC_BATCH` | optional | Recipients per BCC batch (default: `100`) |
| `EMAIL_RECIPIENTS` | optional | Comma-separated override — skips `email_recipients.txt` |
| `EMAIL_DRY_RUN` | optional | `1` = print recipients, do NOT send |
| `EMAIL_FAILURE_MODE` | optional | `1` = send failure notice instead of report |

**Run directly (local/Replit test):**
```bash
GMAIL_USERNAME="you@gmail.com" GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx" \
  python send_report_email.py
```

---

### 4. `email_recipients.txt` — Mailing List

One email address per line. Lines starting with `#` and blank lines are ignored.

```
# NSE Report mailing list
dipenshah2006@gmail.com
tradewithtrenddirection@gmail.com
# another@example.com   ← commented out / paused
```

**To manage subscribers:**
- **Add** → append a line
- **Remove** → delete the line
- **Pause** → prefix the line with `#`
- **One-off override** → use the `email_list` input when triggering manually from GitHub Actions

---

### 5. `momentum_breakout_scanner.py` — Momentum Breakout Scanner

Scores every NSE EQ stock out of **26 points** using multi-indicator momentum alignment.

**Scoring matrix (max 26 pts):**
| Signal | Points |
|--------|--------|
| MACD bullish crossover (Daily) | up to 4 |
| MACD bullish crossover (Weekly) | up to 4 |
| MACD bullish crossover (Monthly) | up to 4 |
| CCI(200) > 0 | 3 |
| RSI > 50 | 3 |
| RSI > 60 | +1 bonus |
| DMI+/DMI− bullish | 4 |
| Volume above average | 3 |

**Scoring tiers:**
| Score | Tier | Action |
|-------|------|--------|
| ≥ 18 | 🔥 Ultra Momentum | Buy breakout |
| ≥ 14 | ⚡ High Momentum | Buy dip / retest |
| ≥ 10 | 👀 Watchlist | Wait for alignment |

**Outputs:**
- Terminal — tiered results with progress bar
- `breakout_scan_YYYYMMDD_HHMM.xlsx` — 4-sheet Excel report
- CSV fallback if openpyxl not installed

---

## 📂 Local Data Files

| File | Path | Contents |
|------|------|----------|
| NSE EQ Master | `india/NSE/NSECash/EQUITY_L.csv` | ~2,138 EQ-series stocks |
| NSE SME List | `india/NSE/NSESME/MW-SME-05-May-2026.csv` | SME (ST + SM series) |
| NIFTY Indices Master | `india/NSE/NIFTY_Indices_Master.xlsx` | Index constituents + sector/industry map |

**Update EQUITY_L.csv:**
```
https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv
```
Save to: `india/NSE/NSECash/EQUITY_L.csv`

---

## ⚙️ Setup — Local Machine

```bash
git clone https://github.com/dipenshah2006/DailyMomentumStockBreakout.git
cd DailyMomentumStockBreakout
pip install -r requirements.txt

# Run full NSE report (best after 3:35 PM IST on trading days)
python rsi_mtf_report_nse.py

# Run momentum scanner
python momentum_breakout_scanner.py

# Test email delivery
GMAIL_USERNAME="you@gmail.com" GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx" \
  python send_report_email.py
```

---

## 🌐 Setup — GitHub Actions (Automated Daily Report + Email)

The report is automatically generated every **weekday at 5:00 AM IST**, deployed to
GitHub Pages, and emailed to all addresses in `email_recipients.txt`.

### Step 1 — Enable GitHub Pages

1. Go to **Settings → Pages**
2. Under **Source**, select **GitHub Actions**
3. Click **Save**

### Step 2 — Add required secrets

Go to **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
|--------|-------|
| `GMAIL_APP_PASSWORD` | 16-character Gmail App Password from [Google App Passwords](https://myaccount.google.com/apppasswords) |
| `REPORT_EMAIL_LIST` | *(optional)* Extra comma-separated BCC recipients (in addition to `email_recipients.txt`) |

### Step 3 — Add subscribers

Edit `email_recipients.txt` — add one address per line, commit and push.

### Step 4 — Verify

1. Go to **Actions → Generate NSE RSI Report**
2. Click **Run workflow**
3. Check the `Send daily summary email` step logs

### Workflow triggers

| Trigger | When |
|---------|------|
| 📅 Schedule | Every Mon–Fri at **5:00 AM IST** (11:30 PM UTC) |
| 📂 CSV update | Whenever `india/NSE/**` files are pushed |
| 🖱️ Manual | Actions tab → Run workflow |

### Manual workflow inputs

| Input | Default | Description |
|-------|---------|-------------|
| `max_chart_stocks` | `50` | `0` = generate charts for all stocks |
| `email_list` | — | Override recipients for this run only (comma-separated) |
| `send_mode` | `bcc` | `bcc` or `individual` |
| `dry_run` | `false` | `true` = print recipients, skip actual send |

---

## 🚀 Setup — Replit (Web Dashboard)

The project runs as a Flask web app with:
- Auto-generation on startup (if no report found)
- Daily 5:00 AM IST scheduled run via APScheduler
- Direct HTML report served on the homepage

**Required Replit secrets:**

| Secret | Purpose |
|--------|---------|
| `GMAIL_APP_PASSWORD` | Email delivery from `send_report_email.py` |
| `GITHUB_PAT` | Push commits to GitHub (if needed) |

```bash
pip install flask apscheduler pytz
python main.py
# Open http://localhost:5000
```

---

## 📊 Report Features

The generated HTML report includes:

- **Filters:** Phase · Cap size · Sector · Index · Signal · ATH distance · F&O
- **Sorting:** RSI, Score, Donchian, ATH%, 52W%, Market Cap (multi-column with Shift+click)
- **ATH tags:** 🏆 At ATH · Within 5% / 10% / 20% · >20% below
- **Explosive score:** Volume surge + Bollinger Band breakout + MACD + MFI composite
- **Fibonacci targets:** 127.2% and 161.8% extension levels
- **Nifty50 badge:** Highlights index constituents
- **SME badge:** Highlights SME-listed stocks
- **Lazy-loaded charts:** PNG charts fetched from GitHub raw URLs on card expand
- **Search:** Live search by ticker or company name
- **Pagination:** 100 rows/page in table · 50 cards per load

---

## 🔧 Configuration Reference

### `rsi_mtf_report_nse.py`

| Variable | Default | Description |
|----------|---------|-------------|
| `DATA_PERIOD` | `"max"` | yfinance history period |
| `MIN_CANDLES` | `1` | Min candles to include stock |
| `MAX_CHART_STOCKS` | `0` | 0 = charts for all stocks |
| `CHART_DPI` | `120` | Chart image resolution |
| `CHART_BARS` | `90` | Bars shown per chart |
| `RSI_P` | `14` | RSI period |
| `RSI_SMA_P` | `34` | RSI smoothing SMA period |
| `CCI_P` | `20` | CCI period |
| `BATCH_SIZE` | `25` | Stocks per download batch |
| `BATCH_PAUSE` | `1.0` | Seconds between batches |
| `PAGE_TBL` | `100` | Table rows per page |
| `PAGE_CARDS` | `50` | Cards per Load More |
| `GITHUB_CHARTS_BASE` | GitHub raw URL | Base URL for chart images in HTML report |

### `send_report_email.py`

| Variable | Default | Description |
|----------|---------|-------------|
| `EMAIL_SEND_MODE` | `bcc` | `bcc` or `individual` |
| `EMAIL_BCC_BATCH` | `100` | Recipients per BCC batch |
| `EMAIL_DRY_RUN` | `0` | `1` = simulate without sending |

### `momentum_breakout_scanner.py`

| Variable | Default | Description |
|----------|---------|-------------|
| `DATA_PERIOD` | `"2y"` | yfinance history period |
| `MIN_CANDLES` | `220` | Min trading days required |
| `TIER1_SCORE` | `18` | Ultra momentum threshold |
| `TIER2_SCORE` | `14` | High momentum threshold |
| `TIER3_SCORE` | `10` | Watchlist threshold |
| `BATCH_SIZE` | `25` | Stocks per download batch |

---

## 📦 Dependencies

```
yfinance       # Market data from Yahoo Finance
pandas         # Data manipulation
numpy          # Numerical calculations
matplotlib     # Chart generation (Agg backend — headless safe)
requests       # HTTP for NSE CSV download
openpyxl       # Excel output for scanner + NIFTY master loading
flask          # Web dashboard (Replit only)
apscheduler    # Daily scheduler (Replit only)
pytz           # IST timezone handling
```

---

## ⚠️ Notes

- **Best run time:** After **3:35 PM IST** on trading days for final closed candles
- **NSE data:** yfinance uses Yahoo Finance (`.NS` suffix) — no NSE API key needed
- **Rate limiting:** Scripts batch downloads with 1-second pauses to avoid blocks
- **Caching:** `rsi_mtf_report_nse.py` caches OHLCV data to `stock_data_cache.pkl` to speed up reruns
- **GitHub Actions runtime:** Full 2,000+ stock run takes ~45–60 min (within the 2-hour cap)
- **Gmail limits:** Regular Gmail ≈ 500 emails/day · Google Workspace ≈ 2,000/day · BCC mode with batch=100 keeps SMTP calls minimal
