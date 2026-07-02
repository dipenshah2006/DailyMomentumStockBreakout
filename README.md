# 📈 DailyMomentumStockBreakout

Automated NSE stock analysis toolkit that generates daily HTML reports and emails them every morning — covering RSI Multi-Timeframe, ATH Breakouts, Multibagger picks, Rocket Scanner, F&O Multi-Indicator Scanner, and Intraday breakouts for Indian equity markets.

---

## 🔗 Live Reports — All URLs

### 🖥️ Replit Dashboard (live server — admin, subscribers, manual run)

The Flask dashboard runs on Replit and provides admin controls not available on GitHub Pages.

| Page | URL |
|------|-----|
| 📊 Dashboard (main RSI report) | https://6c5369b3-24e9-4ba5-910e-569b6fb2817d-00-2jxkdi3uldjd2.sisko.replit.dev/ |
| 📊 F&O Multi-Indicator Scanner | https://6c5369b3-24e9-4ba5-910e-569b6fb2817d-00-2jxkdi3uldjd2.sisko.replit.dev/fo |
| 💎 Multibagger Report | https://6c5369b3-24e9-4ba5-910e-569b6fb2817d-00-2jxkdi3uldjd2.sisko.replit.dev/multibagger |
| 🏆 ATH Breakout | https://6c5369b3-24e9-4ba5-910e-569b6fb2817d-00-2jxkdi3uldjd2.sisko.replit.dev/ath |
| 🚀 Rocket Scanner | https://6c5369b3-24e9-4ba5-910e-569b6fb2817d-00-2jxkdi3uldjd2.sisko.replit.dev/rocket |
| ⚡ Intraday Breakout | https://6c5369b3-24e9-4ba5-910e-569b6fb2817d-00-2jxkdi3uldjd2.sisko.replit.dev/intraday |
| ⚙️ Admin panel | https://6c5369b3-24e9-4ba5-910e-569b6fb2817d-00-2jxkdi3uldjd2.sisko.replit.dev/subscribers |

**Admin panel** (`/subscribers`) requires the `ADMIN_PASSWORD` secret. From there you can:
- View, add, remove, and bulk-import subscribers
- Export the mailing list as CSV
- Trigger a full NSE report run manually
- Send the latest report email to all subscribers (or just yourself for a test preview)

> **Note:** The Replit URL is active while the Replit workflow is running. For always-on access, use the **Publish** button in Replit to get a permanent `.replit.app` deployment URL.

---

### 📡 GitHub Pages (always-on, no login needed)

Updated fresh every day at **5:00 AM IST** via GitHub Actions.

| Report | GitHub Pages URL |
|--------|-----------------|
| 📈 RSI Multi-Timeframe Breakout | https://dipenshah2006.github.io/DailyMomentumStockBreakout/ |
| 📊 F&O Multi-Indicator Scanner | https://dipenshah2006.github.io/DailyMomentumStockBreakout/fo.html |
| 💎 Multibagger | https://dipenshah2006.github.io/DailyMomentumStockBreakout/multibagger.html |
| 🏆 ATH Breakout | https://dipenshah2006.github.io/DailyMomentumStockBreakout/ath.html |
| 🚀 Rocket Scanner | https://dipenshah2006.github.io/DailyMomentumStockBreakout/rocket.html |
| ⚡ Intraday Breakout | https://dipenshah2006.github.io/DailyMomentumStockBreakout/intraday.html |

> **Tip:** GitHub Pages is the fastest way to share reports — no login required, bookmark-friendly, always shows the latest run.

---

## 📅 Automated Schedule

All morning reports run via GitHub Actions at **5:00 AM IST** daily.

| Time (IST) | Report | Ready ~by |
|-----------|--------|-----------|
| 5:00 AM | 📈 RSI MTF Breakout (2657 stocks) | 5:10–5:15 AM |
| 5:00 AM | 🏆 ATH Breakout | 5:15 AM |
| 5:00 AM | 💎 Multibagger | 5:20 AM |
| 5:00 AM | 🚀 Rocket Scanner | 5:25 AM |
| 5:00 AM | 📊 **F&O Scanner** (~212 stocks) | **5:30–5:40 AM** |
| 9:30 AM (Mon–Fri) | ⚡ Intraday Breakout | 9:35 AM |

All reports are emailed to everyone listed in `email_recipients.txt`.

---

## 📁 Repository Structure

```
DailyMomentumStockBreakout/
│
├── rsi_mtf_report_nse.py          # NSE RSI Multi-Timeframe Report (main — 2657 stocks)
├── fo_scanner_report.py           # F&O Multi-Indicator Scanner (~212 stocks, 12 indicators)
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
│       ├── nse_fo_list.csv                # F&O eligible stocks (~212 symbols)
│       └── NIFTY_Indices_Master.xlsx      # Nifty index constituents + sector/industry map
│
├── charts/                        # Generated PNG charts (served via /charts/ route)
│   ├── multibagger/               # Multibagger daily charts
│   └── fo/                        # F&O scanner charts (daily 5-panel + 15-min Fibonacci)
│
└── .github/
    └── workflows/
        ├── generate-report.yml    # Morning reports — runs daily at 5:00 AM IST
        └── intraday-report.yml    # Intraday report — runs Mon–Fri at 9:30 AM IST
```

---

## 🛠️ Scripts Overview

### 1. `rsi_mtf_report_nse.py` — RSI MTF Report (NSE)

Scans the full NSE EQ + SME universe (~2,657 stocks) across **Daily / Weekly / Monthly** timeframes.

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

**Output:** `rsi_mtf_report_NSE.html` → GitHub Pages `index.html`

---

### 2. `fo_scanner_report.py` — F&O Multi-Indicator Scanner 🆕

Scans **~212 NSE F&O stocks** with 12 advanced indicators and generates individual charts.
Reuses the shared `stock_data_cache.pkl` — zero redundant downloads when run after the RSI report.

**Signals & indicators:**
| Indicator | Signal |
|-----------|--------|
| RSI(7) crosses above RSI(34) | ✅ BUY |
| RSI(7) crosses RSI(34) + RSI(200) > 50 | 🔥 STRONG BUY |
| RSI(7) crosses above 70 | ⚡ MOMENTUM |
| MACD(34, 200, 9) bullish crossover | 📈 MACD Cross |
| Chande Kroll Stop (ATR-10, ×1, stop-9) | 🛡️ CK Bullish |
| Volume Oscillator (EMA5−EMA10) zero-cross ↑ | 📊 Vol Osc |
| Bollinger Band (20, 2) upper breakout | 🚀 BB Break |
| Donchian Channel (20-bar) breakout | 💎 Donchian Break |
| Darvas Box top breakout | 🎯 Darvas Break |
| Trend Channel (Linear Regression ±2σ) | 📐 Channel |
| Support / Resistance (pivot clustering) | — overlaid on chart |
| Fibonacci Extension + Retracement (15-min) | 📐 15-min Fib chart |

**Scoring:** Each signal adds points (0–100). Stocks ranked by score.

| Score | Signal Tag |
|-------|-----------|
| ≥ 60 | 🔥 STRONG BUY |
| ≥ 40 | ✅ BUY |
| ≥ 25 | 📈 BULLISH |
| ≥ 10 | 👀 WATCH |
| < 10 | — HOLD |

**Charts generated (top 60 stocks):**
- `charts/fo/<SYM>.png` — 5-panel daily chart (candlestick + BB + Donchian + CK Stop + trend channel + RSI + MACD + Vol Osc + Volume)
- `charts/fo/<SYM>_15m.png` — 15-min chart with Fibonacci Extension (bullish targets) and Retracement (bearish targets)

**Output:** `fo_report.html` → GitHub Pages `fo.html` — available at ~5:30–5:40 AM IST daily

---

### 3. `ath_report.py` — ATH Breakout Report

Scans all NSE EQ stocks and identifies those at or near their **all-time highs**.

**Output:** `ath_report_NSE.html` → GitHub Pages `ath.html`

---

### 4. `multibagger_report.py` — Multibagger Report

Scans for long-term compounders — stocks with consistent growth over 3–5 years.

**Output:** `multibagger_report.html` → GitHub Pages `multibagger.html`

---

### 5. `rocket_scanner.py` — Rocket Scanner

Identifies stocks with explosive momentum: high RSI + volume surge + price breakout alignment.

**Output:** `rocket_scan_latest.html` → GitHub Pages `rocket.html`

---

### 6. `intraday_report.py` — Intraday Breakout Scanner

Runs at **9:30 AM IST** (Mon–Fri). Scans top 500 NSE stocks using live 5-minute bars.

**Signals:** PDH Breakout 🟢 · VWAP Breakout 🔵 · ORH Breakout 🟡

**Output:** `intraday_report_NSE.html` → emailed at 9:30 AM IST

---

### 7. `send_report_email.py` — Bulk Emailer

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

### 8. `email_recipients.txt` — Mailing List

One email address per line. Lines starting with `#` and blank lines are ignored.

```
# NSE Report mailing list
dipenshah2006@gmail.com
tradewithtrenddirection@gmail.com
# another@example.com   ← commented out / paused
```

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

**Morning workflow (`generate-report.yml`) — daily 5:00 AM IST:**

| Trigger | When |
|---------|------|
| 📅 Schedule | Every day at **5:00 AM IST** (11:30 PM UTC) |
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

# Run F&O Multi-Indicator Scanner
python fo_scanner_report.py

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
- **Shared cache:** `stock_data_cache.pkl` is reused by RSI, Multibagger, and F&O scanner — only one download per day
- **GitHub Actions runtime:** Full morning run (all 5 reports) takes ~35–45 min once cache is warm
- **Gmail limits:** Regular Gmail ≈ 500 emails/day · Google Workspace ≈ 2,000/day · BCC batch=100 minimises SMTP calls
