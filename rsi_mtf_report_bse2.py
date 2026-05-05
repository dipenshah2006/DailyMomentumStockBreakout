# ═════════════════════════════════════════════════════════════════════════════
# RSI MULTI-TIMEFRAME BREAKOUT HTML REPORT  v2.0 - BSE VERSION
# ═════════════════════════════════════════════════════════════════════════════

"""
╔═════════════════════════════════════════════════════════════════════════════╗
║   RSI MULTI-TIMEFRAME BREAKOUT HTML REPORT  v2.0 - BSE VERSION              ║
║   Daily · Weekly · Monthly RSI/SMA Crossover | Phase | Entry/Exit           ║
║   NEW v2.0:                                                                  ║
║    • Exception logging → error_log.txt  (ticker + company + full traceback) ║
║    • Ranking vs Sensex30  (relative strength percentile)                     ║
║    • Ranking vs all BSE stocks  (universe percentile)                       ║
║    • Lightweight HTML — charts lazy-loaded on expand, never hangs browser   ║
║    • Native <details> expand/collapse — no JS needed, instant               ║
╚═════════════════════════════════════════════════════════════════════════════╝

INSTALL:  pip install yfinance pandas numpy matplotlib requests openpyxl
RUN:      python rsi_mtf_report_bse.py
OUTPUTS:  rsi_mtf_report_bse_YYYYMMDD_HHMM.html  +  error_log_bse_YYYYMMDD_HHMM.txt
"""

# ═════════════════════════════════════════════════════════════════════════════
# USER CONFIG
# ═════════════════════════════════════════════════════════════════════════════

LOCAL_BSE_CSV       = "india/BSE/BSEcash/BSE_EQ_SCRIP_02012025.csv"
BSE_CSV_URL         = "https://www.bseindia.com/download/bhavcopy/eq_security_master.zip"
SERIES_FILTER       = ["A"]       # BSE equity series (A = cash equities)

DATA_PERIOD         = "max"
MIN_CANDLES         = 80
MAX_CHART_STOCKS    = 0         # 0 = generate charts for all stocks; otherwise top N stocks
CHART_OUTPUT_DIR    = "charts_bse"   # folder for generated PNG chart files
CHART_BARS          = 120       # bars per chart (fewer = smaller PNG)
CHART_DPI           = 72        # lower DPI = smaller file, still readable
FORCE_REBUILD_CHART = False     # set True to regenerate all PNGs even if files exist
CHART_WORKERS       = None   # resolved after os import below

FRESH_DAYS_D        = 3
FRESH_WEEKS_W       = 2

RSI_P               = 14
RSI_SMA_P           = 14
CCI_P               = 20
MACD_F, MACD_S, MACD_SIG_P = 12, 26, 9
ATR_P               = 14

BATCH_SIZE          = 25
BATCH_PAUSE         = 1.0

SCORE_STRONG_BUY    = 16
SCORE_BUY           = 12
SCORE_WATCH         = 8

# Sensex 30 tickers (used for ranking vs index - BSE versions)
SENSEX30 = [
    "RELIANCE","TCS","HDFCBANK","INFY","ICICIBANK","HINDUNILVR","ITC","SBIN",
    "BAJFINANCE","BHARTIARTL","KOTAKBANK","LT","AXISBANK","ASIANPAINT","MARUTI",
    "SUNPHARMA","TITAN","WIPRO","ULTRACEMCO","NTPC","POWERGRID","ONGC","JSWSTEEL",
    "TATASTEEL","COALINDIA","TECHM","HCLTECH","DRREDDY","CIPLA","DIVISLAB",
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

install_missing_packages()

# ═════════════════════════════════════════════════════════════════════════════
# IMPORTS
# ═════════════════════════════════════════════════════════════════════════════

import csv
import io
import logging
import os
import pickle
import sys
import time
import traceback
import warnings
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

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

# Fix Windows UTF-8 encoding issue
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)

# Resolved here — os is now imported
CHART_WORKERS = min(8, max(1, (os.cpu_count() or 4)))

RUN_TS      = datetime.now().strftime("%d %b %Y  %H:%M")
_STAMP      = datetime.now().strftime("%d%m%Y_%H%M")
OUTPUT_HTML = f"rsi_mtf_report_bse_{_STAMP}.html"
ERROR_LOG   = f"error_log_bse_{_STAMP}.txt"
CACHE_FILE  = "stock_data_cache_bse.pkl"

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 0 — ERROR LOGGER
# ═════════════════════════════════════════════════════════════════════════════

# Configure a dedicated file logger — separate from print output
_logger = logging.getLogger("rsi_scanner_bse")
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
        f"TICKER={ticker!r:15s} | COMPANY={company!r:35s} | STAGE={stage}\n"
        f"  ERROR : {type(exc).__name__}: {exc}\n"
        f"  TRACE :\n{tb}"
    )

def log_info(msg: str):
    _logger.info(msg)

def log_warn(ticker: str, company: str, msg: str):
    _logger.warning(f"TICKER={ticker!r:15s} | COMPANY={company!r:35s} | {msg}")


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 1 — COMPANY NAME LOOKUP
# ═════════════════════════════════════════════════════════════════════════════

_COMPANY_MAP: dict[str, str] = {}   # populated by load_universe()
_LISTING_DATE_MAP: dict[str, str] = {}   # symbol → listing date string

def get_company_name(ticker: str) -> str:
    return _COMPANY_MAP.get(ticker, ticker)

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
    """Parse BSE CSV and build symbol → company name and listing date dicts."""
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        sym  = row.get("TckrSymb", "").strip().rstrip("#")  # strip # to match cleaned tickers
        name = row.get("FinInstrmNm", "").strip()  # BSE uses FinInstrmNm
        # Handle listing date column
        date_str = row.get("ListgDt", "").strip()  # BSE uses ListgDt
        if sym:
            _COMPANY_MAP[sym] = name or sym
            if date_str:
                _LISTING_DATE_MAP[sym] = date_str


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 2 — CACHE
# ═════════════════════════════════════════════════════════════════════════════

def _load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "rb") as f:
                return pickle.load(f)
        except Exception as e:
            print(f"  [!] Cache load error: {e}")
    return {}

def _save_cache(cache: dict):
    try:
        with open(CACHE_FILE, "wb") as f:
            pickle.dump(cache, f)
    except Exception as e:
        print(f"  [!] Cache save error: {e}")

_CACHE: dict = {}   # module-level cache, loaded once

def _get_df(ticker: str):
    return _CACHE.get(ticker)

def _set_df(ticker: str, df):
    _CACHE[ticker] = df
    _save_cache(_CACHE)


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

    rank_sensex30    — percentile (0-100) of this stock's score vs the
                      subset of Sensex30 stocks that were successfully analysed.
                      100 = best among Sensex30, 0 = worst.

    rank_universe   — percentile (0-100) of this stock's score vs ALL
                      scanned stocks.  100 = top 1% of entire universe.

    Also adds:
    rank_sensex30_pos  — integer rank  (1 = best Sensex30 stock)
    rank_sensex30_of   — total Sensex30 stocks in scan
    rank_univ_pos     — integer rank in full universe
    rank_univ_of      — total stocks in universe
    """
    s30_set = set(SENSEX30)

    # ── All scores ─────────────────────────────────────────────
    all_scores  = [d["score"] for d in results]
    s30_results = [d for d in results if d["ticker"] in s30_set]
    s30_scores  = [d["score"] for d in s30_results]

    def pct_rank(score, score_list):
        """Percentile rank: what % of scores are ≤ this score."""
        if not score_list:
            return 0
        below = sum(1 for s in score_list if s <= score)
        return round(below / len(score_list) * 100, 1)

    # Sort for integer rank (1 = highest score)
    sorted_all = sorted(results, key=lambda d: d["score"], reverse=True)
    sorted_s30 = sorted(s30_results,  key=lambda d: d["score"], reverse=True)

    rank_all_map = {d["ticker"]: i + 1 for i, d in enumerate(sorted_all)}
    rank_s30_map = {d["ticker"]: i + 1 for i, d in enumerate(sorted_s30)}

    for d in results:
        d["rank_sensex30"]     = pct_rank(d["score"], s30_scores)
        d["rank_universe"]    = pct_rank(d["score"], all_scores)
        d["rank_sensex30_pos"] = rank_s30_map.get(d["ticker"], 0)
        d["rank_sensex30_of"]  = len(s30_results)
        d["rank_univ_pos"]    = rank_all_map.get(d["ticker"], 0)
        d["rank_univ_of"]     = len(results)
        d["is_sensex30"]      = d["ticker"] in s30_set

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
# SECTION 8 — BSE UNIVERSE LOADER
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

def _parse_bse_csv(text: str) -> list[str]:
    _build_company_map(text)
    reader = csv.DictReader(io.StringIO(text))
    tickers, seen = [], set()
    for row in reader:
        series = row.get("SctySrs", "").strip()
        symbol = row.get("TckrSymb", "").strip()
        # Strip trailing '#' — BSE appends it for ex-dividend / corporate action
        # markers; yfinance does NOT recognise "HINDALCO#.BO", only "HINDALCO.BO"
        symbol = symbol.rstrip("#")
        if symbol and series in SERIES_FILTER and symbol not in seen:
            tickers.append(symbol)
            seen.add(symbol)
    return tickers

def _download_bse_master() -> str | None:
    """Download BSE security master from alternative sources."""
    urls = [
        "https://www.bseindia.com/download/bhavcopy/eq_security_master.zip",
        "https://www.bseindia.com/download/bhavcopy/eq_isin_master.zip",
    ]

    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})

    for url in urls:
        try:
            print(f"  ⏳ Downloading from {url}...")
            r = s.get(url, timeout=30)
            r.raise_for_status()

            # Try to extract CSV from ZIP
            try:
                z = zipfile.ZipFile(io.BytesIO(r.content))
                # Look for CSV files in the zip
                for name in z.namelist():
                    if name.endswith('.csv'):
                        return z.read(name).decode('utf-8', errors='replace')
            except:
                # If not a ZIP, might be direct CSV
                if 'TckrSymb' in r.text or 'FinInstrmNm' in r.text:
                    return r.text

        except Exception as e:
            print(f"    [!] {url}: {e}")
            continue

    return None

def load_universe() -> list[str]:
    global _CACHE
    _CACHE = _load_cache()

    if os.path.exists(LOCAL_BSE_CSV):
        try:
            with open(LOCAL_BSE_CSV, encoding="utf-8", errors="replace") as f:
                raw = f.read()
            t = _parse_bse_csv(raw)
            if t:
                print(f"  ✅ Local '{LOCAL_BSE_CSV}': {len(t)} EQ stocks | "
                      f"{len(_COMPANY_MAP)} companies mapped")
                return t
        except Exception as e:
            print(f"  [!] Local CSV error: {e}")

    try:
        s = requests.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "*/*"})
        s.get("https://www.bseindia.com/", timeout=12)
        time.sleep(1.5)
        s.headers["Referer"] = "https://www.bseindia.com/"
        r = s.get(BSE_CSV_URL, timeout=20)
        r.raise_for_status()
        t = _parse_bse_csv(r.text)
        if t:
            print(f"  ✅ Live BSE: {len(t)} EQ stocks | {len(_COMPANY_MAP)} companies")
            try:
                with open(LOCAL_BSE_CSV, "w", encoding="utf-8") as f:
                    f.write(r.text)
                print(f"  💾 Saved → '{LOCAL_BSE_CSV}'")
            except Exception:
                pass
            return t
    except Exception as e:
        print(f"  [!] Primary BSE download failed: {e}")

    # Try alternative BSE download
    csv_data = _download_bse_master()
    if csv_data:
        t = _parse_bse_csv(csv_data)
        if t:
            print(f"  ✅ Alternative BSE source: {len(t)} EQ stocks | {len(_COMPANY_MAP)} companies")
            try:
                with open(LOCAL_BSE_CSV, "w", encoding="utf-8") as f:
                    f.write(csv_data)
                print(f"  💾 Saved → '{LOCAL_BSE_CSV}'")
            except Exception:
                pass
            return t

    print(f"  ⚠️  Using built-in list: {len(BUILTIN)} stocks")
    return list(dict.fromkeys(BUILTIN))


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 9 — PER-STOCK ANALYSIS  (with full error logging)
# ═════════════════════════════════════════════════════════════════════════════

def _normalize_bse_ticker(ticker: str) -> str:
    """Normalise BSE symbols for Yahoo Finance lookup."""
    return ticker.strip().upper().rstrip("#").replace("\u200b", "").replace(" ", "")

def _build_yf_candidates(ticker: str) -> list[str]:
    base = _normalize_bse_ticker(ticker)
    candidates = [
        f"{base}.BO",
        f"{base}.NS",
        base,
    ]
    if "-" in base:
        clean = base.replace("-", "")
        candidates.extend([f"{clean}.BO", f"{clean}.NS"])
    if "." in base:
        clean = base.replace(".", "")
        candidates.extend([f"{clean}.BO", f"{clean}.NS"])
    # Deduplicate while preserving order
    seen = set()
    unique_candidates = []
    for symbol in candidates:
        if symbol not in seen:
            seen.add(symbol)
            unique_candidates.append(symbol)
    return unique_candidates


def _fetch_yf(symbol: str) -> pd.DataFrame:
    """Download from yfinance and normalise MultiIndex columns."""
    df = yf.download(symbol, period=DATA_PERIOD, interval="1d",
                     progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.droplevel(1)
    return df.dropna()


def _download_with_fallback(ticker: str, min_candles: int, company: str) -> pd.DataFrame | None:
    """
    Try a series of Yahoo Finance ticker forms until we get enough bars.
    Returns a DataFrame or None.
    """
    candidates = _build_yf_candidates(ticker)
    for symbol in candidates:
        for attempt in range(2):
            try:
                df = _fetch_yf(symbol)
                if len(df) >= min_candles:
                    log_info(f"{ticker}: using {symbol} ({len(df)} bars)")
                    return df
                if len(df) > 0:
                    log_warn(ticker, company,
                             f"{symbol} returned {len(df)} bars < {min_candles} required")
                    break
                if attempt == 0:
                    time.sleep(1.5)
            except Exception as exc:
                log_warn(ticker, company, f"{symbol} attempt {attempt+1} failed: {exc}")
                if attempt == 0:
                    time.sleep(1.5)
    return None


def analyze_stock(ticker: str) -> dict | None:
    company = get_company_name(ticker)
    min_candles = get_min_candles_required(ticker)

    # ── Stage A: Data download ────────────────────────────────────
    try:
        df = _get_df(ticker)
        if df is None:
            df = _download_with_fallback(ticker, min_candles, company)
            if df is not None and len(df) >= min_candles:
                _set_df(ticker, df)
        else:
            df = df.dropna()

        if df is None or len(df) < min_candles:
            log_warn(ticker, company,
                     f"Insufficient data: {len(df) if df is not None else 0} bars < {min_candles} required")
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
        "rank_sensex30": 0, "rank_universe": 0,
        "rank_sensex30_pos": 0, "rank_sensex30_of": 0,
        "rank_univ_pos": 0,    "rank_univ_of": 0,
        "is_sensex30": False,
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
            f"|  {data['phase']}  |  {data['signal']}  |  Score {data['score']}/22  "            f"|  Univ rank #{data['rank_univ_pos']}/{data['rank_univ_of']}",
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
        os.makedirs(CHART_OUTPUT_DIR, exist_ok=True)
        chart_path = os.path.join(CHART_OUTPUT_DIR, f"{ticker}.png")
        if not FORCE_REBUILD_CHART and os.path.exists(chart_path):
            plt.close(fig)
            return chart_path.replace("\\", "/")
        fig.savefig(chart_path, format="png", dpi=CHART_DPI, bbox_inches="tight", facecolor=BG)
        plt.close(fig)
        return chart_path.replace("\\", "/")

    except Exception as exc:
        log_error(ticker, company, "CHART", exc)
        plt.close("all")
        return ""


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 11 — HTML REPORT
# ═════════════════════════════════════════════════════════════════════════════

_CSS = """
:root{--bg:#0d1117;--card:#161b22;--border:#30363d;--text:#c9d1d9;
      --sub:#8b949e;--green:#26d07c;--red:#ff4d6d;--gold:#ffd700;
      --cyan:#00d4ff;--purple:#b39ddb;--orange:#ff9800}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;font-size:13px}
a{color:var(--cyan)}

/* header */
.header{background:#010409;border-bottom:2px solid #21262d;padding:20px 28px 16px}
.header h1{font-size:20px;font-weight:700;color:var(--cyan);letter-spacing:1px}
.subtitle{color:var(--sub);font-size:12px;margin-top:4px}
.stats-row{display:flex;gap:16px;margin-top:12px;flex-wrap:wrap}
.stat-box{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:9px 16px;min-width:110px}
.stat-box .val{font-size:22px;font-weight:700}
.stat-box .lbl{font-size:10px;color:var(--sub);margin-top:2px}
.stat-box.green .val{color:var(--green)}.stat-box.gold .val{color:var(--gold)}
.stat-box.red .val{color:var(--red)}.stat-box.cyan .val{color:var(--cyan)}

/* filter bar */
.filter-bar{background:#010409;padding:10px 28px;border-bottom:1px solid var(--border);
            display:flex;gap:8px;flex-wrap:wrap;position:sticky;top:0;z-index:100}
.filter-btn{background:var(--card);border:1px solid var(--border);color:var(--sub);
            border-radius:20px;padding:5px 14px;cursor:pointer;font-size:12px;transition:all .15s}
.filter-btn:hover,.filter-btn.active{background:var(--cyan);color:#000;border-color:var(--cyan);font-weight:600}

/* table */
.table-wrap{overflow-x:auto;padding:20px 28px 6px}
.sum-table{width:100%;border-collapse:collapse;font-size:11.5px}
.sum-table th{background:#21262d;color:var(--sub);padding:7px 9px;text-align:left;
              font-weight:600;white-space:nowrap;position:sticky;top:0}
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
.fresh-tag{background:#002d40;color:var(--cyan);border-radius:8px;padding:1px 7px;
           font-size:10px;font-weight:700;border:1px solid #00d4ff44}
.s30-tag{background:#1a0d30;color:var(--purple);border-radius:8px;padding:1px 7px;
         font-size:10px;font-weight:700;border:1px solid #b39ddb44}

/* ── CARDS — native <details> expand/collapse ──────── */
.cards-section{padding:14px 28px 36px}
.cards-section>h2{font-size:13px;color:var(--sub);margin-bottom:12px;letter-spacing:1px}

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

/* footer */
.footer{text-align:center;padding:18px;color:var(--sub);
        font-size:11px;border-top:1px solid var(--border)}

/* sort hint */
.sort-hint{padding:5px 28px 3px;font-size:11px;color:var(--sub)}
"""

# Lazy chart JS: PNGs are loaded from data-src on first open
_JS = """
// ── Filter ───────────────────────────────────────────────────────
function filterPhase(phase, btn) {
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  const isAll = phase === 'all';
  document.querySelectorAll('details.stock-card').forEach(c => {
    const show = isAll || c.dataset.phase === phase ||
                 (phase === 'fresh' && c.dataset.fresh === '1') ||
                 (phase === 'sensex30' && c.dataset.sensex === '1');
    c.style.display = show ? '' : 'none';
  });
  document.querySelectorAll('.sum-row').forEach(r => {
    const show = isAll || r.dataset.phase === phase ||
                 (phase === 'fresh'  && r.dataset.fresh  === '1') ||
                 (phase === 'sensex30'&& r.dataset.sensex  === '1');
    r.style.display = show ? '' : 'none';
  });
}

// ── Multi-column sort ────────────────────────────────────────────
let sortKeys = [];
function sortTable(col, e) {
  const shift = e && e.shiftKey;
  if (!shift) {
    const ex = sortKeys.find(k => k.col === col);
    const nd = (ex && sortKeys[0].col === col && ex.dir === 'desc') ? 'asc' : 'desc';
    sortKeys = [{col, dir: nd}];
  } else {
    const idx = sortKeys.findIndex(k => k.col === col);
    if (idx === -1) { if (sortKeys.length < 3) sortKeys.push({col, dir:'desc'}); }
    else if (sortKeys[idx].dir === 'desc') sortKeys[idx].dir = 'asc';
    else sortKeys.splice(idx, 1);
  }
  document.querySelectorAll('#sumtable th[data-col]').forEach(th => {
    const ki = sortKeys.findIndex(k => k.col === th.dataset.col);
    const si = th.querySelector('.sort-ind');
    if (ki === -1) { si.textContent = '↕'; th.style.color = ''; }
    else {
      const arrow = sortKeys[ki].dir === 'desc' ? '▼' : '▲';
      si.innerHTML = arrow + (sortKeys.length > 1 ? `<sup style="font-size:8px">${ki+1}</sup>` : '');
      th.style.color = 'var(--cyan)';
    }
  });
  const tbl   = document.getElementById('sumtable');
  const rows  = Array.from(tbl.querySelectorAll('tr.sum-row'));
  rows.sort((a, b) => {
    for (const {col:c, dir:d} of sortKeys) {
      const av = parseFloat(a.dataset[c]) || 0, bv = parseFloat(b.dataset[c]) || 0;
      if (av !== bv) return d === 'desc' ? bv - av : av - bv;
    }
    return 0;
  });
  const tbody = tbl.querySelector('tbody');
  rows.forEach(r => tbody.appendChild(r));
}

// ── Lazy chart loading ──────────────────────────────────────────
// Chart PNGs are loaded on first open from data-src
function loadChartImage(card) {
  const img = card.querySelector('img.lazy-chart');
  if (!img) return;
  const src = img.dataset.src;
  if (!src) {
    img.parentElement.innerHTML =
      '<div class="chart-placeholder">📊 Chart not available for this stock</div>';
    return;
  }
  const placeholder = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==';
  if (!img.src || img.src === placeholder) {
    img.src = src;
  }
}
document.addEventListener('toggle', e => {
  if (!e.target.classList?.contains('stock-card')) return;
  if (!e.target.open) return;
  loadChartImage(e.target);
}, true);  // capture phase so toggle fires before paint

document.addEventListener('click', e => {
  const summary = e.target.closest('details.stock-card>summary');
  if (!summary) return;
  loadChartImage(summary.parentElement);
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
    return f'<span class="rank-pill {cls}">#{pos}/{of} ({pct:.0f}%ile)</span>'


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
    <div class="trade-row"><span class="tl">vs Sensex30</span>
      <span class="tv">{_rank_pill(d['rank_sensex30'], d['rank_sensex30_pos'], d['rank_sensex30_of'])}</span></div>
    <div class="trade-row"><span class="tl">vs All BSE</span>
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

    return f"""<div class="card-details">
      {dp("📊 RSI · Daily · Weekly · Monthly", rsi_html, True)}
      {dp("🎯 CCI(20) · D · W · M",            cci_html)}
      {dp("📈 MACD(12,26) · D · W · M",         macd_html)}
      {dp("💼 Entry / Stop Loss / Exit",         trade_html, True)}
      {dp("🏆 Rankings",                         rank_html, True)}
      {dp(fib_lbl,                               fib_html)}
      {dp("⚡ Active Signals",                   sigs_html)}
      <details class="detail-panel" style="grid-column:1/-1">
        <summary>📅 Historical RSI Crossover Signals — recent first</summary>
        <div class="detail-content">{hist_html}</div>
      </details>
    </div>"""


def build_summary_table(results: list[dict]) -> str:
    rows = ""
    for d in results:
        fr_tag  = ' <span class="fresh-tag">FRESH</span>'  if (d["fresh_d"] or d["fresh_w"]) else ""
        s30_tag = ' <span class="s30-tag">S30</span>'       if d["is_sensex30"]               else ""
        above_d = d["rsi_d"] > d["sma_d"]
        rsi_col = "var(--green)" if above_d else "var(--red)"
        m_col   = "var(--green)" if d["macd_l"] > 0 else "var(--red)"
        d52_col = ("var(--red)"   if d["dist52"] < -10 else
                   "var(--green)" if d["dist52"] > -5  else "")
        s30_pct  = d["rank_sensex30"]
        univ_pct = d["rank_universe"]
        s30_cls  = "rank-top" if s30_pct >= 70 else "rank-mid" if s30_pct >= 40 else "rank-low"
        uv_cls   = "rank-top" if univ_pct >= 70 else "rank-mid" if univ_pct >= 40 else "rank-low"

        rows += f"""
        <tr class="sum-row"
            data-phase="{d['phase']}"
            data-fresh="{'1' if (d['fresh_d'] or d['fresh_w']) else '0'}"
            data-sensex="{'1' if d['is_sensex30'] else '0'}"
            data-score="{d['score']}"
            data-rsid="{d['rsi_d']}" data-rsiw="{d['rsi_w']}" data-rsim="{d['rsi_m']}"
            data-cci="{d['cci']}"    data-macd="{d['macd_l']}"
            data-close="{d['close']}" data-dist52="{d['dist52']}"
            data-rs30="{d['rank_sensex30']}" data-runiv="{d['rank_universe']}">
          <td><b style="color:var(--cyan)">{d['ticker']}</b>{fr_tag}{s30_tag}
              <div style="font-size:10px;color:var(--sub)">{d['company'][:28]}</div></td>
          <td>{_phase_badge(d['phase'])}</td>
          <td>{_sig_span(d['signal'], d['sig_cls'])}</td>
          <td style="text-align:right"><b>{d['score']}</b>/22</td>
          <td style="text-align:right">
            <div class="rsi-stack">
              <span class="rv" style="color:{rsi_col}">{d['rsi_d']} {"▲" if above_d else "▼"}</span>
              <span class="sv">SMA {d['sma_d']}</span>
            </div>
          </td>
          <td style="text-align:right">{d['rsi_w']}</td>
          <td style="text-align:right">{d['rsi_m']}</td>
          <td style="text-align:right">{d['cci']}</td>
          <td style="text-align:right;color:{m_col}">{d['macd_l']:.3f}</td>
          <td style="text-align:right">₹{d['close']:,.2f}</td>
          <td style="text-align:right;color:{d52_col}">{d['dist52']}%</td>
          <td style="text-align:right"><span class="rank-pill {s30_cls}">{d['rank_sensex30_pos']}/{d['rank_sensex30_of']} ({s30_pct:.0f}%)</span></td>
          <td style="text-align:right"><span class="rank-pill {uv_cls}">{d['rank_univ_pos']}/{d['rank_univ_of']} ({univ_pct:.0f}%)</span></td>
        </tr>"""

    def th(lbl, col, align="right"):
        return (f'<th data-col="{col}" onclick="sortTable(\'{col}\',event)" '
                f'style="text-align:{align}">{lbl} <span class="sort-ind">↕</span></th>')

    return f"""
    <div class="sort-hint">
      💡 Click header to sort &nbsp;|&nbsp; <b>Shift+click</b> = add 2nd/3rd sort key &nbsp;|&nbsp;
      Click again to toggle ▲▼ &nbsp;|&nbsp; 3rd click on secondary key removes it
    </div>
    <div class="table-wrap">
      <table class="sum-table table table-sm" id="sumtable">
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
          {th('vs S30',   'rs30')}
          {th('vs All',   'runiv')}
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>"""


def build_stock_card(d: dict, has_chart: bool) -> str:
    """
    Build a <details> card. Chart img is always present but src="" (lazy).
    JS fills it from data-src on first open.
    If has_chart=False, show placeholder instead.
    """
    fr_tags = ""
    if d["fresh_d"]: fr_tags += f' <span class="fresh-tag">🚀 Daily ({d["fresh_d_bars"]}d)</span>'
    if d["fresh_w"]: fr_tags += f' <span class="fresh-tag">📅 Weekly ({d["fresh_w_bars"]}w)</span>'
    s30_tag = ' <span class="s30-tag">SENSEX30</span>' if d["is_sensex30"] else ""

    s30_rank  = _rank_pill(d["rank_sensex30"], d["rank_sensex30_pos"], d["rank_sensex30_of"])
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
<div class="col">
  <details class="stock-card card h-100" data-phase="{d['phase']}"
           data-fresh="{'1' if (d['fresh_d'] or d['fresh_w']) else '0'}"
           data-sensex="{'1' if d['is_sensex30'] else '0'}">
    <summary class="card-header d-flex flex-wrap align-items-center gap-2">
      <span class="card-arrow">▶</span>
      <span class="card-ticker fw-bold">{d['ticker']}</span>
      <span class="card-price text-success">₹{d['close']:,.2f}</span>
      {_phase_badge(d['phase'])}
      <span class="{d['sig_cls']} fw-semibold">{d['signal']}</span>
      <span class="card-score badge rounded-pill bg-warning text-dark">Score {d['score']}/22</span>
      {fr_tags}{s30_tag}
      <span class="ms-auto text-muted small text-end">
        D {d['rsi_d']} W {d['rsi_w']} M {d['rsi_m']} RSI<br>
        S30: {s30_rank} | All: {univ_rank}
      </span>
    </summary>
    <div class="card-body">
      {chart_html}
      {panels}
    </div>
  </details>
</div>"""


def build_html_report(all_results: list[dict], chart_data: dict[str, str],
                      run_ts: str, scanned: int) -> str:
    n_up  = sum(1 for d in all_results if d["phase"] == "UPTREND")
    n_sw  = sum(1 for d in all_results if d["phase"] == "SIDEWAYS")
    n_be  = sum(1 for d in all_results if d["phase"] == "BEARISH")
    n_fr  = sum(1 for d in all_results if d["fresh_d"] or d["fresh_w"])
    n_s30 = sum(1 for d in all_results if d["is_sensex30"])

    stat_boxes = f"""<div class="row gx-2 gy-2 mb-3">
      <div class="col-6 col-md-4 col-xl-2"><div class="stat-box cyan p-3 rounded-3 h-100"><div class="val">{scanned}</div><div class="lbl">Scanned</div></div></div>
      <div class="col-6 col-md-4 col-xl-2"><div class="stat-box green p-3 rounded-3 h-100"><div class="val">{n_up}</div><div class="lbl">📈 Uptrend</div></div></div>
      <div class="col-6 col-md-4 col-xl-2"><div class="stat-box gold p-3 rounded-3 h-100"><div class="val">{n_sw}</div><div class="lbl">➡️ Sideways</div></div></div>
      <div class="col-6 col-md-4 col-xl-2"><div class="stat-box red p-3 rounded-3 h-100"><div class="val">{n_be}</div><div class="lbl">📉 Bearish</div></div></div>
      <div class="col-6 col-md-4 col-xl-2"><div class="stat-box cyan p-3 rounded-3 h-100"><div class="val">{n_fr}</div><div class="lbl">🚀 Fresh</div></div></div>
      <div class="col-6 col-md-4 col-xl-2"><div class="stat-box gold p-3 rounded-3 h-100"><div class="val">{n_s30}</div><div class="lbl">🏆 Sensex30</div></div></div>
    </div>"""

    filter_bar = f"""<div class="btn-toolbar flex-wrap gap-2 mb-3" role="toolbar">
      <div class="btn-group" role="group" aria-label="status filters">
        <button type="button" class="btn btn-sm btn-outline-light active" onclick="filterPhase('all',this)">All ({len(all_results)})</button>
        <button type="button" class="btn btn-sm btn-outline-light" onclick="filterPhase('fresh',this)">🚀 Fresh ({n_fr})</button>
        <button type="button" class="btn btn-sm btn-outline-light" onclick="filterPhase('UPTREND',this)">📈 Uptrend ({n_up})</button>
        <button type="button" class="btn btn-sm btn-outline-light" onclick="filterPhase('SIDEWAYS',this)">➡️ Sideways ({n_sw})</button>
        <button type="button" class="btn btn-sm btn-outline-light" onclick="filterPhase('BEARISH',this)">📉 Bearish ({n_be})</button>
        <button type="button" class="btn btn-sm btn-outline-light" onclick="filterPhase('sensex30',this)">🏆 Sensex30 ({n_s30})</button>
      </div>
    </div>"""

    sum_table = build_summary_table(all_results)

    # Build cards (all collapsed by default — browser renders instantly)
    if not all_results:
        cards_html = '<div class="no-results" style="padding:20px 28px;color:var(--sub);">No stocks were successfully analysed. Check the error log for details.</div>'
    else:
        cards_html = ""
        for d in all_results:
            has_chart = d["ticker"] in chart_data
            cards_html += build_stock_card(d, has_chart)

    charts_script = ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>RSI MTF Breakout Report — BSE v2.0 — {run_ts}</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css">
  <style>{_CSS}</style>
</head>
<body>
  <div class="container-fluid px-2">
  <div class="header">
    <h1>📈 RSI Multi-Timeframe Breakout Report v2.0 - BSE</h1>
    <div class="subtitle">
      BSE EQ Universe &nbsp;|&nbsp; {run_ts} IST &nbsp;|&nbsp;
      RSI(14) D/W/M + MACD(12,26) + CCI(20) &nbsp;|&nbsp;
      Ranked vs Sensex30 &amp; All BSE &nbsp;|&nbsp;
      Charts: {len(chart_data)} PNGs generated (lazy-loaded on expand)
    </div>
    {stat_boxes}
  </div>

  {filter_bar}
  {sum_table}

  <div class="cards-section row row-cols-1 row-cols-md-2 row-cols-xl-2 g-3">
    <div class="col-12 mb-2">
      <h2>🔍 DETAILED ANALYSIS — all {len(all_results)} stocks</h2>
      <p class="text-muted mb-0">Click ▶ to expand any card · details panels expand independently</p>
    </div>
    {cards_html}
  </div>

  <div class="footer">
    RSI MTF Report v2.0 - BSE &nbsp;|&nbsp; {run_ts} &nbsp;|&nbsp;
    <b>Not financial advice.</b><br>
    Entry: RSI D+W+M > SMA + CCI>0 + MACD>Signal &nbsp;|&nbsp;
    SL: 2×ATR or swing low &nbsp;|&nbsp;
    Exit: RSI crosses below SMA or CCI &lt; −100
  </div>
  </div>

  {charts_script}
  <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
  <script>{_JS}</script>
</body>
</html>"""


# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════

def main():
    os.system("cls" if os.name == "nt" else "clear")
    print("╔══════════════════════════════════════════════════════════════════╗")
    print("║  📈  RSI MTF BREAKOUT REPORT  v2.0 - BSE VERSION                 ║")
    print("║      Error Logging · Rankings vs Sensex30 & Universe · Lazy Charts║")
    print("╚══════════════════════════════════════════════════════════════════╝")
    print(f"   {RUN_TS} IST  |  Errors → {ERROR_LOG}\n")
    log_info(f"=== BSE Scan started {RUN_TS} ===")

    # ── Step 1: Universe ──────────────────────────────────────────
    print("▶  STEP 1/3  Build BSE universe")
    tickers = load_universe()
    print(f"   {len(tickers)} stocks loaded  |  {len(_COMPANY_MAP)} company names\n")
    log_info(f"BSE Universe: {len(tickers)} stocks, {len(_COMPANY_MAP)} company names")

    # ── Step 2: Scan ──────────────────────────────────────────────
    print("▶  STEP 2/3  Download + analyse (Daily · Weekly · Monthly)")
    print("   ─────────────────────────────────────────────────────────")
    results, errors = [], 0
    t0, total = time.time(), len(tickers)

    for i, ticker in enumerate(tickers, 1):
        pct  = i / total * 100
        fill = int(pct / 2)
        sys.stdout.write(
            f"\r  [{'█'*fill}{'░'*(50-fill)}] {pct:5.1f}%  {i:>4}/{total}  "
            f"{ticker:<14}  ok={len(results)}  err={errors}"
        )
        sys.stdout.flush()

        res = analyze_stock(ticker)
        if res:
            results.append(res)
        else:
            errors += 1

        if i % BATCH_SIZE == 0:
            time.sleep(BATCH_PAUSE)

    elapsed = time.time() - t0
    print(f"\n\n   ✓ {len(results)} ok  |  {errors} failed  |  {elapsed:.0f}s")
    log_info(f"BSE Scan done: {len(results)} ok, {errors} failed, {elapsed:.0f}s")

    if not results:
        print("  ⚠️  No results produced — generating diagnostic HTML report.")
        log_info("No results produced during BSE scan; building empty HTML report.")

    # ── Rankings ──────────────────────────────────────────────────
    print("   Computing rankings vs Sensex30 and full BSE universe...")
    results = compute_rankings(results)
    results.sort(key=lambda d: (d["rank_universe"], d["score"]), reverse=True)

    s30_in_scan = sum(1 for d in results if d["is_sensex30"])
    print(f"   Sensex30 stocks in scan: {s30_in_scan}/{len(SENSEX30)}\n")

    # ── Step 3: HTML ──────────────────────────────────────────────
    print("▶  STEP 3/3  Generate charts + build HTML")
    print("   ─────────────────────────────────────────────────────────")

    chart_candidates = results if MAX_CHART_STOCKS <= 0 else results[:MAX_CHART_STOCKS]
    chart_tickers = [d["ticker"] for d in chart_candidates]
    chart_data    = {}
    os.makedirs(CHART_OUTPUT_DIR, exist_ok=True)
    print(f"   Generating charts for {'all' if MAX_CHART_STOCKS <= 0 else 'top'} {len(chart_tickers)} stocks using {CHART_WORKERS} workers...")

    futures = {}
    with ThreadPoolExecutor(max_workers=CHART_WORKERS) as executor:
        for d in chart_candidates:
            futures[executor.submit(generate_chart, d)] = d

        completed = 0
        for future in as_completed(futures):
            completed += 1
            d = futures[future]
            ticker = d["ticker"]
            try:
                path = future.result()
                if path:
                    chart_data[ticker] = path
                else:
                    print(f"\n   ⚠️ Chart failed for {ticker} — see {ERROR_LOG}")
            except Exception as exc:
                log_error(ticker, d.get("company", ""), "CHART", exc)
                print(f"\n   ⚠️ Chart failed for {ticker} — see {ERROR_LOG}")
            sys.stdout.write(f"\r   Chart {completed}/{len(chart_tickers)}  {ticker:<14}")
            sys.stdout.flush()

    print(f"\n   {len(chart_data)}/{len(chart_tickers)} charts generated")

    # Strip raw series before building HTML (saves RAM + avoids serialising DataFrames)
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
    log_info(f"BSE HTML saved: {OUTPUT_HTML} ({size_mb:.1f} MB)")
    print(f"  ✅ BSE HTML saved : {OUTPUT_HTML}  ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()