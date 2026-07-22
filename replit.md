# DailyMomentumStockBreakout

Automated NSE stock analysis toolkit. Generates daily HTML reports covering RSI Multi-Timeframe, ATH Breakouts, Multibagger picks, Rocket Scanner, F&O Multi-Indicator Scanner, and Intraday breakouts for Indian equity markets. Emails reports to subscribers on a daily schedule.

## How to run

```
python main.py
```

Flask dashboard serves on port 5000. The scheduler auto-triggers the NSE RSI MTF report every day at **5:00 AM IST**. If no report exists on startup, it generates one immediately.

## Dashboard routes

| Route | Description |
|---|---|
| `/` | Main RSI MTF report (or generation status) |
| `/fo` | F&O Multi-Indicator Scanner |
| `/multibagger` | Multibagger Report |
| `/ath` | ATH Breakout |
| `/rocket` | Rocket Scanner |
| `/intraday` | Intraday Breakout |
| `/subscribers` | Admin panel (requires `ADMIN_PASSWORD`) |

## Secrets / environment variables

| Key | Required | Purpose |
|---|---|---|
| `SESSION_SECRET` | Yes | Flask session signing |
| `ADMIN_PASSWORD` | Recommended | Protects `/subscribers` admin panel |
| `GMAIL_USERNAME` | For email | Gmail address to send reports from |
| `GMAIL_APP_PASSWORD` | For email | Gmail App Password (not account password) |
| `GITHUB_TOKEN` | Optional | Auto-pushes subscriber list changes to GitHub |

## Running individual scanners manually

```bash
python rsi_mtf_report_nse.py     # Full NSE RSI report (best after 3:35 PM IST)
python fo_scanner_report.py      # F&O Multi-Indicator Scanner
python ath_report.py             # ATH Breakout report
python multibagger_report.py     # Multibagger report
python rocket_scanner.py         # Rocket Scanner
python intraday_report.py        # Intraday scanner (run during market hours)
```

## Notes

- Reports are cached as HTML files in the project root
- Stock data is cached in `stock_data_cache.pkl` — shared by RSI, Multibagger, and F&O scanner
- Full morning run (all reports) takes ~35–45 min once cache is warm
- Rate limiting: scripts batch downloads with pauses to avoid Yahoo Finance blocks

## User preferences

- GITHUB_TOKEN configured for auto-pushing subscriber list changes
