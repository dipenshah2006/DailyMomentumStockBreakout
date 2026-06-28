"""
╔═════════════════════════════════════════════════════════════════════════════╗
║   RSI MULTI-TIMEFRAME BREAKOUT HTML REPORT  v2.0                            ║
║   Daily · Weekly · Monthly RSI/SMA Crossover | Phase | Entry/Exit           ║
║   NEW v2.0:                                                                  ║
║    • Exception logging → error_log.txt  (ticker + company + full traceback) ║
║    • Ranking vs Nifty50  (relative strength percentile)                     ║
║    • Ranking vs all NSE stocks  (universe percentile)                       ║
║    • Lightweight HTML — charts lazy-loaded on expand, never hangs browser   ║
║    • Native <details> expand/collapse — no JS needed, instant               ║
╚═════════════════════════════════════════════════════════════════════════════╝

INSTALL:  pip install yfinance pandas numpy matplotlib requests openpyxl
RUN:      python rsi_mtf_report_v2.py
OUTPUTS:  rsi_mtf_report_YYYYMMDD_HHMM.html  +  error_log_YYYYMMDD_HHMM.txt
"""

# ═════════════════════════════════════════════════════════════════════════════
# USER CONFIG
# ═════════════════════════════════════════════════════════════════════════════

LOCAL_BSE_CSV       = "india/BSE/BSEcash/BSE_EQ_SCRIP_02012025_1780023206863.csv"
BSE_CSV_URL         = "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w?Group=&Scripcode=&industry=&segment=Equity&status=Active"
BSE_SERIES_FILTER   = ["EQ"]       # BSE equity series (EQ = cash equities)

DATA_PERIOD         = "max"
MIN_CANDLES         = 80
MAX_CHART_STOCKS    = 0         # 0 = generate charts for all stocks; otherwise top N stocks
CHART_OUTPUT_DIR    = "charts"   # folder for generated PNG chart files
CHART_BARS          = 120       # bars per chart (fewer = smaller PNG)
CHART_DPI           = 72        # lower DPI = smaller file, still readable

FRESH_DAYS_D        = 3
FRESH_WEEKS_W       = 2

RSI_P               = 14
RSI_SMA_P           = 14
CCI_P               = 20
MACD_F, MACD_S, MACD_SIG_P = 12, 26, 9
ATR_P               = 14

BATCH_SIZE          = 25
BATCH_PAUSE         = 1.0

# HTML report pagination constants
PAGE_TBL            = 200   # table rows per page
PAGE_CARDS          = 50    # cards per "Load More" batch

SCORE_STRONG_BUY    = 16
SCORE_BUY           = 12
SCORE_WATCH         = 8

# Nifty 50 tickers (used for ranking vs index)
NIFTY50 = [
    "RELIANCE","TCS","HDFCBANK","INFY","ICICIBANK","HINDUNILVR","ITC","SBIN",
    "BAJFINANCE","BHARTIARTL","KOTAKBANK","LT","AXISBANK","ASIANPAINT","MARUTI",
    "SUNPHARMA","TITAN","WIPRO","ULTRACEMCO","NTPC","POWERGRID","ONGC","JSWSTEEL",
    "TATASTEEL","COALINDIA","TECHM","HCLTECH","DRREDDY","CIPLA","DIVISLAB",
    "ADANIENT","ADANIPORTS","BAJAJ-AUTO","EICHERMOT","HEROMOTOCO","NESTLEIND",
    "BRITANNIA","TATACONSUM","TATAMOTORS","M&M","HINDALCO","GRASIM","JSWSTEEL",
    "APOLLOHOSP","BPCL","INDUSINDBK","LTIM","HDFCLIFE","SBILIFE","COALINDIA",
]

# ═════════════════════════════════════════════════════════════════════════════
# AUTO-INSTALL MISSING LIBRARIES
# ═════════════════════════════════════════════════════════════════════════════

import sys
import subprocess

def install_missing_packages():
    required = {
        'yfinance': 'yfinance',
        'pandas': 'pandas',
        'numpy': 'numpy',
        'matplotlib': 'matplotlib',
        'requests': 'requests',
        'openpyxl': 'openpyxl',
    }

    missing = []
    for pkg_name, import_name in required.items():
        try:
            __import__(import_name)
        except ImportError:
            missing.append(pkg_name)

    if missing:
        print(f"Installing missing packages: {', '.join(missing)}")
        subprocess.check_call([sys.executable, "-m", "pip", "install"] + missing)
        print("✓ Packages installed successfully\n")

if __name__ == "__main__":
    install_missing_packages()

# ═════════════════════════════════════════════════════════════════════════════
# IMPORTS
# ═════════════════════════════════════════════════════════════════════════════

import csv
import hashlib
import io
import json
import logging
import os
import pickle
import sys
import time
import traceback
import warnings
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime

import argparse
import matplotlib
matplotlib.use("Agg")
import matplotlib.gridspec as gridspec
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests
import yfinance as yf

warnings.filterwarnings("ignore")

CHART_WORKERS = min(8, max(1, (os.cpu_count() or 4) - 1))

RUN_TS      = datetime.now().strftime("%d %b %Y  %H:%M")
_STAMP      = datetime.now().strftime("%d%m%Y_%H%M")
START_TS    = RUN_TS
START_TIME  = time.time()
OUTPUT_HTML = f"rsi_mtf_report_{_STAMP}.html"
ERROR_LOG   = f"error_log_{_STAMP}.txt"
CACHE_FILE  = "stock_data_cache.pkl"
CHART_CACHE_META = os.path.join(CHART_OUTPUT_DIR, "chart_cache.json")

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 0 — ERROR LOGGER
# ═════════════════════════════════════════════════════════════════════════════

# Configure a dedicated file logger — separate from print output
_logger = logging.getLogger("rsi_scanner")
_logger.setLevel(logging.DEBUG)
_fh = logging.FileHandler(ERROR_LOG, encoding="utf-8")
_fh.setFormatter(logging.Formatter(
    "%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
))
_logger.addHandler(_fh)

def log_error(ticker: str, company: str, stage: str, exc: Exception):
    """
    Log a structured error entry to ERROR_LOG with:
    - Timestamp
    - Ticker symbol
    - Company name
    - Stage where it failed (download / indicators / chart / html)
    - Full traceback
    """
    tb = traceback.format_exc()
    _logger.error(
        f"TICKER='{ticker}.BO' | COMPANY={company!r:35s} | STAGE={stage}\n"
        f"  ERROR : {type(exc).__name__}: {exc}\n"
        f"  TRACE :\n{tb}"
    )

def log_info(msg: str):
    _logger.info(msg)

def log_warn(ticker: str, company: str, msg: str):
    _logger.warning(f"TICKER='{ticker}.BO' | COMPANY={company!r:35s} | {msg}")


def format_timespan(seconds: float) -> str:
    mins, secs = divmod(int(seconds), 60)
    hours, mins = divmod(mins, 60)
    if hours:
        return f"{hours}h {mins}m {secs}s"
    if mins:
        return f"{mins}m {secs}s"
    return f"{secs}s"


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 1 — COMPANY NAME LOOKUP
# ═════════════════════════════════════════════════════════════════════════════

_COMPANY_MAP: dict[str, str] = {}   # populated by load_universe()
_LISTING_DATE_MAP: dict[str, str] = {}   # symbol → listing date string
_SME_STOCKS: set[str] = set()   # set of SME stock symbols
_SECTOR_MAP: dict[str, str] = {}   # symbol → sector/industry name
_INDEX_MAP: dict[str, set[str]] = {}   # index name → set of symbols in that index
_MARKETCAP_MAP: dict[str, float] = {}   # symbol → market cap in rupees

def get_company_name(ticker: str) -> str:
    return _COMPANY_MAP.get(ticker, ticker)

def is_sme_stock(ticker: str) -> bool:
    return ticker in _SME_STOCKS

def get_sector(ticker: str) -> str | None:
    return _SECTOR_MAP.get(ticker)

def get_indices(ticker: str) -> list[str]:
    """Get list of indices this stock belongs to."""
    result = []
    for idx_name, symbols in _INDEX_MAP.items():
        if ticker in symbols:
            result.append(idx_name)
    return result

# Note: get_marketcap() is defined in Section 2 (cache) with TTL-aware logic

def categorize_marketcap(marketcap: float | None) -> tuple[str, str]:
    """Categorize stock by market cap. Returns (category, css_class).
    Market cap in INR:
    - Large Cap: > 20,000 crore
    - Mid Cap: 5,000 - 20,000 crore
    - Small Cap: 500 - 5,000 crore
    - Micro Cap: < 500 crore
    """
    if marketcap is None:
        return "Unknown", "cap-unknown"
    cap_crore = marketcap / 1e7  # Convert to crores
    if cap_crore > 200000:       # > 20,000 cr
        return "Large Cap", "cap-large"
    elif cap_crore > 50000:      # 5,000-20,000 cr
        return "Mid Cap", "cap-mid"
    elif cap_crore > 5000:       # 500-5,000 cr
        return "Small Cap", "cap-small"
    else:                        # < 500 cr
        return "Micro Cap", "cap-micro"

def get_listing_date(ticker: str) -> str | None:
    return _LISTING_DATE_MAP.get(ticker)

def get_min_candles_required(ticker: str) -> int:
    """Get minimum candles required based on stock listing date.
    
    For stocks listed within 90 days: require at least 80% of trading days since listing
    For older stocks: require MIN_CANDLES (80)
    Minimum requirement: 20 candles
    """
    date_str = get_listing_date(ticker)
    if not date_str:
        return MIN_CANDLES
    
    try:
        from datetime import datetime
        listing_date = datetime.strptime(date_str, "%d-%b-%Y")
        today = datetime.now()
        days_since_listing = (today - listing_date).days
        
        # If listed within 90 days, require 80% of trading days (assuming ~5 trading days/week)
        if days_since_listing <= 90:
            trading_days_estimate = days_since_listing * 5 // 7
            required = max(20, int(trading_days_estimate * 0.8))
            return min(required, MIN_CANDLES)
        else:
            return MIN_CANDLES
    except (ValueError, TypeError):
        return MIN_CANDLES

def _build_company_map(text: str):
    """Parse NSE CSV and build symbol → company name and listing date dicts."""
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        sym  = row.get("SYMBOL", "").strip()
        name = row.get("NAME OF COMPANY", row.get("COMPANY NAME", "")).strip()
        # Handle column names with leading spaces
        date_str = row.get("DATE OF LISTING", row.get(" DATE OF LISTING", "")).strip()
        sector = row.get("INDUSTRY", row.get(" INDUSTRY", "")).strip()
        if sym:
            _COMPANY_MAP[sym] = name or sym
            if date_str:
                _LISTING_DATE_MAP[sym] = date_str
            if sector:
                _SECTOR_MAP[sym] = sector


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 2 — SMART CACHE + INCREMENTAL DOWNLOAD ENGINE
# ═════════════════════════════════════════════════════════════════════════════
#
#  Cache layout (v2):
#  {
#    "__version__": 2,
#    "TICKER": {
#        "df":         pd.DataFrame,   # full OHLCV history, daily
#        "last_date":  "YYYY-MM-DD",   # most recent bar date
#        "marketcap":  float | None,   # latest known market cap (INR)
#        "mcap_ts":    float,          # unix timestamp when mcap was cached
#    },
#    ...
#  }
#
#  On every run:
#   • Cached + fresh (last_date >= yesterday): used as-is → 0 downloads
#   • Cached + stale (last_date < yesterday): batch-download last 60d, append
#   • New (never seen): batch-download "max"
#   • Market cap: re-fetched only if missing or >MCAP_TTL_DAYS old
#
# ═════════════════════════════════════════════════════════════════════════════

import concurrent.futures as _cf
from datetime import date as _date, timedelta as _td

MCAP_TTL_DAYS  = 7          # refresh market cap if older than this many days
DL_BATCH_SIZE  = 50         # tickers per yf.download() batch call
DL_MAX_WORKERS = 4          # parallel threads for batch downloads
CACHE_SAVE_INT = 200        # save cache to disk every N tickers processed
STALE_BUCKET_DAYS = 7       # group stale tickers whose missing ranges are close

_CACHE: dict = {}           # module-level, loaded once by _load_cache_v2()
_CACHE_DIRTY  = False       # set True whenever _CACHE is modified

# ── Helpers ────────────────────────────────────────────────────────────────

def _today_str() -> str:
    return _date.today().isoformat()            # "YYYY-MM-DD"


def _last_trading_day_str() -> str:
    today = _date.today()
    if today.weekday() == 0:      # Monday → last trading day was Friday
        return (today - _td(days=3)).isoformat()
    if today.weekday() == 6:      # Sunday → last trading day was Friday
        return (today - _td(days=2)).isoformat()
    if today.weekday() == 5:      # Saturday → last trading day was Friday
        return (today - _td(days=1)).isoformat()
    return (today - _td(days=1)).isoformat()


def _is_fresh(entry: dict) -> bool:
    """True if cached data already has the most recent likely market bar."""
    last = entry.get("last_date", "")
    return last >= _last_trading_day_str()

# ── Load / save ────────────────────────────────────────────────────────────

def _load_cache_v2() -> dict:
    """Load cache, migrating old v1 format (plain dict of DataFrames) if needed."""
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, "rb") as fh:
            raw = pickle.load(fh)
    except Exception as e:
        print(f"  [!] Cache load error: {e} — starting fresh")
        return {}

    # ── Migrate old format (dict of DataFrames, no __version__) ──────────────
    if isinstance(raw, dict) and raw.get("__version__") != 2:
        migrated = {"__version__": 2}
        migrated_count = 0
        for k, v in raw.items():
            if k.startswith("__"):
                continue
            if isinstance(v, pd.DataFrame) and not v.empty:
                last = v.index[-1].date().isoformat() if len(v) else ""
                migrated[k] = {"df": v, "last_date": last,
                                "marketcap": None, "mcap_ts": 0.0}
                migrated_count += 1
        if migrated_count:
            print(f"  🔄 Migrated {migrated_count} cached stocks from old cache format → v2")
        return migrated

    return raw

def _save_cache_v2():
    """Write current _CACHE to disk."""
    global _CACHE_DIRTY
    try:
        with open(CACHE_FILE, "wb") as fh:
            pickle.dump(_CACHE, fh, protocol=pickle.HIGHEST_PROTOCOL)
        _CACHE_DIRTY = False
    except Exception as e:
        print(f"  [!] Cache save error: {e}")

def _maybe_save_cache():
    """Save only if dirty."""
    if _CACHE_DIRTY:
        _save_cache_v2()

# ── In-memory accessors ────────────────────────────────────────────────────

def _get_df(ticker: str) -> pd.DataFrame | None:
    entry = _CACHE.get(ticker)
    if entry and isinstance(entry, dict):
        return entry.get("df")
    return None

def _set_df(ticker: str, df: pd.DataFrame,
            marketcap: float | None = None, mcap_ts: float | None = None):
    """Update in-memory cache only. Caller must trigger _maybe_save_cache()."""
    global _CACHE_DIRTY
    entry = _CACHE.setdefault(ticker, {})
    entry["df"]        = df
    entry["last_date"] = df.index[-1].date().isoformat() if len(df) else ""
    if marketcap is not None:
        entry["marketcap"] = marketcap
        entry["mcap_ts"]   = mcap_ts if mcap_ts is not None else time.time()
    _CACHE_DIRTY = True

def get_marketcap(ticker: str) -> float | None:
    entry = _CACHE.get(ticker)
    if isinstance(entry, dict):
        mc  = entry.get("marketcap")
        ts  = entry.get("mcap_ts", 0.0)
        age = (time.time() - ts) / 86400
        if mc and age < MCAP_TTL_DAYS:
            return mc
    return _MARKETCAP_MAP.get(ticker)

# ── DataFrame merger ────────────────────────────────────────────────────────

def _merge_df(old_df: pd.DataFrame, new_df: pd.DataFrame) -> pd.DataFrame:
    """Append new bars to old_df, deduplicate by date index, return sorted."""
    if new_df is None or new_df.empty:
        return old_df
    if old_df is None or old_df.empty:
        return new_df
    combined = pd.concat([old_df, new_df])
    combined = combined[~combined.index.duplicated(keep="last")]
    return combined.sort_index()

# ── Single-ticker clean download ────────────────────────────────────────────

def _clean_df(raw) -> pd.DataFrame:
    """Flatten MultiIndex columns and drop NaN rows."""
    if raw is None or raw.empty:
        return pd.DataFrame()
    df = raw.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.droplevel(1)
    return df.dropna(subset=["Close"])

# ── Batch download ──────────────────────────────────────────────────────────

def _bucket_start_date(dt: _date, bucket_days: int = STALE_BUCKET_DAYS) -> str:
    epoch = _date(1970, 1, 1)
    offset = ((dt - epoch).days // bucket_days) * bucket_days
    return (epoch + _td(days=offset)).isoformat()


def _batch_download(tickers: list[str], period: str | None = None,
                    start: str | None = None, end: str | None = None) -> dict[str, pd.DataFrame]:
    """
    Download multiple tickers in one yf.download() call.
    Returns {ticker: clean_DataFrame}. Missing/failed tickers are omitted.

    Use either `period` or `start`/`end` to control the range.
    """
    if not tickers:
        return {}
    results = {}
    args = {
        "tickers": [t + ".BO" for t in tickers],
        "interval": "1d",
        "progress": False,
        "auto_adjust": True,
        "group_by": "ticker",
        "threads": True,
    }
    if start and end:
        args["start"] = start
        args["end"] = end
    elif period:
        args["period"] = period
    else:
        args["period"] = DATA_PERIOD

    try:
        raw = yf.download(**args)
    except Exception as e:
        label = f"{period or f'{start}:{end}'}"
        print(f"\n  [!] Batch download error ({len(tickers)} tickers, {label}): {e}")
        return {}

    if raw.empty:
        return {}

    for ticker in tickers:
        ns = ticker + ".BO"
        try:
            if isinstance(raw.columns, pd.MultiIndex):
                if ns in raw.columns.get_level_values(0):
                    df = raw[ns].copy().dropna(subset=["Close"])
                elif ticker in raw.columns.get_level_values(0):
                    df = raw[ticker].copy().dropna(subset=["Close"])
                else:
                    continue
            else:
                # Only one ticker returned → flat columns
                df = raw.copy().dropna(subset=["Close"])
            if not df.empty:
                results[ticker] = df
        except Exception:
            continue

    return results

# ── Market cap batch fetch ──────────────────────────────────────────────────

def _fetch_mcap_batch(tickers: list[str]) -> dict[str, float]:
    """Fetch market cap for multiple tickers using threads. Returns {ticker: mcap}."""
    results = {}

    def _fetch_one(t):
        try:
            info = yf.Ticker(t + ".NS").fast_info
            mc   = getattr(info, "market_cap", None)
            if mc and mc > 0:
                return t, float(mc)
        except Exception:
            pass
        return t, None

    with _cf.ThreadPoolExecutor(max_workers=DL_MAX_WORKERS) as exe:
        for ticker, mc in exe.map(_fetch_one, tickers):
            if mc:
                results[ticker] = mc
    return results

# ── Main pre-fetch entry point (called from main() before analyze loop) ────

def prefetch_all(tickers: list[str]) -> dict[str, int]:
    """
    Smart incremental download for the full universe.

    Returns a stats dict: {fresh, stale_updated, new_downloaded, failed}

    After this function:
      - Every reachable ticker has up-to-date data in _CACHE
      - _CACHE may be dirty; caller should call _maybe_save_cache() when done
    """
    global _CACHE_DIRTY

    today = _today_str()

    fresh_tickers   = []   # cache data already has last trading day → skip
    stale_tickers   = []   # in cache but older than last trading day
    new_tickers     = []   # not in cache at all → fetch max

    for t in tickers:
        entry = _CACHE.get(t)
        if isinstance(entry, dict) and isinstance(entry.get("df"), pd.DataFrame):
            if _is_fresh(entry):
                fresh_tickers.append(t)
            else:
                stale_tickers.append(t)
        else:
            new_tickers.append(t)

    total = len(tickers)
    print(f"\n  📦 Cache status:  "
          f"✅ fresh={len(fresh_tickers)}  "
          f"🔄 stale={len(stale_tickers)}  "
          f"🆕 new={len(new_tickers)}  "
          f"(total={total})")

    stats = {"fresh": len(fresh_tickers), "stale_updated": 0,
             "new_downloaded": 0, "failed": 0}

    # ── Update stale tickers (fetch last 60d and append) ────────────────────
    if stale_tickers:
        print(f"  🔄 Updating {len(stale_tickers)} stale tickers (download only missing dates)…")

        groups: dict[str, list[str]] = {}
        for ticker in stale_tickers:
            entry = _CACHE.get(ticker, {})
            last_date = entry.get("last_date")
            if not last_date:
                start_dt = _date.today() - _td(days=30)
            else:
                start_dt = _date.fromisoformat(last_date) + _td(days=1)
            if start_dt >= _date.today():
                groups.setdefault("SKIP", []).append(ticker)
                continue
            bucket_key = _bucket_start_date(start_dt)
            groups.setdefault(bucket_key, []).append(ticker)

        group_items = sorted((k, v) for k, v in groups.items() if k != "SKIP")
        processed = 0
        total_groups = len(group_items)
        for group_index, (bucket_start, group) in enumerate(group_items, start=1):
            start_date = bucket_start
            end_date = (_date.today() + _td(days=1)).isoformat()
            print(f"  • Bucket {group_index}/{total_groups}: start={start_date} tickers={len(group)}")

            for batch_start in range(0, len(group), DL_BATCH_SIZE):
                batch = group[batch_start: batch_start + DL_BATCH_SIZE]
                pct = min(100, (processed + len(batch)) / len(stale_tickers) * 100)
                sys.stdout.write(
                    f"\r    Stale [{pct:5.1f}%]  batch {processed//DL_BATCH_SIZE+1}"
                    f"/{-(-len(stale_tickers)//DL_BATCH_SIZE)}  "
                    f"({processed+len(batch)}/{len(stale_tickers)})"
                )
                sys.stdout.flush()

                new_data = _batch_download(batch, start=start_date, end=end_date)
                for ticker in batch:
                    if ticker not in new_data:
                        stats["failed"] += 1
                        continue
                    old_df = _get_df(ticker)
                    if old_df is None:
                        old_df = pd.DataFrame()
                    merged  = _merge_df(old_df, new_data[ticker])
                    if len(merged) >= MIN_CANDLES:
                        _set_df(ticker, merged)
                        stats["stale_updated"] += 1
                    else:
                        stats["failed"] += 1

                processed += len(batch)
                if _CACHE_DIRTY and (processed) % CACHE_SAVE_INT == 0:
                    _save_cache_v2()
        if "SKIP" in groups:
            for ticker in groups["SKIP"]:
                print(f"    Stale [SKIP] {ticker} already up-to-date")
        print()

    # ── Download new tickers (full history) ─────────────────────────────────
    if new_tickers:
        print(f"  🆕 Downloading {len(new_tickers)} new tickers (full history)…")
        for batch_start in range(0, len(new_tickers), DL_BATCH_SIZE):
            batch = new_tickers[batch_start: batch_start + DL_BATCH_SIZE]
            pct   = min(100, (batch_start + len(batch)) / len(new_tickers) * 100)
            sys.stdout.write(f"\r    New   [{pct:5.1f}%]  batch {batch_start//DL_BATCH_SIZE+1}"
                             f"/{-(-len(new_tickers)//DL_BATCH_SIZE)}  "
                             f"({batch_start+len(batch)}/{len(new_tickers)})")
            sys.stdout.flush()

            new_data = _batch_download(batch, period=DATA_PERIOD)
            for ticker in batch:
                if ticker not in new_data:
                    stats["failed"] += 1
                    continue
                df = new_data[ticker]
                if len(df) >= MIN_CANDLES:
                    _set_df(ticker, df)
                    stats["new_downloaded"] += 1
                else:
                    stats["failed"] += 1

            if _CACHE_DIRTY and (batch_start + DL_BATCH_SIZE) % CACHE_SAVE_INT == 0:
                _save_cache_v2()
        print()

    # ── Market cap refresh (batch, threaded) ────────────────────────────────
    mcap_stale = [
        t for t in tickers
        if t in _CACHE and isinstance(_CACHE[t], dict)
        and (not _CACHE[t].get("marketcap")
             or (time.time() - _CACHE[t].get("mcap_ts", 0)) / 86400 > MCAP_TTL_DAYS)
    ]
    if mcap_stale:
        print(f"  💰 Refreshing market cap for {len(mcap_stale)} tickers… ", end="", flush=True)
        mcap_map = _fetch_mcap_batch(mcap_stale)
        now = time.time()
        for t, mc in mcap_map.items():
            if t in _CACHE and isinstance(_CACHE[t], dict):
                _CACHE[t]["marketcap"] = mc
                _CACHE[t]["mcap_ts"]   = now
                _MARKETCAP_MAP[t]      = mc
        _CACHE_DIRTY = True
        print(f"updated {len(mcap_map)}/{len(mcap_stale)}")

    # ── Final save ───────────────────────────────────────────────────────────
    _maybe_save_cache()

    cached_total = sum(1 for v in _CACHE.values()
                       if isinstance(v, dict) and isinstance(v.get("df"), pd.DataFrame))
    print(f"\n  ✅ Cache ready: {cached_total} tickers on disk  |  "
          f"updated {stats['stale_updated']+stats['new_downloaded']}  |  "
          f"failed {stats['failed']}\n")
    return stats


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 3 — INDICATORS
# ═════════════════════════════════════════════════════════════════════════════

def calc_rsi(close, period=14):
    delta = close.diff()
    gain  = delta.clip(lower=0).ewm(com=period - 1, min_periods=period).mean()
    loss  = (-delta).clip(lower=0).ewm(com=period - 1, min_periods=period).mean()
    return 100 - (100 / (1 + gain / (loss + 1e-10)))

def calc_macd(close, fast=12, slow=26, sig=9):
    line   = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    signal = line.ewm(span=sig, adjust=False).mean()
    return line, signal, line - signal

def calc_cci(high, low, close, period=20):
    tp  = (high + low + close) / 3
    sma = tp.rolling(period).mean()
    mad = tp.rolling(period).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    return (tp - sma) / (0.015 * mad + 1e-10)

def calc_atr(high, low, close, period=14):
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()

def resample_ohlcv(df, rule):
    return df.resample(rule).agg({
        "Open": "first", "High": "max", "Low": "min",
        "Close": "last", "Volume": "sum",
    }).dropna(subset=["Close"])


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 4 — SWING + FIBONACCI
# ═════════════════════════════════════════════════════════════════════════════

def find_swing_points(high_s, low_s, lookback=252, order=5):
    h = high_s.iloc[-lookback:] if len(high_s) >= lookback else high_s
    l = low_s.iloc[-lookback:]  if len(low_s)  >= lookback else low_s
    pivot_highs, pivot_lows = [], []
    for i in range(order, len(h) - order):
        if h.iloc[i] == h.iloc[i - order: i + order + 1].max():
            pivot_highs.append((h.index[i], float(h.iloc[i]), i))
        if l.iloc[i] == l.iloc[i - order: i + order + 1].min():
            pivot_lows.append((l.index[i], float(l.iloc[i]), i))
    if not pivot_highs or not pivot_lows:
        phi = h.idxmax(); plo = l.idxmin()
        return float(l.loc[plo]), plo, float(h.loc[phi]), phi
    sh_dt, sh_val, _ = max(pivot_highs, key=lambda x: x[2])
    sl_dt, sl_val, _ = max(pivot_lows,  key=lambda x: x[2])
    return sl_val, sl_dt, sh_val, sh_dt

def fib_extensions(swing_low, swing_high):
    rng = swing_high - swing_low
    return {
        "127.2%": round(swing_high + rng * 0.272, 2),
        "161.8%": round(swing_high + rng * 0.618, 2),
        "200.0%": round(swing_high + rng * 1.000, 2),
        "261.8%": round(swing_high + rng * 1.618, 2),
        "423.6%": round(swing_high + rng * 3.236, 2),
    }

def fib_retracements(swing_high, swing_low):
    rng = swing_high - swing_low
    return {
        "23.6%": round(swing_high - rng * 0.236, 2),
        "38.2%": round(swing_high - rng * 0.382, 2),
        "50.0%": round(swing_high - rng * 0.500, 2),
        "61.8%": round(swing_high - rng * 0.618, 2),
        "78.6%": round(swing_high - rng * 0.786, 2),
    }


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 5 — PHASE + SCORING
# ═════════════════════════════════════════════════════════════════════════════

def detect_phase(rsi_d, rsi_w, rsi_m, macd_line, macd_signal, score):
    bulls = sum([rsi_d>55, rsi_w>52, rsi_m>50, macd_line>macd_signal, macd_line>0])
    bears = sum([rsi_d<45, rsi_w<48, rsi_m<50, macd_line<macd_signal, macd_line<0])
    if bulls >= 4 or (score >= SCORE_BUY and rsi_d > 50):  return "UPTREND"
    if bears >= 4 or (score <= 5 and rsi_d < 45):           return "BEARISH"
    return "SIDEWAYS"

def compute_score(rsi_d, rsi_d_sma, rsi_w, rsi_w_sma, rsi_m, rsi_m_sma,
                  macd_line, macd_sig, cci, fresh_d, fresh_w):
    score, sigs = 0, []
    if rsi_m > rsi_m_sma: score += 4; sigs.append("M-RSI>SMA ✅")
    if rsi_w > rsi_w_sma: score += 3; sigs.append("W-RSI>SMA ✅")
    if rsi_d > rsi_d_sma: score += 2; sigs.append("D-RSI>SMA ✅")
    if fresh_d:            score += 3; sigs.append("FRESH Daily 🚀")
    if fresh_w:            score += 2; sigs.append("FRESH Weekly 🔥")
    if macd_line > macd_sig: score += 2; sigs.append("MACD>Sig ✅")
    if macd_line > 0:        score += 1; sigs.append("MACD>0")
    if cci > 100:   score += 2; sigs.append("CCI>100 💪")
    elif cci > 0:   score += 1; sigs.append("CCI>0")
    if rsi_d > 60:  score += 1; sigs.append("D-RSI>60 🔥")
    if rsi_w > 55:  score += 1; sigs.append("W-RSI>55 💪")
    return score, sigs

def signal_label(score, phase, fresh_d, fresh_w, rsi_d, rsi_w, rsi_m,
                 rsi_d_sma, rsi_w_sma, rsi_m_sma):
    triple = rsi_d > rsi_d_sma and rsi_w > rsi_w_sma and rsi_m > rsi_m_sma
    if score >= SCORE_STRONG_BUY and triple and (fresh_d or fresh_w):
        return "STRONG BUY 🚀", "sig-strong-buy"
    if score >= SCORE_BUY and rsi_d > rsi_d_sma and rsi_w > rsi_w_sma:
        return "BUY ✅", "sig-buy"
    if score >= SCORE_WATCH:
        return "WATCH 👀", "sig-watch"
    if phase == "BEARISH":
        return "AVOID ❌", "sig-avoid"
    return "NEUTRAL", "sig-neutral"


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 6 — RANKING ENGINE
# ═════════════════════════════════════════════════════════════════════════════

def compute_rankings(results: list[dict]) -> list[dict]:
    """
    Add two ranking fields to every result dict:

    rank_nifty50    — percentile (0-100) of this stock's score vs the
                      subset of Nifty50 stocks that were successfully analysed.
                      100 = best among Nifty50, 0 = worst.

    rank_universe   — percentile (0-100) of this stock's score vs ALL
                      scanned stocks.  100 = top 1% of entire universe.

    Also adds:
    rank_nifty50_pos  — integer rank  (1 = best Nifty50 stock)
    rank_nifty50_of   — total Nifty50 stocks in scan
    rank_univ_pos     — integer rank in full universe
    rank_univ_of      — total stocks in universe
    """
    n50_set = set(NIFTY50)

    # ── All scores ─────────────────────────────────────────────
    all_scores  = [d["score"] for d in results]
    n50_results = [d for d in results if d["ticker"] in n50_set]
    n50_scores  = [d["score"] for d in n50_results]

    def pct_rank(score, score_list):
        """Percentile rank: what % of scores are ≤ this score."""
        if not score_list:
            return 0
        below = sum(1 for s in score_list if s <= score)
        return round(below / len(score_list) * 100, 1)

    # Sort for integer rank (1 = highest score)
    sorted_all = sorted(results, key=lambda d: d["score"], reverse=True)
    sorted_n50 = sorted(n50_results,  key=lambda d: d["score"], reverse=True)

    rank_all_map = {d["ticker"]: i + 1 for i, d in enumerate(sorted_all)}
    rank_n50_map = {d["ticker"]: i + 1 for i, d in enumerate(sorted_n50)}

    for d in results:
        d["rank_nifty50"]     = pct_rank(d["score"], n50_scores)
        d["rank_universe"]    = pct_rank(d["score"], all_scores)
        d["rank_nifty50_pos"] = rank_n50_map.get(d["ticker"], 0)
        d["rank_nifty50_of"]  = len(n50_results)
        d["rank_univ_pos"]    = rank_all_map.get(d["ticker"], 0)
        d["rank_univ_of"]     = len(results)
        d["is_nifty50"]       = d["ticker"] in n50_set

    return results


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 7 — HISTORICAL SIGNALS
# ═════════════════════════════════════════════════════════════════════════════

def historical_signals(close, rsi_series, rsi_sma_series, max_signals=12):
    results = []
    rsi_arr = rsi_series.values
    sma_arr = rsi_sma_series.values
    cls_arr = close.values
    dates   = close.index
    for i in range(1, len(rsi_arr)):
        if np.isnan(rsi_arr[i]) or np.isnan(sma_arr[i]):
            continue
        crossed_above = rsi_arr[i] > sma_arr[i] and rsi_arr[i-1] <= sma_arr[i-1]
        crossed_below = rsi_arr[i] < sma_arr[i] and rsi_arr[i-1] >= sma_arr[i-1]
        if not (crossed_above or crossed_below):
            continue
        sig_type = "BUY" if crossed_above else "SELL"
        sig_px   = float(cls_arr[i])
        def ret(fwd):
            j = i + fwd
            return round((cls_arr[j] / sig_px - 1) * 100, 1) if j < len(cls_arr) else None
        results.append({
            "date": dates[i].strftime("%d-%b-%y"), "type": sig_type,
            "price": round(sig_px, 2), "rsi": round(float(rsi_arr[i]), 1),
            "r5d": ret(5), "r10d": ret(10), "r20d": ret(20),
        })
    return results[-max_signals:]


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 8 — NSE UNIVERSE LOADER
# ═════════════════════════════════════════════════════════════════════════════

BUILTIN = [
    "RELIANCE","TCS","HDFCBANK","INFY","ICICIBANK","HINDUNILVR","ITC","SBIN",
    "BAJFINANCE","BHARTIARTL","KOTAKBANK","LT","AXISBANK","ASIANPAINT","MARUTI",
    "SUNPHARMA","TITAN","WIPRO","ULTRACEMCO","NTPC","POWERGRID","ONGC","JSWSTEEL",
    "TATASTEEL","COALINDIA","TECHM","HCLTECH","DRREDDY","CIPLA","DIVISLAB",
    "ADANIENT","ADANIGREEN","ADANIPORTS","ADANIPOWER","TATAPOWER","GAIL","IOC",
    "BPCL","HINDPETRO","HINDALCO","VEDL","HINDZINC","NATIONALUM","NMDC",
    "BANKBARODA","PNB","CANBK","FEDERALBNK","RBLBANK","BANDHANBNK","INDUSINDBK",
    "MUTHOOTFIN","BAJAJFINSV","CHOLAFIN","HDFCLIFE","ICICIGI","SBILIFE","LICI",
    "TATAMOTORS","M&M","BAJAJ-AUTO","HEROMOTOCO","EICHERMOT","SIEMENS","ABB",
    "BHEL","HAVELLS","VOLTAS","POLYCAB","RVNL","HAL","BEL","BEML",
    "AUROPHARMA","LUPIN","TORNTPHARM","ALKEM","APOLLOHOSP","FORTIS",
    "TATACONSUM","NESTLEIND","BRITANNIA","DABUR","GODREJCP","DMART","TRENT",
    "DEEPAKNTR","PIIND","SRF","TATACHEM","PERSISTENT","COFORGE","LTIM","NAUKRI",
    "DLF","GODREJPROP","PHOENIXLTD","ZOMATO","IRCTC","PIDILITIND","KALYANKJIL",
    "ATUL","NAVINFLUOR","VINATI","CLEAN","DIXON","AMBER","TATAELXSI",
    "IRFC","RECLTD","PFC","JSWENERGY","TORNTPOWER","CESC","NHPC","SJVN",
    "ANGELONE","BSE","CDSL","CAMS","MOFSL","JUBLFOOD","RADICO","MCDOWELL-N",
    "GRSE","COCHINSHIP","RAILTEL","TITAGARH","DATAPATTNS","KEC","KALPATPOWR",
    "JINDALSTEL","JSL","SAIL","APLAPOLLO","GRAPHITE","APARINDS","CUMMINSIND",
    "ELGIEQUIP","GRINDWELL","EXIDEIND","MOTHERSON","BOSCHLTD","MRF","APOLLOTYRE",
    "LALPATHLAB","METROPOLIS","MAXHEALTH","PAGEIND","COLPAL","EMAMILTD",
    "HDFCAMC","NIPPONLIFE","ABSLAMC","SBICARD","OBEROIRLTY","PRESTIGE","BRIGADE",
]

def _populate_indices():
    """Populate index membership from built-in NIFTY50 list."""
    _INDEX_MAP["NIFTY50"] = set(NIFTY50)

def _parse_nse_csv(text: str, series_filters: list[str], is_sme: bool = False) -> list[str]:
    import re
    
    # Fix malformed CSV with embedded newlines in quoted field names
    # The SME CSV has headers like: "SYMBOL \n","SERIES \n" which breaks CSV parsing
    text = text.lstrip('\ufeff')  # Remove BOM if present
    
    if is_sme:
        # SURGICAL FIX: Identify and fix header line only
        # Strategy: Find where first stock symbol appears, then extract header before it
        # Then fix the embedded \n in that header line
        
        # Look for the first stock symbol pattern (starting with uppercase letter)
        # First data row starts with quote: "SYMBOL_NAME"
        # We'll look for a known pattern: after header closing quote, we have \n then first stock
        
        # Find where actual data line starts (look for first quote followed by stock symbol)
        # Safe approach: find the last \n that comes before the first non-header row
        # The first data row has the first stock in it (e.g., "ADISOFT")
        
        # Since we don't know stock names, find pattern: `\n"` followed by quoted data
        # Better: the header is ONE line with embedded \n, then real data starts
        # Find the end of header by looking for the pattern where quoted field ends before real \n
        
        # Simplest: replace all ` \n` (space-newline) with space in one pass
        # This targets the specific pattern in field names like "SYMBOL \n"
        text = re.sub(r' \n', ' ', text)
    
    # NOW parse the (hopefully fixed) CSV
    _build_company_map(text)
    
    reader, tickers = csv.DictReader(io.StringIO(text)), []
    series_seen = set()   # track unique SERIES values found (for debug)

    for row in reader:
        if not row:  # Skip empty rows
            continue
        # Sanitize row keys by stripping whitespace
        clean_row = {}
        for k, v in row.items():
            if k:
                clean_row[k.strip()] = (v.strip() if isinstance(v, str) else v)
        series = clean_row.get("SERIES", "").strip()
        symbol = clean_row.get("SYMBOL", "").strip()

        if series:
            series_seen.add(series)

        if is_sme:
            # ── SME-dedicated CSV: the ENTIRE file is SME stocks ──────────
            # Accept every row that has a non-empty, non-numeric SYMBOL.
            # We deliberately ignore series_filters here because:
            #   • The file is already filtered to SME at the NSE-export level
            #   • Series codes vary (SM, ST, BE, …) and a mismatch silently
            #     drops all SME stocks, giving n_sme = 0 in the HTML.
            if symbol and not symbol.isdigit():
                tickers.append(symbol)
                _SME_STOCKS.add(symbol)
        else:
            if symbol and series in series_filters:
                tickers.append(symbol)

    if is_sme:
        print(f"  [SME] Series codes found in CSV : {sorted(series_seen) or '(none)'}")
        print(f"  [SME] Symbols loaded into _SME_STOCKS : {len(_SME_STOCKS)}")

    return tickers


def _parse_bse_csv(text: str, series_filters: list[str]) -> list[str]:
    """Parse BSE scrip CSV (BSE_EQ_SCRIP_*.csv) and return list of ticker symbols.

    BSE CSV columns relevant here:
      TckrSymb  — ticker symbol (may contain trailing # for ex-date stocks)
      SctySrs   — series code (EQ, BE, BZ, …)
      FinInstrmNm — full company name
    """
    text = text.lstrip('﻿')   # strip BOM
    reader = csv.DictReader(io.StringIO(text))
    tickers = []
    for row in reader:
        if not row:
            continue
        clean = {k.strip(): (v.strip() if isinstance(v, str) else v)
                 for k, v in row.items() if k}
        series = clean.get("SctySrs", "").strip()
        symbol = clean.get("TckrSymb", "").strip().rstrip("#")  # drop trailing #
        name   = clean.get("FinInstrmNm", "").strip()
        if symbol and series in series_filters:
            tickers.append(symbol)
            if name:
                _COMPANY_MAP[symbol] = name
    return tickers


def load_universe() -> list[str]:
    global _CACHE
    _populate_indices()          # Populate index membership mapping
    _CACHE = _load_cache_v2()   # Smart v2 cache (migrates old format automatically)

    all_tickers = []

    # ── Primary: local BSE EQ CSV ────────────────────────────────────────────
    if os.path.exists(LOCAL_BSE_CSV):
        try:
            with open(LOCAL_BSE_CSV, encoding="utf-8", errors="replace") as f:
                raw = f.read()
            t = _parse_bse_csv(raw, BSE_SERIES_FILTER)
            if t:
                print(f"  ✅ Local '{LOCAL_BSE_CSV}': {len(t)} EQ stocks | "
                      f"{len(_COMPANY_MAP)} companies mapped")
                all_tickers.extend(t)
            else:
                print(f"  ⚠️  BSE CSV found but 0 EQ symbols parsed — check CSV format")
        except Exception as e:
            print(f"  [!] Local BSE CSV error: {e}")

    if all_tickers:
        return list(dict.fromkeys(all_tickers))  # Remove duplicates, preserve order

    # ── Fallback: live BSE API ────────────────────────────────────────────────
    try:
        s = requests.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "*/*"})
        r = s.get(BSE_CSV_URL, timeout=20)
        r.raise_for_status()
        t = _parse_bse_csv(r.text, BSE_SERIES_FILTER)
        if t:
            print(f"  ✅ Live BSE API: {len(t)} EQ stocks | {len(_COMPANY_MAP)} companies")
            try:
                os.makedirs(os.path.dirname(LOCAL_BSE_CSV), exist_ok=True)
                with open(LOCAL_BSE_CSV, "w", encoding="utf-8") as f:
                    f.write(r.text)
                print(f"  💾 Saved → '{LOCAL_BSE_CSV}'")
            except Exception:
                pass
            all_tickers.extend(t)
            return list(dict.fromkeys(all_tickers))
    except Exception as e:
        print(f"  [!] BSE live download failed: {e}")

    print(f"  ⚠️  Using built-in list: {len(BUILTIN)} stocks")
    return list(dict.fromkeys(BUILTIN))


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 9 — PER-STOCK ANALYSIS  (with full error logging)
# ═════════════════════════════════════════════════════════════════════════════

def analyze_stock(ticker: str) -> dict | None:
    company = get_company_name(ticker)
    min_candles = get_min_candles_required(ticker)

    # ── Stage A: Data (always from cache after prefetch_all) ─────────────
    try:
        df = _get_df(ticker)
        if df is None:
            # Fallback: single download if prefetch missed this ticker
            raw = yf.download(ticker + ".BO", period=DATA_PERIOD, interval="1d",
                              progress=False, auto_adjust=True)
            df  = _clean_df(raw)
            if len(df) >= min_candles:
                _set_df(ticker, df)
                _maybe_save_cache()
        else:
            df = df.dropna(subset=["Close"])

        if len(df) < min_candles:
            log_warn(ticker, company,
                     f"Insufficient data: {len(df)} bars < {min_candles} required")
            return None
    except Exception as exc:
        log_error(ticker, company, "DOWNLOAD", exc)
        return None

    # ── Stage B: Resample ─────────────────────────────────────────
    try:
        wk = resample_ohlcv(df, "W-FRI")
        mo = resample_ohlcv(df, "ME")
        if len(wk) < 20 or len(mo) < 6:
            log_warn(ticker, company,
                     f"Insufficient resampled bars: W={len(wk)} M={len(mo)}")
            return None
    except Exception as exc:
        log_error(ticker, company, "RESAMPLE", exc)
        return None

    # ── Stage C: Indicators ───────────────────────────────────────
    try:
        rsi_d  = calc_rsi(df["Close"], RSI_P)
        sma_d  = rsi_d.rolling(RSI_SMA_P).mean()
        ml_d, ms_d, mh_d = calc_macd(df["Close"], MACD_F, MACD_S, MACD_SIG_P)
        cci_d  = calc_cci(df["High"], df["Low"], df["Close"], CCI_P)
        atr_d  = calc_atr(df["High"], df["Low"], df["Close"], ATR_P)

        rsi_w  = calc_rsi(wk["Close"], RSI_P)
        sma_w  = rsi_w.rolling(RSI_SMA_P).mean()
        ml_w, ms_w, mh_w = calc_macd(wk["Close"], MACD_F, MACD_S, MACD_SIG_P)
        cci_w  = calc_cci(wk["High"], wk["Low"], wk["Close"], CCI_P)

        rsi_m  = calc_rsi(mo["Close"], RSI_P)
        sma_m  = rsi_m.rolling(RSI_SMA_P).mean()
        ml_m, ms_m, mh_m = calc_macd(mo["Close"], MACD_F, MACD_S, MACD_SIG_P)
        cci_m  = calc_cci(mo["High"], mo["Low"], mo["Close"], CCI_P)
    except Exception as exc:
        log_error(ticker, company, "INDICATORS", exc)
        return None

    # ── Stage D: Feature extraction ───────────────────────────────
    try:
        def f(s, i=-1):
            v = s.iloc[i]
            return float(v) if not (isinstance(v, float) and np.isnan(v)) else 0.0

        v_rsi_d = f(rsi_d); v_sma_d = f(sma_d)
        v_rsi_w = f(rsi_w); v_sma_w = f(sma_w)
        v_rsi_m = f(rsi_m); v_sma_m = f(sma_m)
        v_ml_d  = f(ml_d);  v_ms_d  = f(ms_d)
        v_ml_w  = f(ml_w);  v_ms_w  = f(ms_w)
        v_ml_m  = f(ml_m);  v_ms_m  = f(ms_m)
        v_cci   = f(cci_d); v_cci_w = f(cci_w); v_cci_m = f(cci_m)
        v_atr   = f(atr_d)
        v_close = f(df["Close"])
        v_h52   = float(df["Close"].rolling(252).max().iloc[-1])
        v_l52   = float(df["Close"].rolling(252).min().iloc[-1])
        v_d52   = round((v_close / v_h52 - 1) * 100, 1)

        # Donchian breakout metrics (20-period prior high breakout on D/W/M)
        def prev_period_max(series, window):
            val = series.shift(1).rolling(window).max().iloc[-1]
            return float(val) if not np.isnan(val) else None
        def prev_period_min(series, window):
            val = series.shift(1).rolling(window).min().iloc[-1]
            return float(val) if not np.isnan(val) else None

        high20_d = prev_period_max(df["High"], 20)
        low20_d  = prev_period_min(df["Low"], 20)
        v_donch_d = round((v_close / high20_d - 1) * 100, 1) if high20_d else None

        v_close_w = f(wk["Close"])
        high20_w  = prev_period_max(wk["High"], 20)
        low20_w   = prev_period_min(wk["Low"], 20)
        v_donch_w = round((v_close_w / high20_w - 1) * 100, 1) if high20_w else None

        v_close_m = f(mo["Close"])
        high20_m  = prev_period_max(mo["High"], 20)
        low20_m   = prev_period_min(mo["Low"], 20)
        v_donch_m = round((v_close_m / high20_m - 1) * 100, 1) if high20_m else None

        def is_fresh(rsi_s, sma_s, window):
            for lag in range(1, window + 2):
                if len(rsi_s) <= lag: break
                if rsi_s.iloc[-lag] > sma_s.iloc[-lag] and rsi_s.iloc[-lag-1] <= sma_s.iloc[-lag-1]:
                    return True, lag
            return False, 0

        fresh_d, fresh_d_bars = is_fresh(rsi_d, sma_d, FRESH_DAYS_D)
        fresh_w, fresh_w_bars = is_fresh(rsi_w, sma_w, FRESH_WEEKS_W)

        score, sig_list = compute_score(
            v_rsi_d, v_sma_d, v_rsi_w, v_sma_w, v_rsi_m, v_sma_m,
            v_ml_d, v_ms_d, v_cci, fresh_d, fresh_w)
        phase  = detect_phase(v_rsi_d, v_rsi_w, v_rsi_m, v_ml_d, v_ms_d, score)
        signal, sig_cls = signal_label(score, phase, fresh_d, fresh_w,
                                        v_rsi_d, v_rsi_w, v_rsi_m,
                                        v_sma_d, v_sma_w, v_sma_m)

        sw_low, _, sw_high, _ = find_swing_points(df["High"], df["Low"])
        atr_sl   = round(v_close - 2.0 * v_atr, 2)
        swing_sl = round(sw_low * 0.99, 2)

        if phase == "UPTREND":
            fib_levels = {k: v for k, v in fib_extensions(sw_low, sw_high).items() if v > v_close}
            fib_type   = "EXTENSION"
            fib_base   = f"Swing Low ₹{sw_low:,.0f} → Swing High ₹{sw_high:,.0f}"
        else:
            fib_levels = {k: v for k, v in fib_retracements(sw_high, sw_low).items()
                          if sw_low < v < sw_high}
            fib_type   = "RETRACEMENT"
            fib_base   = f"Swing High ₹{sw_high:,.0f} → Swing Low ₹{sw_low:,.0f}"

        hist_sigs = historical_signals(df["Close"], rsi_d, sma_d)

        if v_rsi_d > 65:
            entry_note = f"Wait for pullback to RSI~55 zone (~₹{v_close * 0.96:,.0f})"
        elif v_rsi_d > 55 and v_rsi_d > v_sma_d:
            entry_note = f"Entry at current close ₹{v_close:,.0f} or next dip"
        elif fresh_d:
            entry_note = f"Fresh cross — confirm next candle above ₹{v_close:,.0f}"
        else:
            entry_note = "Wait for RSI(14) to cross above SMA(14) on daily"

        sell_conds = ["RSI(14) daily crosses BELOW SMA(14)",
                      "CCI(20) drops below −100",
                      "MACD(12,26) crosses below signal line"]
        if v_rsi_d > 75:
            sell_conds.insert(0, "⚠️ RSI >75 — consider partial profit booking")

        r_sl_pct = round((atr_sl   / v_close - 1) * 100, 1)
        s_sl_pct = round((swing_sl / v_close - 1) * 100, 1)

    except Exception as exc:
        log_error(ticker, company, "FEATURES", exc)
        return None

    return {
        "ticker": ticker, "company": company,
        "close":  v_close, "high52": v_h52, "low52": v_l52, "dist52": v_d52,
        "rsi_d":  round(v_rsi_d,1), "sma_d": round(v_sma_d,1),
        "rsi_w":  round(v_rsi_w,1), "sma_w": round(v_sma_w,1),
        "rsi_m":  round(v_rsi_m,1), "sma_m": round(v_sma_m,1),
        "macd_l": round(v_ml_d,3),  "macd_s": round(v_ms_d,3),
        "macd_l_w": round(v_ml_w,3),"macd_s_w": round(v_ms_w,3),
        "macd_l_m": round(v_ml_m,3),"macd_s_m": round(v_ms_m,3),
        "cci":    round(v_cci,1),   "cci_w": round(v_cci_w,1), "cci_m": round(v_cci_m,1),
        "atr":    round(v_atr,2),
        "donchian_d": v_donch_d, "donchian_w": v_donch_w, "donchian_m": v_donch_m,
        "fresh_d": fresh_d, "fresh_d_bars": fresh_d_bars,
        "fresh_w": fresh_w, "fresh_w_bars": fresh_w_bars,
        "score":   score,   "sig_list": sig_list,
        "phase":   phase,   "signal": signal, "sig_cls": sig_cls,
        "entry_note": entry_note, "sell_conds": sell_conds,
        "atr_sl":  atr_sl,  "swing_sl": swing_sl,
        "r_sl_pct": r_sl_pct, "s_sl_pct": s_sl_pct,
        "sw_low": sw_low, "sw_high": sw_high,
        "fib_type": fib_type, "fib_levels": fib_levels, "fib_base": fib_base,
        "hist_sigs": hist_sigs,
        # raw series — used for chart only, stripped before HTML table
        "_df": df, "_rsi_d": rsi_d, "_sma_d": sma_d,
        "_rsi_w_daily": rsi_w.reindex(df.index, method="ffill"),
        "_rsi_m_daily": rsi_m.reindex(df.index, method="ffill"),
        "_sma_w_daily": sma_w.reindex(df.index, method="ffill"),
        "_macd_l": ml_d, "_macd_s": ms_d, "_macd_h": mh_d,
        "_cci": cci_d,
        # ranking (filled later by compute_rankings)
        "rank_nifty50": 0, "rank_universe": 0,
        "rank_nifty50_pos": 0, "rank_nifty50_of": 0,
        "rank_univ_pos": 0,    "rank_univ_of": 0,
        "is_nifty50": False,
        "is_sme": is_sme_stock(ticker),
        "sector": get_sector(ticker) or "Unknown",
        "indices": get_indices(ticker),
        "marketcap": get_marketcap(ticker),
    }


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 10 — CHART GENERATOR
# ═════════════════════════════════════════════════════════════════════════════

def generate_chart(data: dict) -> str:
    """Generate a chart PNG file and return its relative path, or '' on error."""
    ticker  = data["ticker"]
    company = data["company"]
    try:
        df_all = data["_df"]
        n_bars = min(CHART_BARS, len(df_all))
        df     = df_all.iloc[-n_bars:].copy()
        idx    = np.arange(len(df))

        rsi_d  = data["_rsi_d"].iloc[-n_bars:].values
        sma_d  = data["_sma_d"].iloc[-n_bars:].values
        rsi_w  = data["_rsi_w_daily"].iloc[-n_bars:].values
        rsi_m  = data["_rsi_m_daily"].iloc[-n_bars:].values
        macd_l = data["_macd_l"].iloc[-n_bars:].values
        macd_s = data["_macd_s"].iloc[-n_bars:].values
        macd_h = data["_macd_h"].iloc[-n_bars:].values
        cci    = data["_cci"].iloc[-n_bars:].values

        BG="#0d1117"; PANEL="#161b22"; GREEN="#26d07c"; RED="#ff4d6d"
        GOLD="#ffd700"; CYAN="#00d4ff"; PURPLE="#b39ddb"; ORANGE="#ff9800"
        GREY="#30363d"; TXT="#c9d1d9"; FIB_EXT="#4caf50"; FIB_RET="#ff7043"

        fig = plt.figure(figsize=(14, 10), facecolor=BG)
        fig.suptitle(
            f"{ticker} — {data['company']}  |  ₹{data['close']:,.2f}  "
            f"|  {data['phase']}  |  {data['signal']}  |  Score {data['score']}/22  "
            f"|  Univ rank #{data['rank_univ_pos']}/{data['rank_univ_of']}",
            color=TXT, fontsize=11, fontweight="bold", y=0.998
        )
        gs   = gridspec.GridSpec(5, 1, figure=fig, hspace=0.04,
                                 height_ratios=[4, 1.2, 1.8, 1.4, 1.4])
        axes = [fig.add_subplot(gs[i]) for i in range(5)]
        for ax in axes:
            ax.set_facecolor(PANEL)
            ax.tick_params(colors=TXT, labelsize=7)
            ax.spines[:].set_color(GREY)
            ax.grid(True, color=GREY, linewidth=0.3, linestyle="--")
            ax.set_xlim(-1, len(idx))

        step = max(1, len(idx) // 10)
        tpos = idx[::step]
        tlbl = [df.index[i].strftime("%b'%y") for i in tpos]
        for ax in axes:
            ax.set_xticks(tpos)
            ax.set_xticklabels([] if ax != axes[-1] else tlbl,
                               rotation=30, ha="right", fontsize=6.5)

        # Panel 1: Candlestick
        ax1 = axes[0]
        for i, (_, row) in enumerate(df.iterrows()):
            up  = float(row["Close"]) >= float(row["Open"])
            col = GREEN if up else RED
            ax1.plot([i, i], [float(row["Low"]), float(row["High"])], color=col, lw=0.7, zorder=2)
            ax1.bar(i, abs(float(row["Close"]) - float(row["Open"])),
                    bottom=min(float(row["Open"]), float(row["Close"])),
                    color=col, width=0.7, linewidth=0, zorder=3)
        ax1.axhline(data["close"], color=GOLD, lw=0.8, linestyle="--", alpha=0.6)
        fib_col = FIB_EXT if data["fib_type"] == "EXTENSION" else FIB_RET
        for lbl, level in data["fib_levels"].items():
            ax1.axhline(level, color=fib_col, lw=0.8, linestyle=":", alpha=0.75)
            ax1.text(len(idx)-1, level, f" {lbl} ₹{level:,.0f}",
                     color=fib_col, fontsize=5.5, va="center")
        ax1.axhline(data["atr_sl"],   color=RED, lw=0.6, linestyle="-.", alpha=0.5)
        ax1.axhline(data["swing_sl"], color=RED, lw=0.5, linestyle="-.", alpha=0.3)
        sig_dates = {s["date"]: s["type"] for s in data["hist_sigs"][-8:]}
        for i, dt in enumerate(df.index):
            lbl = sig_dates.get(dt.strftime("%d-%b-%y"))
            if lbl == "BUY":
                ax1.plot(i, float(df["Low"].iloc[i]) * 0.993, "^", color=GREEN, markersize=6, zorder=5)
            elif lbl == "SELL":
                ax1.plot(i, float(df["High"].iloc[i]) * 1.007, "v", color=RED, markersize=6, zorder=5)
        ax1.set_ylabel("Price ₹", color=TXT, fontsize=7)
        ax1.legend(handles=[mpatches.Patch(color=fib_col, label=f"Fib {data['fib_type']}")],
                   loc="upper left", facecolor=BG, edgecolor=GREY, labelcolor=TXT, fontsize=6)

        # Panel 2: Volume
        ax2 = axes[1]
        vol_avg = pd.Series(df["Volume"].values).rolling(20).mean().values
        for i, (_, row) in enumerate(df.iterrows()):
            col = GREEN if float(row["Close"]) >= float(row["Open"]) else RED
            ax2.bar(i, float(row["Volume"]), color=col, width=0.7, alpha=0.7, linewidth=0)
        ax2.plot(idx, vol_avg, color=GOLD, lw=0.8)
        ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x/1e6:.1f}M"))
        ax2.set_ylabel("Vol", color=TXT, fontsize=7)

        # Panel 3: RSI D/W/M
        ax3 = axes[2]
        ax3.fill_between(idx, 30, 70, alpha=0.06, color=CYAN)
        ax3.axhline(70, color=RED,   lw=0.6, linestyle="--", alpha=0.5)
        ax3.axhline(55, color=GREEN, lw=0.5, linestyle=":",  alpha=0.4)
        ax3.axhline(50, color=TXT,   lw=0.5, linestyle="--", alpha=0.3)
        ax3.axhline(30, color=GREEN, lw=0.6, linestyle="--", alpha=0.5)
        ax3.plot(idx, rsi_d, color=CYAN,   lw=1.2, label=f"RSI-D {data['rsi_d']}")
        ax3.plot(idx, sma_d, color=ORANGE, lw=0.9, linestyle="--", label=f"SMA {data['sma_d']}")
        ax3.plot(idx, rsi_w, color=PURPLE, lw=0.8, linestyle="-.", label=f"RSI-W {data['rsi_w']}")
        ax3.plot(idx, rsi_m, color=GOLD,   lw=0.8, linestyle=":",  label=f"RSI-M {data['rsi_m']}")
        if data["fresh_d"] and data["fresh_d_bars"] <= n_bars:
            cx = len(idx) - data["fresh_d_bars"]
            ax3.axvline(cx, color=GREEN, lw=0.8, linestyle="--", alpha=0.6)
            ax3.text(cx, 74, "FRESH", color=GREEN, fontsize=5, ha="center")
        ax3.set_ylim(10, 90); ax3.set_ylabel("RSI", color=TXT, fontsize=7)
        ax3.legend(loc="upper left", facecolor=BG, edgecolor=GREY, labelcolor=TXT, fontsize=6, ncol=4)

        # Panel 4: MACD
        ax4 = axes[3]
        ax4.axhline(0, color=GREY, lw=0.6)
        ax4.bar(idx, macd_h, color=[GREEN if v >= 0 else RED for v in macd_h],
                width=0.7, alpha=0.6, linewidth=0)
        ax4.plot(idx, macd_l, color=CYAN,   lw=1.0, label=f"MACD {data['macd_l']:.3f}")
        ax4.plot(idx, macd_s, color=ORANGE, lw=0.8, linestyle="--",
                 label=f"Sig {data['macd_s']:.3f}")
        ax4.set_ylabel("MACD(12,26)", color=TXT, fontsize=7)
        ax4.legend(loc="upper left", facecolor=BG, edgecolor=GREY, labelcolor=TXT, fontsize=6, ncol=2)

        # Panel 5: CCI
        ax5 = axes[4]
        ax5.axhline(100,  color=RED,   lw=0.6, linestyle="--", alpha=0.7)
        ax5.axhline(0,    color=GREY,  lw=0.5)
        ax5.axhline(-100, color=GREEN, lw=0.6, linestyle="--", alpha=0.7)
        ax5.bar(idx, cci, color=[GREEN if v >= 0 else RED for v in cci],
                width=0.7, alpha=0.55, linewidth=0)
        ax5.plot(idx, cci, color=CYAN, lw=0.8, label=f"CCI(20) {data['cci']:.1f}")
        ax5.set_ylabel("CCI(20)", color=TXT, fontsize=7)
        ax5.legend(loc="upper left", facecolor=BG, edgecolor=GREY, labelcolor=TXT, fontsize=6)

        plt.tight_layout(rect=[0, 0, 1, 0.996])
        updated_at = datetime.now().strftime("%d %b %Y %H:%M")
        fig.text(0.995, 0.005, f"Updated: {updated_at}", ha="right", va="bottom", color=TXT, fontsize=7)
        os.makedirs(CHART_OUTPUT_DIR, exist_ok=True)
        chart_path = os.path.join(CHART_OUTPUT_DIR, f"{ticker}.png")
        fig.savefig(chart_path, format="png", dpi=CHART_DPI, bbox_inches="tight", facecolor=BG)
        plt.close(fig)
        return chart_path.replace("\\", "/")

    except Exception as exc:
        log_error(ticker, company, "CHART", exc)
        plt.close("all")
        return ""


def _generate_chart_worker(data: dict) -> tuple[str, str]:
    ticker = data["ticker"]
    path = generate_chart(data)
    return ticker, path


def _load_chart_cache_meta() -> dict:
    try:
        with open(CHART_CACHE_META, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_chart_cache_meta(meta: dict) -> None:
    try:
        with open(CHART_CACHE_META, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        log_error("CACHE", "CACHE", "CHARTMETA", exc)


def _compute_chart_hash(data: dict) -> str:
    try:
        n_bars = min(CHART_BARS, len(data["_df"]))
        if n_bars <= 0:
            return ""
        df = data["_df"].iloc[-n_bars:][["Open", "High", "Low", "Close", "Volume"]].astype("float64")
        hasher = hashlib.sha256()
        hasher.update(df.to_numpy().tobytes())
        for arr_key in ["_rsi_d", "_sma_d", "_rsi_w_daily", "_rsi_m_daily", "_macd_l", "_macd_s", "_macd_h", "_cci"]:
            arr = np.asarray(data[arr_key].iloc[-n_bars:])
            hasher.update(arr.astype("float64").tobytes())
        hasher.update(json.dumps(data.get("fib_levels", {}), sort_keys=True, default=str).encode("utf-8"))
        hasher.update(str(data.get("atr_sl", "")).encode("utf-8"))
        hasher.update(str(data.get("swing_sl", "")).encode("utf-8"))
        return hasher.hexdigest()
    except Exception:
        return ""


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 11 — HTML REPORT
# ═════════════════════════════════════════════════════════════════════════════

_CSS = """
:root{
  --bg:#0d1117;--card:#161b22;--border:#30363d;--text:#c9d1d9;
  --sub:#8b949e;--green:#26d07c;--red:#ff4d6d;--gold:#ffd700;
  --cyan:#00d4ff;--purple:#b39ddb;--orange:#ff9800;
  --bs-body-bg:#0d1117;--bs-body-color:#c9d1d9;--bs-border-color:#30363d;
}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;font-size:13px}
a{color:var(--cyan)}
/* Bootstrap dark overrides */
.form-control,.form-select{background:var(--card)!important;border-color:var(--border)!important;color:var(--text)!important;font-size:12px}
.form-control::placeholder{color:var(--sub)}
.form-control:focus,.form-select:focus{background:var(--card)!important;border-color:var(--cyan)!important;color:var(--text)!important;box-shadow:0 0 0 .2rem rgba(0,212,255,.15)!important}
.form-select option{background:#161b22;color:var(--text)}
.btn-outline-secondary{color:var(--sub);border-color:var(--border);font-size:12px}
.btn-outline-secondary:hover{background:var(--border);color:var(--text);border-color:var(--border)}

/* ── Header ─────────────────────────────────────────── */
.app-header{background:#010409;border-bottom:2px solid #21262d;padding:18px 20px 14px}
.app-header h1{font-size:20px;font-weight:700;color:var(--cyan);letter-spacing:1px;margin:0}
.subtitle{color:var(--sub);font-size:11.5px;margin-top:4px}
.stats-row{display:flex;gap:10px;margin-top:12px;flex-wrap:wrap}
.stat-box{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:9px 16px;min-width:100px;flex:1;min-width:90px;max-width:160px}
.stat-box .val{font-size:22px;font-weight:700}
.stat-box .lbl{font-size:10px;color:var(--sub);margin-top:2px}
.stat-box.green .val{color:var(--green)}.stat-box.gold .val{color:var(--gold)}
.stat-box.red .val{color:var(--red)}.stat-box.cyan .val{color:var(--cyan)}

/* ── Filter section ────────────────────────────────── */
.filter-section{background:#010409;padding:10px 20px;border-bottom:1px solid var(--border);position:sticky;top:0;z-index:1000}
.filter-row1{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px}
.filter-row2{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:6px}
.filter-row3{display:flex;gap:6px;flex-wrap:wrap;align-items:center;padding:6px 0;border-top:1px solid #21262d;padding-top:8px}
.filter-input{flex:1;min-width:180px;max-width:260px;background:var(--card);border:1px solid var(--border);color:var(--text);border-radius:20px;padding:5px 14px;font-size:12px;outline:none}
.filter-input:focus{border-color:var(--cyan);box-shadow:0 0 0 2px rgba(0,212,255,.12)}
.filter-input::placeholder{color:var(--sub)}
.filter-select{background:var(--card);border:1px solid var(--border);color:var(--text);border-radius:20px;padding:5px 12px;font-size:12px;cursor:pointer;outline:none;appearance:auto}
.filter-select:focus{border-color:var(--cyan)}
.filter-select option{background:#161b22}
.phase-btn{background:var(--card);border:1px solid var(--border);color:var(--sub);border-radius:20px;padding:4px 13px;cursor:pointer;font-size:12px;transition:all .15s;white-space:nowrap}
.phase-btn:hover,.phase-btn.active{background:var(--cyan);color:#000;border-color:var(--cyan);font-weight:600}
.rank-sort-label{font-size:11px;color:var(--sub);font-weight:700;white-space:nowrap;letter-spacing:.5px;padding:0 4px}
.rank-sort-btn{background:var(--card);border:2px solid var(--border);color:var(--sub);border-radius:18px;padding:3px 11px;cursor:pointer;font-size:11px;transition:all .15s;white-space:nowrap;font-weight:600}
.rank-sort-btn:hover{background:var(--border);color:var(--text);border-color:var(--gold)}
.rank-sort-btn.active{background:var(--gold);color:#000;border-color:var(--gold);font-weight:700}
.clear-btn{background:transparent;border:1px solid #444;color:var(--sub);border-radius:20px;padding:4px 12px;font-size:12px;cursor:pointer;transition:all .15s}
.clear-btn:hover{border-color:var(--red);color:var(--red)}
.results-info{font-size:11px;color:var(--sub);margin-left:4px;white-space:nowrap}
.results-info b{color:var(--cyan)}
.active-chips{display:flex;gap:5px;flex-wrap:wrap;align-items:center}
.chip{display:inline-flex;align-items:center;gap:4px;background:#002d40;color:var(--cyan);border:1px solid #00d4ff33;border-radius:12px;padding:2px 10px;font-size:10.5px;font-weight:600}
.chip .x{cursor:pointer;opacity:.7;font-size:12px;line-height:1}
.chip .x:hover{opacity:1}

/* ── Summary table ─────────────────────────────────── */
.table-section{padding:0 20px 6px}
.sort-hint{padding:6px 0 4px;font-size:11px;color:var(--sub)}
.table-wrap{display:block;overflow-x:auto;overflow-y:hidden;-webkit-overflow-scrolling:touch;scrollbar-width:thin;scrollbar-color:var(--cyan) var(--card);}
.table-wrap::-webkit-scrollbar{height:10px}
.table-wrap::-webkit-scrollbar-track{background:var(--card)}
.table-wrap::-webkit-scrollbar-thumb{background:var(--cyan);border-radius:10px}
.sum-table{min-width:1380px;border-collapse:collapse;font-size:11.5px}
.sum-table th{background:#21262d;color:var(--sub);padding:7px 9px;text-align:left;
              font-weight:600;white-space:nowrap;position:sticky;top:0;z-index:5}
.sum-table th[data-col]{cursor:pointer;user-select:none}
.sum-table th[data-col]:hover{color:var(--cyan)}
.sort-ind{display:inline-block;min-width:12px;font-size:10px;margin-left:2px;opacity:.7}
.sum-table td{padding:6px 9px;border-bottom:1px solid #21262d;white-space:nowrap}
.sum-table tr:hover td{background:#1c2128}
.rsi-stack{display:flex;flex-direction:column;align-items:flex-end;line-height:1.2}
.rsi-stack .rv{font-weight:600;font-size:12px}
.rsi-stack .sv{font-size:10px;color:var(--sub);margin-top:1px}

/* rank pill */
.rank-pill{display:inline-block;border-radius:10px;padding:1px 8px;font-size:10px;font-weight:700}
.rank-top{background:#0d3320;color:var(--green);border:1px solid #26d07c44}
.rank-mid{background:#2d2600;color:var(--gold); border:1px solid #ffd70044}
.rank-low{background:#2d0a0a;color:var(--red);  border:1px solid #ff4d6d44}

/* badges */
.badge{display:inline-block;border-radius:12px;padding:2px 9px;font-size:10px;font-weight:700;letter-spacing:.4px}
.badge-UPTREND {background:#0d3320;color:var(--green);border:1px solid #26d07c33}
.badge-SIDEWAYS{background:#2d2600;color:var(--gold); border:1px solid #ffd70033}
.badge-BEARISH {background:#2d0a0a;color:var(--red);  border:1px solid #ff4d6d33}
.sig-strong-buy{color:#00e676;font-weight:700}.sig-buy{color:var(--green);font-weight:600}
.sig-watch{color:var(--gold)}.sig-avoid{color:var(--red)}.sig-neutral{color:var(--sub)}
.fresh-tag{background:#002d40;color:var(--cyan);border-radius:8px;padding:1px 7px;
           font-size:10px;font-weight:700;border:1px solid #00d4ff44}
.n50-tag{background:#1a0d30;color:var(--purple);border-radius:8px;padding:1px 7px;
         font-size:10px;font-weight:700;border:1px solid #b39ddb44}
.sme-tag{background:#1a2d0d;color:#4caf50;border-radius:8px;padding:1px 7px;
         font-size:10px;font-weight:700;border:1px solid #4caf5044}
.index-tag{background:#0d2440;color:#03a9f4;border-radius:8px;padding:1px 7px;
           font-size:10px;font-weight:700;border:1px solid #03a9f444}
.sector-tag{background:#2d1a0d;color:#ff9800;border-radius:8px;padding:1px 7px;
            font-size:10px;font-weight:700;border:1px solid #ff980044}
.cap-large{background:#0d1a2d;color:#4caf50;border-radius:8px;padding:1px 7px;
           font-size:10px;font-weight:700;border:1px solid #4caf5044}
.cap-mid{background:#1a2d0d;color:#8bc34a;border-radius:8px;padding:1px 7px;
         font-size:10px;font-weight:700;border:1px solid #8bc34a44}
.cap-small{background:#2d2d0d;color:#fdd835;border-radius:8px;padding:1px 7px;
           font-size:10px;font-weight:700;border:1px solid #fdd83544}
.cap-micro{background:#2d1a1a;color:#ff6f00;border-radius:8px;padding:1px 7px;
           font-size:10px;font-weight:700;border:1px solid #ff6f0044}
.cap-unknown{background:#1a1a1a;color:#888;border-radius:8px;padding:1px 7px;
             font-size:10px;font-weight:700;border:1px solid #88888844}

/* ── Cards ──────────────────────────────────────────── */
.cards-section{padding:12px 20px 40px}
.cards-section>h2{font-size:13px;color:var(--sub);margin-bottom:10px;letter-spacing:1px}

details.stock-card{background:var(--card);border:1px solid var(--border);
                   border-radius:10px;margin-bottom:20px;overflow:hidden}
details.stock-card[open]{border-color:var(--cyan)}
details.stock-card>summary{
  list-style:none;display:flex;align-items:center;gap:12px;padding:13px 18px;
  background:#0d1117;cursor:pointer;flex-wrap:wrap;user-select:none;position:relative}
details.stock-card>summary::-webkit-details-marker,
details.stock-card>summary::-moz-list-bullet{display:none}
details.stock-card>summary .card-arrow{
  display:inline-flex;align-items:center;justify-content:center;
  width:18px;height:18px;color:var(--sub);font-size:12px;flex-shrink:0;
  transition:transform .2s,color .2s}
details.stock-card[open]>summary .card-arrow{transform:rotate(90deg);color:var(--cyan)}
.card-ticker{font-size:17px;font-weight:700;color:var(--cyan)}
.card-price {font-size:15px;font-weight:600}
.card-score {font-size:12px;background:#21262d;border-radius:7px;padding:2px 11px;
             color:var(--gold);font-weight:700}
.card-body{border-top:1px solid var(--border)}
.chart-wrap img{width:100%;display:block}
.chart-placeholder{display:flex;align-items:center;justify-content:center;
                   height:80px;color:var(--sub);font-size:12px;
                   background:var(--bg);border-bottom:1px solid var(--border)}

/* detail panels inside <details> */
.card-details{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));
              gap:0;padding:0}
details.detail-panel{border-right:1px solid var(--border);border-top:1px solid var(--border)}
details.detail-panel:last-child{border-right:none}
details.detail-panel>summary{
  list-style:none;padding:9px 14px;cursor:pointer;font-size:11px;
  font-weight:700;text-transform:uppercase;letter-spacing:.8px;
  color:var(--sub);user-select:none;display:flex;align-items:center;gap:6px}
details.detail-panel>summary::-webkit-details-marker{display:none}
details.detail-panel>summary::after{content:"⌄";margin-left:auto;font-size:13px}
details.detail-panel[open]>summary{color:var(--cyan);border-bottom:1px solid var(--border)}
details.detail-panel[open]>summary::after{transform:rotate(180deg);display:inline-block}
.detail-content{padding:12px 14px}

/* inner tables */
.mini-table{width:100%;border-collapse:collapse;font-size:11.5px}
.mini-table th{color:var(--sub);text-align:left;padding:3px 5px;font-size:10px;font-weight:600}
.mini-table td{padding:4px 5px;border-bottom:1px solid #21262d}
.mini-table tr:last-child td{border-bottom:none}
.g{color:var(--green)}.r{color:var(--red)}

/* trade + sell */
.trade-row{display:flex;justify-content:space-between;padding:4px 0;
           border-bottom:1px solid #21262d;font-size:12px}
.trade-row:last-child{border-bottom:none}
.tl{color:var(--sub)}.tv{font-weight:600}
.tv.green{color:var(--green)}.tv.red{color:var(--red)}.tv.gold{color:var(--gold)}
.entry-box{background:#0d2218;border:1px solid #26d07c33;border-radius:5px;
           padding:7px 9px;margin-top:7px;font-size:11px;color:var(--green)}
.sell-cond{color:var(--red);font-size:11px;padding:3px 0;border-bottom:1px solid #21262d}
.sell-cond:last-child{border-bottom:none}

/* fib */
.fib-row{display:flex;justify-content:space-between;padding:4px 0;
         border-bottom:1px solid #21262d;font-size:11.5px}
.fib-row:last-child{border-bottom:none}
.fl{color:var(--sub);font-size:10.5px}.fv{font-weight:700}
.ext-val{color:#4caf50}.ret-val{color:#ff7043}

/* hist */
.hist-table{width:100%;border-collapse:collapse;font-size:11px}
.hist-table th{color:var(--sub);text-align:right;padding:3px 5px;font-size:10px;font-weight:600}
.hist-table th:first-child,.hist-table th:nth-child(2),.hist-table th:nth-child(3){text-align:left}
.hist-table td{padding:4px 5px;border-bottom:1px solid #21262d;text-align:right}
.hist-table td:first-child,.hist-table td:nth-child(2),.hist-table td:nth-child(3){text-align:left}
.hist-buy{color:var(--green);font-weight:700}.hist-sell{color:var(--red);font-weight:700}
.ret-pos{color:var(--green)}.ret-neg{color:var(--red)}

/* sig dots */
.sig-item{display:flex;align-items:center;gap:7px;padding:4px 0;
          border-bottom:1px solid #21262d;font-size:11.5px}
.sig-item:last-child{border-bottom:none}
.sig-dot{width:6px;height:6px;border-radius:50%;flex-shrink:0}

/* ── Footer ─────────────────────────────────────────── */
.footer{text-align:center;padding:18px;color:var(--sub);
        font-size:11px;border-top:1px solid var(--border)}
/* sort hint */
.sort-hint{padding:5px 0 3px;font-size:11px;color:var(--sub)}
/* ── Pagination ─────────────────────────────────────────── */
#tbl-pager{display:flex;align-items:center;gap:10px;padding:8px 0 4px;flex-wrap:wrap}
.page-btn{background:var(--card);border:1px solid var(--border);color:var(--text);
          border-radius:8px;padding:4px 14px;cursor:pointer;font-size:12px;transition:all .15s}
.page-btn:hover:not(:disabled){border-color:var(--cyan);color:var(--cyan)}
.page-btn:disabled{opacity:.35;cursor:default}
.page-info{font-size:11px;color:var(--sub)}
/* ── Load More ──────────────────────────────────────────── */
.load-more-btn{display:block;width:100%;max-width:420px;margin:14px auto 24px;
               background:var(--card);border:1px solid var(--border);color:var(--cyan);
               border-radius:10px;padding:10px 0;font-size:13px;font-weight:600;
               cursor:pointer;transition:all .2s;letter-spacing:.4px}
.load-more-btn:hover{background:#002d40;border-color:var(--cyan)}
/* mobile */
@media(max-width:600px){
  .app-header{padding:14px 14px 12px}
  .filter-section{padding:8px 14px}
  .cards-section{padding:10px 14px 30px}
  .table-section{padding:0 14px 4px}
  .card-ticker{font-size:14px}
  .stat-box{min-width:75px;padding:8px 10px}
  .stat-box .val{font-size:18px}
}
"""

# ─── All rendering is data-driven from STOCKS JSON (injected below) ───────────
# No stock HTML is pre-rendered in Python; JS renders table rows and cards on demand.
_JS = """
// ═══════════════════════════════════════════════════════════════════
//  VIRTUAL RENDER ENGINE  — operates on STOCKS array, not DOM nodes
//  Table: 200 rows/page  |  Cards: 50 at a time with Load More
//  Filter/Sort: pure array ops, then re-render
// ═══════════════════════════════════════════════════════════════════

// STOCKS, CHART_DIR, PAGE_TBL, PAGE_CARDS are injected by Python above this block

const F = { phase:'all', cap:'all', sector:'all', index:'all', search:'' };
let sortKeys   = [];
let filtered   = [];
let tblPage    = 0;
let cardCount  = 0;

// ── HTML escape ───────────────────────────────────────────────────
function esc(s){
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;')
                      .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Currency formatter ────────────────────────────────────────────
function fmtINR(n){
  if(n===null||n===undefined||n==='') return '—';
  return '₹'+Number(n).toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2});
}
function fmtINR0(n){
  if(!n&&n!==0) return '—';
  return '₹'+Number(n).toLocaleString('en-IN',{maximumFractionDigits:0});
}

// ── Badge / pill helpers ──────────────────────────────────────────
function phaseBadge(ph){
  return `<span class="badge badge-${ph}">${ph}</span>`;
}
function rankPill(pct,pos,of){
  if(!of) return '—';
  const cls=pct>=70?'rank-top':pct>=40?'rank-mid':'rank-low';
  const indicator = pct>=90?'⭐⭐⭐':pct>=70?'⭐⭐':pct>=40?'⭐':'•';
  return `<span class="rank-pill ${cls}">${indicator} #${pos}/${of} (${Math.round(pct)}%ile)</span>`;
}

// ── Match stock against current filters ───────────────────────────
function matchStock(s){
  if(F.phase!=='all'){
    if(F.phase==='fresh'   &&!(s.fresh_d||s.fresh_w)) return false;
    if(F.phase==='nifty50' &&!s.is_nifty50)           return false;
    if(F.phase==='sme'     &&!s.is_sme)               return false;
    if(!['fresh','nifty50','sme'].includes(F.phase)&&s.phase!==F.phase) return false;
  }
  if(F.cap   !=='all'&&s.cap_cls !==F.cap)    return false;
  if(F.sector!=='all'&&s.sector  !==F.sector) return false;
  if(F.index !=='all'&&!(s.indices||[]).includes(F.index)) return false;
  if(F.search){
    const q=F.search.toLowerCase();
    if(!s.ticker.toLowerCase().includes(q)&&!s.company.toLowerCase().includes(q)) return false;
  }
  return true;
}

// ── Sort filtered array in-place ──────────────────────────────────
const COL_FIELD={ticker:'ticker',score:'score',rsid:'rsi_d',rsiw:'rsi_w',rsim:'rsi_m',
  cci:'cci',macd:'macd_l',close:'close',dist52:'dist52',
  donchd:'donchian_d',donchw:'donchian_w',donchm:'donchian_m',
  rn50:'rank_nifty50',runiv:'rank_universe'};

function applySort(){
  if(!sortKeys.length) return;
  filtered.sort((a,b)=>{
    for(const {col,dir} of sortKeys){
      const f=COL_FIELD[col]||col;
      const av=a[f], bv=b[f];
      if(av===bv) continue;
      if(typeof av==='string') return dir==='desc'?bv.localeCompare(av):av.localeCompare(bv);
      return dir==='desc'?bv-av:av-bv;
    }
    return 0;
  });
}

// ── Main filter entry-point ───────────────────────────────────────
function applyFilters(){
  filtered=STOCKS.filter(matchStock);
  applySort();
  tblPage=0;
  cardCount=0;
  renderTable();
  renderCards(true);
  renderChips();
  const rc=document.getElementById('rc');
  if(rc) rc.textContent=filtered.length;
}

// ═══════════════════════════════════════════════════════════════════
//  TABLE RENDERING  (paginated, 200 rows/page)
// ═══════════════════════════════════════════════════════════════════
function rowHTML(s){
  const frTag =(s.fresh_d||s.fresh_w)?'<span class="fresh-tag">FRESH</span>':'';
  const n50Tag=s.is_nifty50?'<span class="n50-tag">N50</span>':'';
  const smeTag=s.is_sme?'<span class="sme-tag">SME</span>':'';
  const capTag=s.cap_cat!=='Unknown'?`<span class="${s.cap_cls} badge">${s.cap_cat}</span>`:'';
  const rsiCol=s.rsi_d>s.sma_d?'var(--green)':'var(--red)';
  const rsiArr=s.rsi_d>s.sma_d?'▲':'▼';
  const mCol  =s.macd_l>0?'var(--green)':'var(--red)';
  const d52Col=s.dist52<-10?'var(--red)':s.dist52>-5?'var(--green)':'';
  const mcap  =s.marketcap?`<div style="font-size:10px;color:var(--sub)">₹${(s.marketcap/1e7).toLocaleString('en-IN',{maximumFractionDigits:0})} Cr</div>`:'';
  return `<tr class="sum-row">
  <td><b style="color:var(--cyan)">${esc(s.ticker)}</b> ${frTag}${n50Tag}${smeTag}
      <div style="font-size:10px;color:var(--sub)">${esc(s.company.substring(0,28))}</div></td>
  <td>${phaseBadge(s.phase)}</td>
  <td><span class="${s.sig_cls}">${esc(s.signal)}</span></td>
  <td style="text-align:right"><b>${s.score}</b>/22</td>
  <td style="text-align:right">
    <div class="rsi-stack">
      <span class="rv" style="color:${rsiCol}">${s.rsi_d} ${rsiArr}</span>
      <span class="sv">SMA ${s.sma_d}</span>
    </div></td>
  <td style="text-align:right">${s.rsi_w}</td>
  <td style="text-align:right">${s.rsi_m}</td>
  <td style="text-align:right">${s.cci}</td>
  <td style="text-align:right;color:${mCol}">${s.macd_l.toFixed(3)}</td>
  <td style="text-align:right">${fmtINR(s.close)}</td>
  <td style="text-align:right;color:${d52Col}">${s.dist52}%</td>
  <td style="text-align:right;color:${s.donchian_d>0?'var(--green)':s.donchian_d<0?'var(--red)':''}">${s.donchian_d!==null?`${s.donchian_d}%`:'—'}</td>
  <td style="text-align:right;color:${s.donchian_w>0?'var(--green)':s.donchian_w<0?'var(--red)':''}">${s.donchian_w!==null?`${s.donchian_w}%`:'—'}</td>
  <td style="text-align:right;color:${s.donchian_m>0?'var(--green)':s.donchian_m<0?'var(--red)':''}">${s.donchian_m!==null?`${s.donchian_m}%`:'—'}</td>
  <td style="text-align:center">${capTag}${mcap}</td>
  <td style="text-align:right">${rankPill(s.rank_nifty50,s.rank_nifty50_pos,s.rank_nifty50_of)}</td>
  <td style="text-align:right">${rankPill(s.rank_universe,s.rank_univ_pos,s.rank_univ_of)}</td>
</tr>`;
}

function renderTable(){
  const tbody=document.getElementById('tbl-body');
  if(!tbody) return;
  const start=tblPage*PAGE_TBL;
  tbody.innerHTML=filtered.slice(start,start+PAGE_TBL).map(rowHTML).join('');
  const pager=document.getElementById('tbl-pager');
  if(pager){
    const total=filtered.length, pages=Math.ceil(total/PAGE_TBL)||1;
    if(pages<=1){ pager.innerHTML=''; return; }
    pager.innerHTML=
      `<button class="page-btn"${tblPage===0?' disabled':''} onclick="goPage(-1)">◀ Prev</button>`+
      `<span class="page-info">Page ${tblPage+1} / ${pages} &nbsp;·&nbsp; ${total} rows</span>`+
      `<button class="page-btn"${tblPage>=pages-1?' disabled':''} onclick="goPage(1)">Next ▶</button>`;
  }
}

function goPage(delta){
  const pages=Math.ceil(filtered.length/PAGE_TBL)||1;
  tblPage=Math.max(0,Math.min(pages-1,tblPage+delta));
  renderTable();
  document.getElementById('sumtable')?.scrollIntoView({behavior:'smooth',block:'start'});
}

// ── Multi-column sort (click header) ─────────────────────────────
function sortTable(col,e){
  const shift=e&&e.shiftKey;
  if(!shift){
    const ex=sortKeys.find(k=>k.col===col);
    const nd=(ex&&sortKeys[0].col===col&&ex.dir==='desc')?'asc':'desc';
    sortKeys=[{col,dir:nd}];
  } else {
    const idx=sortKeys.findIndex(k=>k.col===col);
    if(idx===-1){ if(sortKeys.length<3) sortKeys.push({col,dir:'desc'}); }
    else if(sortKeys[idx].dir==='desc') sortKeys[idx].dir='asc';
    else sortKeys.splice(idx,1);
  }
  document.querySelectorAll('#sumtable th[data-col]').forEach(th=>{
    const ki=sortKeys.findIndex(k=>k.col===th.dataset.col);
    const si=th.querySelector('.sort-ind');
    if(ki===-1){si.textContent='↕';th.style.color='';}
    else{
      const arrow=sortKeys[ki].dir==='desc'?'▼':'▲';
      si.innerHTML=arrow+(sortKeys.length>1?`<sup style="font-size:8px">${ki+1}</sup>`:'');
      th.style.color='var(--cyan)';
    }
  });
  applySort();
  tblPage=0;
  renderTable();
}

// ═══════════════════════════════════════════════════════════════════
//  CARD RENDERING  (50 at a time, Load More)
// ═══════════════════════════════════════════════════════════════════
function cardSummaryHTML(s,idx){
  const frTags=(s.fresh_d?`<span class="fresh-tag">🚀 Daily (${s.fresh_d_bars}d)</span>`:'')
              +(s.fresh_w?`<span class="fresh-tag">📅 Weekly (${s.fresh_w_bars}w)</span>`:'');
  const n50Tag=s.is_nifty50?'<span class="n50-tag">NIFTY50</span>':'';
  const smeTag=s.is_sme?'<span class="sme-tag">SME</span>':'';
  const capTag=s.cap_cat!=='Unknown'?`<span class="${s.cap_cls}">${s.cap_cat}</span>`:'';
  const idxTags=(s.indices||[]).map(i=>`<span class="index-tag">${esc(i)}</span>`).join(' ');
  const secTag=(s.sector&&s.sector!=='Unknown')?`<span class="sector-tag">${esc(s.sector)}</span>`:'';
  return `<details class="stock-card" data-idx="${idx}">
  <summary>
    <span class="card-arrow">▶</span>
    <span class="card-ticker">${esc(s.ticker)}</span>
    <span class="card-price">${fmtINR(s.close)}</span>
    ${phaseBadge(s.phase)}
    <span class="${s.sig_cls}" style="font-weight:700">${esc(s.signal)}</span>
    <span class="card-score">Score ${s.score}/22</span>
    ${frTags}${n50Tag}${smeTag}${capTag}${idxTags}${secTag}
    <span style="margin-left:auto;color:var(--sub);font-size:11px;text-align:right">
      D ${s.rsi_d} W ${s.rsi_w} M ${s.rsi_m} RSI
      &nbsp;|&nbsp; N50: ${rankPill(s.rank_nifty50,s.rank_nifty50_pos,s.rank_nifty50_of)}
      &nbsp;|&nbsp; All: ${rankPill(s.rank_universe,s.rank_univ_pos,s.rank_univ_of)}
      &nbsp;|&nbsp; D:${s.donchian_d!==null?`${s.donchian_d}%`:'—'} W:${s.donchian_w!==null?`${s.donchian_w}%`:'—'} M:${s.donchian_m!==null?`${s.donchian_m}%`:'—'}
    </span>
  </summary>
  <div class="card-body" data-rendered="0">
    <div class="chart-placeholder" style="height:36px;font-size:12px">▶ Click to expand details</div>
  </div>
</details>`;
}

function renderCards(reset){
  const container=document.getElementById('cards-container');
  if(!container) return;
  if(reset){ container.innerHTML=''; cardCount=0; }
  const batch=filtered.slice(cardCount,cardCount+PAGE_CARDS);
  if(!batch.length){
    if(!cardCount) container.innerHTML='<div style="padding:20px;color:var(--sub)">No stocks match the current filter.</div>';
    document.getElementById('load-more-btn')?.remove();
    return;
  }
  // Build DOM fragment — only summaries, no detail content yet
  const frag=document.createDocumentFragment();
  batch.forEach((s,i)=>{
    const wrap=document.createElement('div');
    wrap.innerHTML=cardSummaryHTML(s,cardCount+i);
    frag.appendChild(wrap.firstElementChild);
  });
  container.appendChild(frag);
  cardCount+=batch.length;
  // Load More button
  let btn=document.getElementById('load-more-btn');
  if(cardCount<filtered.length){
    if(!btn){
      btn=document.createElement('button');
      btn.id='load-more-btn';
      btn.className='load-more-btn';
      btn.onclick=()=>renderCards(false);
      container.after(btn);
    }
    btn.textContent=`⬇ Load ${Math.min(PAGE_CARDS,filtered.length-cardCount)} more  (${filtered.length-cardCount} remaining)`;
  } else {
    btn?.remove();
  }
}

// ═══════════════════════════════════════════════════════════════════
//  CARD DETAIL RENDERING  (lazy — only when expanded)
// ═══════════════════════════════════════════════════════════════════
function renderCardDetail(card){
  const body=card.querySelector('.card-body');
  if(!body||body.dataset.rendered==='1') return;
  body.dataset.rendered='1';
  const s=filtered[parseInt(card.dataset.idx)];
  if(!s){ body.innerHTML='<div style="padding:12px;color:var(--sub)">Data not found.</div>'; return; }
  body.innerHTML=buildCardBody(s);
  // Lazy-load chart image
  const img=body.querySelector('img.lazy-chart');
  if(img&&img.dataset.src) img.src=img.dataset.src;
}

function buildCardBody(s){
  // Chart
  const chartHtml=s.has_chart
    ?`<div class="chart-wrap"><img class="lazy-chart"
        src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
        data-src="${CHART_DIR}/${esc(s.ticker)}.png"
        alt="${esc(s.ticker)} chart" loading="lazy"
        style="opacity:0;transition:opacity .4s;width:100%;display:block"
        onload="this.style.opacity=1"></div>`
    :'<div class="chart-placeholder">📊 Chart not included</div>';

  // RSI table
  function rsiRow(tf,rv,sv,isFr){
    const cls=rv>sv?'g':'r', arr=rv>sv?'▲':'▼';
    const fr=isFr?'<span class="fresh-tag">FRESH</span>':'';
    return `<tr><td>${tf}</td><td class="${cls}"><b>${rv}</b></td>
      <td style="font-size:10px;color:var(--sub)">${sv}</td>
      <td class="${cls}">${arr}${rv>sv?'ABOVE':'BELOW'} ${fr}</td></tr>`;
  }
  const rsiHtml=`<table class="mini-table">
    <tr><th>TF</th><th>RSI(14)</th><th>SMA(14)</th><th>Status</th></tr>
    ${rsiRow('Daily',s.rsi_d,s.sma_d,s.fresh_d)}
    ${rsiRow('Weekly',s.rsi_w,s.sma_w,s.fresh_w)}
    ${rsiRow('Monthly',s.rsi_m,s.sma_m,false)}</table>`;

  // CCI table
  function cciRow(tf,v){
    const cls=v>0?'g':'r';
    const lbl=v>100?'🚀 STRONG':v>0?'✅ Positive':v<-100?'⚠️ EXTREME':'❌ Negative';
    return `<tr><td>${tf}</td><td class="${cls}"><b>${v}</b></td><td class="${cls}">${lbl}</td></tr>`;
  }
  const cciHtml=`<table class="mini-table">
    <tr><th>TF</th><th>CCI(20)</th><th>Signal</th></tr>
    ${cciRow('Daily',s.cci)}${cciRow('Weekly',s.cci_w)}${cciRow('Monthly',s.cci_m)}</table>`;

  // MACD table
  function macdRow(tf,ml,ms){
    const cls=ml>ms?'g':'r', lbl=ml>ms?'▲ BULLISH':'▼ BEARISH';
    return `<tr><td>${tf}</td><td class="${cls}"><b>${ml.toFixed(3)}</b></td><td class="${cls}">${lbl}</td></tr>`;
  }
  const macdHtml=`<table class="mini-table">
    <tr><th>TF</th><th>MACD(12,26)</th><th>Status</th></tr>
    ${macdRow('Daily',s.macd_l,s.macd_s)}
    ${macdRow('Weekly',s.macd_l_w,s.macd_s_w)}
    ${macdRow('Monthly',s.macd_l_m,s.macd_s_m)}</table>`;

  // Trade panel
  const sellHtml=(s.sell_conds||[]).map(c=>`<div class="sell-cond">⚠ ${esc(c)}</div>`).join('');
  const tradeHtml=`
    <div class="trade-row"><span class="tl">Close</span><span class="tv gold">${fmtINR(s.close)}</span></div>
    <div class="trade-row"><span class="tl">ATR(14) SL</span>
      <span class="tv red">${fmtINR(s.atr_sl)} (${s.r_sl_pct>0?'+':''}${s.r_sl_pct}%)</span></div>
    <div class="trade-row"><span class="tl">Swing Low SL</span>
      <span class="tv red">${fmtINR(s.swing_sl)} (${s.s_sl_pct>0?'+':''}${s.s_sl_pct}%)</span></div>
    <div class="trade-row"><span class="tl">52W High</span><span class="tv">${fmtINR(s.high52)}</span></div>
    <div class="trade-row"><span class="tl">52W Low</span><span class="tv">${fmtINR(s.low52)}</span></div>
    <div class="entry-box">💡 ${esc(s.entry_note)}</div>
    <div style="margin-top:9px;font-size:10px;color:var(--sub);font-weight:700;
                text-transform:uppercase;letter-spacing:.8px;margin-bottom:4px">EXIT when:</div>
    ${sellHtml}`;

  // Rankings panel
  const rankHtml=`
    <div class="trade-row"><span class="tl">vs Nifty50</span>
      <span class="tv">${rankPill(s.rank_nifty50,s.rank_nifty50_pos,s.rank_nifty50_of)}</span></div>
    <div class="trade-row"><span class="tl">vs All NSE</span>
      <span class="tv">${rankPill(s.rank_universe,s.rank_univ_pos,s.rank_univ_of)}</span></div>
    <div class="trade-row"><span class="tl">Score</span><span class="tv gold">${s.score}/22</span></div>
    <div class="trade-row"><span class="tl">Phase</span><span class="tv">${phaseBadge(s.phase)}</span></div>
    <div class="trade-row"><span class="tl">Signal</span><span class="tv ${s.sig_cls}">${esc(s.signal)}</span></div>`;

  // Market Cap panel
  let capHtml='';
  if(s.marketcap){
    const cr=s.marketcap/1e7;
    const disp=cr>=1?`₹${cr.toLocaleString('en-IN',{maximumFractionDigits:0})} Cr`:`₹${(s.marketcap/1e6).toFixed(1)}M`;
    capHtml+=`<div class="trade-row"><span class="tl">Market Cap</span><span class="tv gold">${disp}</span></div>`;
    capHtml+=`<div class="trade-row"><span class="tl">Category</span><span class="tv"><span class="${s.cap_cls} badge">${s.cap_cat}</span></span></div>`;
  } else {
    capHtml+='<div class="trade-row"><span class="tl">Market Cap</span><span class="tv" style="color:var(--sub)">Not available</span></div>';
  }
  if(s.sector&&s.sector!=='Unknown')
    capHtml+=`<div class="trade-row"><span class="tl">Sector</span><span class="tv"><span class="sector-tag badge">${esc(s.sector)}</span></span></div>`;
  if((s.indices||[]).length)
    capHtml+=`<div class="trade-row"><span class="tl">Indices</span><span class="tv">${s.indices.map(i=>`<span class="index-tag badge">${esc(i)}</span>`).join(' ')}</span></div>`;

  // Fib panel
  const fibCol=s.fib_type==='EXTENSION'?'ext-val':'ret-val';
  const fibLbl=s.fib_type==='EXTENSION'?'🎯 Fib Extension — Upside Targets':'🛡️ Fib Retracement — Support';
  const fibBody=Object.entries(s.fib_levels||{}).map(([lvl,price])=>{
    const pct=((price/s.close-1)*100).toFixed(1);
    return `<div class="fib-row"><span class="fl">${lvl}</span>
      <span class="fv ${fibCol}">${fmtINR(price)}
        <span style="color:var(--sub);font-size:10px">${pct>=0?'+':''}${pct}%</span>
      </span></div>`;
  }).join('')||'<span style="color:var(--sub)">No levels near price</span>';
  const fibHtml=`<div style="font-size:10.5px;color:var(--sub);margin-bottom:6px">${esc(s.fib_base)}</div>${fibBody}`;

  // Active signals
  const dotMap={'✅':'#26d07c','🚀':'#00d4ff','🔥':'#ff9800','💪':'#b39ddb','💰':'#ffd700'};
  const sigsHtml=(s.sig_list||[]).map(sig=>{
    const hit=Object.entries(dotMap).find(([e])=>sig.includes(e));
    return `<div class="sig-item"><div class="sig-dot" style="background:${hit?hit[1]:'#26d07c'}"></div><span>${esc(sig)}</span></div>`;
  }).join('')||'<span style="color:var(--sub)">No active signals</span>';

  // Historical signals
  function retSpan(v){
    if(v===null||v===undefined) return '<span style="color:#555">—</span>';
    return `<span class="${v>=0?'ret-pos':'ret-neg'}">${v>=0?'+':''}${v}%</span>`;
  }
  const histRows=[...(s.hist_sigs||[])].reverse().map(h=>
    `<tr><td>${esc(h.date)}</td>
     <td class="${h.type==='BUY'?'hist-buy':'hist-sell'}">${h.type}</td>
     <td>${fmtINR(h.price)}</td><td>RSI ${h.rsi}</td>
     <td>${retSpan(h.r5d)}</td><td>${retSpan(h.r10d)}</td><td>${retSpan(h.r20d)}</td></tr>`
  ).join('')||'<tr><td colspan="7" style="color:var(--sub)">No signals in history</td></tr>';
  const histHtml=`<table class="hist-table">
    <tr><th>Date</th><th>Type</th><th>Price</th><th>RSI</th>
        <th>5D</th><th>10D</th><th>20D</th></tr>
    ${histRows}</table>`;

  function dp(title,content,open=false){
    return `<details class="detail-panel"${open?' open':''}><summary>${title}</summary><div class="detail-content">${content}</div></details>`;
  }
  return `${chartHtml}<div class="card-details">
    ${dp('📊 RSI · Daily · Weekly · Monthly',rsiHtml,true)}
    ${dp('🎯 CCI(20) · D · W · M',cciHtml)}
    ${dp('📈 MACD(12,26) · D · W · M',macdHtml)}
    ${dp('💼 Entry / Stop Loss / Exit',tradeHtml,true)}
    ${dp('🏆 Rankings',rankHtml,true)}
    ${dp('💰 Market Cap',capHtml)}
    ${dp(fibLbl,fibHtml)}
    ${dp('⚡ Active Signals',sigsHtml)}
    <details class="detail-panel" style="grid-column:1/-1">
      <summary>📅 Historical RSI Crossover Signals — recent first</summary>
      <div class="detail-content">${histHtml}</div>
    </details>
  </div>`;
}

// ─── Expand card lazily ───────────────────────────────────────────
document.addEventListener('toggle', e=>{
  const card=e.target;
  if(!card.classList?.contains('stock-card')||!card.open) return;
  renderCardDetail(card);
}, true);

// ─── Filter controls ──────────────────────────────────────────────
function filterPhase(phase,btn){
  document.querySelectorAll('.phase-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  F.phase=phase;
  applyFilters();
}

// ─── Quick ranking sort ────────────────────────────────────────────
function sortByRanking(col,dir){
  sortKeys=[{col,dir}];
  applySort();
  tblPage=0;
  cardCount=0;
  renderTable();
  renderCards(true);
  // Visual feedback
  const btn=event.target;
  document.querySelectorAll('.rank-sort-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
}
function onDropChange(){
  F.cap   =document.getElementById('capSel').value;
  F.sector=document.getElementById('secSel').value;
  F.index =document.getElementById('idxSel').value;
  applyFilters();
}
let _st=null;
function onSearch(v){
  clearTimeout(_st);
  _st=setTimeout(()=>{ F.search=v.trim(); applyFilters(); },220);
}
function clearAll(){
  Object.assign(F,{phase:'all',cap:'all',sector:'all',index:'all',search:''});
  ['capSel','secSel','idxSel'].forEach(id=>{const el=document.getElementById(id);if(el)el.value='all';});
  const si=document.getElementById('searchInp');if(si)si.value='';
  document.querySelectorAll('.phase-btn').forEach(b=>b.classList.remove('active'));
  document.querySelector('.phase-btn[data-phase="all"]')?.classList.add('active');
  applyFilters();
}

// ─── Filter chips ─────────────────────────────────────────────────
const CAP_LABELS={'cap-large':'Large Cap','cap-mid':'Mid Cap','cap-small':'Small Cap','cap-micro':'Micro Cap'};
function renderChips(){
  const c=document.getElementById('chips');
  if(!c) return;
  const chips=[];
  if(F.phase!=='all')  chips.push([`Phase: ${F.phase}`,()=>{F.phase='all';document.querySelectorAll('.phase-btn').forEach(b=>b.classList.remove('active'));document.querySelector('.phase-btn[data-phase="all"]')?.classList.add('active');}]);
  if(F.cap!=='all')    chips.push([CAP_LABELS[F.cap]||F.cap,()=>{F.cap='all';document.getElementById('capSel').value='all';}]);
  if(F.sector!=='all') chips.push([F.sector,()=>{F.sector='all';document.getElementById('secSel').value='all';}]);
  if(F.index!=='all')  chips.push([F.index,()=>{F.index='all';document.getElementById('idxSel').value='all';}]);
  if(F.search)         chips.push([`"${F.search}"`,()=>{F.search='';document.getElementById('searchInp').value='';}]);
  c.innerHTML=chips.map((ch,i)=>
    `<span class="chip">${esc(ch[0])} <span class="x" onclick="(${chips[i][1].toString()})();applyFilters()">✕</span></span>`
  ).join('');
}

// ─── Init ─────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded',()=>{
  document.querySelector('.phase-btn[data-phase="all"]')?.classList.add('active');
  filtered=STOCKS.slice();
  renderTable();
  renderCards(true);
  const rc=document.getElementById('rc');
  if(rc) rc.textContent=filtered.length;
});
"""


def _ret_span(val):
    if val is None: return '<span style="color:#555">—</span>'
    cls = "ret-pos" if val >= 0 else "ret-neg"
    return f'<span class="{cls}">{val:+.1f}%</span>'

def _phase_badge(phase):
    return f'<span class="badge badge-{phase}">{phase}</span>'

def _sig_span(signal, cls):
    return f'<span class="{cls}">{signal}</span>'

def _rank_pill(pct, pos, of):
    if of == 0: return "—"
    cls = "rank-top" if pct >= 70 else "rank-mid" if pct >= 40 else "rank-low"
    # Add visual indicator based on ranking tier
    indicator = "⭐⭐⭐" if pct >= 90 else "⭐⭐" if pct >= 70 else "⭐" if pct >= 40 else "•"
    return f'<span class="rank-pill {cls}">{indicator} #{pos}/{of} ({pct:.0f}%ile)</span>'


def _build_detail_panels(d: dict) -> str:
    close = d["close"]

    # RSI table
    def rsi_row(tf, rv, sv, is_fresh_tf):
        cls = "g" if rv > sv else "r"
        arr = "▲" if rv > sv else "▼"
        fr  = ' <span class="fresh-tag">FRESH</span>' if is_fresh_tf else ""
        return (f'<tr><td>{tf}</td>'
                f'<td class="{cls}"><b>{rv}</b></td>'
                f'<td style="font-size:10px;color:var(--sub)">{sv}</td>'
                f'<td class="{cls}">{arr}{"ABOVE" if rv>sv else "BELOW"}{fr}</td></tr>')

    def cci_row(tf, v):
        cls = "g" if v > 0 else "r"
        lbl = ("🚀 STRONG" if v > 100 else "✅ Positive" if v > 0
               else "⚠️ EXTREME" if v < -100 else "❌ Negative")
        return f'<tr><td>{tf}</td><td class="{cls}"><b>{v}</b></td><td class="{cls}">{lbl}</td></tr>'

    def macd_row(tf, ml, ms):
        cls = "g" if ml > ms else "r"
        lbl = "▲ BULLISH" if ml > ms else "▼ BEARISH"
        return f'<tr><td>{tf}</td><td class="{cls}"><b>{ml:.3f}</b></td><td class="{cls}">{lbl}</td></tr>'

    rsi_html = f"""<table class="mini-table">
      <tr><th>TF</th><th>RSI(14)</th><th>SMA(14)</th><th>Status</th></tr>
      {rsi_row("Daily",   d["rsi_d"], d["sma_d"], d["fresh_d"])}
      {rsi_row("Weekly",  d["rsi_w"], d["sma_w"], d["fresh_w"])}
      {rsi_row("Monthly", d["rsi_m"], d["sma_m"], False)}
    </table>"""

    cci_html = f"""<table class="mini-table">
      <tr><th>TF</th><th>CCI(20)</th><th>Signal</th></tr>
      {cci_row("Daily",   d["cci"])}
      {cci_row("Weekly",  d["cci_w"])}
      {cci_row("Monthly", d["cci_m"])}
    </table>"""

    macd_html = f"""<table class="mini-table">
      <tr><th>TF</th><th>MACD(12,26)</th><th>Status</th></tr>
      {macd_row("Daily",   d["macd_l"],   d["macd_s"])}
      {macd_row("Weekly",  d["macd_l_w"], d["macd_s_w"])}
      {macd_row("Monthly", d["macd_l_m"], d["macd_s_m"])}
    </table>"""

    trade_html = f"""
    <div class="trade-row"><span class="tl">Close</span><span class="tv gold">₹{close:,.2f}</span></div>
    <div class="trade-row"><span class="tl">ATR(14) SL</span>
      <span class="tv red">₹{d['atr_sl']:,.2f} ({d['r_sl_pct']:+.1f}%)</span></div>
    <div class="trade-row"><span class="tl">Swing Low SL</span>
      <span class="tv red">₹{d['swing_sl']:,.2f} ({d['s_sl_pct']:+.1f}%)</span></div>
    <div class="trade-row"><span class="tl">52W High</span><span class="tv">₹{d['high52']:,.2f}</span></div>
    <div class="trade-row"><span class="tl">52W Low</span> <span class="tv">₹{d['low52']:,.2f}</span></div>
    <div class="entry-box">💡 {d['entry_note']}</div>
    <div style="margin-top:9px;font-size:10px;color:var(--sub);font-weight:700;
                text-transform:uppercase;letter-spacing:.8px;margin-bottom:4px">EXIT when:</div>
    {"".join(f'<div class="sell-cond">⚠ {c}</div>' for c in d['sell_conds'])}"""

    fib_col = "ext-val" if d["fib_type"] == "EXTENSION" else "ret-val"
    fib_lbl = "🎯 Fib Extension — Upside Targets" if d["fib_type"] == "EXTENSION" else "🛡️ Fib Retracement — Support"
    fib_body = "".join(
        f'<div class="fib-row"><span class="fl">{lvl}</span>'
        f'<span class="fv {fib_col}">₹{price:,.2f} '
        f'<span style="color:var(--sub);font-size:10px">{round((price/close-1)*100,1):+.1f}%</span>'
        f'</span></div>'
        for lvl, price in d["fib_levels"].items()
    ) or '<span style="color:var(--sub)">No levels near price</span>'
    fib_html = f'<div style="font-size:10.5px;color:var(--sub);margin-bottom:6px">{d["fib_base"]}</div>{fib_body}'

    dot_map = {"✅":"#26d07c","🚀":"#00d4ff","🔥":"#ff9800","💪":"#b39ddb","💰":"#ffd700"}
    sigs_html = "".join(
        f'<div class="sig-item"><div class="sig-dot" style="background:'
        f'{next((c for e,c in dot_map.items() if e in s),"#26d07c")}"></div>'
        f'<span>{s}</span></div>'
        for s in d["sig_list"]
    ) or '<span style="color:var(--sub)">No active signals</span>'

    hist_rows = "".join(
        f'<tr><td>{s["date"]}</td>'
        f'<td class="{"hist-buy" if s["type"]=="BUY" else "hist-sell"}">{s["type"]}</td>'
        f'<td>₹{s["price"]:,.2f}</td><td>RSI {s["rsi"]}</td>'
        f'<td>{_ret_span(s["r5d"])}</td><td>{_ret_span(s["r10d"])}</td>'
        f'<td>{_ret_span(s["r20d"])}</td></tr>'
        for s in reversed(d["hist_sigs"])
    ) or '<tr><td colspan="7" style="color:var(--sub)">No signals in history</td></tr>'
    hist_html = f"""<table class="hist-table">
      <tr><th>Date</th><th>Type</th><th>Price</th><th>RSI</th>
          <th>5D</th><th>10D</th><th>20D</th></tr>
      {hist_rows}</table>"""

    # Rankings
    rank_html = f"""
    <div class="trade-row"><span class="tl">vs Nifty50</span>
      <span class="tv">{_rank_pill(d['rank_nifty50'], d['rank_nifty50_pos'], d['rank_nifty50_of'])}</span></div>
    <div class="trade-row"><span class="tl">vs All NSE</span>
      <span class="tv">{_rank_pill(d['rank_universe'], d['rank_univ_pos'], d['rank_univ_of'])}</span></div>
    <div class="trade-row"><span class="tl">Score</span>
      <span class="tv gold">{d['score']}/22</span></div>
    <div class="trade-row"><span class="tl">Phase</span>
      <span class="tv">{_phase_badge(d['phase'])}</span></div>
    <div class="trade-row"><span class="tl">Signal</span>
      <span class="tv {d['sig_cls']}">{d['signal']}</span></div>"""

    def dp(title, content, open_default=False):
        op = " open" if open_default else ""
        return (f'<details class="detail-panel"{op}>'
                f'<summary>{title}</summary>'
                f'<div class="detail-content">{content}</div>'
                f'</details>')

    # Market Cap Panel
    cap_cat, cap_cls = categorize_marketcap(d['marketcap'])
    if d['marketcap']:
        cap_crore = d['marketcap'] / 1e7
        cap_display = f"₹{cap_crore:,.0f} Cr" if cap_crore >= 1 else f"₹{d['marketcap']/1e6:,.1f}M"
        cap_html = f"""<div class="trade-row"><span class="tl">Market Cap</span>
          <span class="tv gold">{cap_display}</span></div>
        <div class="trade-row"><span class="tl">Category</span>
          <span class="tv"><span class="{cap_cls} badge">{cap_cat}</span></span></div>"""
    else:
        cap_html = '<div class="trade-row"><span class="tl">Market Cap</span><span class="tv" style="color:var(--sub)">Not available</span></div>'
    # Add sector & indices to cap panel
    if d.get('sector') and d['sector'] != 'Unknown':
        cap_html += f'<div class="trade-row"><span class="tl">Sector</span><span class="tv"><span class="sector-tag badge">{d["sector"]}</span></span></div>'
    if d.get('indices'):
        idx_html = ' '.join(f'<span class="index-tag badge">{i}</span>' for i in d['indices'])
        cap_html += f'<div class="trade-row"><span class="tl">Indices</span><span class="tv">{idx_html}</span></div>'

    return f"""<div class="card-details">
      {dp("📊 RSI · Daily · Weekly · Monthly", rsi_html, True)}
      {dp("🎯 CCI(20) · D · W · M",            cci_html)}
      {dp("📈 MACD(12,26) · D · W · M",         macd_html)}
      {dp("💼 Entry / Stop Loss / Exit",         trade_html, True)}
      {dp("🏆 Rankings",                         rank_html, True)}
      {dp("💰 Market Cap",                       cap_html)}
      {dp(fib_lbl,                               fib_html)}
      {dp("⚡ Active Signals",                   sigs_html)}
      <details class="detail-panel" style="grid-column:1/-1">
        <summary>📅 Historical RSI Crossover Signals — recent first</summary>
        <div class="detail-content">{hist_html}</div>
      </details>
    </div>"""


def build_summary_table() -> str:
    """Return the table skeleton only — tbody is populated by JS from STOCKS JSON."""
    def th(lbl, col, align="right"):
        return (f'<th data-col="{col}" onclick="sortTable(\'{col}\',event)" '
                f'style="text-align:{align}">{lbl} <span class="sort-ind">↕</span></th>')

    return f"""
    <div class="sort-hint">
      💡 Click header to sort &nbsp;|&nbsp; <b>Shift+click</b> = add 2nd/3rd sort key &nbsp;|&nbsp;
      Click again to toggle ▲▼ &nbsp;|&nbsp; Shows {PAGE_TBL} rows per page
    </div>
    <div class="table-wrap">
      <table class="sum-table" id="sumtable">
        <thead><tr>
          {th('Ticker / Company', 'ticker', 'left')}
          <th>Phase</th><th>Signal</th>
          {th('Score',    'score')}
          {th('D-RSI/SMA','rsid')}
          {th('W-RSI',    'rsiw')}
          {th('M-RSI',    'rsim')}
          {th('D-CCI',    'cci')}
          {th('D-MACD',   'macd')}
          {th('Close',    'close')}
          {th('52W%',     'dist52')}
          <th style="text-align:center">Market Cap</th>
          {th('D-Donch', 'donchd')}
          {th('W-Donch', 'donchw')}
          {th('M-Donch', 'donchm')}
          {th('vs N50',   'rn50')}
          {th('vs All',   'runiv')}
        </tr></thead>
        <tbody id="tbl-body"></tbody>
      </table>
    </div>
    <div id="tbl-pager"></div>"""


def build_stock_card(d: dict, has_chart: bool) -> str:
    """
    Build a <details> card. Chart img is always present but src="" (lazy).
    JS fills it from data-src on first open.
    If has_chart=False, show placeholder instead.
    """
    fr_tags = ""
    if d["fresh_d"]: fr_tags += f' <span class="fresh-tag">🚀 Daily ({d["fresh_d_bars"]}d)</span>'
    if d["fresh_w"]: fr_tags += f' <span class="fresh-tag">📅 Weekly ({d["fresh_w_bars"]}w)</span>'
    n50_tag = ' <span class="n50-tag">NIFTY50</span>' if d["is_nifty50"] else ""
    sme_tag = ' <span class="sme-tag">SME</span>' if d["is_sme"] else ""
    idx_tags = ' ' + ' '.join(f'<span class="index-tag">{idx}</span>' for idx in d['indices']) if d['indices'] else ""
    sector_tag = f' <span class="sector-tag">{d["sector"]}</span>' if d['sector'] and d['sector'] != 'Unknown' else ""
    cap_cat, cap_cls = categorize_marketcap(d["marketcap"])
    cap_tag = f' <span class="{cap_cls}">{cap_cat}</span>' if cap_cat != "Unknown" else ""

    n50_rank  = _rank_pill(d["rank_nifty50"], d["rank_nifty50_pos"], d["rank_nifty50_of"])
    univ_rank = _rank_pill(d["rank_universe"], d["rank_univ_pos"],   d["rank_univ_of"])

    if has_chart:
        chart_html = (f'<div class="chart-wrap">'
                      f'<img class="lazy-chart" data-src="{CHART_OUTPUT_DIR}/{d["ticker"]}.png" '
                      f'src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==" '
                      f'alt="{d["ticker"]} chart" loading="lazy">'
                      f'</div>')
    else:
        chart_html = '<div class="chart-placeholder">📊 Chart not included (outside top limit)</div>'

    panels = _build_detail_panels(d)

    return f"""
<details class="stock-card" data-phase="{d['phase']}"
         data-fresh="{'1' if (d['fresh_d'] or d['fresh_w']) else '0'}"
         data-nifty="{'1' if d['is_nifty50'] else '0'}"
         data-sme="{'1' if d['is_sme'] else '0'}"
         data-cap="{cap_cls}"
         data-sector="{d.get('sector','')}"
         data-indices="{','.join(d.get('indices', []))}"
         data-ticker="{d['ticker']}"
         data-company="{d['company'][:50]}">
  <summary>
    <span class="card-arrow">▶</span>
    <span class="card-ticker">{d['ticker']}</span>
    <span class="card-price">₹{d['close']:,.2f}</span>
    {_phase_badge(d['phase'])}
    <span class="{d['sig_cls']}" style="font-weight:700">{d['signal']}</span>
    <span class="card-score">Score {d['score']}/22</span>
    {fr_tags}{n50_tag}{sme_tag}{cap_tag}{idx_tags}{sector_tag}
    <span style="margin-left:auto;color:var(--sub);font-size:11px;text-align:right">
      D {d['rsi_d']} W {d['rsi_w']} M {d['rsi_m']} RSI
      &nbsp;|&nbsp; N50: {n50_rank}
      &nbsp;|&nbsp; All: {univ_rank}
    </span>
  </summary>
  <div class="card-body">
    {chart_html}
    {panels}
  </div>
</details>"""


def _build_filter_options(all_results: list[dict]) -> tuple[str, str]:
    """Build sector and index <option> lists for filter dropdowns."""
    sectors = sorted({d["sector"] for d in all_results if d.get("sector") and d["sector"] != "Unknown"})
    indices = sorted({idx for d in all_results for idx in (d.get("indices") or [])})
    sec_opts = "".join(f'<option value="{s}">{s}</option>' for s in sectors)
    idx_opts = "".join(f'<option value="{i}">{i}</option>' for i in indices)
    return sec_opts, idx_opts


_HTML_FIELDS = {
    'ticker','company','close','high52','low52','dist52','rsi_d','sma_d','rsi_w','sma_w',
    'rsi_m','sma_m','macd_l','macd_s','macd_l_w','macd_s_w','macd_l_m','macd_s_m',
    'cci','cci_w','cci_m','atr_sl','swing_sl','r_sl_pct','s_sl_pct','entry_note',
    'sell_conds','score','phase','signal','sig_cls','fresh_d','fresh_d_bars','fresh_w',
    'fresh_w_bars','donchian_d','donchian_w','donchian_m','is_nifty50','is_sme','sector','indices','marketcap','cap_cat','cap_cls',
    'rank_nifty50','rank_nifty50_pos','rank_nifty50_of','rank_universe','rank_univ_pos',
    'rank_univ_of','fib_type','fib_levels','fib_base','sig_list','hist_sigs','has_chart'
}


def _html_safe_stock(d: dict) -> dict:
    return {k: d[k] for k in _HTML_FIELDS if k in d}


def build_html_report(all_results: list[dict], chart_data: dict[str, str],
                      run_ts: str, scanned: int) -> str:
    n_up  = sum(1 for d in all_results if d["phase"] == "UPTREND")
    n_sw  = sum(1 for d in all_results if d["phase"] == "SIDEWAYS")
    n_be  = sum(1 for d in all_results if d["phase"] == "BEARISH")
    n_fr  = sum(1 for d in all_results if d["fresh_d"] or d["fresh_w"])
    n_n50 = sum(1 for d in all_results if d["is_nifty50"])
    n_sme = sum(1 for d in all_results if d["is_sme"])
    total = len(all_results)

    sec_opts, idx_opts = _build_filter_options(all_results)

    # ── Add computed display fields + has_chart to each record ────────────────
    chart_tickers = set(chart_data.keys())
    for d in all_results:
        cap_cat, cap_cls = categorize_marketcap(d.get("marketcap"))
        d["cap_cat"]   = cap_cat
        d["cap_cls"]   = cap_cls
        d["has_chart"] = d["ticker"] in chart_tickers

    # ── Serialize to compact JSON (only the fields needed by HTML/JS) ───────
    safe_results = [_html_safe_stock(d) for d in all_results]
    stocks_json = json.dumps(safe_results, ensure_ascii=False, default=str, separators=(",", ":"))

    stat_boxes = f"""<div class="stats-row">
      <div class="stat-box cyan"><div class="val">{scanned}</div><div class="lbl">Scanned</div></div>
      <div class="stat-box green"><div class="val">{n_up}</div><div class="lbl">📈 Uptrend</div></div>
      <div class="stat-box gold"><div class="val">{n_sw}</div><div class="lbl">➡️ Sideways</div></div>
      <div class="stat-box red"><div class="val">{n_be}</div><div class="lbl">📉 Bearish</div></div>
      <div class="stat-box cyan"><div class="val">{n_fr}</div><div class="lbl">🚀 Fresh</div></div>
      <div class="stat-box gold"><div class="val">{n_n50}</div><div class="lbl">🏆 Nifty50</div></div>
      <div class="stat-box green"><div class="val">{n_sme}</div><div class="lbl">📊 SME</div></div>
    </div>"""

    filter_bar = f"""<div class="filter-section">
      <div class="filter-row1">
        <input id="searchInp" class="filter-input" type="text"
               placeholder="🔍 Search ticker / company…" oninput="onSearch(this.value)">
        <select id="capSel" class="filter-select" onchange="onDropChange()">
          <option value="all">💰 All Cap Sizes</option>
          <option value="cap-large">🟢 Large Cap (&gt;₹2L Cr)</option>
          <option value="cap-mid">🔵 Mid Cap (₹50K–2L Cr)</option>
          <option value="cap-small">🟡 Small Cap (₹5K–50K Cr)</option>
          <option value="cap-micro">🟠 Micro Cap (&lt;₹5K Cr)</option>
        </select>
        <select id="secSel" class="filter-select" onchange="onDropChange()">
          <option value="all">🏭 All Sectors / Industries</option>
          {sec_opts}
        </select>
        <select id="idxSel" class="filter-select" onchange="onDropChange()">
          <option value="all">📊 All Indices</option>
          {idx_opts}
        </select>
        <button class="clear-btn" onclick="clearAll()">✖ Clear</button>
        <span class="results-info">Showing <b id="rc">{total}</b> of {total} stocks</span>
      </div>
      <div class="filter-row2">
        <button class="phase-btn" data-phase="all"      onclick="filterPhase('all',this)">All ({total})</button>
        <button class="phase-btn" data-phase="fresh"    onclick="filterPhase('fresh',this)">🚀 Fresh ({n_fr})</button>
        <button class="phase-btn" data-phase="UPTREND"  onclick="filterPhase('UPTREND',this)">📈 Uptrend ({n_up})</button>
        <button class="phase-btn" data-phase="SIDEWAYS" onclick="filterPhase('SIDEWAYS',this)">➡️ Sideways ({n_sw})</button>
        <button class="phase-btn" data-phase="BEARISH"  onclick="filterPhase('BEARISH',this)">📉 Bearish ({n_be})</button>
        <button class="phase-btn" data-phase="nifty50"  onclick="filterPhase('nifty50',this)">🏆 Nifty50 ({n_n50})</button>
        <button class="phase-btn" data-phase="sme"      onclick="filterPhase('sme',this)">📊 SME ({n_sme})</button>
      </div>
      <div class="filter-row3">
        <span class="rank-sort-label">🏆 RANKING SORT:</span>
        <button class="rank-sort-btn" onclick="sortByRanking('rn50','desc')">⭐ Top vs N50</button>
        <button class="rank-sort-btn" onclick="sortByRanking('runiv','desc')">⭐ Top vs All Stocks</button>
        <button class="rank-sort-btn" onclick="sortByRanking('score','desc')">🎯 Highest Score</button>
        <button class="rank-sort-btn" onclick="sortByRanking('donchd','desc')">📈 Best D-Donch</button>
        <button class="rank-sort-btn" onclick="sortByRanking('donchw','desc')">📈 Best W-Donch</button>
        <button class="rank-sort-btn" onclick="sortByRanking('donchm','desc')">📈 Best M-Donch</button>
        <button class="rank-sort-btn" onclick="sortByRanking('rn50','asc')">📉 Lowest vs N50</button>
        <button class="rank-sort-btn" onclick="sortByRanking('runiv','asc')">📉 Lowest vs All</button>
        <div id="chips" class="active-chips"></div>
      </div>
    </div>"""

    sum_table = build_summary_table()   # skeleton only — JS fills tbody

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>RSI MTF Breakout Report — {run_ts}</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css"
        integrity="sha384-QWTKZyjpPEjISv5WaRU9OFeRpok6YctnYmDr5pNlyT2bRjXh0JMhjY6hW+ALEwIH"
        crossorigin="anonymous">
  <style>{_CSS}</style>
</head>
<body>
  <div class="app-header">
    <h1>📈 RSI Multi-Timeframe Breakout Report <small style="font-size:13px;color:var(--sub);font-weight:400">v4.0</small></h1>
    <div class="subtitle">
      NSE EQ Universe &nbsp;|&nbsp; {run_ts} IST &nbsp;|&nbsp;
      RSI(14) D/W/M + MACD(12,26) + CCI(20) &nbsp;|&nbsp;
      Ranked vs Nifty50 &amp; All NSE &nbsp;|&nbsp;
      {len(chart_data)} charts · virtual render (no browser hang)
    </div>
    {stat_boxes}
  </div>

  {filter_bar}

  <div class="table-section">
    {sum_table}
  </div>

  <div class="cards-section">
    <h2>🔍 DETAILED ANALYSIS — {total} stocks &nbsp;
      <span style="font-weight:400;font-size:11px">(click ▶ to expand · {PAGE_CARDS} cards loaded at a time)</span>
    </h2>
    <div id="cards-container"></div>
  </div>

  <div class="footer">
    RSI MTF Report v4.0 &nbsp;|&nbsp; {run_ts} &nbsp;|&nbsp;
    <b>Not financial advice.</b><br>
    Entry: RSI D+W+M &gt; SMA + CCI&gt;0 + MACD&gt;Signal &nbsp;|&nbsp;
    SL: 2×ATR or swing low &nbsp;|&nbsp;
    Exit: RSI crosses below SMA or CCI &lt; −100
  </div>

  <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"
          integrity="sha384-YvpcrYf0tY3lHB60NNkmXc4s9bIOgUxi8T/jzmFG8RMbAIg1M5DW9Vk3f67vZkY"
          crossorigin="anonymous"></script>
  <script>
const STOCKS={stocks_json};
const CHART_DIR={json.dumps(CHART_OUTPUT_DIR)};
const PAGE_TBL={PAGE_TBL};
const PAGE_CARDS={PAGE_CARDS};
{_JS}
  </script>
</body>
</html>"""


# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════

def main(force_charts: bool = False):
    os.system("cls" if os.name == "nt" else "clear")
    print("╔══════════════════════════════════════════════════════════════════╗")
    print("║  📈  RSI MTF BREAKOUT REPORT  v4.0                               ║")
    print("║      Smart Cache · Batch Download · Virtual HTML · No Hang       ║")
    print("╚══════════════════════════════════════════════════════════════════╝")
    print(f"   {RUN_TS} IST  |  Errors → {ERROR_LOG}\n")
    print(f"   Start time : {START_TS} IST\n")
    if force_charts:
        print("   Force chart regeneration enabled — cache will be bypassed for all charts.\n")
    log_info(f"=== Scan started {START_TS} ===")

    # ── Step 1: Universe ──────────────────────────────────────────
    print("▶  STEP 1/4  Build universe")
    tickers = load_universe()
    print(f"   {len(tickers)} stocks loaded  |  {len(_COMPANY_MAP)} company names\n")
    log_info(f"Universe: {len(tickers)} stocks, {len(_COMPANY_MAP)} company names")

    # ── Step 2: Smart incremental cache update ────────────────────
    print("▶  STEP 2/4  Smart cache update (incremental download)")
    print("   ─────────────────────────────────────────────────────────")
    dl_stats = prefetch_all(tickers)
    log_info(f"Download: fresh={dl_stats['fresh']} updated={dl_stats['stale_updated']} "
             f"new={dl_stats['new_downloaded']} failed={dl_stats['failed']}")

    # ── Step 3: Analyse (all data comes from cache — no downloads) ─
    print("▶  STEP 3/4  Analyse (Daily · Weekly · Monthly indicators)")
    print("   ─────────────────────────────────────────────────────────")
    results, errors = [], 0
    t0, total = time.time(), len(tickers)

    for i, ticker in enumerate(tickers, 1):
        pct  = i / total * 100
        fill = int(pct / 2)
        elapsed = time.time() - t0
        current_time = datetime.now().strftime("%H:%M:%S")
        sys.stdout.write(
            f"\r  [{'█'*fill}{'░'*(50-fill)}] {pct:5.1f}%  {i:>4}/{total}  "
            f"{ticker:<14}  ok={len(results)}  err={errors}  "
            f"{current_time}  {format_timespan(elapsed)}"
        )
        sys.stdout.flush()

        res = analyze_stock(ticker)
        if res:
            results.append(res)
        else:
            errors += 1

    elapsed = time.time() - t0
    print(f"\n\n   ✓ {len(results)} ok  |  {errors} failed  |  {elapsed:.0f}s")
    log_info(f"Analysis done: {len(results)} ok, {errors} failed, {elapsed:.0f}s")

    if not results:
        print("  ⚠️  No results produced — generating diagnostic HTML report.")
        log_info("No results produced during scan; building empty HTML report.")

    # ── Rankings ──────────────────────────────────────────────────
    print("   Computing rankings vs Nifty50 and full universe...")
    results = compute_rankings(results)
    results.sort(key=lambda d: (d["rank_universe"], d["score"]), reverse=True)

    n50_in_scan = sum(1 for d in results if d["is_nifty50"])
    sme_in_scan = sum(1 for d in results if d["is_sme"])
    print(f"   Nifty50 stocks in scan: {n50_in_scan}/{len(NIFTY50)}")
    print(f"   SME stocks in scan    : {sme_in_scan}  "
          f"(of {len(_SME_STOCKS)} in SME universe)\n")

    # ── Step 4: HTML ──────────────────────────────────────────────
    print("▶  STEP 4/4  Generate charts + build HTML")
    print("   ─────────────────────────────────────────────────────────")

    chart_candidates = results if MAX_CHART_STOCKS <= 0 else results[:MAX_CHART_STOCKS]
    chart_tickers = [d["ticker"] for d in chart_candidates]
    chart_data    = {}
    os.makedirs(CHART_OUTPUT_DIR, exist_ok=True)
    print(f"   Generating charts for {'all' if MAX_CHART_STOCKS <= 0 else 'top'} {len(chart_tickers)} stocks...")

    meta = _load_chart_cache_meta()
    stale = []
    cached = 0
    for d in chart_candidates:
        ticker = d["ticker"]
        chart_path = os.path.join(CHART_OUTPUT_DIR, f"{ticker}.png").replace("\\", "/")
        chart_hash = _compute_chart_hash(d)
        if not force_charts and chart_hash and meta.get(ticker, {}).get("hash") == chart_hash and os.path.exists(chart_path):
            chart_data[ticker] = chart_path
            cached += 1
            continue
        stale.append((d, chart_hash, chart_path))

    print(f"   Reusing {cached}/{len(chart_tickers)} cached charts")

    if stale:
        workers = min(CHART_WORKERS, len(stale))
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_generate_chart_worker, d): (d["ticker"], hash_val, path)
                       for d, hash_val, path in stale}
            completed = 0
            for future in as_completed(futures):
                ticker, chart_hash, chart_path = futures[future]
                completed += 1
                try:
                    _, generated_path = future.result()
                except Exception as exc:
                    generated_path = ""
                    log_error(ticker, get_company_name(ticker), "CHART-PARALLEL", exc)
                if generated_path:
                    chart_data[ticker] = generated_path
                    meta[ticker] = {"hash": chart_hash, "updated_at": datetime.now().strftime("%d %b %Y %H:%M")}
                elif os.path.exists(chart_path):
                    chart_data[ticker] = chart_path
                else:
                    print(f"\n   ⚠️ Chart failed for {ticker} — see {ERROR_LOG}")
                sys.stdout.write(f"\r   Charts completed: {completed}/{len(stale)}")
                sys.stdout.flush()
        print()
        _save_chart_cache_meta(meta)
    else:
        print("   No charts to generate.")

    print(f"   {len(chart_data)}/{len(chart_tickers)} charts generated")

    # Strip raw series before building HTML
    all_light = [{k: v for k, v in d.items() if not k.startswith("_")} for d in results]

    # Terminal summary
    print(f"\n   Phase summary:")
    print(f"     Uptrend : {sum(1 for d in results if d['phase']=='UPTREND')}")
    print(f"     Sideways: {sum(1 for d in results if d['phase']=='SIDEWAYS')}")
    print(f"     Bearish : {sum(1 for d in results if d['phase']=='BEARISH')}")
    print(f"   Fresh breakouts (Daily): {sum(1 for d in results if d['fresh_d'])}")
    print(f"   Fresh breakouts (Weekly): {sum(1 for d in results if d['fresh_w'])}\n")

    html = build_html_report(all_light, chart_data, RUN_TS, len(results))

    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)

    size_mb = os.path.getsize(OUTPUT_HTML) / 1024 / 1024
    total_elapsed = time.time() - START_TIME
    log_info(f"HTML saved: {OUTPUT_HTML} ({size_mb:.1f} MB)")
    print(f"  ✅ HTML saved : {OUTPUT_HTML}  ({size_mb:.1f} MB)")
    print(f"  📋 Error log  : {ERROR_LOG}  ({errors} entries)")
    print(f"  Open HTML in any browser — cards start collapsed, charts lazy-load on expand\n")
    print(f"  Total runtime: {format_timespan(total_elapsed)}\n")

    print("  ─────────────────────────────────────────────────────────")
    print("  STRATEGY  ENTRY : RSI(14) D+W+M > SMA + CCI>0 + MACD>Signal")
    print("            SL    : 2×ATR(14) OR 1% below last swing low")
    print("            TARGET: Fib Extension from swing low→high (uptrend)")
    print("            EXIT  : RSI(14) daily crosses below SMA(14)")
    print("  ─────────────────────────────────────────────────────────\n")
    log_info(f"=== Run complete ===")


def parse_args():
    parser = argparse.ArgumentParser(description='RSI MTF report generator')
    parser.add_argument('-help', action='help',
                        help='Show this help message and exit.')
    parser.add_argument('--force-charts', action='store_true',
                        help='Always regenerate chart images, ignoring cached chart hashes.')
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    main(force_charts=args.force_charts)
