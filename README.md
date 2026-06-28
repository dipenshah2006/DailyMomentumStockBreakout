# 📈 DailyMomentumStockBreakout

Automated NSE stock analysis toolkit that generates daily HTML reports and emails them every morning — covering RSI Multi-Timeframe, ATH Breakouts, Multibagger picks, Rocket Scanner, and Intraday breakouts for Indian equity markets.

---

## 🔗 Live Reports — All URLs

### 📡 GitHub Pages (always-on, no login needed)

| Report | URL |
|--------|-----|
| 📈 RSI Multi-Timeframe Breakout | https://dipenshah2006.github.io/DailyMomentumStockBreakout/ |
| 🏆 ATH Breakout | https://dipenshah2006.github.io/DailyMomentumStockBreakout/ath.html |
| 💎 Multibagger | https://dipenshah2006.github.io/DailyMomentumStockBreakout/multibagger.html |
| 🚀 Rocket Scanner | https://dipenshah2006.github.io/DailyMomentumStockBreakout/rocket.html |
| ⚡ Intraday Breakout | https://dipenshah2006.github.io/DailyMomentumStockBreakout/intraday.html |

> **Tip:** GitHub Pages is the fastest way to share reports — no login required, bookmark-friendly, always shows the latest run.

---

## 📅 Automated Email Schedule

| Time (IST) | Days | Email |
|-----------|------|-------|
| 5:00 AM | Mon – Sat | 📈 RSI MTF Breakout Report |
| 5:00 AM | Mon – Sat | 🏆 ATH Breakout Report |
| 5:00 AM | Mon – Sat | 💎 Multibagger Report |
| 5:00 AM | Mon – Sat | 🚀 Rocket Scanner Report |
| 9:30 AM | Mon – Fri | ⚡ Intraday Breakout Report |

All emails are sent to everyone listed in `email_recipients.txt`.

---

## 📁 Repository Structure

```
DailyMomentumStockBreakout/
│
├── rsi_mtf_report_nse.py          # NSE RSI Multi-Timeframe Report (main script)
├── ath_report.py                  # ATH Breakout scanner — stocks at or near all-time highs
├── multibagger_report.py          # Multibagger scanner — long-term compounders
├── rocket_scanner.py              # Rocket Scanner — explosive momentum breakouts
├── intraday_report.py             # Intraday Breakout scanner — PDH/VWAP/ORH (runs at 9:30 AM IST)
├── momentum_breakout_scanner.py   # Multi-indicator momentum scorer (Excel output)
├── generate_summary.py            # Builds email_summary.html from full RSI report
├── send_report_email.py           # Bulk emailer — BCC batches or individual sends
├── email_recipients.txt           # Mailing list — one address per line
├── main.py                        # Flask web dashboard (Replit hosting)
├── requirements.txt               # Python dependencies
│
├── india/                         # Local stock universe data (tracked in git)
│   └── NSE/
│       ├── NSECash/
│       │   └── EQUITY_L.csv               # NSE EQ-series master list (~2,360 stocks)
│       ├── NSESME/
│       │   └── MW-SME-*.csv               # NSE SME stocks (ST + SM series)
│       └── NIFTY_Indices_Master.xlsx      # Nifty index constituents + sector/industry map
│
├── charts/                        # Generated PNG charts (served via GitHub raw URLs)
│
└── .github/
    └── workflows/
        ├── generate-report.yml    # Morning reports — runs Mon–Sat at 5:00 AM IST
        └── intraday-report.yml    # Intraday report — runs Mon–Fri at 9:30 AM IST
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
- 52-week high/low % distance

**Output:** `rsi_mtf_report_NSE.html` → deployed to GitHub Pages as `index.html`

---

### 2. `ath_report.py` — ATH Breakout Report

Scans all 2,360 NSE EQ stocks and identifies those at or near their **all-time highs**.

**Columns:**
| Column | Description |
|--------|-------------|
| ATH Status | 🏆 AT ATH badge or proximity category |
| % vs ATH | Exact % above (green) or below (red) the all-time high |
| ATH Price / Date | Price and date the ATH was made |
| ↑ 52W Low | % the stock has risen from its 52-week low |
| ↑ ATH from Low | % the ATH price is above the all-time historical low |
| RSI D / W / M | Multi-timeframe RSI |
| Vol Ratio | Today's volume vs 20-day average |
| Phase | 📈 UPTREND / ➡️ SIDEWAYS / 📉 BEARISH |

**Filters:** 🏆 AT ATH · ✅ <5% · 🟡 <10% · 🟠 <20% · 📉 >20% · Phase buttons · Search

**Output:** `ath_report_NSE.html` → deployed as `ath.html`

---

### 3. `multibagger_report.py` — Multibagger Report

Scans for long-term compounders — stocks with consistent revenue/price growth over 3–5 years.

**Output:** `multibagger_report.html` → deployed as `multibagger.html`

---

### 4. `rocket_scanner.py` — Rocket Scanner

Identifies stocks with explosive momentum: high RSI + volume surge + price breakout alignment.

**Output:** `rocket_scan_latest.html` → deployed as `rocket.html`

---

### 5. `intraday_report.py` — Intraday Breakout Scanner

Runs at **9:30 AM IST** (Mon–Fri). Scans top 500 NSE stocks using live 5-minute bars.

**Signals detected:**
| Signal | Description |
|--------|-------------|
| 🟢 PDH Breakout | Price crossed above previous day's high + 0.2% buffer with volume |
| 🔵 VWAP Breakout | Price trading above intraday VWAP |
| 🟡 ORH Breakout | Price broke above opening range high (first 15 min) |

**Score 0–100** — weighted combination of all signals + volume surge + daily RSI trend.

**Columns:** Price · Signals · % vs PDH · PDH ₹ · % vs VWAP · VWAP ₹ · RSI(D) · Vol Ratio · Score

**Filters:** All · PDH Only · VWAP Only · Score ≥ 70 · F&O Only

**Output:** `intraday_report_NSE.html` → emailed at 9:30 AM IST (not hosted on Pages, changes throughout day)

---

### 6. `send_report_email.py` — Bulk Emailer

Sends reports to any number of subscribers using Gmail SMTP.

**Two delivery modes:**

| Mode | Behaviour | Best for |
|------|-----------|----------|
| `bcc` (default) | Batches of `EMAIL_BCC_BATCH` (default 100) per SMTP call | Large lists — fast |
| `individual` | One SMTP call per recipient, 0.3 s delay | Personalised delivery |

**Environment variables:**

| Variable | Required | Description |
|----------|----------|-------------|
| `GMAIL_USERNAME` | ✅ | Sender Gmail address |
| `GMAIL_APP_PASSWORD` | ✅ | Gmail App Password (16-char, not login password) |
| `EMAIL_BODY_FILE` | optional | HTML file to send (default: `email_summary.html`) |
| `EMAIL_SUBJECT_FILE` | optional | File containing subject line |
| `EMAIL_SEND_MODE` | optional | `bcc` or `individual` (default: `bcc`) |
| `EMAIL_BCC_BATCH` | optional | Recipients per BCC batch (default: `100`) |
| `EMAIL_RECIPIENTS` | optional | Comma-separated override — skips `email_recipients.txt` |
| `EMAIL_DRY_RUN` | optional | `1` = print recipients, do NOT send |

---

### 7. `email_recipients.txt` — Mailing List

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
- **Pause** → prefix with `#`
- **One-off override** → use the `email_list` input when triggering manually from GitHub Actions

---

## 🌐 Setup — GitHub Actions (Automated Reports + Email)

### Step 1 — Enable GitHub Pages

1. Go to **Settings → Pages**
2. Under **Source**, select **GitHub Actions**
3. Click **Save**

### Step 2 — Add required secret

Go to **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
|--------|-------|
| `GMAIL_APP_PASSWORD` | 16-character Gmail App Password from [Google App Passwords](https://myaccount.google.com/apppasswords) |

> You must have **2-Step Verification** enabled on your Google account before App Passwords become available.

### Step 3 — Add subscribers

Edit `email_recipients.txt` — add one address per line, commit and push.

### Step 4 — Verify

1. Go to **Actions → Generate NSE RSI Report**
2. Click **Run workflow**
3. Check the email send steps in the logs

### Workflow triggers

**Morning workflow (`generate-report.yml`) — Mon–Sat 5:00 AM IST:**

| Trigger | When |
|---------|------|
| 📅 Schedule | Every Mon–Sat at **5:00 AM IST** (11:30 PM UTC) |
| 📂 CSV update | Whenever `india/NSE/**` files are pushed |
| 🖱️ Manual | Actions tab → Run workflow |

**Intraday workflow (`intraday-report.yml`) — Mon–Fri 9:30 AM IST:**

| Trigger | When |
|---------|------|
| 📅 Schedule | Every Mon–Fri at **9:30 AM IST** (4:00 AM UTC) |
| 🖱️ Manual | Actions tab → Run workflow |

### Manual workflow inputs (morning workflow)

| Input | Default | Description |
|-------|---------|-------------|
| `max_chart_stocks` | `50` | `0` = generate charts for all stocks |
| `email_list` | — | Override recipients for this run only |
| `send_mode` | `bcc` | `bcc` or `individual` |
| `dry_run` | `false` | `true` = print recipients, skip actual send |

---

## ⚙️ Setup — Local Machine

```bash
git clone https://github.com/dipenshah2006/DailyMomentumStockBreakout.git
cd DailyMomentumStockBreakout
pip install -r requirements.txt

# Run full NSE RSI report (best after 3:35 PM IST on trading days)
python rsi_mtf_report_nse.py

# Run ATH Breakout report
python ath_report.py

# Run Multibagger report
python multibagger_report.py

# Run Rocket Scanner
python rocket_scanner.py

# Run Intraday scanner (during market hours: 9:15 AM – 3:30 PM IST)
python intraday_report.py

# Test email delivery
GMAIL_USERNAME="you@gmail.com" GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx" \
  python send_report_email.py
```

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
- **Intraday scanner:** Run during market hours (9:15 AM – 3:30 PM IST) for live data
- **NSE data:** yfinance uses Yahoo Finance (`.NS` suffix) — no NSE API key needed
- **Rate limiting:** Scripts batch downloads with pauses to avoid blocks
- **Caching:** `rsi_mtf_report_nse.py` caches OHLCV data to `stock_data_cache.pkl` for faster reruns
- **GitHub Actions runtime:** Full 2,000+ stock morning run takes ~45–60 min (within 2-hour cap)
- **Gmail limits:** Regular Gmail ≈ 500 emails/day · Google Workspace ≈ 2,000/day · BCC batch=100 minimises SMTP calls
