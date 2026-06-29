---
name: Full NSE Scanner (multibagger_report.py)
description: Details about the full NSE+SME stock scanner architecture and key design decisions
---

## Architecture

- `multibagger_report.py` scans 2,657 stocks (2,136 NSE Cash EQ + 521 NSE SME)
- CSV sources: `india/NSE/NSECash/EQUITY_L.csv`, `india/NSE/NSESME/MW-SME-05-May-2026.csv`
- Charts saved as PNG files to `charts/multibagger/TICKER_daily/weekly/monthly.png`
- Flask route `/charts/<path:filename>` already in main.py (line 536) serves them
- HTML report at `multibagger_report.html` references chart URLs (NOT base64 embedded)
- Progress cache at `charts/multibagger/scan_cache.json` for resume capability

## Key Design Decisions

- Charts are file-based (not base64) because 2657 stocks × 3 charts × ~250KB = ~2GB if embedded
- ThreadPoolExecutor with 12 workers for parallel yfinance downloads
- `tprint()` uses a Lock to prevent garbled console output from parallel threads

**Why file-based charts:** With 2657 stocks × 3 timeframes each, embedding base64 in HTML would produce a ~2GB file. File-based charts + URL references keep HTML small and charts lazy-loaded.

## New Features Added

1. **Support/Resistance**: `find_support_resistance()` — pivot clustering with tolerance bands
2. **Trend Channel**: `trend_channel()` — linear regression ± 2σ of residuals
3. **Darvas Box**: `darvas_boxes()` — confirm_days=3, returns last 10 boxes per timeframe
4. **Blast Signal**: `detect_blast()` — requires vol_ratio≥2.0, RSI in [52,85], price above EMA21+EMA50
5. **HTML**: Filterable table with BLAST, Darvas D/W/M, S/R pills, modal chart viewer

## Running the Full Scan

Full scan of 2657 stocks takes ~3-4 hours with 12 workers.
Run: `python multibagger_report.py`
Auto-pushes to GitHub at end if GITHUB_TOKEN is set.

## GitHub Push

GITHUB_TOKEN must be set in Replit secrets. The `push_to_github()` function handles git add/commit/push automatically at scan completion.
