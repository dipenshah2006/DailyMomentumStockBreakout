"""
╔═════════════════════════════════════════════════════════════════════════════╗
║   RSI MULTI-TIMEFRAME BREAKOUT HTML REPORT  v1.0                            ║
║   Daily · Weekly · Monthly RSI/SMA Crossover | Phase | Entry/Exit           ║
║   Trend: MACD(12,26)  |  Entry/Exit: RSI(14) + CCI(20)                     ║
║   Uptrend Targets: Fibonacci Extensions                                      ║
║   Downtrend Support: Fibonacci Retracements                                  ║
║   Historical Signal Backtest included                                        ║
╚═════════════════════════════════════════════════════════════════════════════╝

INSTALL:
    pip install yfinance pandas numpy matplotlib requests openpyxl

RUN:
    python rsi_mtf_report.py

OUTPUTS:
    rsi_mtf_report_YYYYMMDD_HHMM.html   ← self-contained, open in any browser

CONFIG: edit the USER CONFIG block below.
"""

# ═════════════════════════════════════════════════════════════════════════════
# USER CONFIG
# ═════════════════════════════════════════════════════════════════════════════

LOCAL_NSE_CSV       = "EQUITY_L.csv"    # local NSE equity master — fastest
NSE_CSV_URL         = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
SERIES_FILTER       = ["EQ"]            # EQ = cash, BE = trade-to-trade

DATA_PERIOD         = "2y"              # yfinance period
MIN_CANDLES         = 80               # skip stocks with less daily data

MAX_REPORT_STOCKS   = 60               # stocks with full chart in HTML (top N by score)
CHART_BARS          = 160              # last N daily bars shown in chart

FRESH_DAYS_D        = 3                # daily crossover ≤ N bars ago = "fresh"
FRESH_WEEKS_W       = 2                # weekly crossover ≤ N bars = "fresh"

# Indicator periods
RSI_P               = 14
RSI_SMA_P           = 14
CCI_P               = 20
MACD_F, MACD_S, MACD_SIG_P = 12, 26, 9
ATR_P               = 14

BATCH_SIZE          = 25
BATCH_PAUSE         = 1.0

# Score thresholds (out of 22)
SCORE_STRONG_BUY    = 16
SCORE_BUY           = 12
SCORE_WATCH         = 8

# ═════════════════════════════════════════════════════════════════════════════
# IMPORTS
# ═════════════════════════════════════════════════════════════════════════════

import base64
import csv
import io
from itertools import count
import os
import pickle
import sys
import time
import warnings
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

RUN_TS = datetime.now().strftime("%d %b %Y  %H:%M")
OUTPUT_HTML = f"rsi_mtf_report_{datetime.now().strftime('%d%m%Y_%H%M')}.html"
CACHE_FILE = "stock_data_cache.pkl"

# ═════════════════════════════════════════════════════════════════════════════
# CACHE MANAGEMENT — Persistent Stock Data (never expires)
# ═════════════════════════════════════════════════════════════════════════════

def load_cache():
    """Load cached stock data from pickle file."""
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "rb") as f:
                return pickle.load(f)
        except Exception as e:
            print(f"  [!] Cache load error: {e}. Starting fresh.")
            return {}
    return {}

def save_cache(cache):
    """Save stock data cache to pickle file."""
    try:
        with open(CACHE_FILE, "wb") as f:
            pickle.dump(cache, f)
        print(f"  💾 Cache saved: {len(cache)} stocks")
    except Exception as e:
        print(f"  [!] Cache save error: {e}")

def get_cached_data(ticker):
    """Retrieve cached OHLCV data for ticker (never expires)."""
    cache = load_cache()
    return cache.get(ticker)

def set_cached_data(ticker, df):
    """Store OHLCV data in cache (datewise indexed, never expires)."""
    cache = load_cache()
    cache[ticker] = df
    save_cache(cache)

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 1 — INDICATOR FUNCTIONS
# ═════════════════════════════════════════════════════════════════════════════

def calc_rsi(close, period=14):
    delta = close.diff()
    gain  = delta.clip(lower=0).ewm(com=period - 1, min_periods=period).mean()
    loss  = (-delta).clip(lower=0).ewm(com=period - 1, min_periods=period).mean()
    return 100 - (100 / (1 + gain / (loss + 1e-10)))


def calc_macd(close, fast=12, slow=26, sig=9):
    line   = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    signal = line.ewm(span=sig, adjust=False).mean()
    hist   = line - signal
    return line, signal, hist


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
    """Resample daily OHLCV to weekly ('W-FRI') or monthly ('ME')."""
    return df.resample(rule).agg({
        "Open":   "first",
        "High":   "max",
        "Low":    "min",
        "Close":  "last",
        "Volume": "sum",
    }).dropna(subset=["Close"])


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 2 — SWING DETECTION + FIBONACCI
# ═════════════════════════════════════════════════════════════════════════════

def find_swing_points(high_s, low_s, lookback=252, order=5):
    """
    Return (swing_low_price, swing_low_idx, swing_high_price, swing_high_idx)
    using the most significant pivot within the lookback window.
    """
    h = high_s.iloc[-lookback:] if len(high_s) >= lookback else high_s
    l = low_s.iloc[-lookback:]  if len(low_s)  >= lookback else low_s

    pivot_highs, pivot_lows = [], []
    for i in range(order, len(h) - order):
        if h.iloc[i] == h.iloc[i - order: i + order + 1].max():
            pivot_highs.append((h.index[i], float(h.iloc[i]), i))
        if l.iloc[i] == l.iloc[i - order: i + order + 1].min():
            pivot_lows.append((l.index[i], float(l.iloc[i]), i))

    if not pivot_highs or not pivot_lows:
        # Fallback to absolute max/min
        phi = h.idxmax(); plo = l.idxmin()
        return (float(l.loc[plo]), plo,
                float(h.loc[phi]), phi)

    # Most recent significant swing high and low
    swing_high_dt, swing_high_val, swing_high_pos = max(pivot_highs, key=lambda x: x[2])
    swing_low_dt,  swing_low_val,  swing_low_pos  = max(pivot_lows,  key=lambda x: x[2])

    return swing_low_val, swing_low_dt, swing_high_val, swing_high_dt


def fib_extensions(swing_low, swing_high):
    """
    Fibonacci extension targets ABOVE swing_high.
    Standard Fibonacci extension levels from A (low) → B (high).
    """
    rng = swing_high - swing_low
    return {
        "127.2%": round(swing_high + rng * 0.272, 2),
        "161.8%": round(swing_high + rng * 0.618, 2),
        "200.0%": round(swing_high + rng * 1.000, 2),
        "261.8%": round(swing_high + rng * 1.618, 2),
        "423.6%": round(swing_high + rng * 3.236, 2),
    }


def fib_retracements(swing_high, swing_low):
    """
    Fibonacci retracement support levels BETWEEN swing_high and swing_low.
    Standard Fibonacci retracement from A (high) → B (low).
    """
    rng = swing_high - swing_low
    return {
        "23.6%": round(swing_high - rng * 0.236, 2),
        "38.2%": round(swing_high - rng * 0.382, 2),
        "50.0%": round(swing_high - rng * 0.500, 2),
        "61.8%": round(swing_high - rng * 0.618, 2),
        "78.6%": round(swing_high - rng * 0.786, 2),
    }


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 3 — PHASE + SCORING + SIGNAL ENGINE
# ═════════════════════════════════════════════════════════════════════════════

def detect_phase(rsi_d, rsi_w, rsi_m, macd_line, macd_signal, score):
    """Classify market phase into UPTREND / SIDEWAYS / BEARISH."""
    bulls = sum([
        rsi_d  > 55,
        rsi_w  > 52,
        rsi_m  > 50,
        macd_line > macd_signal,
        macd_line > 0,
    ])
    bears = sum([
        rsi_d  < 45,
        rsi_w  < 48,
        rsi_m  < 50,
        macd_line < macd_signal,
        macd_line < 0,
    ])
    if bulls >= 4 or (score >= SCORE_BUY and rsi_d > 50):
        return "UPTREND"
    elif bears >= 4 or (score <= 5 and rsi_d < 45):
        return "BEARISH"
    return "SIDEWAYS"


def compute_score(rsi_d, rsi_d_sma, rsi_w, rsi_w_sma, rsi_m, rsi_m_sma,
                  macd_line, macd_sig, cci, fresh_d, fresh_w):
    """
    22-point RSI multi-timeframe scoring.
    Monthly RSI > SMA carries highest weight (longest-term confirmation).
    """
    score = 0
    sigs  = []

    # Monthly RSI alignment (+4)
    if rsi_m > rsi_m_sma:
        score += 4; sigs.append("M-RSI>SMA ✅")
    # Weekly RSI alignment (+3)
    if rsi_w > rsi_w_sma:
        score += 3; sigs.append("W-RSI>SMA ✅")
    # Daily RSI alignment (+2)
    if rsi_d > rsi_d_sma:
        score += 2; sigs.append("D-RSI>SMA ✅")

    # Fresh crossovers (bonus points)
    if fresh_d:
        score += 3; sigs.append("FRESH Daily 🚀")
    if fresh_w:
        score += 2; sigs.append("FRESH Weekly 🔥")

    # MACD trend (+2+1)
    if macd_line > macd_sig:
        score += 2; sigs.append("MACD>Sig ✅")
    if macd_line > 0:
        score += 1; sigs.append("MACD>0")

    # CCI entry (+1/+2)
    if cci > 100:
        score += 2; sigs.append("CCI>100 💪")
    elif cci > 0:
        score += 1; sigs.append("CCI>0")

    # RSI momentum (+1 each)
    if rsi_d > 60:
        score += 1; sigs.append("D-RSI>60 🔥")
    if rsi_w > 55:
        score += 1; sigs.append("W-RSI>55 💪")

    return score, sigs


def signal_label(score, phase, fresh_d, fresh_w, rsi_d, rsi_w, rsi_m,
                 rsi_d_sma, rsi_w_sma, rsi_m_sma):
    """Return signal string + CSS class."""
    triple = rsi_d > rsi_d_sma and rsi_w > rsi_w_sma and rsi_m > rsi_m_sma
    if score >= SCORE_STRONG_BUY and triple and (fresh_d or fresh_w):
        return "STRONG BUY 🚀", "sig-strong-buy"
    elif score >= SCORE_BUY and (rsi_d > rsi_d_sma) and (rsi_w > rsi_w_sma):
        return "BUY ✅", "sig-buy"
    elif score >= SCORE_WATCH:
        return "WATCH 👀", "sig-watch"
    elif phase == "BEARISH":
        return "AVOID ❌", "sig-avoid"
    return "NEUTRAL", "sig-neutral"


def calc_stop_loss(close, atr, swing_low):
    """Return (atr_sl, swing_sl) tuple."""
    atr_sl   = round(close - 2.0 * atr, 2)
    swing_sl = round(swing_low * 0.99,  2)  # 1% below last swing low
    return atr_sl, swing_sl


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 4 — HISTORICAL SIGNAL BACKTEST
# ═════════════════════════════════════════════════════════════════════════════

def historical_signals(close, rsi_series, rsi_sma_series, max_signals=12):
    """
    Scan full history for daily RSI-SMA crossovers.
    For BUY signals: compute 5d, 10d, 20d forward returns.
    Returns list of dicts.
    """
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

        sig_type = "BUY"  if crossed_above else "SELL"
        sig_px   = float(cls_arr[i])

        def ret(fwd):
            j = i + fwd
            if j < len(cls_arr):
                return round((cls_arr[j] / sig_px - 1) * 100, 1)
            return None

        results.append({
            "date":   dates[i].strftime("%d-%b-%y"),
            "type":   sig_type,
            "price":  round(sig_px, 2),
            "rsi":    round(float(rsi_arr[i]), 1),
            "r5d":    ret(5),
            "r10d":   ret(10),
            "r20d":   ret(20),
        })

    # Return most recent N signals
    return results[-max_signals:]


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 5 — NSE UNIVERSE LOADER
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


def _parse_nse_csv(text):
    reader  = csv.DictReader(io.StringIO(text))
    tickers = []
    for row in reader:
        series = row.get(" SERIES", row.get("SERIES", "")).strip()
        symbol = row.get("SYMBOL", "").strip()
        if symbol and series in SERIES_FILTER:
            tickers.append(symbol)
    return tickers


def load_universe():
    # 1. Local CSV
    if os.path.exists(LOCAL_NSE_CSV):
        try:
            with open(LOCAL_NSE_CSV, encoding="utf-8", errors="replace") as f:
                t = _parse_nse_csv(f.read())
            if t:
                print(f"  ✅ Local '{LOCAL_NSE_CSV}': {len(t)} EQ stocks")
                return t
        except Exception as e:
            print(f"  [!] Local CSV error: {e}")

    # 2. Live download
    try:
        s = requests.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "*/*"})
        s.get("https://www.nseindia.com/", timeout=12)
        time.sleep(1.5)
        s.headers["Referer"] = "https://www.nseindia.com/"
        r = s.get(NSE_CSV_URL, timeout=20)
        r.raise_for_status()
        t = _parse_nse_csv(r.text)
        if t:
            print(f"  ✅ Live NSE download: {len(t)} EQ stocks")
            try:
                with open(LOCAL_NSE_CSV, "w", encoding="utf-8") as f:
                    f.write(r.text)
                print(f"  💾 Saved → '{LOCAL_NSE_CSV}'")
            except Exception:
                pass
            return t
    except Exception as e:
        print(f"  [!] NSE download failed: {e}")

    print(f"  ⚠️  Using built-in list: {len(BUILTIN)} stocks")
    return list(dict.fromkeys(BUILTIN))


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 6 — PER-STOCK ANALYSIS
# ═════════════════════════════════════════════════════════════════════════════

def analyze_stock(ticker):
    """
    Full multi-timeframe analysis for one NSE stock.
    Returns a rich dict or None if data insufficient.
    Uses persistent cache — data never expires.
    """
    try:
        # Try to load from cache first
        df = get_cached_data(ticker)
        if df is None:
            # Download from yfinance if not in cache
            df = yf.download(ticker + ".NS", period=DATA_PERIOD, interval="1d",
                             progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.droplevel(1)
            df = df.dropna()
            # Cache the fresh data
            if len(df) >= MIN_CANDLES:
                set_cached_data(ticker, df)
        else:
            # Data from cache — clean it up
            df = df.dropna()
        
        if len(df) < MIN_CANDLES:
            return None

        # ── Resample to weekly and monthly ────────────────────────
        wk = resample_ohlcv(df, "W-FRI")
        mo = resample_ohlcv(df, "ME")
        if len(wk) < 20 or len(mo) < 6:
            return None

        # ── Daily indicators ──────────────────────────────────────
        rsi_d     = calc_rsi(df["Close"], RSI_P)
        sma_d     = rsi_d.rolling(RSI_SMA_P).mean()
        macd_l, macd_sig, macd_hist = calc_macd(df["Close"], MACD_F, MACD_S, MACD_SIG_P)
        cci_d     = calc_cci(df["High"], df["Low"], df["Close"], CCI_P)
        atr_d     = calc_atr(df["High"], df["Low"], df["Close"], ATR_P)

        # ── Weekly indicators ─────────────────────────────────────
        rsi_w     = calc_rsi(wk["Close"], RSI_P)
        sma_w     = rsi_w.rolling(RSI_SMA_P).mean()
        macd_l_w, macd_sig_w, macd_hist_w = calc_macd(wk["Close"], MACD_F, MACD_S, MACD_SIG_P)
        cci_w     = calc_cci(wk["High"], wk["Low"], wk["Close"], CCI_P)

        # ── Monthly indicators ────────────────────────────────────
        rsi_m     = calc_rsi(mo["Close"], RSI_P)
        sma_m     = rsi_m.rolling(RSI_SMA_P).mean()
        macd_l_m, macd_sig_m, macd_hist_m = calc_macd(mo["Close"], MACD_F, MACD_S, MACD_SIG_P)
        cci_m     = calc_cci(mo["High"], mo["Low"], mo["Close"], CCI_P)

        # ── Latest values ─────────────────────────────────────────
        def f(s, i=-1):
            v = s.iloc[i]
            return float(v) if not (isinstance(v, float) and np.isnan(v)) else 0.0

        v_rsi_d    = f(rsi_d)
        v_sma_d    = f(sma_d)
        v_rsi_w    = f(rsi_w)
        v_sma_w    = f(sma_w)
        v_rsi_m    = f(rsi_m)
        v_sma_m    = f(sma_m)
        v_macd_l   = f(macd_l)
        v_macd_s   = f(macd_sig)
        v_macd_l_w = f(macd_l_w)
        v_macd_s_w = f(macd_sig_w)
        v_macd_l_m = f(macd_l_m)
        v_macd_s_m = f(macd_sig_m)
        v_cci      = f(cci_d)
        v_cci_w    = f(cci_w)
        v_cci_m    = f(cci_m)
        v_atr      = f(atr_d)
        v_close    = f(df["Close"])
        v_high52   = float(df["Close"].rolling(252).max().iloc[-1])
        v_low52    = float(df["Close"].rolling(252).min().iloc[-1])
        v_dist52   = round((v_close / v_high52 - 1) * 100, 1)

        # ── Fresh crossover detection ─────────────────────────────
        def is_fresh_cross(rsi_s, sma_s, window):
            for lag in range(1, window + 2):
                if len(rsi_s) <= lag:
                    break
                now  = rsi_s.iloc[-lag]  > sma_s.iloc[-lag]
                prev = rsi_s.iloc[-lag-1] > sma_s.iloc[-lag-1]
                if now and not prev:
                    return True, lag
            return False, 0

        fresh_d, fresh_d_bars = is_fresh_cross(rsi_d, sma_d, FRESH_DAYS_D)
        fresh_w, fresh_w_bars = is_fresh_cross(rsi_w, sma_w, FRESH_WEEKS_W)

        # ── Scoring ───────────────────────────────────────────────
        score, sig_list = compute_score(
            v_rsi_d, v_sma_d, v_rsi_w, v_sma_w, v_rsi_m, v_sma_m,
            v_macd_l, v_macd_s, v_cci, fresh_d, fresh_w)

        # ── Phase ─────────────────────────────────────────────────
        phase = detect_phase(v_rsi_d, v_rsi_w, v_rsi_m, v_macd_l, v_macd_s, score)

        # ── Signal ───────────────────────────────────────────────
        signal, sig_cls = signal_label(score, phase, fresh_d, fresh_w,
                                       v_rsi_d, v_rsi_w, v_rsi_m,
                                       v_sma_d, v_sma_w, v_sma_m)

        # ── Stop loss ─────────────────────────────────────────────
        sl_low, sl_high, _, swing_high_dt = find_swing_points(df["High"], df["Low"])
        atr_sl, swing_sl = calc_stop_loss(v_close, v_atr, sl_low)

        # ── Fibonacci ─────────────────────────────────────────────
        sw_low, sw_low_dt, sw_high, sw_high_dt = find_swing_points(df["High"], df["Low"])
        if phase == "UPTREND":
            fib_levels  = fib_extensions(sw_low, sw_high)
            fib_type    = "EXTENSION"
            fib_base    = f"Swing Low ₹{sw_low:,.0f} → Swing High ₹{sw_high:,.0f}"
        else:
            fib_levels  = fib_retracements(sw_high, sw_low)
            fib_type    = "RETRACEMENT"
            fib_base    = f"Swing High ₹{sw_high:,.0f} → Swing Low ₹{sw_low:,.0f}"

        # Filter fib levels relevant to current price
        if fib_type == "EXTENSION":
            fib_levels = {k: v for k, v in fib_levels.items() if v > v_close}
        else:
            fib_levels = {k: v for k, v in fib_levels.items() if sw_low < v < sw_high}

        # ── Historical signals ────────────────────────────────────
        hist_sigs = historical_signals(df["Close"], rsi_d, sma_d, max_signals=12)

        # ── Entry suggestion ──────────────────────────────────────
        if v_rsi_d > 65:
            entry_note = f"Wait for pullback to RSI~55 zone (~₹{v_close * 0.96:,.0f})"
        elif v_rsi_d > 55 and v_rsi_d > v_sma_d:
            entry_note = f"Entry on current close ₹{v_close:,.0f} or next dip"
        elif v_rsi_d < v_sma_d and fresh_d:
            entry_note = f"Fresh cross — confirm with next candle close above ₹{v_close:,.0f}"
        else:
            entry_note = f"Wait for RSI(14) to cross above its SMA on daily chart"

        # ── Sell signal conditions ────────────────────────────────
        sell_conditions = [
            "RSI(14) daily crosses BELOW its SMA(14)",
            "CCI(20) drops below −100",
            "MACD(12,26) line crosses below signal line",
        ]
        if v_rsi_d > 75:
            sell_conditions.insert(0, "⚠️ RSI already >75 — consider partial profit booking")

        return {
            # identity
            "ticker":       ticker,
            "close":        v_close,
            "high52":       v_high52,
            "low52":        v_low52,
            "dist52":       v_dist52,
            # RSI multi-TF
            "rsi_d":        round(v_rsi_d, 1),
            "sma_d":        round(v_sma_d, 1),
            "rsi_w":        round(v_rsi_w, 1),
            "sma_w":        round(v_sma_w, 1),
            "rsi_m":        round(v_rsi_m, 1),
            "sma_m":        round(v_sma_m, 1),
            # MACD / CCI
            "macd_l":       round(v_macd_l, 3),
            "macd_s":       round(v_macd_s, 3),
            "macd_l_w":     round(v_macd_l_w, 3),
            "macd_s_w":     round(v_macd_s_w, 3),
            "macd_l_m":     round(v_macd_l_m, 3),
            "macd_s_m":     round(v_macd_s_m, 3),
            "cci":          round(v_cci,    1),
            "cci_w":        round(v_cci_w,  1),
            "cci_m":        round(v_cci_m,  1),
            "atr":          round(v_atr,    2),
            # fresh
            "fresh_d":      fresh_d,
            "fresh_d_bars": fresh_d_bars,
            "fresh_w":      fresh_w,
            "fresh_w_bars": fresh_w_bars,
            # scoring / phase / signal
            "score":        score,
            "sig_list":     sig_list,
            "phase":        phase,
            "signal":       signal,
            "sig_cls":      sig_cls,
            # trade levels
            "entry_note":   entry_note,
            "atr_sl":       atr_sl,
            "swing_sl":     swing_sl,
            "sw_low":       sw_low,
            "sw_high":      sw_high,
            "fib_type":     fib_type,
            "fib_levels":   fib_levels,
            "fib_base":     fib_base,
            "sell_conds":   sell_conditions,
            # historical
            "hist_sigs":    hist_sigs,
            # raw series for chart
            "_df":          df,
            "_rsi_d":       rsi_d,
            "_sma_d":       sma_d,
            "_rsi_w_daily": rsi_w.reindex(df.index, method="ffill"),
            "_rsi_m_daily": rsi_m.reindex(df.index, method="ffill"),
            "_sma_w_daily": sma_w.reindex(df.index, method="ffill"),
            "_macd_l":      macd_l,
            "_macd_s":      macd_sig,
            "_macd_h":      macd_hist,
            "_macd_l_w":    macd_l_w,
            "_macd_s_w":    macd_sig_w,
            "_macd_h_w":    macd_hist_w,
            "_macd_l_m":    macd_l_m,
            "_macd_s_m":    macd_sig_m,
            "_macd_h_m":    macd_hist_m,
            "_cci":         cci_d,
            "_cci_w":       cci_w,
            "_cci_m":       cci_m,
        }
    except Exception:
        return None


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 7 — CHART GENERATOR
# ═════════════════════════════════════════════════════════════════════════════

def generate_chart(data):
    """
    5-panel chart:
      1. Candlestick + Fibonacci + Entry/Sell markers
      2. Volume
      3. RSI Daily + SMA | Weekly RSI | Monthly RSI (3 lines) + overbought/oversold
      4. MACD(12,26) line + signal + histogram
      5. CCI(20) + ±100 lines
    Returns base64 PNG string.
    """
    df_all = data["_df"]
    n_bars = min(CHART_BARS, len(df_all))
    df     = df_all.iloc[-n_bars:].copy()
    idx    = np.arange(len(df))

    rsi_d  = data["_rsi_d"].iloc[-n_bars:].values
    sma_d  = data["_sma_d"].iloc[-n_bars:].values
    rsi_w  = data["_rsi_w_daily"].iloc[-n_bars:].values
    rsi_m  = data["_rsi_m_daily"].iloc[-n_bars:].values
    sma_w  = data["_sma_w_daily"].iloc[-n_bars:].values
    macd_l = data["_macd_l"].iloc[-n_bars:].values
    macd_s = data["_macd_s"].iloc[-n_bars:].values
    macd_h = data["_macd_h"].iloc[-n_bars:].values
    cci    = data["_cci"].iloc[-n_bars:].values

    # ── Colors (dark theme) ────────────────────────────────────────
    BG       = "#0d1117"
    PANEL    = "#161b22"
    GREEN    = "#26d07c"
    RED      = "#ff4d6d"
    GOLD     = "#ffd700"
    CYAN     = "#00d4ff"
    PURPLE   = "#b39ddb"
    ORANGE   = "#ff9800"
    GREY     = "#30363d"
    TXT      = "#c9d1d9"
    FIB_EXT  = "#4caf50"
    FIB_RET  = "#ff7043"

    fig = plt.figure(figsize=(15, 11), facecolor=BG)
    fig.suptitle(
        f"{data['ticker']}  ₹{data['close']:,.2f}  |  {data['phase']}  |  {data['signal']}  |  Score {data['score']}/22",
        color=TXT, fontsize=13, fontweight="bold", y=0.995
    )

    gs = gridspec.GridSpec(
        5, 1,
        figure=fig,
        hspace=0.04,
        height_ratios=[4, 1.2, 1.8, 1.4, 1.4],
    )
    axes = [fig.add_subplot(gs[i]) for i in range(5)]
    for ax in axes:
        ax.set_facecolor(PANEL)
        ax.tick_params(colors=TXT, labelsize=7)
        ax.spines[:].set_color(GREY)
        ax.grid(True, color=GREY, linewidth=0.3, linestyle="--")
        ax.set_xlim(-1, len(idx))

    # ── X-axis ticks (shared) ─────────────────────────────────────
    step   = max(1, len(idx) // 10)
    tpos   = idx[::step]
    tlbl   = [df.index[i].strftime("%b'%y") for i in tpos]
    for ax in axes:
        ax.set_xticks(tpos)
        ax.set_xticklabels([] if ax != axes[-1] else tlbl,
                           rotation=30, ha="right", fontsize=6.5)

    # ── Panel 1: Candlestick ──────────────────────────────────────
    ax1 = axes[0]
    for i, (_, row) in enumerate(df.iterrows()):
        up  = float(row["Close"]) >= float(row["Open"])
        col = GREEN if up else RED
        lo, hi = float(row["Low"]), float(row["High"])
        op, cl = float(row["Open"]), float(row["Close"])
        ax1.plot([i, i], [lo, hi], color=col, lw=0.7, zorder=2)
        ax1.bar(i, abs(cl - op), bottom=min(op, cl), color=col,
                width=0.7, linewidth=0, zorder=3)

    # Entry / sell markers from historical signals (last few)
    hist = data["hist_sigs"]
    sig_dates = {s["date"]: s["type"] for s in hist[-8:]}
    for i, dt in enumerate(df.index):
        label = sig_dates.get(dt.strftime("%d-%b-%y"))
        if label == "BUY":
            ax1.plot(i, float(df["Low"].iloc[i]) * 0.993, "^",
                     color=GREEN, markersize=7, zorder=5)
        elif label == "SELL":
            ax1.plot(i, float(df["High"].iloc[i]) * 1.007, "v",
                     color=RED, markersize=7, zorder=5)

    # Current price line
    ax1.axhline(data["close"], color=GOLD, lw=0.8, linestyle="--", alpha=0.6)

    # Fibonacci levels
    fib_col = FIB_EXT if data["fib_type"] == "EXTENSION" else FIB_RET
    fib_start = int(len(idx) * 0.7)
    for label, level in data["fib_levels"].items():
        ax1.axhline(level, color=fib_col, lw=0.8, linestyle=":", alpha=0.75)
        ax1.text(len(idx) - 1, level, f" {label} ₹{level:,.0f}",
                 color=fib_col, fontsize=6, va="center")

    # Stop loss lines
    ax1.axhline(data["atr_sl"],   color=RED, lw=0.6, linestyle="-.", alpha=0.5)
    ax1.axhline(data["swing_sl"], color=RED, lw=0.6, linestyle="-.", alpha=0.35)

    ax1.set_ylabel("Price ₹", color=TXT, fontsize=7)
    fib_patch = mpatches.Patch(color=fib_col, label=f"Fib {data['fib_type']}")
    ax1.legend(handles=[fib_patch], loc="upper left",
               facecolor=BG, edgecolor=GREY, labelcolor=TXT, fontsize=6.5)

    # ── Panel 2: Volume ───────────────────────────────────────────
    ax2 = axes[1]
    vol_avg = pd.Series(df["Volume"].values).rolling(20).mean().values
    for i, (_, row) in enumerate(df.iterrows()):
        col = GREEN if float(row["Close"]) >= float(row["Open"]) else RED
        ax2.bar(i, float(row["Volume"]), color=col, width=0.7, alpha=0.7, linewidth=0)
    ax2.plot(idx, vol_avg, color=GOLD, lw=0.8, label="Vol MA20")
    ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x/1e6:.1f}M"))
    ax2.set_ylabel("Volume", color=TXT, fontsize=7)

    # ── Panel 3: RSI D/W/M ────────────────────────────────────────
    ax3 = axes[2]
    ax3.fill_between(idx, 30, 70, alpha=0.06, color=CYAN)
    ax3.axhline(70, color=RED,   lw=0.6, linestyle="--", alpha=0.5)
    ax3.axhline(55, color=GREEN, lw=0.5, linestyle=":",  alpha=0.4)
    ax3.axhline(50, color=TXT,   lw=0.5, linestyle="--", alpha=0.3)
    ax3.axhline(30, color=GREEN, lw=0.6, linestyle="--", alpha=0.5)

    ax3.plot(idx, rsi_d, color=CYAN,   lw=1.2, label=f"RSI-D {data['rsi_d']}")
    ax3.plot(idx, sma_d, color=ORANGE, lw=0.9, linestyle="--", label=f"SMA(14) {data['sma_d']}")
    ax3.plot(idx, rsi_w, color=PURPLE, lw=0.8, linestyle="-.", label=f"RSI-W {data['rsi_w']}")
    ax3.plot(idx, rsi_m, color=GOLD,   lw=0.8, linestyle=":",  label=f"RSI-M {data['rsi_m']}")

    # Mark fresh daily crossover
    if data["fresh_d"] and data["fresh_d_bars"] <= n_bars:
        cx = len(idx) - data["fresh_d_bars"]
        ax3.axvline(cx, color=GREEN, lw=0.8, linestyle="--", alpha=0.6)
        ax3.text(cx, 72, "FRESH", color=GREEN, fontsize=5.5, ha="center")

    ax3.set_ylim(10, 90)
    ax3.set_ylabel("RSI", color=TXT, fontsize=7)
    ax3.legend(loc="upper left", facecolor=BG, edgecolor=GREY,
               labelcolor=TXT, fontsize=6, ncol=4)

    # ── Panel 4: MACD ─────────────────────────────────────────────
    ax4 = axes[3]
    ax4.axhline(0, color=GREY, lw=0.6)
    colors = [GREEN if v >= 0 else RED for v in macd_h]
    ax4.bar(idx, macd_h, color=colors, width=0.7, alpha=0.6, linewidth=0)
    ax4.plot(idx, macd_l, color=CYAN,   lw=1.0, label=f"MACD {data['macd_l']:.3f}")
    ax4.plot(idx, macd_s, color=ORANGE, lw=0.8, linestyle="--",
             label=f"Signal {data['macd_s']:.3f}")
    ax4.set_ylabel("MACD(12,26)", color=TXT, fontsize=7)
    ax4.legend(loc="upper left", facecolor=BG, edgecolor=GREY,
               labelcolor=TXT, fontsize=6, ncol=2)

    # ── Panel 5: CCI ──────────────────────────────────────────────
    ax5 = axes[4]
    ax5.axhline(100,  color=RED,   lw=0.6, linestyle="--", alpha=0.7)
    ax5.axhline(0,    color=GREY,  lw=0.5)
    ax5.axhline(-100, color=GREEN, lw=0.6, linestyle="--", alpha=0.7)
    cci_clr = [GREEN if v >= 0 else RED for v in cci]
    ax5.bar(idx, cci, color=cci_clr, width=0.7, alpha=0.55, linewidth=0)
    ax5.plot(idx, cci, color=CYAN, lw=0.8, label=f"CCI(20) {data['cci']:.1f}")
    ax5.set_ylabel(f"CCI({CCI_P})", color=TXT, fontsize=7)
    ax5.legend(loc="upper left", facecolor=BG, edgecolor=GREY,
               labelcolor=TXT, fontsize=6)

    plt.tight_layout(rect=[0, 0, 1, 0.995])

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=85, bbox_inches="tight",
                facecolor=BG)
    buf.seek(0)
    b64 = base64.b64encode(buf.read()).decode("utf-8")
    plt.close(fig)
    return b64


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 8 — HTML REPORT BUILDER
# ═════════════════════════════════════════════════════════════════════════════

_CSS = """
:root {
  --bg: #0d1117; --card: #161b22; --border: #30363d; --text: #c9d1d9;
  --sub: #8b949e; --green: #26d07c; --red: #ff4d6d; --gold: #ffd700;
  --cyan: #00d4ff; --purple: #b39ddb; --orange: #ff9800;
  --uptrend: #26d07c; --sideways: #ffd700; --bearish: #ff4d6d;
  --strong-buy: #00e676; --buy: #26d07c; --watch: #ffd700; --avoid: #ff4d6d;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text);
       font-family: 'Segoe UI', system-ui, sans-serif; font-size: 13px; }
a { color: var(--cyan); }

/* ── Header ───────────────────────────────────────── */
.header { background: #010409; border-bottom: 2px solid #21262d;
          padding: 22px 32px 18px; }
.header h1 { font-size: 22px; font-weight: 700; color: var(--cyan);
             letter-spacing: 1px; }
.header .subtitle { color: var(--sub); font-size: 12px; margin-top: 4px; }
.stats-row { display: flex; gap: 20px; margin-top: 14px; flex-wrap: wrap; }
.stat-box { background: var(--card); border: 1px solid var(--border);
            border-radius: 8px; padding: 10px 18px; min-width: 120px; }
.stat-box .val { font-size: 24px; font-weight: 700; }
.stat-box .lbl { font-size: 10px; color: var(--sub); margin-top: 2px; }
.stat-box.green .val { color: var(--green); }
.stat-box.gold  .val { color: var(--gold); }
.stat-box.red   .val { color: var(--red); }
.stat-box.cyan  .val { color: var(--cyan); }

/* ── Filter bar ───────────────────────────────────── */
.filter-bar { background: #010409; padding: 12px 32px;
              border-bottom: 1px solid var(--border);
              display: flex; gap: 8px; flex-wrap: wrap; position: sticky;
              top: 0; z-index: 100; }
.filter-btn { background: var(--card); border: 1px solid var(--border);
              color: var(--sub); border-radius: 20px; padding: 6px 16px;
              cursor: pointer; font-size: 12px; transition: all 0.15s; }
.filter-btn:hover, .filter-btn.active {
  background: var(--cyan); color: #000; border-color: var(--cyan);
  font-weight: 600; }

/* ── Summary table ────────────────────────────────── */
.table-wrap { overflow-x: auto; padding: 24px 32px 8px; }
.sum-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.sum-table th { background: #21262d; color: var(--sub); padding: 8px 10px;
                text-align: left; font-weight: 600; cursor: pointer;
                position: sticky; top: 0; white-space: nowrap; }
.sum-table th:hover { color: var(--cyan); }
.sum-table td { padding: 7px 10px; border-bottom: 1px solid #21262d;
                white-space: nowrap; }
.sum-table tr:hover td { background: #1c2128; }

/* ── Phase badges ─────────────────────────────────── */
.badge { display: inline-block; border-radius: 12px; padding: 2px 10px;
         font-size: 10px; font-weight: 700; letter-spacing: 0.5px; }
.badge-UPTREND  { background: #0d3320; color: var(--green); border: 1px solid #26d07c33; }
.badge-SIDEWAYS { background: #2d2600; color: var(--gold);  border: 1px solid #ffd70033; }
.badge-BEARISH  { background: #2d0a0a; color: var(--red);   border: 1px solid #ff4d6d33; }

.sig-strong-buy { color: #00e676; font-weight: 700; }
.sig-buy        { color: var(--green); font-weight: 600; }
.sig-watch      { color: var(--gold); }
.sig-avoid      { color: var(--red); }
.sig-neutral    { color: var(--sub); }

.fresh-tag { background: #002d40; color: var(--cyan); border-radius: 8px;
             padding: 1px 7px; font-size: 10px; font-weight: 700;
             border: 1px solid #00d4ff44; }

/* ── Stock cards ──────────────────────────────────── */
.cards-section { padding: 16px 32px 40px; }
.cards-section h2 { font-size: 14px; color: var(--sub); margin-bottom: 14px;
                    letter-spacing: 1px; }
.stock-card { background: var(--card); border: 1px solid var(--border);
              border-radius: 12px; margin-bottom: 28px;
              overflow: hidden; }
.card-header { display: flex; align-items: center; gap: 14px;
               padding: 14px 20px; background: #0d1117;
               border-bottom: 1px solid var(--border); flex-wrap: wrap; }
.card-header .ticker { font-size: 18px; font-weight: 700; color: var(--cyan); }
.card-header .price  { font-size: 16px; font-weight: 600; }
.card-header .score  { font-size: 13px; background: #21262d; border-radius: 8px;
                       padding: 3px 12px; color: var(--gold); font-weight: 700; }
.card-header .signal-label { font-size: 13px; font-weight: 700; }
.card-body { display: grid; grid-template-columns: 1fr; }
.chart-wrap img { width: 100%; display: block; }
.card-details { display: grid; grid-template-columns: repeat(2, 1fr);
                gap: 12px; border-top: 1px solid var(--border); padding: 12px; }

/* ── Detail panels ────────────────────────────────── */
.detail-panel { padding: 14px 16px; border: 1px solid var(--border); border-radius: 6px;
                background: #0d1117; }
.detail-panel h3 { font-size: 11px; text-transform: uppercase; letter-spacing: 1px;
                   color: var(--sub); margin-bottom: 10px; padding-bottom: 6px;
                   border-bottom: 1px solid var(--border); }

/* ── RSI table ────────────────────────────────────── */
.rsi-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.rsi-table th { color: var(--sub); text-align: left; padding: 4px 6px;
                font-size: 10px; font-weight: 600; }
.rsi-table td { padding: 5px 6px; border-bottom: 1px solid #21262d; }
.rsi-above { color: var(--green); }
.rsi-below { color: var(--red); }

/* ── Trade levels ─────────────────────────────────── */
.trade-row { display: flex; justify-content: space-between;
             padding: 5px 0; border-bottom: 1px solid #21262d; font-size: 12px; }
.trade-row:last-child { border-bottom: none; }
.trade-lbl { color: var(--sub); }
.trade-val { font-weight: 600; }
.trade-val.green { color: var(--green); }
.trade-val.red   { color: var(--red); }
.trade-val.gold  { color: var(--gold); }

/* ── Fib table ────────────────────────────────────── */
.fib-row { display: flex; justify-content: space-between; padding: 5px 0;
           border-bottom: 1px solid #21262d; font-size: 12px; }
.fib-row:last-child { border-bottom: none; }
.fib-lbl { color: var(--sub); font-size: 11px; }
.fib-val { font-weight: 700; }
.ext-val { color: #4caf50; }
.ret-val { color: #ff7043; }

/* ── History table ────────────────────────────────── */
.hist-table { width: 100%; border-collapse: collapse; font-size: 11px; }
.hist-table th { color: var(--sub); text-align: right; padding: 4px 6px;
                 font-size: 10px; font-weight: 600; }
.hist-table th:first-child, .hist-table th:nth-child(2),
.hist-table th:nth-child(3) { text-align: left; }
.hist-table td { padding: 4px 6px; border-bottom: 1px solid #21262d;
                 text-align: right; }
.hist-table td:first-child, .hist-table td:nth-child(2),
.hist-table td:nth-child(3) { text-align: left; }
.hist-buy  { color: var(--green); font-weight: 700; }
.hist-sell { color: var(--red);   font-weight: 700; }
.ret-pos   { color: var(--green); }
.ret-neg   { color: var(--red); }

/* ── Signals list ─────────────────────────────────── */
.sig-item { display: flex; align-items: center; gap: 8px; padding: 4px 0;
            border-bottom: 1px solid #21262d; font-size: 12px; }
.sig-item:last-child { border-bottom: none; }
.sig-dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
.sell-cond { color: var(--red); font-size: 11px; padding: 3px 0;
             border-bottom: 1px solid #21262d; }
.sell-cond:last-child { border-bottom: none; }
.entry-box { background: #0d2218; border: 1px solid #26d07c33;
             border-radius: 6px; padding: 8px 10px; margin-top: 8px;
             font-size: 11.5px; color: var(--green); }

/* ── Footer ───────────────────────────────────────── */
.footer { text-align: center; padding: 20px; color: var(--sub);
          font-size: 11px; border-top: 1px solid var(--border); }

/* ── Detail rows in summary table ──────────────────── */
.detail-row { background: #0d1117; }
.detail-row td { padding: 0 !important; }
.stock-details { padding: 16px; background: #161b22; border-radius: 8px;
                 margin: 8px 0; border: 1px solid var(--border); }
"""

_JS = """
function filterPhase(phase, btn) {
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.querySelectorAll('.stock-card').forEach(c => {
    c.style.display = (phase === 'all' || c.dataset.phase === phase ||
                       (phase === 'fresh' && c.dataset.fresh === '1')) ? '' : 'none';
  });
  // also filter table rows
  document.querySelectorAll('.sum-row').forEach(r => {
    r.style.display = (phase === 'all' || r.dataset.phase === phase ||
                       (phase === 'fresh' && r.dataset.fresh === '1')) ? '' : 'none';
  });
}

// Multi-column sort state
let sortState = { col: 'score', dir: 'desc' };

function sortTable(col) {
  const tbl = document.getElementById('sumtable');
  const rows = Array.from(tbl.querySelectorAll('tr.sum-row'));
  
  // Toggle direction if same column, reset to desc if different column
  if (sortState.col === col) {
    sortState.dir = sortState.dir === 'asc' ? 'desc' : 'asc';
  } else {
    sortState.col = col;
    sortState.dir = 'desc';
  }
  
  const multiplier = sortState.dir === 'desc' ? -1 : 1;
  
  rows.sort((a, b) => {
    const av = parseFloat(a.dataset[col]) || 0;
    const bv = parseFloat(b.dataset[col]) || 0;
    return (bv - av) * multiplier;
  });
  
  rows.forEach(r => tbl.querySelector('tbody').appendChild(r));
  
  // Update column header indicators
  document.querySelectorAll('.sum-table th').forEach(th => {
    th.style.color = 'var(--sub)';
  });
  // Highlight current sort column
  Array.from(document.querySelectorAll('.sum-table th')).forEach(th => {
    if (th.textContent.toLowerCase().includes(col.replace(/_/g, ' '))) {
      th.style.color = 'var(--cyan)';
    }
  });
}

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.stock-card').forEach(card => {
    card.querySelector('.card-header')?.addEventListener('click', () => {
      const body = card.querySelector('.card-body');
      body.style.display = body.style.display === 'none' ? '' : 'none';
    });
  });
});

function toggleDetails(ticker) {
  const detailRow = document.getElementById('details-' + ticker);
  if (detailRow.style.display === 'none' || detailRow.style.display === '') {
    detailRow.style.display = 'table-row';
  } else {
    detailRow.style.display = 'none';
  }
}
"""


def phase_badge(phase):
    return f'<span class="badge badge-{phase}">{phase}</span>'


def sig_span(signal, cls):
    return f'<span class="{cls}">{signal}</span>'


def ret_span(val):
    if val is None:
        return '<span style="color:#555">—</span>'
    cls = "ret-pos" if val >= 0 else "ret-neg"
    return f'<span class="{cls}">{val:+.1f}%</span>'


def build_summary_table(results):
    rows = ""
    for d in results:
        fresh_tag = ' <span class="fresh-tag">FRESH</span>' if (d["fresh_d"] or d["fresh_w"]) else ""
        # Generate detailed panels HTML for this stock
        detail_html = build_detail_panels(d)
        rows += f"""
        <tr class="sum-row" data-phase="{d['phase']}"
            data-fresh="{'1' if (d['fresh_d'] or d['fresh_w']) else '0'}"
            data-score="{d['score']}"
            data-rsi_d="{d['rsi_d']}"
            data-sma_d="{d['sma_d']}"
            data-rsi_w="{d['rsi_w']}"
            data-rsi_m="{d['rsi_m']}"
            data-cci_d="{d['cci']}"
            data-cci_w="{d['cci_w']}"
            data-cci_m="{d['cci_m']}"
            data-macd_d="{d['macd_l']}"
            data-macd_w="{d['macd_l_w']}"
            data-macd_m="{d['macd_l_m']}"
            data-close="{d['close']}"
            data-dist52="{d['dist52']}">
          <td><b style="color:var(--cyan);cursor:pointer" onclick="toggleDetails('{d['ticker']}')">{d['ticker']}</b>{fresh_tag}</td>
          <td>{phase_badge(d['phase'])}</td>
          <td>{sig_span(d['signal'], d['sig_cls'])}</td>
          <td style="text-align:right"><b>{d['score']}</b>/22</td>
          <td style="text-align:right">{d['rsi_d']}</td>
          <td style="text-align:right {'color:var(--green)' if d['rsi_d']>d['sma_d'] else 'color:var(--red)'}">{d['sma_d']}</td>
          <td style="text-align:right">{d['rsi_w']}</td>
          <td style="text-align:right">{d['rsi_m']}</td>
          <td style="text-align:right">{d['cci']}</td>
          <td style="text-align:right">{d['cci_w']}</td>
          <td style="text-align:right">{d['cci_m']}</td>
          <td style="text-align:right; {'color:var(--green)' if d['macd_l']>0 else 'color:var(--red)'}">{d['macd_l']:.3f}</td>
          <td style="text-align:right; {'color:var(--green)' if d['macd_l_w']>0 else 'color:var(--red)'}">{d['macd_l_w']:.3f}</td>
          <td style="text-align:right; {'color:var(--green)' if d['macd_l_m']>0 else 'color:var(--red)'}">{d['macd_l_m']:.3f}</td>
          <td style="text-align:right">₹{d['close']:,.2f}</td>
          <td style="text-align:right; {'color:var(--red)' if d['dist52']<-10 else 'color:var(--green)' if d['dist52']>-5 else ''}">{d['dist52']}%</td>
        </tr>
        <tr id="details-{d['ticker']}" class="detail-row" style="display:none">
          <td colspan="17" style="padding:0;border:none">
            <div class="stock-details">
              {detail_html}
            </div>
          </td>
        </tr>"""
    return f"""
    <div class="table-wrap">
      <table class="sum-table" id="sumtable" data-sort-dir="desc">
        <thead>
          <tr>
            <th>Ticker</th>
            <th>Phase</th>
            <th>Signal</th>
            <th onclick="sortTable('score')" style="text-align:right">Score ↕</th>
            <th onclick="sortTable('rsi_d')" style="text-align:right">D-RSI ↕</th>
            <th onclick="sortTable('sma_d')" style="text-align:right">D-SMA ↕</th>
            <th onclick="sortTable('rsi_w')" style="text-align:right">W-RSI ↕</th>
            <th onclick="sortTable('rsi_m')" style="text-align:right">M-RSI ↕</th>
            <th onclick="sortTable('cci_d')" style="text-align:right">D-CCI(20) ↕</th>
            <th onclick="sortTable('cci_w')" style="text-align:right">W-CCI(20) ↕</th>
            <th onclick="sortTable('cci_m')" style="text-align:right">M-CCI(20) ↕</th>
            <th onclick="sortTable('macd_d')" style="text-align:right">D-MACD ↕</th>
            <th onclick="sortTable('macd_w')" style="text-align:right">W-MACD ↕</th>
            <th onclick="sortTable('macd_m')" style="text-align:right">M-MACD ↕</th>
            <th onclick="sortTable('close')" style="text-align:right">Close ↕</th>
            <th onclick="sortTable('dist52')" style="text-align:right">52W% ↕</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </div>"""


def build_detail_panels(data):
    ticker  = data["ticker"]
    phase   = data["phase"]
    signal  = data["signal"]
    sig_cls = data["sig_cls"]
    score   = data["score"]
    close   = data["close"]

    # ── RSI + CCI + MACD table ────────────────────────────────────
    def rsi_row(tf, rsi_v, sma_v):
        above = rsi_v > sma_v
        cls   = "rsi-above" if above else "rsi-below"
        arrow = "▲" if above else "▼"
        cross = "YES" if (tf == "Daily" and data["fresh_d"]) or (tf == "Weekly" and data["fresh_w"]) else ""
        fresh_badge = ' <span class="fresh-tag">FRESH</span>' if cross else ""
        return (f'<tr><td>{tf}</td>'
                f'<td class="{cls}"><b>{rsi_v}</b></td>'
                f'<td>{sma_v}</td>'
                f'<td class="{cls}">{arrow} {"ABOVE" if above else "BELOW"}{fresh_badge}</td></tr>')

    def cci_row(tf, cci_v):
        if cci_v > 100:
            cls = "rsi-above"
            badge = '🚀 STRONG'
        elif cci_v > 0:
            cls = "rsi-above"
            badge = '✅ Positive'
        elif cci_v < -100:
            cls = "rsi-below"
            badge = '⚠️ EXTREME'
        else:
            cls = "rsi-below"
            badge = '❌ Negative'
        return f'<tr><td>{tf}</td><td class="{cls}"><b>{cci_v}</b></td><td class="{cls}">{badge}</td></tr>'

    def macd_row(tf, macd_l, macd_s):
        if macd_l > macd_s:
            cls = "rsi-above"
            status = '▲ BULLISH'
        else:
            cls = "rsi-below"
            status = '▼ BEARISH'
        return f'<tr><td>{tf}</td><td class="{cls}"><b>{macd_l:.3f}</b></td><td class="{cls}">{status}</td></tr>'

    rsi_html = f"""
    <table class="rsi-table">
      <tr><th>TF</th><th>RSI(14)</th><th>SMA(14)</th><th>Status</th></tr>
      {rsi_row("Daily",   data["rsi_d"], data["sma_d"])}
      {rsi_row("Weekly",  data["rsi_w"], data["sma_w"])}
      {rsi_row("Monthly", data["rsi_m"], data["sma_m"])}
    </table>"""
    
    cci_html = f"""
    <table class="rsi-table">
      <tr><th>TF</th><th>CCI(20)</th><th>Signal</th></tr>
      {cci_row("Daily",   data["cci"])}
      {cci_row("Weekly",  data["cci_w"])}
      {cci_row("Monthly", data["cci_m"])}
    </table>"""
    
    macd_html = f"""
    <table class="rsi-table">
      <tr><th>TF</th><th>MACD Line</th><th>Status</th></tr>
      {macd_row("Daily",   data["macd_l"], data["macd_s"])}
      {macd_row("Weekly",  data["macd_l_w"], data["macd_s_w"])}
      {macd_row("Monthly", data["macd_l_m"], data["macd_s_m"])}
    </table>"""

    # ── Trade levels ───────────────────────────────────────────────
    r_sl_pct = round((data["atr_sl"]   / close - 1) * 100, 1)
    s_sl_pct = round((data["swing_sl"] / close - 1) * 100, 1)
    trade_html = f"""
    <div class="trade-row"><span class="trade-lbl">Current Price</span>
         <span class="trade-val gold">₹{close:,.2f}</span></div>
    <div class="trade-row"><span class="trade-lbl">ATR(14) Stop Loss</span>
         <span class="trade-val red">₹{data['atr_sl']:,.2f} ({r_sl_pct:+.1f}%)</span></div>
    <div class="trade-row"><span class="trade-lbl">Swing Low Stop Loss</span>
         <span class="trade-val red">₹{data['swing_sl']:,.2f} ({s_sl_pct:+.1f}%)</span></div>
    <div class="trade-row"><span class="trade-lbl">52W High</span>
         <span class="trade-val">₹{data['high52']:,.2f}</span></div>
    <div class="trade-row"><span class="trade-lbl">52W Low</span>
         <span class="trade-val">₹{data['low52']:,.2f}</span></div>
    <div class="entry-box">💡 {data['entry_note']}</div>
    <div style="margin-top:10px;font-size:10px;color:var(--sub);font-weight:700;
                text-transform:uppercase;letter-spacing:1px;margin-bottom:5px">
         SELL / EXIT when:</div>
    {"".join(f'<div class="sell-cond">⚠ {c}</div>' for c in data['sell_conds'])}
    """

    # ── Fibonacci ──────────────────────────────────────────────────
    fib_color = "ext-val" if data["fib_type"] == "EXTENSION" else "ret-val"
    fib_label = ("🎯 Upside Targets (Fibonacci Extension)"
                 if data["fib_type"] == "EXTENSION"
                 else "🛡️ Support Zones (Fibonacci Retracement)")
    fib_rows  = ""
    for lvl, price in data["fib_levels"].items():
        pct = round((price / close - 1) * 100, 1)
        fib_rows += (f'<div class="fib-row">'
                     f'<span class="fib-lbl">{lvl} extension</span>'
                     f'<span class="fib-val {fib_color}">₹{price:,.2f} '
                     f'<span style="color:var(--sub);font-size:10px">{pct:+.1f}%</span></span></div>'
                     if data["fib_type"] == "EXTENSION" else
                     f'<div class="fib-row">'
                     f'<span class="fib-lbl">{lvl} retracement</span>'
                     f'<span class="fib-val {fib_color}">₹{price:,.2f} '
                     f'<span style="color:var(--sub);font-size:10px">{pct:+.1f}%</span></span></div>')
    fib_html = f"""
    <div style="font-size:11px;color:var(--sub);margin-bottom:8px">{data['fib_base']}</div>
    {fib_rows if fib_rows else '<div style="color:var(--sub)">No relevant levels near current price</div>'}
    """

    # ── Current signal indicators ─────────────────────────────────
    dot_colors = {
        "✅": "#26d07c", "🚀": "#00d4ff", "🔥": "#ff9800",
        "💪": "#b39ddb", "💰": "#ffd700",
    }
    sigs_html = ""
    for s in data["sig_list"]:
        dot_clr = "#26d07c"
        for emoji, col in dot_colors.items():
            if emoji in s:
                dot_clr = col; break
        sigs_html += (f'<div class="sig-item">'
                      f'<div class="sig-dot" style="background:{dot_clr}"></div>'
                      f'<span>{s}</span></div>')

    # ── Historical signals table ───────────────────────────────────
    hist_rows = ""
    for s in reversed(data["hist_sigs"]):
        tc    = "hist-buy" if s["type"] == "BUY" else "hist-sell"
        hist_rows += (f'<tr><td>{s["date"]}</td>'
                      f'<td class="{tc}">{s["type"]}</td>'
                      f'<td>₹{s["price"]:,.2f}</td>'
                      f'<td>RSI {s["rsi"]}</td>'
                      f'<td>{ret_span(s["r5d"])}</td>'
                      f'<td>{ret_span(s["r10d"])}</td>'
                      f'<td>{ret_span(s["r20d"])}</td></tr>')
    hist_html = f"""
    <table class="hist-table">
      <tr><th>Date</th><th>Type</th><th>Price</th><th>RSI</th>
          <th>5D Ret</th><th>10D Ret</th><th>20D Ret</th></tr>
      {hist_rows if hist_rows else '<tr><td colspan=7 style="color:var(--sub)">No signals in history</td></tr>'}
    </table>"""

    return f"""
    <div class="card-details">
      <!-- RSI Multi-Timeframe -->
      <div class="detail-panel">
        <h3>📊 RSI (Daily · Weekly · Monthly)</h3>
        {rsi_html}
      </div>
      <!-- CCI Multi-Timeframe -->
      <div class="detail-panel">
        <h3>🎯 CCI(20) (Daily · Weekly · Monthly)</h3>
        {cci_html}
      </div>
      <!-- MACD Multi-Timeframe -->
      <div class="detail-panel">
        <h3>📈 MACD(12,26) (Daily · Weekly · Monthly)</h3>
        {macd_html}
      </div>
      <!-- Trade Levels -->
      <div class="detail-panel">
        <h3>💼 Entry / Stop Loss / Exit</h3>
        {trade_html}
      </div>
      <!-- Fibonacci -->
      <div class="detail-panel">
        <h3>📐 {fib_label}</h3>
        {fib_html}
      </div>
      <!-- Active Signals -->
      <div class="detail-panel">
        <h3>⚡ Active Signals</h3>
        {sigs_html if sigs_html else '<div style="color:var(--sub)">No active signals</div>'}
      </div>
      <!-- Historical -->
      <div class="detail-panel" style="grid-column: 1 / -1">
        <h3>📅 Historical RSI Crossover Signals (Daily) — recent first</h3>
        {hist_html}
      </div>
    </div>"""
    ticker  = data["ticker"]
    phase   = data["phase"]
    signal  = data["signal"]
    sig_cls = data["sig_cls"]
    score   = data["score"]
    close   = data["close"]

    fresh_tags = ""
    if data["fresh_d"]:
        fresh_tags += f' <span class="fresh-tag">🚀 Daily Fresh ({data["fresh_d_bars"]}d ago)</span>'
    if data["fresh_w"]:
        fresh_tags += f' <span class="fresh-tag">📅 Weekly Fresh ({data["fresh_w_bars"]}w ago)</span>'

    # ── RSI + CCI + MACD table ────────────────────────────────────
    def rsi_row(tf, rsi_v, sma_v):
        above = rsi_v > sma_v
        cls   = "rsi-above" if above else "rsi-below"
        arrow = "▲" if above else "▼"
        cross = "YES" if (tf == "Daily" and data["fresh_d"]) or (tf == "Weekly" and data["fresh_w"]) else ""
        fresh_badge = ' <span class="fresh-tag">FRESH</span>' if cross else ""
        return (f'<tr><td>{tf}</td>'
                f'<td class="{cls}"><b>{rsi_v}</b></td>'
                f'<td>{sma_v}</td>'
                f'<td class="{cls}">{arrow} {"ABOVE" if above else "BELOW"}{fresh_badge}</td></tr>')

    def cci_row(tf, cci_v):
        if cci_v > 100:
            cls = "rsi-above"
            badge = '🚀 STRONG'
        elif cci_v > 0:
            cls = "rsi-above"
            badge = '✅ Positive'
        elif cci_v < -100:
            cls = "rsi-below"
            badge = '⚠️ EXTREME'
        else:
            cls = "rsi-below"
            badge = '❌ Negative'
        return f'<tr><td>{tf}</td><td class="{cls}"><b>{cci_v}</b></td><td class="{cls}">{badge}</td></tr>'

    def macd_row(tf, macd_l, macd_s):
        if macd_l > macd_s:
            cls = "rsi-above"
            status = '▲ BULLISH'
        else:
            cls = "rsi-below"
            status = '▼ BEARISH'
        return f'<tr><td>{tf}</td><td class="{cls}"><b>{macd_l:.3f}</b></td><td class="{cls}">{status}</td></tr>'

    rsi_html = f"""
    <table class="rsi-table">
      <tr><th>TF</th><th>RSI(14)</th><th>SMA(14)</th><th>Status</th></tr>
      {rsi_row("Daily",   data["rsi_d"], data["sma_d"])}
      {rsi_row("Weekly",  data["rsi_w"], data["sma_w"])}
      {rsi_row("Monthly", data["rsi_m"], data["sma_m"])}
    </table>"""
    
    cci_html = f"""
    <table class="rsi-table">
      <tr><th>TF</th><th>CCI(20)</th><th>Signal</th></tr>
      {cci_row("Daily",   data["cci"])}
      {cci_row("Weekly",  data["cci_w"])}
      {cci_row("Monthly", data["cci_m"])}
    </table>"""
    
    macd_html = f"""
    <table class="rsi-table">
      <tr><th>TF</th><th>MACD Line</th><th>Status</th></tr>
      {macd_row("Daily",   data["macd_l"], data["macd_s"])}
      {macd_row("Weekly",  data["macd_l_w"], data["macd_s_w"])}
      {macd_row("Monthly", data["macd_l_m"], data["macd_s_m"])}
    </table>"""

    # ── Trade levels ───────────────────────────────────────────────
    r_sl_pct = round((data["atr_sl"]   / close - 1) * 100, 1)
    s_sl_pct = round((data["swing_sl"] / close - 1) * 100, 1)
    trade_html = f"""
    <div class="trade-row"><span class="trade-lbl">Current Price</span>
         <span class="trade-val gold">₹{close:,.2f}</span></div>
    <div class="trade-row"><span class="trade-lbl">ATR(14) Stop Loss</span>
         <span class="trade-val red">₹{data['atr_sl']:,.2f} ({r_sl_pct:+.1f}%)</span></div>
    <div class="trade-row"><span class="trade-lbl">Swing Low Stop Loss</span>
         <span class="trade-val red">₹{data['swing_sl']:,.2f} ({s_sl_pct:+.1f}%)</span></div>
    <div class="trade-row"><span class="trade-lbl">52W High</span>
         <span class="trade-val">₹{data['high52']:,.2f}</span></div>
    <div class="trade-row"><span class="trade-lbl">52W Low</span>
         <span class="trade-val">₹{data['low52']:,.2f}</span></div>
    <div class="entry-box">💡 {data['entry_note']}</div>
    <div style="margin-top:10px;font-size:10px;color:var(--sub);font-weight:700;
                text-transform:uppercase;letter-spacing:1px;margin-bottom:5px">
         SELL / EXIT when:</div>
    {"".join(f'<div class="sell-cond">⚠ {c}</div>' for c in data['sell_conds'])}
    """

    # ── Fibonacci ──────────────────────────────────────────────────
    fib_color = "ext-val" if data["fib_type"] == "EXTENSION" else "ret-val"
    fib_label = ("🎯 Upside Targets (Fibonacci Extension)"
                 if data["fib_type"] == "EXTENSION"
                 else "🛡️ Support Zones (Fibonacci Retracement)")
    fib_rows  = ""
    for lvl, price in data["fib_levels"].items():
        pct = round((price / close - 1) * 100, 1)
        fib_rows += (f'<div class="fib-row">'
                     f'<span class="fib-lbl">{lvl} extension</span>'
                     f'<span class="fib-val {fib_color}">₹{price:,.2f} '
                     f'<span style="color:var(--sub);font-size:10px">{pct:+.1f}%</span></span></div>'
                     if data["fib_type"] == "EXTENSION" else
                     f'<div class="fib-row">'
                     f'<span class="fib-lbl">{lvl} retracement</span>'
                     f'<span class="fib-val {fib_color}">₹{price:,.2f} '
                     f'<span style="color:var(--sub);font-size:10px">{pct:+.1f}%</span></span></div>')
    fib_html = f"""
    <div style="font-size:11px;color:var(--sub);margin-bottom:8px">{data['fib_base']}</div>
    {fib_rows if fib_rows else '<div style="color:var(--sub)">No relevant levels near current price</div>'}
    """

    # ── Current signal indicators ─────────────────────────────────
    dot_colors = {
        "✅": "#26d07c", "🚀": "#00d4ff", "🔥": "#ff9800",
        "💪": "#b39ddb", "💰": "#ffd700",
    }
    sigs_html = ""
    for s in data["sig_list"]:
        dot_clr = "#26d07c"
        for emoji, col in dot_colors.items():
            if emoji in s:
                dot_clr = col; break
        sigs_html += (f'<div class="sig-item">'
                      f'<div class="sig-dot" style="background:{dot_clr}"></div>'
                      f'<span>{s}</span></div>')

    # ── Historical signals table ───────────────────────────────────
    hist_rows = ""
    for s in reversed(data["hist_sigs"]):
        tc    = "hist-buy" if s["type"] == "BUY" else "hist-sell"
        hist_rows += (f'<tr><td>{s["date"]}</td>'
                      f'<td class="{tc}">{s["type"]}</td>'
                      f'<td>₹{s["price"]:,.2f}</td>'
                      f'<td>RSI {s["rsi"]}</td>'
                      f'<td>{ret_span(s["r5d"])}</td>'
                      f'<td>{ret_span(s["r10d"])}</td>'
                      f'<td>{ret_span(s["r20d"])}</td></tr>')
    hist_html = f"""
    <table class="hist-table">
      <tr><th>Date</th><th>Type</th><th>Price</th><th>RSI</th>
          <th>5D Ret</th><th>10D Ret</th><th>20D Ret</th></tr>
      {hist_rows if hist_rows else '<tr><td colspan=7 style="color:var(--sub)">No signals in history</td></tr>'}
    </table>"""

    return f"""
<div class="stock-card" data-phase="{phase}" data-fresh="{'1' if (data['fresh_d'] or data['fresh_w']) else '0'}">
  <div class="card-header" style="cursor:pointer" title="Click to collapse">
    <span class="ticker">{ticker}</span>
    <span class="price">₹{close:,.2f}</span>
    {phase_badge(phase)}
    <span class="signal-label {sig_cls}">{signal}</span>
    <span class="score">Score {score}/22</span>
    {fresh_tags}
    <span style="margin-left:auto;color:var(--sub);font-size:11px">
      D-RSI {data['rsi_d']} | W-RSI {data['rsi_w']} | M-RSI {data['rsi_m']}
      | MACD {'▲' if data['macd_l']>data['macd_s'] else '▼'}
      | CCI {data['cci']}
    </span>
  </div>
  <div class="card-body">
    <div class="chart-wrap">
      <img src="data:image/png;base64,{chart_b64}" alt="{ticker} chart" loading="lazy">
    </div>
    <div class="card-details">
      <!-- RSI Multi-Timeframe -->
      <div class="detail-panel">
        <h3>📊 RSI (Daily · Weekly · Monthly)</h3>
        {rsi_html}
      </div>
      <!-- CCI Multi-Timeframe -->
      <div class="detail-panel">
        <h3>🎯 CCI(20) (Daily · Weekly · Monthly)</h3>
        {cci_html}
      </div>
      <!-- MACD Multi-Timeframe -->
      <div class="detail-panel">
        <h3>📈 MACD(12,26) (Daily · Weekly · Monthly)</h3>
        {macd_html}
      </div>
      <!-- Trade Levels -->
      <div class="detail-panel">
        <h3>💼 Entry / Stop Loss / Exit</h3>
        {trade_html}
      </div>
      <!-- Fibonacci -->
      <div class="detail-panel">
        <h3>📐 {fib_label}</h3>
        {fib_html}
      </div>
      <!-- Active Signals -->
      <div class="detail-panel">
        <h3>⚡ Active Signals</h3>
        {sigs_html if sigs_html else '<div style="color:var(--sub)">No active signals</div>'}
      </div>
      <!-- Historical -->
      <div class="detail-panel" style="grid-column: 1 / -1">
        <h3>📅 Historical RSI Crossover Signals (Daily) — recent first</h3>
        {hist_html}
      </div>
    </div>
  </div>
</div>"""


def build_stock_card(data, chart_b64):
    ticker  = data["ticker"]
    phase   = data["phase"]
    signal  = data["signal"]
    sig_cls = data["sig_cls"]
    score   = data["score"]
    close   = data["close"]

    fresh_tags = ""
    if data["fresh_d"]:
        fresh_tags += f' <span class="fresh-tag">🚀 Daily Fresh ({data["fresh_d_bars"]}d ago)</span>'
    if data["fresh_w"]:
        fresh_tags += f' <span class="fresh-tag">📅 Weekly Fresh ({data["fresh_w_bars"]}w ago)</span>'

    # ── RSI + CCI + MACD table ────────────────────────────────────
    def rsi_row(tf, rsi_v, sma_v):
        above = rsi_v > sma_v
        cls   = "rsi-above" if above else "rsi-below"
        arrow = "▲" if above else "▼"
        cross = "YES" if (tf == "Daily" and data["fresh_d"]) or (tf == "Weekly" and data["fresh_w"]) else ""
        fresh_badge = ' <span class="fresh-tag">FRESH</span>' if cross else ""
        return (f'<tr><td>{tf}</td>'
                f'<td class="{cls}"><b>{rsi_v}</b></td>'
                f'<td>{sma_v}</td>'
                f'<td class="{cls}">{arrow} {"ABOVE" if above else "BELOW"}{fresh_badge}</td></tr>')

    def cci_row(tf, cci_v):
        if cci_v > 100:
            cls = "rsi-above"
            badge = '🚀 STRONG'
        elif cci_v > 0:
            cls = "rsi-above"
            badge = '✅ Positive'
        elif cci_v < -100:
            cls = "rsi-below"
            badge = '⚠️ EXTREME'
        else:
            cls = "rsi-below"
            badge = '❌ Negative'
        return f'<tr><td>{tf}</td><td class="{cls}"><b>{cci_v}</b></td><td class="{cls}">{badge}</td></tr>'

    def macd_row(tf, macd_l, macd_s):
        if macd_l > macd_s:
            cls = "rsi-above"
            status = '▲ BULLISH'
        else:
            cls = "rsi-below"
            status = '▼ BEARISH'
        return f'<tr><td>{tf}</td><td class="{cls}"><b>{macd_l:.3f}</b></td><td class="{cls}">{status}</td></tr>'

    rsi_html = f"""
    <table class="rsi-table">
      <tr><th>TF</th><th>RSI(14)</th><th>SMA(14)</th><th>Status</th></tr>
      {rsi_row("Daily",   data["rsi_d"], data["sma_d"])}
      {rsi_row("Weekly",  data["rsi_w"], data["sma_w"])}
      {rsi_row("Monthly", data["rsi_m"], data["sma_m"])}
    </table>"""
    
    cci_html = f"""
    <table class="rsi-table">
      <tr><th>TF</th><th>CCI(20)</th><th>Signal</th></tr>
      {cci_row("Daily",   data["cci"])}
      {cci_row("Weekly",  data["cci_w"])}
      {cci_row("Monthly", data["cci_m"])}
    </table>"""
    
    macd_html = f"""
    <table class="rsi-table">
      <tr><th>TF</th><th>MACD(12,26)</th><th>Status</th></tr>
      {macd_row("Daily",   data["macd_l"], data["macd_s"])}
      {macd_row("Weekly",  data["macd_l_w"], data["macd_s_w"])}
      {macd_row("Monthly", data["macd_l_m"], data["macd_s_m"])}
    </table>"""

    # ── Trade levels ───────────────────────────────────────────────
    r_sl_pct = round((data["atr_sl"]   / close - 1) * 100, 1)
    s_sl_pct = round((data["swing_sl"] / close - 1) * 100, 1)
    trade_html = f"""
    <div class="trade-row"><span class="trade-lbl">Current Price</span>
         <span class="trade-val gold">₹{close:,.2f}</span></div>
    <div class="trade-row"><span class="trade-lbl">ATR(14) Stop Loss</span>
         <span class="trade-val red">₹{data['atr_sl']:,.2f} ({r_sl_pct:+.1f}%)</span></div>
    <div class="trade-row"><span class="trade-lbl">Swing Low Stop Loss</span>
         <span class="trade-val red">₹{data['swing_sl']:,.2f} ({s_sl_pct:+.1f}%)</span></div>
    <div class="trade-row"><span class="trade-lbl">52W High</span>
         <span class="trade-val">₹{data['high52']:,.2f}</span></div>
    <div class="trade-row"><span class="trade-lbl">52W Low</span>
         <span class="trade-val">₹{data['low52']:,.2f}</span></div>
    <div class="entry-box">💡 {data['entry_note']}</div>
    <div style="margin-top:10px;font-size:10px;color:var(--sub);font-weight:700;
                text-transform:uppercase;letter-spacing:1px;margin-bottom:5px">
         SELL / EXIT when:</div>
    {"".join(f'<div class="sell-cond">⚠ {c}</div>' for c in data['sell_conds'])}
    """

    # ── Fibonacci ──────────────────────────────────────────────────
    fib_color = "ext-val" if data["fib_type"] == "EXTENSION" else "ret-val"
    fib_label = ("🎯 Upside Targets (Fibonacci Extension)"
                 if data["fib_type"] == "EXTENSION"
                 else "🛡️ Support Zones (Fibonacci Retracement)")
    fib_rows  = ""
    for lvl, price in data["fib_levels"].items():
        pct = round((price / close - 1) * 100, 1)
        fib_rows += (f'<div class="fib-row">'
                     f'<span class="fib-lbl">{lvl} extension</span>'
                     f'<span class="fib-val {fib_color}">₹{price:,.2f} '
                     f'<span style="color:var(--sub);font-size:10px">{pct:+.1f}%</span></span></div>'
                     if data["fib_type"] == "EXTENSION" else
                     f'<div class="fib-row">'
                     f'<span class="fib-lbl">{lvl} retracement</span>'
                     f'<span class="fib-val {fib_color}">₹{price:,.2f} '
                     f'<span style="color:var(--sub);font-size:10px">{pct:+.1f}%</span></span></div>')
    fib_html = f"""
    <div style="font-size:11px;color:var(--sub);margin-bottom:8px">{data['fib_base']}</div>
    {fib_rows if fib_rows else '<div style="color:var(--sub)">No relevant levels near current price</div>'}
    """

    # ── Current signal indicators ─────────────────────────────────
    dot_colors = {
        "✅": "#26d07c", "🚀": "#00d4ff", "🔥": "#ff9800",
        "💪": "#b39ddb", "💰": "#ffd700",
    }
    sigs_html = ""
    for s in data["sig_list"]:
        dot_clr = "#26d07c"
        for emoji, col in dot_colors.items():
            if emoji in s:
                dot_clr = col; break
        sigs_html += (f'<div class="sig-item">'
                      f'<div class="sig-dot" style="background:{dot_clr}"></div>'
                      f'<span>{s}</span></div>')

    # ── Historical signals table ───────────────────────────────────
    hist_rows = ""
    for s in reversed(data["hist_sigs"]):
        tc    = "hist-buy" if s["type"] == "BUY" else "hist-sell"
        hist_rows += (f'<tr><td>{s["date"]}</td>'
                      f'<td class="{tc}">{s["type"]}</td>'
                      f'<td>₹{s["price"]:,.2f}</td>'
                      f'<td>RSI {s["rsi"]}</td>'
                      f'<td>{ret_span(s["r5d"])}</td>'
                      f'<td>{ret_span(s["r10d"])}</td>'
                      f'<td>{ret_span(s["r20d"])}</td></tr>')
    hist_html = f"""
    <table class="hist-table">
      <tr><th>Date</th><th>Type</th><th>Price</th><th>RSI</th>
          <th>5D Ret</th><th>10D Ret</th><th>20D Ret</th></tr>
      {hist_rows if hist_rows else '<tr><td colspan=7 style="color:var(--sub)">No signals in history</td></tr>'}
    </table>"""

    return f"""
<div class="stock-card" data-phase="{phase}" data-fresh="{'1' if (data['fresh_d'] or data['fresh_w']) else '0'}">
  <div class="card-header" style="cursor:pointer" title="Click to collapse">
    <span class="ticker">{ticker}</span>
    <span class="price">₹{close:,.2f}</span>
    {phase_badge(phase)}
    <span class="signal-label {sig_cls}">{signal}</span>
    <span class="score">Score {score}/22</span>
    {fresh_tags}
    <span style="margin-left:auto;color:var(--sub);font-size:11px">
      D-RSI {data['rsi_d']} | W-RSI {data['rsi_w']} | M-RSI {data['rsi_m']}
      | MACD {'▲' if data['macd_l']>data['macd_s'] else '▼'}
      | CCI {data['cci']}
    </span>
  </div>
  <div class="card-body">
    <div class="chart-wrap">
      <img src="data:image/png;base64,{chart_b64}" alt="{ticker} chart" loading="lazy">
    </div>
    <div class="card-details">
      <!-- RSI Multi-Timeframe -->
      <div class="detail-panel">
        <h3>📊 RSI (Daily · Weekly · Monthly)</h3>
        {rsi_html}
      </div>
      <!-- CCI Multi-Timeframe -->
      <div class="detail-panel">
        <h3>🎯 CCI(20) (Daily · Weekly · Monthly)</h3>
        {cci_html}
      </div>
      <!-- MACD Multi-Timeframe -->
      <div class="detail-panel">
        <h3>📈 MACD(12,26) (Daily · Weekly · Monthly)</h3>
        {macd_html}
      </div>
      <!-- Trade Levels -->
      <div class="detail-panel">
        <h3>💼 Entry / Stop Loss / Exit</h3>
        {trade_html}
      </div>
      <!-- Fibonacci -->
      <div class="detail-panel">
        <h3>📐 {fib_label}</h3>
        {fib_html}
      </div>
      <!-- Active Signals -->
      <div class="detail-panel">
        <h3>⚡ Active Signals</h3>
        {sigs_html if sigs_html else '<div style="color:var(--sub)">No active signals</div>'}
      </div>
      <!-- Historical -->
      <div class="detail-panel" style="grid-column: 1 / -1">
        <h3>📅 Historical RSI Crossover Signals (Daily) — recent first</h3>
        {hist_html}
      </div>
    </div>
  </div>
</div>"""


def build_html_report(all_results, top_results, run_ts, scanned):
    n_up   = sum(1 for d in all_results if d["phase"] == "UPTREND")
    n_sw   = sum(1 for d in all_results if d["phase"] == "SIDEWAYS")
    n_be   = sum(1 for d in all_results if d["phase"] == "BEARISH")
    n_fr   = sum(1 for d in all_results if d["fresh_d"] or d["fresh_w"])
    n_top  = len(top_results)

    stat_boxes = f"""
    <div class="stats-row">
      <div class="stat-box cyan"><div class="val">{scanned}</div><div class="lbl">Scanned</div></div>
      <div class="stat-box green"><div class="val">{n_up}</div><div class="lbl">📈 Uptrend</div></div>
      <div class="stat-box gold"><div class="val">{n_sw}</div><div class="lbl">➡️ Sideways</div></div>
      <div class="stat-box red"><div class="val">{n_be}</div><div class="lbl">📉 Bearish</div></div>
      <div class="stat-box cyan"><div class="val">{n_fr}</div><div class="lbl">🚀 Fresh Break</div></div>
    </div>"""

    filter_bar = f"""
    <div class="filter-bar">
      <button class="filter-btn active" data-filter="all"
              onclick="filterPhase('all',this)">All ({len(all_results)})</button>
      <button class="filter-btn" data-filter="fresh"
              onclick="filterPhase('fresh',this)">🚀 Fresh ({n_fr})</button>
      <button class="filter-btn" data-filter="UPTREND"
              onclick="filterPhase('UPTREND',this)">📈 Uptrend ({n_up})</button>
      <button class="filter-btn" data-filter="SIDEWAYS"
              onclick="filterPhase('SIDEWAYS',this)">➡️ Sideways ({n_sw})</button>
      <button class="filter-btn" data-filter="BEARISH"
              onclick="filterPhase('BEARISH',this)">📉 Bearish ({n_be})</button>
    </div>"""

    sum_table = build_summary_table(all_results)

    cards_html = ""
    print(f"\n  Generating charts for top {n_top} stocks...")
    for i, d in enumerate(top_results, 1):
        sys.stdout.write(f"\r  Chart {i}/{n_top}  {d['ticker']:<14}")
        sys.stdout.flush()
        try:
            b64 = generate_chart(d)
        except Exception as e:
            b64 = ""
            sys.stdout.write(f" [chart error: {e}]")
        cards_html += build_stock_card(d, b64)
    print()

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>RSI MTF Breakout Report — {run_ts}</title>
  <style>{_CSS}</style>
</head>
<body>
  <div class="header">
    <h1>📈 RSI Multi-Timeframe Breakout Report</h1>
    <div style="font-size:14px; color:#00d4ff; font-weight:bold; margin-bottom:8px;">
      📅 Generated: {run_ts} IST
    </div>
    <div class="subtitle">
      NSE EQ Universe &nbsp;|&nbsp; Run: {run_ts} IST &nbsp;|&nbsp;
      Strategy: RSI(14) D/W/M + MACD(12,26) + CCI(20) &nbsp;|&nbsp;
      Total Stocks Analyzed: {scanned}
    </div>
    {stat_boxes}
  </div>

  {filter_bar}

  <div style="padding:8px 32px 2px;color:var(--sub);font-size:11px">
    📋 Full results table — click column header <b>Score ↕</b> to sort. Click any card header to collapse/expand.
  </div>
  {sum_table}

  <div class="cards-section">
    <h2>🔍 DETAILED ANALYSIS — TOP {n_top} STOCKS BY SCORE (chart + Fibonacci + history)</h2>
    {cards_html}
  </div>

  <div class="footer">
    Generated by RSI MTF Breakout Report v1.0 &nbsp;|&nbsp; {run_ts} &nbsp;|&nbsp;
    <b>Not financial advice — for educational purposes only.</b><br>
    Entry: RSI(14) D+W+M above SMA + CCI(20)>0 + MACD>Signal &nbsp;|&nbsp;
    Exit: RSI crosses below SMA + CCI&lt;−100 &nbsp;|&nbsp;
    Targets: Fibonacci Extension from last swing &nbsp;|&nbsp;
    Stop Loss: 2×ATR(14) or below last swing low
  </div>

  <script>{_JS}</script>
</body>
</html>"""


# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════

def main():
    os.system("cls" if os.name == "nt" else "clear")
    print("╔═══════════════════════════════════════════════════════════════════════╗")
    print("║  📈  RSI MULTI-TIMEFRAME BREAKOUT HTML REPORT  v1.0                  ║")
    print("║      Daily · Weekly · Monthly RSI/SMA | MACD | CCI | Fibonacci       ║")
    print("╚═══════════════════════════════════════════════════════════════════════╝")
    print(f"   {RUN_TS} IST\n")

    # ── Universe ──────────────────────────────────────────────────
    print("▶  STEP 1/3  Build universe")
    tickers = load_universe()
    print(f"   Total: {len(tickers)} stocks\n")

    # ── Scan ──────────────────────────────────────────────────────
    print("▶  STEP 2/3  Download + analyse (Daily · Weekly · Monthly RSI)")
    print("   ─────────────────────────────────────────────────────────────")

    results = []
    errors  = 0
    t0      = time.time()
    total   = len(tickers)

    for i, ticker in enumerate(tickers, 1):
        pct  = i / total * 100
        fill = int(pct / 2)
        bar  = "█" * fill + "░" * (50 - fill)
        sys.stdout.write(
            f"\r  [{bar}] {pct:5.1f}%  {i:>4}/{total}  {ticker:<14}  "
            f"hits={len(results)}  err={errors}"
        )
        sys.stdout.flush()

        res = analyze_stock(ticker)
        if res:
            results.append(res)
        else:
            errors += 1

        if i % BATCH_SIZE == 0:
            time.sleep(BATCH_PAUSE)

    print(f"\n\n   ✓ {len(results)} analysed  |  {errors} skipped  |  {time.time()-t0:.0f}s\n")

    if not results:
        print("  ❌ No results — check internet connection.")
        sys.exit(1)

    # Sort by score descending
    results.sort(key=lambda d: d["score"], reverse=True)

    # Generate charts for ALL stocks (not just top N)
    top_n = results  # All stocks will have charts generated
   

    # Strip raw series from all_results (for summary table only) to save RAM
    all_light = []
    for d in results:
        ld = {k: v for k, v in d.items() if not k.startswith("_")}
        all_light.append(ld)

    # Print quick summary to terminal
    print("▶  STEP 3/3  Build HTML report")
    print("   ─────────────────────────────────────────────────────────────")
    print(f"   Uptrend : {sum(1 for d in results if d['phase']=='UPTREND')}")
    print(f"   Sideways: {sum(1 for d in results if d['phase']=='SIDEWAYS')}")
    print(f"   Bearish : {sum(1 for d in results if d['phase']=='BEARISH')}")
    print(f"   Fresh D : {sum(1 for d in results if d['fresh_d'])}")
    print(f"   Fresh W : {sum(1 for d in results if d['fresh_w'])}\n")
    print(f"  Generating charts for ALL {len(results)} stocks...")

    html = build_html_report(all_light, top_n, RUN_TS, len(results))

    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)

    size_kb = os.path.getsize(OUTPUT_HTML) / 1024
    print(f"\n  ✅ Report saved: {OUTPUT_HTML}  ({size_kb:.0f} KB)")
    print(f"     Open it in any browser — fully self-contained.\n")
    print("  ─────────────────────────────────────────────────────────────")
    print("  STRATEGY REFERENCE")
    print("  ENTRY : RSI(14) Daily + Weekly + Monthly ALL above their SMA(14)")
    print("          + CCI(20) > 0  +  MACD(12,26) > Signal")
    print("  FRESH : RSI-SMA crossover within last 3 daily / 2 weekly candles")
    print("  TARGET: Fibonacci Extension levels above last swing high (uptrend)")
    print("  SL    : 2×ATR(14) below entry  OR  1% below last swing low")
    print("  EXIT  : RSI(14) daily crosses BELOW SMA  OR  CCI(20) < −100")
    print("  ─────────────────────────────────────────────────────────────\n")


if __name__ == "__main__":
    main()
