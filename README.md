# 📈 DailyMomentumStockBreakout

Automated NSE stock analysis toolkit that generates a daily **RSI Multi-Timeframe HTML report** and **Momentum Breakout Excel scanner** for Indian equity markets.

---

## 🔗 Live Report

> **GitHub Pages** (auto-updated every trading day at 5:00 AM IST):
> `https://dipenshah2006.github.io/DailyMomentumStockBreakout/`

> **Replit Hosted App:**
> `https://daily-momentum-stock-breakout--dipenshaah.replit.app`

---

## 📁 Repository Structure

```
DailyMomentumStockBreakout/
│
├── rsi_mtf_report_nse.py          # NSE RSI Multi-Timeframe Report (main script)
├── rsi_mtf_report_bse2.py         # BSE RSI Multi-Timeframe Report
├── momentum_breakout_scanner.py   # NSE Momentum Breakout Scanner
├── main.py                        # Flask web dashboard (Replit hosting)
├── requirements.txt               # Python dependencies
│
├── india/                         # Local stock universe data (tracked in git)
│   └── NSE/
│       ├── NSECash/
│       │   └── EQUITY_L.csv       # NSE EQ-series master list (~2,138 stocks)
│       └── NSESME/
│           └── MW-SME-05-May-2026.csv  # NSE SME stocks (ST + SM series)
│
├── charts/                        # Generated PNG charts (auto-created, git-ignored)
│
└── .github/
    └── workflows/
        └── generate-report.yml    # GitHub Actions — auto-generate & deploy report
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

**Signals generated:**
- Phase detection: Uptrend / Sideways / Bearish
- RSI/SMA crossover: Strong Buy 🚀 / Buy / Watch / Sell
- All-Time High (ATH) proximity tagging
- Ranking vs Nifty50 (percentile)
- Ranking vs all NSE stocks (universe percentile)
- 52-week high/low % distance

**Outputs:**
- `rsi_mtf_report_NSE_YYYYMMDD_HHMM.html` — interactive sortable/filterable report
- `charts/TICKER.png` — per-stock HD charts (lazy-loaded, no browser hang)
- `error_log_YYYYMMDD_HHMM.txt` — exception log per ticker

**Key config (top of script):**
```python
DATA_PERIOD      = "max"     # full history for true ATH
MIN_CANDLES      = 1         # include all stocks
MAX_CHART_STOCKS = 0         # 0 = charts for all stocks
CHART_DPI        = 200       # ultra HD
RSI_P            = 14
RSI_SMA_P        = 34
SCORE_STRONG_BUY = 16
SCORE_BUY        = 12
SCORE_WATCH      = 8
```

---

### 2. `momentum_breakout_scanner.py` — Momentum Breakout Scanner

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

## 📂 Local CSV Data Files

Both scripts **check for local CSV files first** before downloading from NSE (which can be unreliable):

| File | Path | Contents |
|------|------|----------|
| NSE EQ Master | `india/NSE/NSECash/EQUITY_L.csv` | ~2,138 EQ-series stocks |
| NSE SME List | `india/NSE/NSESME/MW-SME-05-May-2026.csv` | SME (ST + SM series) stocks |

### Updating the CSV files

**EQUITY_L.csv** — Download fresh from NSE:
```
https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv
```
Save to: `india/NSE/NSECash/EQUITY_L.csv`

**SME CSV** — Download from NSE Market Watch:
```
https://www.nseindia.com/market-data/live-equity-market?series=SME
```
Export → save to: `india/NSE/NSESME/MW-SME-05-May-2026.csv`
> Update `LOCAL_SME_CSV` path in `rsi_mtf_report_nse.py` to match the new filename.

---

## ⚙️ Setup — Local Machine

### Prerequisites
- Python 3.10+
- pip

### Install

```bash
git clone https://github.com/dipenshah2006/DailyMomentumStockBreakout.git
cd DailyMomentumStockBreakout
pip install -r requirements.txt
```

### Run RSI MTF Report
```bash
python rsi_mtf_report_nse.py
```
> ⏱️ Best time: **after 3:35 PM IST** on any trading day (final closed candles).
> Takes 30–60 minutes for full 2,000+ stock universe.

### Run Momentum Breakout Scanner
```bash
python momentum_breakout_scanner.py
```

### Optional: Custom stock list
Create `my_stocks.txt` in the project root:
```
RELIANCE
TCS
INFY
# commented lines are ignored
```

---

## 🌐 Setup — GitHub Actions (Automated Daily Report)

The report is automatically generated every **weekday at 5:00 AM IST** and published to GitHub Pages.

### Step 1 — Enable GitHub Pages

1. Go to your repo → **Settings → Pages**
2. Under **Source**, select **GitHub Actions**
3. Click **Save**

### Step 2 — Push CSV data files to GitHub

```bash
git pull origin main
git add india/NSE/NSECash/EQUITY_L.csv
git add "india/NSE/NSESME/MW-SME-05-May-2026.csv"
git add .github/workflows/generate-report.yml requirements.txt README.md
git commit -m "Add local NSE/SME CSV data + GitHub Actions workflow"
git push origin main
```

### Step 3 — Verify Actions run

1. Go to your repo → **Actions** tab
2. You should see **Generate NSE RSI Report** workflow
3. Click **Run workflow** to trigger it manually the first time

### Workflow triggers

| Trigger | When |
|---------|------|
| 📅 Schedule | Every Mon–Fri at **5:00 AM IST** (11:30 PM UTC) |
| 📂 CSV update | Whenever `india/NSE/**` files are pushed |
| 🖱️ Manual | Actions tab → Run workflow |

### Workflow file: `.github/workflows/generate-report.yml`

```yaml
on:
  push:
    paths:
      - 'india/NSE/**'
  schedule:
    - cron: '30 23 * * 0-4'   # 5:00 AM IST Mon–Fri
  workflow_dispatch:
```

**Live report URL after setup:**
```
https://dipenshah2006.github.io/DailyMomentumStockBreakout/
```

---

## 🚀 Setup — Replit (Web Dashboard)

The project runs as a Flask web app on Replit with:
- Auto-generation on startup (if no report found)
- Daily 5:00 AM IST scheduled run via APScheduler
- Direct HTML report served on homepage

**Run locally:**
```bash
pip install flask apscheduler pytz
python main.py
# Open http://localhost:5000
```

---

## 📊 Report Features

The generated HTML report includes:

- **Filters:** Phase · Cap size · Sector · Index · Signal · ATH distance
- **Sorting:** RSI, Score, Donchian, ATH%, 52W%, Market Cap
- **ATH tags:** 🏆 At ATH · Within 5% / 10% / 20% · >20% below
- **Nifty50 badge:** Highlights index constituents
- **SME badge:** Highlights SME-listed stocks
- **Lazy-loaded charts:** PNG charts load on expand (no browser hang)
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
| `CHART_DPI` | `200` | Chart image resolution |
| `CHART_BARS` | `120` | Bars shown per chart |
| `RSI_P` | `14` | RSI period |
| `RSI_SMA_P` | `34` | RSI smoothing SMA period |
| `CCI_P` | `20` | CCI period |
| `BATCH_SIZE` | `25` | Stocks per download batch |
| `BATCH_PAUSE` | `1.0` | Seconds between batches |
| `PAGE_TBL` | `100` | Table rows per page |
| `PAGE_CARDS` | `50` | Cards per Load More |

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
openpyxl       # Excel output for scanner
flask          # Web dashboard (Replit only)
apscheduler    # Daily scheduler (Replit only)
pytz           # IST timezone (Replit only)
```

---

## ⚠️ Notes

- **Best run time:** After **3:35 PM IST** on trading days for final closed candles
- **NSE data:** yfinance uses Yahoo Finance (`.NS` suffix) — no NSE API key needed
- **Rate limiting:** Scripts batch downloads with 1-second pauses to avoid blocks
- **Caching:** `rsi_mtf_report_nse.py` caches OHLCV data locally (pickle) to speed up reruns
- **GitHub Actions runtime:** Full 2,000+ stock run takes ~45–60 min (within 2-hour cap)
