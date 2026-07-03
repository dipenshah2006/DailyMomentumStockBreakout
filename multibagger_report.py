#!/usr/bin/env python3
"""
DailyMomentumStockBreakout — Full NSE + SME Scanner
Scans ALL NSE Cash + SME stocks (~2881 total) for:
  • Support / Resistance levels  (monthly + weekly charts)
  • Trend Channels               (monthly + weekly charts)
  • Darvas Box                   (daily / weekly / monthly)
  • BLAST column                 (strong daily breakout detection)
  • MTF signals, Fibonacci, Backtesting
  • Auto-push to GitHub at end
"""

import os
import io
import sys
import json
import time
import warnings
import subprocess
import threading
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.patches as mpl_patches
from matplotlib.gridspec import GridSpec

warnings.filterwarnings('ignore')

# Suppress noisy yfinance / urllib3 logs
import logging
logging.getLogger('yfinance').setLevel(logging.CRITICAL)
logging.getLogger('peewee').setLevel(logging.CRITICAL)
logging.getLogger('urllib3').setLevel(logging.CRITICAL)

# Force UTF-8 output on Windows (avoids cp1252 UnicodeEncodeError with emoji)
if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

# ── Constants ────────────────────────────────────────────────────────────────
REPORT_HTML    = "multibagger_report.html"
CHARTS_DIR     = Path("charts/multibagger")
CACHE_FILE     = Path("charts/multibagger/scan_cache.json")
LOOKBACK_YEARS = 'max'       # fetch full history from IPO date
MAX_WORKERS    = 20           # parallel workers (increased for I/O-bound workloads)
BATCH_SIZE     = 50           # tickers per yfinance batch download
MIN_DAILY_BARS = 10            # accept stocks with as few as 10 trading days (new IPOs)

NSE_CASH_CSV   = Path("india/NSE/NSECash/EQUITY_L.csv")
NSE_SME_CSV    = Path("india/NSE/NSESME/MW-SME-05-May-2026.csv")

# Strategy thresholds
M_RSI_STRONG  = 70
W_RSI_BULL    = 55
D_RSI_ENTRY   = 60
VOL_SURGE     = 1.5

# MACD configs
MACD_FAST, MACD_SLOW, MACD_SIG  = 12, 26, 9
MACD_SF, MACD_SS, MACD_SSIG     = 34, 1000, 20

# Blast thresholds
BLAST_VOL_RATIO = 2.0
BLAST_RSI_MIN   = 52
BLAST_RSI_MAX   = 85

MPLSTYLE = {
    'axes.facecolor':   '#0d1117',
    'figure.facecolor': '#0d1117',
    'axes.edgecolor':   '#30363d',
    'axes.labelcolor':  '#8b949e',
    'xtick.color':      '#8b949e',
    'ytick.color':      '#8b949e',
    'grid.color':       '#21262d',
    'text.color':       '#e6edf3',
    'grid.linestyle':   '--',
    'grid.linewidth':   0.4,
}

_print_lock = threading.Lock()

def tprint(*args, **kwargs):
    with _print_lock:
        try:
            print(*args, **kwargs)
        except UnicodeEncodeError:
            # Fallback: encode to ascii, replacing unmappable chars
            safe = [str(a).encode('ascii', 'replace').decode('ascii') for a in args]
            print(*safe, **kwargs)

# ── Load Stock Universe ───────────────────────────────────────────────────────
def load_nse_stocks():
    stocks = {}   # name → ticker

    # NSE Cash (EQUITY_L.csv) — EQ series only  [vectorised: no iterrows]
    if NSE_CASH_CSV.exists():
        try:
            df = pd.read_csv(NSE_CASH_CSV)
            df.columns = [c.strip() for c in df.columns]
            ser_col = next((c for c in df.columns if 'SERIES' in c.upper()), None)
            if ser_col:
                df = df[df[ser_col].str.strip() == 'EQ'].copy()
            name_col = next((c for c in df.columns if 'NAME' in c.upper()), None)
            symbols = df['SYMBOL'].astype(str).str.strip()
            names   = df[name_col].astype(str).str.strip() if name_col else symbols
            stocks.update({n: f"{s}.NS" for s, n in zip(symbols, names)})
            tprint(f"  📋 NSE Cash (EQ): {len(stocks)} stocks")
        except Exception as e:
            tprint(f"  ⚠️  NSE Cash CSV error: {e}")

    # NSE SME (MW-SME-05-May-2026.csv)  [vectorised: no iterrows]
    sme_start = len(stocks)
    if NSE_SME_CSV.exists():
        try:
            df = pd.read_csv(NSE_SME_CSV)
            df.columns = [c.strip() for c in df.columns]
            sym_col = next((c for c in df.columns if 'SYMBOL' in c.upper()), None)
            if sym_col:
                syms = df[sym_col].astype(str).str.strip().str.strip('"')
                valid = syms[~syms.isin(['SYMBOL', 'nan', '']) & (syms.str.len() > 0)]
                for sym in valid:
                    stocks[f"{sym} (SME)"] = f"{sym}.NS"
            sme_count = len(stocks) - sme_start
            tprint(f"  📋 NSE SME: {sme_count} stocks")
        except Exception as e:
            tprint(f"  ⚠️  NSE SME CSV error: {e}")

    return stocks

# ── Technical Helpers ─────────────────────────────────────────────────────────
def rsi(series, period=14):
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(com=period-1, min_periods=period).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=period-1, min_periods=period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def atr(high, low, close, period=14):
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(com=period-1, min_periods=period).mean()

def adx_calc(high, low, close, period=14):
    tr  = atr(high, low, close, period)
    up  = high.diff().clip(lower=0)
    dn  = (-low.diff()).clip(lower=0)
    dmp = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=high.index)
    dmn = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=high.index)
    dmp = dmp.ewm(com=period-1, min_periods=period).mean()
    dmn = dmn.ewm(com=period-1, min_periods=period).mean()
    dip = 100 * dmp / tr.replace(0, np.nan)
    din = 100 * dmn / tr.replace(0, np.nan)
    dx  = 100 * (dip - din).abs() / (dip + din).replace(0, np.nan)
    return dx.ewm(com=period-1, min_periods=period).mean()

def macd_calc(close, fast=12, slow=26, signal=9):
    ema_f = close.ewm(span=fast,   adjust=False).mean()
    ema_s = close.ewm(span=slow,   adjust=False).mean()
    line  = ema_f - ema_s
    sig   = line.ewm(span=signal,  adjust=False).mean()
    return line, sig, line - sig

def bollinger(close, period=20, std_dev=2):
    mid = close.rolling(period).mean()
    std = close.rolling(period).std()
    return mid - std_dev*std, mid, mid + std_dev*std

def obv_calc(close, volume):
    return (np.sign(close.diff()).fillna(0) * volume).cumsum()

def stochastic(high, low, close, k=14, d=3):
    lo_k = low.rolling(k).min()
    hi_k = high.rolling(k).max()
    stk  = 100 * (close - lo_k) / (hi_k - lo_k).replace(0, np.nan)
    return stk, stk.rolling(d).mean()

def psar(high, low, iaf=0.02, maxaf=0.2):
    """Parabolic SAR — raw numpy loop avoids pandas .iloc overhead (~3× faster)."""
    h = high.values if hasattr(high, 'values') else np.asarray(high)
    l = low.values  if hasattr(low,  'values') else np.asarray(low)
    n = len(h)
    s = np.full(n, np.nan)

    bull = True
    af   = iaf
    hp   = float(h[0])
    lp   = float(l[0])
    ep   = float(l[0])
    s[0] = lp

    for i in range(1, n):
        if bull:
            s[i] = s[i-1] + af * (hp - s[i-1])
            s[i] = min(s[i], l[i-1], l[max(0, i-2)])
            if l[i] < s[i]:
                bull, af, ep, s[i] = False, iaf, float(l[i]), hp
            else:
                if h[i] > hp:
                    hp = float(h[i])
                    af = min(af + iaf, maxaf)
        else:
            s[i] = s[i-1] + af * (ep - s[i-1])
            s[i] = max(s[i], h[i-1], h[max(0, i-2)])
            if h[i] > s[i]:
                bull, af, ep, s[i], hp = True, iaf, float(h[i]), lp, float(h[i])
            else:
                if l[i] < ep:
                    ep = float(l[i])
                    af = min(af + iaf, maxaf)
            lp = float(l[i]) if not bull else lp

    return pd.Series(s, index=high.index)

# ── Support / Resistance Detection ───────────────────────────────────────────
def find_support_resistance(df, window=10, tolerance=0.015, min_touches=2, max_levels=5):
    """Find key S/R levels using pivot clustering (numpy-accelerated: no .iloc)."""
    h = df['High'].values   # raw numpy arrays — avoids repeated pandas indexing
    l = df['Low'].values
    n = len(h)
    res_raw, sup_raw = [], []

    for i in range(window, n - window):
        seg_h = h[i - window: i + window + 1]
        seg_l = l[i - window: i + window + 1]
        if h[i] == seg_h.max():   # local high  → resistance pivot
            res_raw.append(h[i])
        if l[i] == seg_l.min():   # local low   → support pivot
            sup_raw.append(l[i])

    def cluster(levels):
        if not levels:
            return []
        levels = sorted(levels)
        clusters = [[levels[0]]]
        for lv in levels[1:]:
            if lv <= clusters[-1][-1] * (1 + tolerance):
                clusters[-1].append(lv)
            else:
                clusters.append([lv])
        result = []
        for c in clusters:
            if len(c) >= min_touches:
                result.append((np.mean(c), len(c)))
        result.sort(key=lambda x: -x[1])
        return [r[0] for r in result[:max_levels]]

    return cluster(res_raw), cluster(sup_raw)

# ── Trend Channel (linear regression) ────────────────────────────────────────
def trend_channel(df, lookback=60):
    """Return (trend_vals, upper_vals, lower_vals, index) for plotting."""
    data = df.tail(lookback).copy()
    if len(data) < 20:
        return None
    x   = np.arange(len(data))
    y   = data['Close'].values
    coeffs = np.polyfit(x, y, 1)
    trend  = np.polyval(coeffs, x)
    resid  = y - trend
    std    = np.std(resid)
    return trend, trend + 2*std, trend - 2*std, data.index

# ── Darvas Box Detection ──────────────────────────────────────────────────────
def darvas_boxes(df, confirm_days=3):
    """
    Detect Darvas Boxes.
    Returns list of dicts: {box_start, box_end, top, bottom, breakout, breakdown}
    """
    if len(df) < 30:
        return []

    highs  = df['High'].values
    lows   = df['Low'].values
    closes = df['Close'].values
    dates  = df.index
    boxes  = []

    i = confirm_days
    while i < len(df) - 1:
        # Find candidate box top: a high that wasn't exceeded for confirm_days
        box_top = highs[i]
        if all(highs[i+1:i+1+confirm_days] <= box_top):
            # Box top established — now find box bottom
            j = i + confirm_days
            box_bottom = lows[i]
            while j < len(df) and highs[j] <= box_top * 1.002:
                if lows[j] < box_bottom:
                    box_bottom = lows[j]
                j += 1
            if j < len(df) and j - i >= confirm_days:
                # Check breakout or breakdown
                breakout  = bool(closes[j] > box_top)
                breakdown = bool(closes[j] < box_bottom)
                boxes.append({
                    'box_start': dates[i],
                    'box_end':   dates[min(j, len(df)-1)],
                    'top':       float(box_top),
                    'bottom':    float(box_bottom),
                    'breakout':  breakout,
                    'breakdown': breakdown,
                })
            i = j + 1
        else:
            i += 1

    return boxes[-10:]  # Keep last 10 boxes

def darvas_latest_status(boxes):
    """Return latest Darvas box status for the table."""
    if not boxes:
        return 'None', 0.0, 0.0
    b = boxes[-1]
    status = 'BREAKOUT' if b['breakout'] else ('BREAKDOWN' if b['breakdown'] else 'IN BOX')
    return status, b['top'], b['bottom']

# ── Blast Signal Detection ────────────────────────────────────────────────────
def detect_blast(df_d, resistance_levels, lookback_days=3):
    """
    BLAST = strong daily breakout:
      - Closed above nearest resistance OR near 52-week high
      - Volume ≥ BLAST_VOL_RATIO × 20-day avg
      - RSI in [BLAST_RSI_MIN, BLAST_RSI_MAX]
      - Price above EMA21 and EMA50
    Returns (is_blast: bool, blast_score: int 0-100, reason: str)
    """
    if len(df_d) < 22:
        return False, 0, ''

    recent  = df_d.tail(lookback_days)
    last    = df_d.iloc[-1]
    close   = float(last['Close'])
    vol_r   = float(last.get('VOL_RATIO', 1.0))
    d_rsi   = float(last.get('RSI', 50))
    ema21   = float(last.get('EMA21', close))
    ema50   = float(last.get('EMA50', close))
    high52  = float(last.get('HIGH52W', close))
    ath     = float(last.get('ATH', close))

    score  = 0
    reason = []

    # Volume surge
    if vol_r < BLAST_VOL_RATIO:
        return False, 0, ''

    # RSI check
    if not (BLAST_RSI_MIN <= d_rsi <= BLAST_RSI_MAX):
        return False, 0, ''

    # Price above key MAs
    if close < ema21 or close < ema50:
        return False, 0, ''

    # Score components
    score += min(30, int((vol_r - BLAST_VOL_RATIO) * 15))   # volume bonus
    score += min(20, int((d_rsi - 50) * 0.7))                # RSI bonus

    # 52-week high breakout
    if close >= high52 * 0.998:
        score += 30
        reason.append('52W High')
    # ATH breakout
    if close >= ath * 0.998:
        score += 20
        reason.append('ATH')
    # Resistance breakout
    if resistance_levels:
        for lvl in resistance_levels:
            if close > lvl * 1.001 and close < lvl * 1.05:
                score += 15
                reason.append(f'Res ₹{lvl:,.0f}')
                break

    score = min(100, score)
    is_blast = score >= 30 and bool(reason)

    return is_blast, score, ' + '.join(reason) if reason else 'Vol Surge'

# ── Data Fetching ─────────────────────────────────────────────────────────────
def fetch_data(ticker):
    """Single-ticker full-history download. Returns OHLCV DataFrame or None."""
    import yfinance as yf
    try:
        df = yf.download(
            ticker,
            period='max',
            progress=False, auto_adjust=True, actions=False
        )
        if df is None or df.empty or len(df) < MIN_DAILY_BARS:
            return None
        df.index = pd.to_datetime(df.index)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df[['Open','High','Low','Close','Volume']].copy()
        df.dropna(inplace=True)
        return df
    except Exception:
        return None


def batch_fetch(tickers, max_workers=30):
    """Download full OHLCV history for multiple tickers in parallel.
    Uses individual fetch_data() calls via ThreadPoolExecutor — reliable for
    period='max' which yfinance batch-download handles inconsistently.
    Returns dict {ticker: DataFrame}.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    tickers = list(tickers)
    result  = {}
    if not tickers:
        return result
    with ThreadPoolExecutor(max_workers=max_workers) as exe:
        futs = {exe.submit(fetch_data, t): t for t in tickers}
        for fut in as_completed(futs):
            t = futs[fut]
            try:
                df = fut.result()
                if df is not None:
                    result[t] = df
            except Exception:
                pass
    return result


def chart_is_fresh(path):
    """Return True if the PNG was written today (IST midnight or later)."""
    if not path.exists():
        return False
    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    today_midnight = datetime(ist_now.year, ist_now.month, ist_now.day)
    return datetime.fromtimestamp(path.stat().st_mtime) >= today_midnight


def add_indicators(df):
    """Full indicator set for daily timeframe.
    Uses adaptive min_periods so recently-listed stocks (< 200 bars) still get values.
    """
    n   = len(df)
    h, l, c, v = df['High'], df['Low'], df['Close'], df['Volume']

    for p in [9, 21, 50, 200]:
        df[f'EMA{p}'] = c.ewm(span=p, adjust=False, min_periods=min(p, n)).mean()
    for p in [20, 50, 200]:
        df[f'SMA{p}'] = c.rolling(p, min_periods=min(p, n)).mean()

    rsi_p = min(14, max(2, n // 3))
    df['RSI']  = rsi(c, rsi_p)
    df['RSI9'] = rsi(c, min(9, rsi_p))

    df['MACD'], df['MACD_sig'], df['MACD_hist'] = macd_calc(c, MACD_FAST, MACD_SLOW, MACD_SIG)
    df['MACD_US'], df['MACD_US_sig'], df['MACD_US_hist'] = macd_calc(c, MACD_SF, MACD_SS, MACD_SSIG)

    adx_p = min(14, max(2, n // 3))
    df['ADX'] = adx_calc(h, l, c, adx_p)
    df['ATR'] = atr(h, l, c, adx_p)

    df['STOCH_K'], df['STOCH_D'] = stochastic(h, l, c)
    bb_p = min(20, max(2, n // 2))
    df['BB_lo'], df['BB_mid'], df['BB_hi'] = bollinger(c, bb_p, 2)

    df['OBV']      = obv_calc(c, v)
    vm = min(20, max(2, n // 2))
    df['VOL_MA20']  = v.rolling(vm, min_periods=1).mean()
    df['VOL_RATIO'] = v / df['VOL_MA20'].replace(0, np.nan)

    df['ATH']      = c.expanding().max()
    prev_ath       = c.shift(1).expanding().max()
    df['ATH_PCT']  = ((c / prev_ath.replace(0, np.nan)) - 1) * 100
    df['ATH_BREAK']= c > prev_ath

    w52 = min(252, n)
    df['HIGH52W']  = h.rolling(w52, min_periods=1).max()
    df['LOW52W']   = l.rolling(w52, min_periods=1).min()

    try:
        df['SAR'] = psar(h, l)
    except Exception:
        df['SAR'] = np.nan

    return df


def resample_weekly(df):
    return df.resample('W').agg(
        {'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'}
    ).dropna()


def resample_monthly(df):
    return df.resample('ME').agg(
        {'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'}
    ).dropna()


# ── Progress Bar ──────────────────────────────────────────────────────────────
def progress_bar(done, total, width=40, prefix='', suffix=''):
    """Print an in-place ASCII progress bar.
    Call with done==total to finalize (prints newline).
    """
    pct  = done / max(total, 1)
    fill = int(width * pct)
    bar  = '#' * fill + '-' * (width - fill)
    line = f"\r  {prefix} [{bar}] {done}/{total} ({pct*100:.1f}%) {suffix}"
    sys.stdout.write(line)
    sys.stdout.flush()
    if done >= total:
        sys.stdout.write('\n')
        sys.stdout.flush()


def add_indicators_lite(df):
    """Lightweight indicator set for weekly / monthly frames.
    Uses min_periods=1 so recently-listed stocks with few bars still get values.
    """
    n   = len(df)
    h, l, c, v = df['High'], df['Low'], df['Close'], df['Volume']

    for p in [21, 50, 200]:
        df[f'EMA{p}'] = c.ewm(span=p, adjust=False, min_periods=min(p, max(1, n))).mean()

    df['RSI'] = rsi(c, min(14, max(2, n // 2)))

    df['MACD'], df['MACD_sig'], df['MACD_hist'] = macd_calc(c, MACD_FAST, MACD_SLOW, MACD_SIG)
    df['MACD_US'], df['MACD_US_sig'], df['MACD_US_hist'] = macd_calc(c, MACD_SF, MACD_SS, MACD_SSIG)

    vm = min(20, max(1, n // 2))
    df['VOL_MA20']  = v.rolling(vm, min_periods=1).mean()
    df['VOL_RATIO'] = v / df['VOL_MA20'].replace(0, np.nan)

    df['ATH']       = c.expanding().max()
    prev_ath        = c.shift(1).expanding().max()
    df['ATH_PCT']   = ((c / prev_ath.replace(0, np.nan)) - 1) * 100
    df['ATH_BREAK'] = c > prev_ath

    return df

# ── Signal Generation ─────────────────────────────────────────────────────────
def generate_signals(df_d, df_w, df_m):
    sig = pd.Series('HOLD', index=df_d.index)
    w_rsi_d = df_w['RSI'].reindex(df_d.index, method='ffill')
    m_rsi_d = df_m['RSI'].reindex(df_d.index, method='ffill')
    c = df_d['Close']; r = df_d['RSI']
    v = df_d['VOL_RATIO']; macd_us = df_d['MACD_US']

    s1 = df_d['ATH_BREAK'] & (m_rsi_d > M_RSI_STRONG)
    sig[s1] = 'STRONG BUY'

    us_cross_up = (macd_us > 0) & (macd_us.shift(1) <= 0)
    s2 = us_cross_up & (m_rsi_d > 60) & (w_rsi_d > W_RSI_BULL)
    sig[s2 & (sig != 'STRONG BUY')] = 'MACD MEGA BUY'

    rsi_c60 = (r > D_RSI_ENTRY) & (r.shift(1) <= D_RSI_ENTRY)
    s3 = rsi_c60 & (w_rsi_d > W_RSI_BULL) & (m_rsi_d > 55) & (v > VOL_SURGE)
    sig[s3 & (sig == 'HOLD')] = 'BUY'

    above_emas = (c > df_d['EMA21']) & (c > df_d['EMA50'])
    s4 = (v > 2.5) & above_emas & (r > 55) & (m_rsi_d > 55)
    sig[s4 & (sig == 'HOLD')] = 'VOL BUY'

    m_r70 = (m_rsi_d > 70) & (m_rsi_d.shift(1) <= 70)
    sig[m_r70 & (c > df_d['EMA50']) & (sig == 'HOLD')] = 'M-RSI BUY'

    rsi_c50 = (r < 50) & (r.shift(1) >= 50)
    below_sma20 = (c < df_d['SMA20']) & (c.shift(1) >= df_d['SMA20'].shift(1))
    sig[rsi_c50 & below_sma20 & ~sig.isin(['STRONG BUY','MACD MEGA BUY','BUY','VOL BUY','M-RSI BUY'])] = 'SELL'
    sig[(r < 45) & (r.shift(1) >= 45) & sig.isin(['BUY','VOL BUY','HOLD'])] = 'SELL'

    return sig

# ── Backtesting ───────────────────────────────────────────────────────────────
def backtest(df_d, signals):
    """Vectorisation: use set for O(1) membership, zip to avoid tuple alloc in .items()."""
    trades, pos = [], None
    BUY_SIGS = {'STRONG BUY', 'MACD MEGA BUY', 'BUY', 'VOL BUY', 'M-RSI BUY'}
    closes   = df_d['Close']
    for date, sig in zip(signals.index, signals.values):
        price = closes.at[date]          # .at[] is faster than .loc[] for scalar
        if sig in BUY_SIGS and pos is None:
            pos = {'entry_date': date, 'entry_price': price, 'signal': sig}
        elif sig == 'SELL' and pos is not None:
            ret = (price - pos['entry_price']) / pos['entry_price'] * 100
            trades.append({**pos, 'exit_date': date, 'exit_price': price,
                           'return_pct': ret,
                           'days_held': (date - pos['entry_date']).days})
            pos = None
    if pos:
        p = df_d['Close'].iloc[-1]; d = df_d.index[-1]
        ret = (p - pos['entry_price']) / pos['entry_price'] * 100
        trades.append({**pos, 'exit_date': d, 'exit_price': p,
                       'return_pct': ret,
                       'days_held': (d - pos['entry_date']).days, 'open': True})
    return trades

# ── Fibonacci Extensions ──────────────────────────────────────────────────────
def fibonacci_targets(df_d, lookback=252):
    recent = df_d.tail(lookback)
    sl = float(recent['Low'].min()); sh = float(recent['High'].max())
    cur = float(df_d['Close'].iloc[-1]); rng = sh - sl
    return {
        'swing_low':  round(sl, 2),  'swing_high': round(sh, 2),
        'current':    round(cur, 2),
        'fib_0618':   round(cur + 0.618*rng, 2),
        'fib_1618':   round(cur + 1.618*rng, 2),
        'fib_2618':   round(cur + 2.618*rng, 2),
        'fib_4236':   round(cur + 4.236*rng, 2),
    }

# ── Score ─────────────────────────────────────────────────────────────────────
def compute_score(df_d, df_w, df_m, signals, is_blast):
    score = 0
    c_val = df_d['Close'].iloc[-1]

    m_rsi = df_m['RSI'].iloc[-1] if len(df_m) > 0 else 50
    if m_rsi > 70:   score += 25
    elif m_rsi > 60: score += 15
    elif m_rsi > 50: score += 5

    if df_d['ATH_PCT'].iloc[-1] >= 0: score += 25

    us = df_d['MACD_US'].iloc[-1]
    us_prev = df_d['MACD_US'].iloc[-5] if len(df_d) > 5 else us
    if us >= 0:          score += 15
    elif us > -0.02*c_val: score += 8
    if us > us_prev:     score += 5

    w_rsi = df_w['RSI'].iloc[-1] if len(df_w) > 0 else 50
    if w_rsi > 60: score += 10
    elif w_rsi > 50: score += 5

    d_rsi = df_d['RSI'].iloc[-1]
    if 60 <= d_rsi <= 75: score += 8
    elif d_rsi > 75:      score += 4

    vr = df_d['VOL_RATIO'].iloc[-1]
    if vr > 2.5: score += 7
    elif vr > 1.5: score += 4

    if df_d['ADX'].iloc[-1] > 30: score += 5
    if is_blast: score += 15

    recent = signals.tail(20)
    if 'STRONG BUY' in recent.values:    score += 10
    elif 'MACD MEGA BUY' in recent.values: score += 8

    return min(score, 100)

# ── Chart: Daily (5-panel) with Darvas + Blast + S/R ─────────────────────────
def chart_daily(ticker, name, df_d, signals, fibs, boxes_d, is_blast, blast_score,
                resistance, support, out_path):
    with plt.rc_context(MPLSTYLE):
        fig = plt.figure(figsize=(16, 18), facecolor='#0d1117')
        gs  = GridSpec(5, 1, figure=fig, hspace=0.05,
                       height_ratios=[3.5, 1, 1, 1, 1])
        ax_p = fig.add_subplot(gs[0])
        ax_v = fig.add_subplot(gs[1], sharex=ax_p)
        ax_r = fig.add_subplot(gs[2], sharex=ax_p)
        ax_m = fig.add_subplot(gs[3], sharex=ax_p)
        ax_u = fig.add_subplot(gs[4], sharex=ax_p)

        df = df_d.tail(252*3).copy()
        sig_plot = signals.reindex(df.index)
        x = df.index

        # ── Price panel ──────────────────────────────────────────────────────
        ax_p.plot(x, df['Close'],  color='#e6edf3', lw=1.3, zorder=3, label='Close')
        ax_p.plot(x, df['EMA21'],  color='#f0b429', lw=0.9, alpha=0.8, label='EMA21')
        ax_p.plot(x, df['EMA50'],  color='#58a6ff', lw=0.9, alpha=0.8, label='EMA50')
        ax_p.plot(x, df['EMA200'], color='#ff6b6b', lw=0.9, alpha=0.8, label='EMA200')
        ax_p.fill_between(x, df['BB_lo'], df['BB_hi'], color='#58a6ff', alpha=0.05)
        ax_p.plot(x, df['BB_hi'], color='#58a6ff', lw=0.4, ls='--', alpha=0.35)
        ax_p.plot(x, df['BB_lo'], color='#58a6ff', lw=0.4, ls='--', alpha=0.35)

        if 'SAR' in df.columns:
            ax_p.scatter(x, df['SAR'], s=1.5, color='#9e6eff', alpha=0.4, zorder=2)

        # Support / Resistance lines
        for lvl in resistance:
            ax_p.axhline(lvl, color='#ff6b6b', lw=0.7, ls='--', alpha=0.6)
            ax_p.annotate(f'R ₹{lvl:,.0f}', xy=(x[-1], lvl),
                          xytext=(5, 2), textcoords='offset points',
                          fontsize=6, color='#ff6b6b', va='bottom')
        for lvl in support:
            ax_p.axhline(lvl, color='#26d07c', lw=0.7, ls='--', alpha=0.6)
            ax_p.annotate(f'S ₹{lvl:,.0f}', xy=(x[-1], lvl),
                          xytext=(5, -8), textcoords='offset points',
                          fontsize=6, color='#26d07c', va='top')

        # Darvas Boxes on daily chart
        for box in boxes_d[-5:]:
            try:
                bs = pd.Timestamp(box['box_start'])
                be = pd.Timestamp(box['box_end'])
                if bs >= x[0]:
                    color = '#00ff88' if box['breakout'] else ('#ff4444' if box['breakdown'] else '#fbbf24')
                    rect = mpl_patches.FancyArrowPatch
                    ax_p.axhspan(box['bottom'], box['top'],
                                 xmin=max(0, (bs - x[0]).days / (x[-1] - x[0]).days),
                                 xmax=min(1, (be - x[0]).days / (x[-1] - x[0]).days),
                                 alpha=0.07, color=color, zorder=1)
                    ax_p.axhline(box['top'],    color=color, lw=0.7, ls='-',  alpha=0.5)
                    ax_p.axhline(box['bottom'], color=color, lw=0.5, ls='--', alpha=0.4)
            except Exception:
                pass

        # Buy/Sell markers
        SIG_COLORS = {
            'STRONG BUY': '#00ff88', 'MACD MEGA BUY': '#00d4ff',
            'BUY': '#26d07c',        'VOL BUY': '#fbbf24',
            'M-RSI BUY': '#c084fc',  'SELL': '#ff6b6b',
        }
        for sv, sc in SIG_COLORS.items():
            mask = sig_plot == sv
            if mask.any():
                prices = df.loc[mask, 'Close']
                off = -0.03 if sv == 'SELL' else 0.03
                ax_p.scatter(prices.index, prices*(1+off), s=55, color=sc,
                             marker='v' if sv == 'SELL' else '^',
                             zorder=5, edgecolors='white', linewidths=0.25, label=sv)

        # Fibonacci
        fib_colors = ['#fbbf24','#f97316','#ef4444','#dc2626']
        for fk, fc in zip(['fib_0618','fib_1618','fib_2618','fib_4236'], fib_colors):
            fv = fibs[fk]
            if fv > df['Low'].min():
                ax_p.axhline(fv, color=fc, lw=0.6, ls=':', alpha=0.7)
                ax_p.annotate(f'₹{fv:,.0f}', xy=(x[-1], fv), xytext=(5, 0),
                              textcoords='offset points', fontsize=5.5, color=fc)

        ax_p.axhline(float(df['ATH'].iloc[-1]), color='#00ff88', lw=0.6, ls='-.', alpha=0.6)
        ax_p.annotate(f'ATH ₹{df["ATH"].iloc[-1]:,.0f}',
                      xy=(x[-1], df['ATH'].iloc[-1]), xytext=(-55, 3),
                      textcoords='offset points', fontsize=6, color='#00ff88')

        blast_txt = f' 🚀 BLAST ({blast_score})' if is_blast else ''
        ax_p.set_title(f'{name} [{ticker}] — Daily Chart{blast_txt}',
                       fontsize=12, color='#00ff88' if is_blast else '#e6edf3',
                       fontweight='bold', pad=6)
        ax_p.legend(fontsize=6.5, loc='upper left', ncol=5,
                    facecolor='#161b22', edgecolor='#30363d', labelcolor='#e6edf3')
        ax_p.grid(True, alpha=0.25)
        ax_p.yaxis.tick_right()

        # ── Volume ────────────────────────────────────────────────────────────
        vc = np.where(df['VOL_RATIO'] > BLAST_VOL_RATIO, '#fbbf24',
                      np.where(df['Close'] >= df['Open'], '#26d07c', '#ff6b6b'))
        ax_v.bar(x, df['Volume']/1e6, color=vc, alpha=0.8, width=1.2)
        ax_v.plot(x, df['VOL_MA20']/1e6, color='#9e6eff', lw=0.8, label='Vol MA20')
        ax_v.set_ylabel('Vol(M)', color='#8b949e', fontsize=6.5)
        ax_v.yaxis.tick_right()
        ax_v.legend(fontsize=6, loc='upper left', facecolor='#161b22',
                    edgecolor='#30363d', labelcolor='#e6edf3')
        ax_v.grid(True, alpha=0.25)

        # ── RSI ───────────────────────────────────────────────────────────────
        ax_r.plot(x, df['RSI'], color='#c084fc', lw=1.0, label='RSI(14)')
        ax_r.axhline(70, color='#ff6b6b', lw=0.6, ls='--', alpha=0.6)
        ax_r.axhline(50, color='#8b949e', lw=0.4, ls='-',  alpha=0.4)
        ax_r.axhline(30, color='#26d07c', lw=0.6, ls='--', alpha=0.6)
        ax_r.fill_between(x, df['RSI'], 70, where=df['RSI']>=70, color='#ff6b6b', alpha=0.12)
        ax_r.fill_between(x, df['RSI'], 30, where=df['RSI']<=30, color='#26d07c', alpha=0.12)
        ax_r.set_ylim(0, 100); ax_r.set_ylabel('RSI', color='#8b949e', fontsize=6.5)
        ax_r.yaxis.tick_right()
        ax_r.legend(fontsize=6, loc='upper left', facecolor='#161b22',
                    edgecolor='#30363d', labelcolor='#e6edf3')
        ax_r.grid(True, alpha=0.25)

        # ── MACD ──────────────────────────────────────────────────────────────
        ax_m.plot(x, df['MACD'],     color='#58a6ff', lw=0.9, label='MACD(12,26)')
        ax_m.plot(x, df['MACD_sig'], color='#f0b429', lw=0.8, ls='--', label='Sig(9)')
        hc = np.where(df['MACD_hist'] >= 0, '#26d07c', '#ff6b6b')
        ax_m.bar(x, df['MACD_hist'], color=hc, alpha=0.55, width=1.2)
        ax_m.axhline(0, color='#8b949e', lw=0.4)
        ax_m.set_ylabel('MACD', color='#8b949e', fontsize=6.5)
        ax_m.yaxis.tick_right()
        ax_m.legend(fontsize=6, loc='upper left', facecolor='#161b22',
                    edgecolor='#30363d', labelcolor='#e6edf3')
        ax_m.grid(True, alpha=0.25)

        # ── Ultra-slow MACD ───────────────────────────────────────────────────
        ax_u.plot(x, df['MACD_US'],     color='#00d4ff', lw=1.1, label='MACD(34,1000,20)')
        ax_u.plot(x, df['MACD_US_sig'], color='#ff9800', lw=0.8, ls='--', label='Sig(20)')
        uhc = np.where(df['MACD_US_hist'] >= 0, '#26d07c', '#ff6b6b')
        ax_u.bar(x, df['MACD_US_hist'], color=uhc, alpha=0.45, width=1.2)
        ax_u.axhline(0, color='#00ff88', lw=0.7, alpha=0.7)
        ax_u.set_ylabel('Ultra-Slow\nMACD', color='#8b949e', fontsize=6.5)
        ax_u.yaxis.tick_right()
        ax_u.legend(fontsize=6, loc='upper left', facecolor='#161b22',
                    edgecolor='#30363d', labelcolor='#e6edf3')
        ax_u.grid(True, alpha=0.25)

        plt.setp(ax_p.get_xticklabels(), visible=False)
        plt.setp(ax_v.get_xticklabels(), visible=False)
        plt.setp(ax_r.get_xticklabels(), visible=False)
        plt.setp(ax_m.get_xticklabels(), visible=False)
        ax_u.tick_params(axis='x', labelsize=7, rotation=30)

        fig.tight_layout(rect=[0, 0, 1, 1])
        fig.savefig(out_path, dpi=90, bbox_inches='tight',
                    facecolor='#0d1117', edgecolor='none')
        plt.close(fig)

# ── Chart: Weekly with S/R + Trend Channel + Darvas ──────────────────────────
def chart_weekly(ticker, name, df_w, boxes_w, resistance, support, channel_data, out_path):
    with plt.rc_context(MPLSTYLE):
        fig = plt.figure(figsize=(14, 9), facecolor='#0d1117')
        gs  = GridSpec(3, 1, figure=fig, hspace=0.05, height_ratios=[3, 1, 1])
        ax_p = fig.add_subplot(gs[0])
        ax_r = fig.add_subplot(gs[1], sharex=ax_p)
        ax_v = fig.add_subplot(gs[2], sharex=ax_p)

        df = df_w.tail(156).copy()   # ~3 years weekly
        x  = df.index

        ax_p.plot(x, df['Close'], color='#e6edf3', lw=1.3, label='Close', zorder=3)
        ax_p.plot(x, df['EMA21'] if 'EMA21' in df else df['Close'].ewm(21).mean(),
                  color='#f0b429', lw=0.9, label='EMA21', alpha=0.8)
        ax_p.plot(x, df['EMA50'] if 'EMA50' in df else df['Close'].ewm(50).mean(),
                  color='#58a6ff', lw=0.9, label='EMA50', alpha=0.8)

        # Trend Channel
        if channel_data:
            trend, upper, lower, idx = channel_data
            if len(idx) == len(trend):
                ax_p.plot(idx, trend, color='#9e6eff', lw=1.0, ls='--', alpha=0.7, label='Trend')
                ax_p.plot(idx, upper, color='#ff6b6b', lw=0.8, ls=':', alpha=0.6, label='Upper Ch')
                ax_p.plot(idx, lower, color='#26d07c', lw=0.8, ls=':', alpha=0.6, label='Lower Ch')
                ax_p.fill_between(idx, lower, upper, color='#9e6eff', alpha=0.04)

        # S/R lines
        for lvl in resistance:
            ax_p.axhline(lvl, color='#ff6b6b', lw=0.9, ls='--', alpha=0.65)
            ax_p.annotate(f'R ₹{lvl:,.0f}', xy=(x[-1], lvl), xytext=(5, 2),
                          textcoords='offset points', fontsize=6, color='#ff6b6b')
        for lvl in support:
            ax_p.axhline(lvl, color='#26d07c', lw=0.9, ls='--', alpha=0.65)
            ax_p.annotate(f'S ₹{lvl:,.0f}', xy=(x[-1], lvl), xytext=(5, -8),
                          textcoords='offset points', fontsize=6, color='#26d07c')

        # Darvas Boxes
        for box in boxes_w[-4:]:
            try:
                bs = pd.Timestamp(box['box_start'])
                be = pd.Timestamp(box['box_end'])
                if bs >= x[0]:
                    col = '#00ff88' if box['breakout'] else ('#ff4444' if box['breakdown'] else '#fbbf24')
                    span = (x[-1] - x[0]).days
                    xmin = max(0, (bs - x[0]).days / span) if span > 0 else 0
                    xmax = min(1, (be - x[0]).days / span) if span > 0 else 1
                    ax_p.axhspan(box['bottom'], box['top'], xmin=xmin, xmax=xmax,
                                 alpha=0.06, color=col)
                    ax_p.axhline(box['top'],    color=col, lw=0.8, ls='-',  alpha=0.5)
                    ax_p.axhline(box['bottom'], color=col, lw=0.6, ls='--', alpha=0.4)
            except Exception:
                pass

        ax_p.set_title(f'{name} [{ticker}] — Weekly Chart', fontsize=11,
                       color='#e6edf3', fontweight='bold', pad=5)
        ax_p.legend(fontsize=6.5, loc='upper left', ncol=4,
                    facecolor='#161b22', edgecolor='#30363d', labelcolor='#e6edf3')
        ax_p.grid(True, alpha=0.25); ax_p.yaxis.tick_right()

        # RSI
        rsi_w = rsi(df['Close'], 14)
        ax_r.plot(x, rsi_w, color='#c084fc', lw=1.0, label='RSI(14)')
        ax_r.axhline(70, color='#ff6b6b', lw=0.5, ls='--', alpha=0.6)
        ax_r.axhline(50, color='#8b949e', lw=0.4, alpha=0.4)
        ax_r.axhline(30, color='#26d07c', lw=0.5, ls='--', alpha=0.6)
        ax_r.fill_between(x, rsi_w, 70, where=rsi_w>=70, color='#ff6b6b', alpha=0.1)
        ax_r.fill_between(x, rsi_w, 30, where=rsi_w<=30, color='#26d07c', alpha=0.1)
        ax_r.set_ylim(0, 100); ax_r.set_ylabel('RSI', color='#8b949e', fontsize=6.5)
        ax_r.yaxis.tick_right()
        ax_r.legend(fontsize=6, loc='upper left', facecolor='#161b22',
                    edgecolor='#30363d', labelcolor='#e6edf3')
        ax_r.grid(True, alpha=0.25)

        # Volume
        vc = np.where(df['Close'] >= df['Open'], '#26d07c', '#ff6b6b')
        ax_v.bar(x, df['Volume']/1e6, color=vc, alpha=0.75, width=5)
        ax_v.set_ylabel('Vol(M)', color='#8b949e', fontsize=6.5)
        ax_v.yaxis.tick_right()
        ax_v.tick_params(axis='x', labelsize=7, rotation=20)
        ax_v.grid(True, alpha=0.25)

        plt.setp(ax_p.get_xticklabels(), visible=False)
        plt.setp(ax_r.get_xticklabels(), visible=False)
        fig.tight_layout(rect=[0, 0, 1, 1])
        fig.savefig(out_path, dpi=90, bbox_inches='tight',
                    facecolor='#0d1117', edgecolor='none')
        plt.close(fig)

# ── Chart: Monthly with S/R + Trend Channel + Darvas ─────────────────────────
def chart_monthly(ticker, name, df_m, boxes_m, resistance, support, channel_data, out_path):
    with plt.rc_context(MPLSTYLE):
        fig = plt.figure(figsize=(14, 9), facecolor='#0d1117')
        gs  = GridSpec(3, 1, figure=fig, hspace=0.05, height_ratios=[3, 1, 1])
        ax_p = fig.add_subplot(gs[0])
        ax_r = fig.add_subplot(gs[1], sharex=ax_p)
        ax_v = fig.add_subplot(gs[2], sharex=ax_p)

        df = df_m.tail(120).copy()  # 10 years monthly
        x  = df.index

        ax_p.plot(x, df['Close'], color='#e6edf3', lw=1.5, label='Close', zorder=3)
        ax_p.plot(x, df['Close'].ewm(12).mean(), color='#f0b429', lw=1.0, label='EMA12', alpha=0.8)
        ax_p.plot(x, df['Close'].ewm(26).mean(), color='#58a6ff', lw=1.0, label='EMA26', alpha=0.8)

        # Trend Channel
        if channel_data:
            trend, upper, lower, idx = channel_data
            if len(idx) == len(trend):
                ax_p.plot(idx, trend, color='#9e6eff', lw=1.2, ls='--', alpha=0.7, label='Trend')
                ax_p.plot(idx, upper, color='#ff6b6b', lw=1.0, ls=':', alpha=0.65, label='Upper')
                ax_p.plot(idx, lower, color='#26d07c', lw=1.0, ls=':', alpha=0.65, label='Lower')
                ax_p.fill_between(idx, lower, upper, color='#9e6eff', alpha=0.05)

        # S/R
        for lvl in resistance:
            ax_p.axhline(lvl, color='#ff6b6b', lw=1.0, ls='--', alpha=0.65)
            ax_p.annotate(f'R ₹{lvl:,.0f}', xy=(x[-1], lvl), xytext=(5, 2),
                          textcoords='offset points', fontsize=6.5, color='#ff6b6b')
        for lvl in support:
            ax_p.axhline(lvl, color='#26d07c', lw=1.0, ls='--', alpha=0.65)
            ax_p.annotate(f'S ₹{lvl:,.0f}', xy=(x[-1], lvl), xytext=(5, -9),
                          textcoords='offset points', fontsize=6.5, color='#26d07c')

        # Darvas Boxes
        for box in boxes_m[-4:]:
            try:
                bs = pd.Timestamp(box['box_start'])
                be = pd.Timestamp(box['box_end'])
                if bs >= x[0]:
                    col = '#00ff88' if box['breakout'] else ('#ff4444' if box['breakdown'] else '#fbbf24')
                    span = (x[-1] - x[0]).days
                    xmin = max(0, (bs - x[0]).days / span) if span > 0 else 0
                    xmax = min(1, (be - x[0]).days / span) if span > 0 else 1
                    ax_p.axhspan(box['bottom'], box['top'], xmin=xmin, xmax=xmax,
                                 alpha=0.07, color=col)
                    ax_p.axhline(box['top'],    color=col, lw=1.0, ls='-',  alpha=0.5)
                    ax_p.axhline(box['bottom'], color=col, lw=0.7, ls='--', alpha=0.4)
            except Exception:
                pass

        ax_p.set_title(f'{name} [{ticker}] — Monthly Chart', fontsize=11,
                       color='#e6edf3', fontweight='bold', pad=5)
        ax_p.legend(fontsize=7, loc='upper left', ncol=4,
                    facecolor='#161b22', edgecolor='#30363d', labelcolor='#e6edf3')
        ax_p.grid(True, alpha=0.25); ax_p.yaxis.tick_right()

        rsi_m = rsi(df['Close'], 14)
        ax_r.plot(x, rsi_m, color='#c084fc', lw=1.1, label='RSI(14)')
        ax_r.axhline(70, color='#ff6b6b', lw=0.6, ls='--', alpha=0.6)
        ax_r.axhline(50, color='#8b949e', lw=0.4, alpha=0.4)
        ax_r.axhline(30, color='#26d07c', lw=0.6, ls='--', alpha=0.6)
        ax_r.fill_between(x, rsi_m, 70, where=rsi_m>=70, color='#ff6b6b', alpha=0.1)
        ax_r.fill_between(x, rsi_m, 30, where=rsi_m<=30, color='#26d07c', alpha=0.1)
        ax_r.set_ylim(0, 100); ax_r.set_ylabel('RSI', color='#8b949e', fontsize=7)
        ax_r.yaxis.tick_right()
        ax_r.legend(fontsize=6.5, loc='upper left', facecolor='#161b22',
                    edgecolor='#30363d', labelcolor='#e6edf3')
        ax_r.grid(True, alpha=0.25)

        vc = np.where(df['Close'] >= df['Open'], '#26d07c', '#ff6b6b')
        ax_v.bar(x, df['Volume']/1e6, color=vc, alpha=0.75, width=20)
        ax_v.set_ylabel('Vol(M)', color='#8b949e', fontsize=7)
        ax_v.yaxis.tick_right()
        ax_v.tick_params(axis='x', labelsize=7, rotation=20)
        ax_v.grid(True, alpha=0.25)

        plt.setp(ax_p.get_xticklabels(), visible=False)
        plt.setp(ax_r.get_xticklabels(), visible=False)
        fig.tight_layout(rect=[0, 0, 1, 1])
        fig.savefig(out_path, dpi=90, bbox_inches='tight',
                    facecolor='#0d1117', edgecolor='none')
        plt.close(fig)

# ── Process One Stock ──────────────────────────────────────────────────────────
def process_stock(name, ticker, df_raw=None):
    """Full pipeline for one stock.  Returns result dict or None on failure.

    df_raw can be pre-supplied from batch_fetch() to avoid a redundant download;
    if None, falls back to the single-ticker fetch_data().
    """
    try:
        if df_raw is None:
            df_raw = fetch_data(ticker)
        if df_raw is None:
            return None

        # Compute days listed and IPO flag
        data_days    = len(df_raw)
        listing_date = df_raw.index[0].strftime('%Y-%m-%d')
        is_recent_ipo = data_days < 365   # listed within last ~1 year
        is_new_ipo    = data_days < 90    # listed within last ~3 months

        # Daily: full indicators (adaptive).  Weekly/monthly: lightweight set
        df_d = add_indicators(df_raw.copy())
        df_w = add_indicators_lite(resample_weekly(df_raw))
        df_m = add_indicators_lite(resample_monthly(df_raw))

        # Minimum viable data — accept even new IPOs with >= MIN_DAILY_BARS
        if len(df_d) < MIN_DAILY_BARS or len(df_w) < 2 or len(df_m) < 1:
            return None

        # Signals & Backtest
        signals   = generate_signals(df_d, df_w, df_m)
        trades    = backtest(df_d, signals)
        fibs      = fibonacci_targets(df_d)

        # Support/Resistance on daily, weekly, monthly
        res_d, sup_d = find_support_resistance(df_d.tail(252), window=8)
        res_w, sup_w = find_support_resistance(df_w.tail(104), window=5)
        res_m, sup_m = find_support_resistance(df_m.tail(60),  window=3)

        # Trend Channels
        ch_w = trend_channel(df_w, lookback=60)
        ch_m = trend_channel(df_m, lookback=40)

        # Darvas Boxes
        boxes_d = darvas_boxes(df_d.tail(252),  confirm_days=3)
        boxes_w = darvas_boxes(df_w.tail(104),  confirm_days=3)
        boxes_m = darvas_boxes(df_m.tail(60),   confirm_days=2)

        # Darvas summary
        dv_d_status, dv_d_top, dv_d_bot = darvas_latest_status(boxes_d)
        dv_w_status, dv_w_top, dv_w_bot = darvas_latest_status(boxes_w)
        dv_m_status, dv_m_top, dv_m_bot = darvas_latest_status(boxes_m)

        # Blast
        is_blast, blast_score, blast_reason = detect_blast(df_d, res_d)

        # Score
        score = compute_score(df_d, df_w, df_m, signals, is_blast)

        # Key metrics
        cur_price  = float(df_d['Close'].iloc[-1])
        ath_val    = float(df_d['ATH'].iloc[-1])
        ath_pct    = float(df_d['ATH_PCT'].iloc[-1])
        m_rsi_val  = float(df_m['RSI'].iloc[-1]) if len(df_m) > 0 else 0
        w_rsi_val  = float(df_w['RSI'].iloc[-1]) if len(df_w) > 0 else 0
        d_rsi_val  = float(df_d['RSI'].iloc[-1])
        macd_us_v  = float(df_d['MACD_US'].iloc[-1])
        vol_ratio  = float(df_d['VOL_RATIO'].iloc[-1]) if not pd.isna(df_d['VOL_RATIO'].iloc[-1]) else 1.0
        adx_val    = float(df_d['ADX'].iloc[-1]) if not pd.isna(df_d['ADX'].iloc[-1]) else 0
        last_sig   = signals.iloc[-1]

        # Backtest stats
        closed = [t for t in trades if not t.get('open')]
        win_rate = 0.0
        avg_ret  = 0.0
        if closed:
            wins = [t for t in closed if t['return_pct'] > 0]
            win_rate = len(wins) / len(closed) * 100
            avg_ret  = np.mean([t['return_pct'] for t in closed])

        # Save charts — skip individual chart if it was already written today
        CHARTS_DIR.mkdir(parents=True, exist_ok=True)
        safe_sym = ticker.replace('.NS','').replace('.BO','')
        path_d = CHARTS_DIR / f"{safe_sym}_daily.png"
        path_w = CHARTS_DIR / f"{safe_sym}_weekly.png"
        path_m = CHARTS_DIR / f"{safe_sym}_monthly.png"

        if not chart_is_fresh(path_d):
            chart_daily(ticker, name, df_d, signals, fibs, boxes_d,
                        is_blast, blast_score, res_d, sup_d, path_d)
        if not chart_is_fresh(path_w):
            chart_weekly(ticker, name, df_w, boxes_w, res_w, sup_w, ch_w, path_w)
        if not chart_is_fresh(path_m):
            chart_monthly(ticker, name, df_m, boxes_m, res_m, sup_m, ch_m, path_m)

        return {
            'name': name, 'ticker': ticker, 'symbol': safe_sym,
            'price': cur_price, 'ath': ath_val, 'ath_pct': ath_pct,
            'm_rsi': m_rsi_val, 'w_rsi': w_rsi_val, 'd_rsi': d_rsi_val,
            'macd_us': macd_us_v, 'adx': adx_val, 'vol_ratio': vol_ratio,
            'signal': last_sig, 'score': score,
            'trades': len(trades), 'win_rate': win_rate, 'avg_ret': avg_ret,
            'fibs': fibs,
            # IPO / listing metadata
            'data_days': data_days,
            'listing_date': listing_date,
            'is_recent_ipo': is_recent_ipo,
            'is_new_ipo': is_new_ipo,
            # Blast
            'blast': is_blast, 'blast_score': blast_score, 'blast_reason': blast_reason,
            # Support / Resistance
            'res_d': [round(r, 2) for r in res_d[:3]],
            'sup_d': [round(s, 2) for s in sup_d[:3]],
            'res_w': [round(r, 2) for r in res_w[:3]],
            'sup_w': [round(s, 2) for s in sup_w[:3]],
            # Darvas
            'darvas_d': dv_d_status, 'darvas_d_top': dv_d_top, 'darvas_d_bot': dv_d_bot,
            'darvas_w': dv_w_status, 'darvas_w_top': dv_w_top, 'darvas_w_bot': dv_w_bot,
            'darvas_m': dv_m_status, 'darvas_m_top': dv_m_top, 'darvas_m_bot': dv_m_bot,
            # Chart paths
            'chart_d': f"/charts/multibagger/{safe_sym}_daily.png",
            'chart_w': f"/charts/multibagger/{safe_sym}_weekly.png",
            'chart_m': f"/charts/multibagger/{safe_sym}_monthly.png",
        }
    except Exception as e:
        tprint(f"  ⚠️  {ticker}: {e}")
        return None

# ── HTML Builder ──────────────────────────────────────────────────────────────
def sig_badge(sig):
    colors = {
        'STRONG BUY':   ('#00ff88','#003319'),
        'MACD MEGA BUY':('#00d4ff','#001a2c'),
        'BUY':          ('#26d07c','#002b1a'),
        'VOL BUY':      ('#fbbf24','#1a1200'),
        'M-RSI BUY':    ('#c084fc','#1a0033'),
        'HOLD':         ('#8b949e','#1a1f2c'),
        'SELL':         ('#ff6b6b','#2c0000'),
    }
    c, bg = colors.get(sig, ('#8b949e','#1a1f2c'))
    return f'<span style="background:{bg};color:{c};border:1px solid {c};border-radius:4px;padding:2px 7px;font-size:11px;font-weight:700;white-space:nowrap;">{sig}</span>'

def blast_badge(is_blast, score, reason):
    if not is_blast:
        return '<span style="color:#444;font-size:11px;">—</span>'
    return (f'<span style="background:#1a0a00;color:#ff9800;border:1px solid #ff9800;'
            f'border-radius:4px;padding:2px 7px;font-size:11px;font-weight:700;" '
            f'title="{reason}">🚀 BLAST {score}</span>')

def darvas_badge(status):
    colors = {'BREAKOUT': '#00ff88', 'BREAKDOWN': '#ff4444', 'IN BOX': '#fbbf24', 'None': '#555'}
    c = colors.get(status, '#555')
    return f'<span style="color:{c};font-size:11px;font-weight:600;">{status}</span>'

def rsi_color(v):
    if v >= 70: return '#ff6b6b'
    if v >= 60: return '#fbbf24'
    if v >= 50: return '#26d07c'
    return '#8b949e'

def fmt_price(v):
    return f'₹{v:,.1f}'

def fmt_pct(v, positive_green=True):
    color = '#26d07c' if (v >= 0) == positive_green else '#ff6b6b'
    return f'<span style="color:{color}">{v:+.1f}%</span>'

def sr_pills(levels, color):
    if not levels:
        return '<span style="color:#444;">—</span>'
    pills = []
    for lvl in levels[:3]:
        pills.append(f'<span style="background:#111;color:{color};border:1px solid {color}33;'
                     f'border-radius:3px;padding:1px 5px;font-size:10px;">₹{lvl:,.0f}</span>')
    return ' '.join(pills)

def chart_modal_btn(sym, label, url):
    return (f'<button onclick="showChart(\'{sym}\',\'{label}\',\'{url}\')" '
            f'style="background:#1a1f2c;color:#58a6ff;border:1px solid #30363d;'
            f'border-radius:4px;padding:2px 7px;font-size:10px;cursor:pointer;'
            f'margin:1px;">{label}</button>')

def build_html(results, scan_time, total_scanned, total_ok):
    blast_cnt   = sum(1 for r in results if r['blast'])
    strong_cnt  = sum(1 for r in results if r['signal'] in ('STRONG BUY','MACD MEGA BUY'))
    mrsi70_cnt  = sum(1 for r in results if r['m_rsi'] > 70)
    avg_score   = np.mean([r['score'] for r in results]) if results else 0

    # Sort by score desc
    results_sorted = sorted(results, key=lambda x: -x['score'])

    rows_html = []
    for i, r in enumerate(results_sorted, 1):
        sym = r['symbol']
        res_d_html = sr_pills(r['res_d'], '#ff6b6b')
        sup_d_html = sr_pills(r['sup_d'], '#26d07c')

        btn_d = chart_modal_btn(sym, 'Daily',   r['chart_d'])
        btn_w = chart_modal_btn(sym, 'Weekly',  r['chart_w'])
        btn_m = chart_modal_btn(sym, 'Monthly', r['chart_m'])

        rows_html.append(f"""
<tr class="stock-row {'blast-row' if r['blast'] else ''}"
    data-signal="{r['signal']}"
    data-blast="{1 if r['blast'] else 0}"
    data-score="{r['score']}"
    data-mrsi="{r['m_rsi']:.1f}"
    data-wrsi="{r['w_rsi']:.1f}"
    data-drsi="{r['d_rsi']:.1f}"
    data-darvas="{r['darvas_d']}">
  <td style="color:#555;font-size:11px;padding:6px 4px;">{i}</td>
  <td style="padding:6px 8px;min-width:130px;">
    <div style="font-weight:700;color:#e6edf3;font-size:12px;">{r['symbol']}</div>
    <div style="color:#8b949e;font-size:10px;max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{r['name']}</div>
  </td>
  <td style="padding:6px 8px;color:#e6edf3;font-size:12px;white-space:nowrap;">{fmt_price(r['price'])}</td>
  <td style="padding:6px 4px;">{fmt_pct(r['ath_pct'], positive_green=False)}</td>
  <td style="padding:6px 4px;color:{rsi_color(r['m_rsi'])};font-weight:700;">{r['m_rsi']:.1f}</td>
  <td style="padding:6px 4px;color:{rsi_color(r['w_rsi'])};">{r['w_rsi']:.1f}</td>
  <td style="padding:6px 4px;color:{rsi_color(r['d_rsi'])};">{r['d_rsi']:.1f}</td>
  <td style="padding:6px 4px;color:{'#26d07c' if r['macd_us']>=0 else '#ff6b6b'};font-size:11px;">
    {'▲' if r['macd_us']>=0 else '▼'}{abs(r['macd_us']):.2f}
  </td>
  <td style="padding:6px 8px;">{blast_badge(r['blast'], r['blast_score'], r['blast_reason'])}</td>
  <td style="padding:6px 8px;">{sig_badge(r['signal'])}</td>
  <td style="padding:6px 4px;color:#e6edf3;font-weight:700;font-size:13px;">{r['score']}</td>
  <td style="padding:6px 6px;">{darvas_badge(r['darvas_d'])}</td>
  <td style="padding:6px 6px;">{darvas_badge(r['darvas_w'])}</td>
  <td style="padding:6px 6px;">{darvas_badge(r['darvas_m'])}</td>
  <td style="padding:6px 6px;">{res_d_html}</td>
  <td style="padding:6px 6px;">{sup_d_html}</td>
  <td style="padding:6px 4px;color:#8b949e;font-size:11px;">{r['vol_ratio']:.1f}x</td>
  <td style="padding:6px 4px;color:#8b949e;font-size:11px;">{r['trades']}</td>
  <td style="padding:6px 4px;font-size:11px;">
    {'<span style="color:#26d07c">'+f"{r['win_rate']:.0f}%"+'</span>' if r['trades'] > 0 else '—'}
  </td>
  <td style="padding:6px 6px;white-space:nowrap;">{btn_d}{btn_w}{btn_m}</td>
</tr>""")

    rows = '\n'.join(rows_html)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>🏆 Multibagger Report — NSE Full Scan</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Segoe UI',system-ui,sans-serif;background:#0d1117;color:#e6edf3;min-height:100vh}}
.header{{background:#161b22;border-bottom:1px solid #30363d;padding:14px 24px;display:flex;align-items:center;gap:20px;flex-wrap:wrap}}
.header h1{{font-size:1.3rem;font-weight:800;color:#e6edf3}}
.header .sub{{font-size:11px;color:#8b949e}}
.nav-pills{{display:flex;gap:8px;flex-wrap:wrap;margin-left:auto}}
.nav-pills a{{background:#1c2128;border:1px solid #30363d;color:#8b949e;text-decoration:none;
             border-radius:6px;padding:5px 12px;font-size:12px;transition:.15s}}
.nav-pills a:hover,.nav-pills a.active{{background:#388bfd22;color:#58a6ff;border-color:#388bfd}}
.stats{{display:flex;gap:12px;padding:14px 24px;background:#161b22;border-bottom:1px solid #30363d;flex-wrap:wrap}}
.stat{{background:#0d1117;border:1px solid #30363d;border-radius:8px;padding:10px 18px;min-width:120px;text-align:center}}
.stat .val{{font-size:1.5rem;font-weight:800;color:#e6edf3}}
.stat .lbl{{font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:.8px;margin-top:2px}}
.blast-stat .val{{color:#ff9800}}
.controls{{padding:12px 24px;background:#0d1117;border-bottom:1px solid #21262d;display:flex;gap:10px;flex-wrap:wrap;align-items:center}}
.controls input{{background:#161b22;border:1px solid #30363d;color:#e6edf3;border-radius:6px;
               padding:6px 12px;font-size:12px;outline:none;width:240px}}
.controls input:focus{{border-color:#388bfd}}
.filter-btn{{background:#1c2128;border:1px solid #30363d;color:#8b949e;border-radius:6px;
             padding:6px 12px;font-size:12px;cursor:pointer;transition:.15s}}
.filter-btn:hover,.filter-btn.active{{background:#388bfd22;color:#58a6ff;border-color:#388bfd}}
.blast-btn{{border-color:#ff9800;color:#ff9800;background:#1a0a00}}
.blast-btn:hover,.blast-btn.active{{background:#ff980022;border-color:#ff9800;color:#ff9800}}
.sort-btn{{background:#0d1117;border:1px solid #21262d;color:#555;border-radius:4px;
           padding:4px 8px;font-size:11px;cursor:pointer}}
.sort-btn:hover{{color:#8b949e}}
.count-badge{{font-size:11px;color:#8b949e;margin-left:8px}}
.table-wrap{{overflow-x:auto;padding:0 24px 24px}}
table{{width:100%;border-collapse:collapse;font-size:12px;min-width:1400px}}
thead th{{background:#161b22;color:#8b949e;font-size:10px;font-weight:600;text-transform:uppercase;
          letter-spacing:.5px;padding:8px 6px;border-bottom:2px solid #30363d;
          cursor:pointer;white-space:nowrap;position:sticky;top:0;z-index:5}}
thead th:hover{{color:#e6edf3}}
tbody tr{{border-bottom:1px solid #161b22;transition:.1s}}
tbody tr:hover{{background:#161b2255}}
.blast-row{{background:#1a0a0033}}
.blast-row:hover{{background:#1a0a0066}}
.hidden{{display:none}}
/* Modal */
#chartModal{{display:none;position:fixed;inset:0;background:#000000cc;z-index:9999;
             align-items:center;justify-content:center}}
#chartModal.open{{display:flex}}
#modalBox{{background:#0d1117;border:1px solid #30363d;border-radius:12px;
           padding:0;max-width:95vw;max-height:92vh;overflow:hidden;display:flex;flex-direction:column}}
#modalHeader{{padding:12px 16px;background:#161b22;border-bottom:1px solid #30363d;
              display:flex;justify-content:space-between;align-items:center}}
#modalTitle{{font-weight:700;font-size:14px;color:#e6edf3}}
#modalClose{{background:none;border:none;color:#8b949e;font-size:20px;cursor:pointer;padding:0 4px}}
#modalClose:hover{{color:#e6edf3}}
#modalChartTabs{{display:flex;gap:0;background:#0d1117;border-bottom:1px solid #30363d}}
.chart-tab{{padding:8px 16px;background:none;border:none;color:#8b949e;cursor:pointer;
            font-size:12px;border-bottom:2px solid transparent}}
.chart-tab:hover{{color:#e6edf3}}
.chart-tab.active{{color:#58a6ff;border-bottom-color:#58a6ff}}
#modalImgWrap{{overflow:auto;flex:1;display:flex;align-items:center;justify-content:center;padding:8px}}
#modalImg{{max-width:100%;height:auto;border-radius:4px}}
#modalLoading{{padding:40px;color:#8b949e;font-size:14px}}
/* Darvas legend */
.darvas-legend{{display:flex;gap:12px;font-size:11px;color:#8b949e;margin-left:auto}}
.dl{{display:flex;align-items:center;gap:4px}}
.dl-dot{{width:10px;height:10px;border-radius:2px}}
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>🏆 Multibagger Report — NSE Full Scan</h1>
    <div class="sub">Strategy: ATH Breakout · Ultra-Slow MACD(34,1000,20) · MTF RSI · Darvas Box · Blast Breakout &nbsp;|&nbsp; {scan_time}</div>
  </div>
  <div class="nav-pills">
    <a href="/">📊 Full Report</a>
    <a href="/multibagger" class="active">💎 Multibagger</a>
    <a href="/ath">🏆 ATH Breakout</a>
    <a href="/rocket">🚀 Rocket Scanner</a>
  </div>
</div>

<div class="stats">
  <div class="stat"><div class="val">{total_scanned}</div><div class="lbl">Stocks Scanned</div></div>
  <div class="stat"><div class="val">{total_ok}</div><div class="lbl">With Data</div></div>
  <div class="stat blast-stat"><div class="val">{blast_cnt}</div><div class="lbl">🚀 BLAST Today</div></div>
  <div class="stat"><div class="val">{strong_cnt}</div><div class="lbl">Strong Signals</div></div>
  <div class="stat"><div class="val">{mrsi70_cnt}</div><div class="lbl">M-RSI &gt; 70</div></div>
  <div class="stat"><div class="val">{avg_score:.1f}</div><div class="lbl">Avg Score</div></div>
</div>

<div class="controls">
  <input type="text" id="searchBox" placeholder="🔍  Search ticker or company…" oninput="filterTable()">

  <button class="filter-btn blast-btn" id="blastBtn" onclick="toggleFilter('blast')">🚀 BLAST Only</button>
  <button class="filter-btn" id="buyBtn"   onclick="toggleFilter('buy')">📈 BUY Signals</button>
  <button class="filter-btn" id="mrsi70Btn" onclick="toggleFilter('mrsi70')">💜 M-RSI &gt; 70</button>
  <button class="filter-btn" id="dboxBtn"  onclick="toggleFilter('darvas')">📦 Darvas Break</button>
  <button class="filter-btn" id="allBtn"   onclick="clearFilters()" style="color:#26d07c;border-color:#26d07c;">Show All</button>

  <span class="count-badge" id="countBadge">{total_ok} stocks</span>

  <div class="darvas-legend">
    <div class="dl"><div class="dl-dot" style="background:#00ff88"></div>Breakout</div>
    <div class="dl"><div class="dl-dot" style="background:#ff4444"></div>Breakdown</div>
    <div class="dl"><div class="dl-dot" style="background:#fbbf24"></div>In Box</div>
  </div>
</div>

<div class="table-wrap">
<table id="stockTable">
<thead>
<tr>
  <th onclick="sortTable(0,'num')">#</th>
  <th onclick="sortTable(1,'str')">Ticker / Company</th>
  <th onclick="sortTable(2,'num')">Price</th>
  <th onclick="sortTable(3,'num')">ATH%</th>
  <th onclick="sortTable(4,'num')">M-RSI</th>
  <th onclick="sortTable(5,'num')">W-RSI</th>
  <th onclick="sortTable(6,'num')">D-RSI</th>
  <th onclick="sortTable(7,'num')">MACD US</th>
  <th onclick="sortTable(8,'str')">🚀 BLAST</th>
  <th onclick="sortTable(9,'str')">Signal</th>
  <th onclick="sortTable(10,'num')">Score</th>
  <th onclick="sortTable(11,'str')">Darvas D</th>
  <th onclick="sortTable(12,'str')">Darvas W</th>
  <th onclick="sortTable(13,'str')">Darvas M</th>
  <th>Resistance</th>
  <th>Support</th>
  <th onclick="sortTable(16,'num')">Vol</th>
  <th onclick="sortTable(17,'num')">Trades</th>
  <th onclick="sortTable(18,'num')">Win%</th>
  <th>Charts</th>
</tr>
</thead>
<tbody id="tableBody">
{rows}
</tbody>
</table>
</div>

<!-- Chart Modal -->
<div id="chartModal" onclick="if(event.target===this)closeModal()">
  <div id="modalBox">
    <div id="modalHeader">
      <span id="modalTitle">Chart</span>
      <button id="modalClose" onclick="closeModal()">✕</button>
    </div>
    <div id="modalChartTabs">
      <button class="chart-tab active" id="tab-d" onclick="switchTab('d')">Daily</button>
      <button class="chart-tab"        id="tab-w" onclick="switchTab('w')">Weekly</button>
      <button class="chart-tab"        id="tab-m" onclick="switchTab('m')">Monthly</button>
    </div>
    <div id="modalImgWrap">
      <div id="modalLoading">Loading chart…</div>
      <img id="modalImg" style="display:none" onload="imgLoaded()" onerror="imgError()">
    </div>
  </div>
</div>

<script>
// ── State ─────────────────────────────────────────────────────────────────────
let activeFilter = null;
let sortCol = 10, sortDir = -1;  // default: sort by score desc

let currentSym = '', currentUrls = {{}};

// ── Filter ────────────────────────────────────────────────────────────────────
function filterTable() {{
  const q    = document.getElementById('searchBox').value.toLowerCase();
  const rows = document.querySelectorAll('#tableBody .stock-row');
  let   vis  = 0;
  rows.forEach(r => {{
    const text = r.textContent.toLowerCase();
    const matchSearch = !q || text.includes(q);
    let   matchFilter = true;

    if (activeFilter === 'blast')  matchFilter = r.dataset.blast  === '1';
    if (activeFilter === 'buy')    matchFilter = ['STRONG BUY','MACD MEGA BUY','BUY','VOL BUY','M-RSI BUY'].includes(r.dataset.signal);
    if (activeFilter === 'mrsi70') matchFilter = parseFloat(r.dataset.mrsi) >= 70;
    if (activeFilter === 'darvas') matchFilter = r.dataset.darvas === 'BREAKOUT';

    const show = matchSearch && matchFilter;
    r.classList.toggle('hidden', !show);
    if (show) vis++;
  }});
  document.getElementById('countBadge').textContent = vis + ' stocks';
}}

function toggleFilter(name) {{
  activeFilter = (activeFilter === name) ? null : name;
  ['blastBtn','buyBtn','mrsi70Btn','dboxBtn'].forEach(id => {{
    document.getElementById(id).classList.remove('active');
  }});
  const map = {{blast:'blastBtn',buy:'buyBtn',mrsi70:'mrsi70Btn',darvas:'dboxBtn'}};
  if (activeFilter && map[activeFilter]) document.getElementById(map[activeFilter]).classList.add('active');
  filterTable();
}}

function clearFilters() {{
  activeFilter = null;
  document.getElementById('searchBox').value = '';
  ['blastBtn','buyBtn','mrsi70Btn','dboxBtn'].forEach(id => document.getElementById(id).classList.remove('active'));
  filterTable();
}}

// ── Sort ──────────────────────────────────────────────────────────────────────
function sortTable(col, type) {{
  if (sortCol === col) sortDir *= -1;
  else {{ sortCol = col; sortDir = -1; }}
  const tbody = document.getElementById('tableBody');
  const rows  = Array.from(tbody.querySelectorAll('.stock-row'));
  rows.sort((a, b) => {{
    const av = a.cells[col].textContent.replace(/[₹,%+]/g,'').trim();
    const bv = b.cells[col].textContent.replace(/[₹,%+]/g,'').trim();
    if (type === 'num') return (parseFloat(av)||0 - parseFloat(bv)||0) * sortDir;
    return av.localeCompare(bv) * sortDir;
  }});
  rows.forEach(r => tbody.appendChild(r));
}}

// ── Chart Modal ───────────────────────────────────────────────────────────────
function showChart(sym, tab, url) {{
  currentSym  = sym;
  const suffix = tab.toLowerCase() === 'daily'   ? 'd' :
                 tab.toLowerCase() === 'weekly'  ? 'w' : 'm';
  // Build all 3 URLs from the daily URL pattern
  const base = url.replace(/_daily|_weekly|_monthly/, '');
  currentUrls = {{
    d: `/charts/multibagger/${{sym}}_daily.png`,
    w: `/charts/multibagger/${{sym}}_weekly.png`,
    m: `/charts/multibagger/${{sym}}_monthly.png`,
  }};
  document.getElementById('modalTitle').textContent = sym + ' — ' + tab + ' Chart';
  document.getElementById('chartModal').classList.add('open');
  switchTab(suffix);
}}

function switchTab(t) {{
  ['d','w','m'].forEach(tab => {{
    document.getElementById('tab-'+tab).classList.toggle('active', tab===t);
  }});
  document.getElementById('modalLoading').style.display = 'block';
  document.getElementById('modalImg').style.display     = 'none';
  document.getElementById('modalImg').src = currentUrls[t] + '?t=' + Date.now();
}}

function imgLoaded() {{
  document.getElementById('modalLoading').style.display = 'none';
  document.getElementById('modalImg').style.display     = 'block';
}}
function imgError() {{
  document.getElementById('modalLoading').textContent = '⚠️ Chart not available yet';
}}

function closeModal() {{
  document.getElementById('chartModal').classList.remove('open');
  document.getElementById('modalImg').src = '';
}}

document.addEventListener('keydown', e => {{ if(e.key==='Escape') closeModal(); }});
</script>
</body>
</html>"""

# ── GitHub Push ───────────────────────────────────────────────────────────────
def push_to_github():
    token = os.environ.get('GITHUB_TOKEN', '')
    if not token:
        tprint("  \u2139\ufe0f  GITHUB_TOKEN not set \u2014 skipping push.")
        return
    try:
        env = {**os.environ,
               'GIT_AUTHOR_NAME': 'NSE Bot', 'GIT_AUTHOR_EMAIL': 'bot@noreply',
               'GIT_COMMITTER_NAME': 'NSE Bot', 'GIT_COMMITTER_EMAIL': 'bot@noreply'}
        subprocess.run(['git', 'add', REPORT_HTML, str(CHARTS_DIR)],
                       check=False, env=env, capture_output=True)
        subprocess.run(['git', 'add', '-A'], check=False, env=env, capture_output=True)
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
        result = subprocess.run(
            ['git', 'commit', '-m', f'multibagger: full scan + Darvas + Blast {now_str}'],
            check=False, env=env, capture_output=True, text=True
        )
        if 'nothing to commit' in result.stdout.lower():
            tprint("  \u2139\ufe0f  Nothing new to commit.")
            return
        remote_raw = subprocess.run(
            ['git', 'remote', 'get-url', 'origin'],
            capture_output=True, text=True
        ).stdout.strip()
        remote = remote_raw
        if remote.startswith('https://') and '@' not in remote:
            remote = remote.replace('https://', f'https://x-access-token:{token}@')
        subprocess.run(['git', 'push', remote, 'HEAD'], check=True, env=env, capture_output=True)
        tprint("  \u2705 Pushed to GitHub!")
    except subprocess.CalledProcessError as e:
        tprint(f"  \u26a0\ufe0f  GitHub push failed: {e}")
    except Exception as e:
        tprint(f"  \u26a0\ufe0f  GitHub push error: {e}")

# ── Progress Cache ───────────────────────────────────────────────────────────────
def load_cache(today_str):
    """Load per-ticker result cache for today date.
    Returns dict {ticker: result_dict}.  Returns {} if stale or missing.
    """
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, 'r') as f:
                data = json.load(f)
            if data.get('date') == today_str:
                results = data.get('results', {})
                tprint(f"  📂 Cache hit: {len(results)} stocks already processed today")
                return results
            else:
                tprint(f"  📂 Cache from {data.get('date','?')} — starting fresh for {today_str}")
        except Exception:
            pass
    return {}

def save_cache(cache):
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, 'w') as f:
        json.dump(cache, f)

# ── Main ──────────────────────────────────────────────────────────────────────────
def main():
    t_start = time.time()
    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    ist_now   = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    scan_time = ist_now.strftime('%Y-%m-%d %H:%M IST')
    today_str = ist_now.strftime('%Y-%m-%d')

    tprint("=" * 65)
    tprint(f"  NSE Full Scan  |  {scan_time}")
    tprint(f"  Charts  -> {CHARTS_DIR.resolve()}")
    tprint("=" * 65)

    # Load universe
    stocks = load_nse_stocks()
    total_scanned = len(stocks)
    tprint(f"\n  Universe  : {total_scanned:,} stocks loaded")

    # Cache partition
    cache_results = load_cache(today_str)
    items = list(stocks.items())

    items_cached   = []
    items_to_fetch = []
    for name, ticker in items:
        safe_sym = ticker.replace('.NS', '').replace('.BO', '')
        path_d = CHARTS_DIR / f"{safe_sym}_daily.png"
        path_w = CHARTS_DIR / f"{safe_sym}_weekly.png"
        path_m = CHARTS_DIR / f"{safe_sym}_monthly.png"
        if (ticker in cache_results and
                chart_is_fresh(path_d) and
                chart_is_fresh(path_w) and
                chart_is_fresh(path_m)):
            items_cached.append((name, ticker))
        else:
            items_to_fetch.append((name, ticker))

    tprint(f"  Cached    : {len(items_cached):,} stocks (already done today)")
    tprint(f"  To scan   : {len(items_to_fetch):,} stocks\n")

    results    = []
    ok_count   = 0
    fail_count = 0
    done       = len(items_cached)

    for name, ticker in items_cached:
        results.append(cache_results[ticker])
        ok_count += 1

    if items_to_fetch:
        # ── Phase 1: Batch Download ──────────────────────────────────
        tickers_needed = [t for _, t in items_to_fetch]
        batches        = [tickers_needed[i: i + BATCH_SIZE]
                         for i in range(0, len(tickers_needed), BATCH_SIZE)]
        nb = len(batches)

        tprint(f"  PHASE 1/2 - Downloading {len(tickers_needed):,} tickers")
        tprint(f"  Batches   : {nb} x {BATCH_SIZE}  |  {min(MAX_WORKERS,8)} parallel downloaders")
        tprint("-" * 65)

        prefetched  = {}
        dl_done     = 0
        dl_lock     = threading.Lock()
        t_dl_start  = time.time()

        def _fetch_batch(args):
            idx, batch = args
            data = batch_fetch(batch)
            return idx, data

        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, 8)) as dl_exe:
            batch_futs = {dl_exe.submit(_fetch_batch, (idx, b)): idx
                          for idx, b in enumerate(batches)}
            for fut in as_completed(batch_futs):
                try:
                    idx, batch_data = fut.result()
                    with dl_lock:
                        prefetched.update(batch_data)
                        dl_done += 1
                        elapsed = time.time() - t_dl_start
                        eta_s   = (elapsed / dl_done) * (nb - dl_done) if dl_done else 0
                        suffix  = (f"{len(prefetched):,} OK  "
                                   f"elapsed {elapsed:.0f}s  "
                                   f"ETA ~{eta_s:.0f}s")
                        progress_bar(dl_done, nb, width=36,
                                     prefix='Download', suffix=suffix)
                        if dl_done % 10 == 0 or dl_done == nb:
                            tprint(f"\n    Batch {dl_done}/{nb}  "
                                   f"| {len(prefetched):,} fetched  "
                                   f"| {elapsed:.0f}s elapsed")
                except Exception as e:
                    tprint(f"\n    Batch error: {e}")

        dl_elapsed = time.time() - t_dl_start
        tprint(f"\n  Downloaded: {len(prefetched):,} / {len(tickers_needed):,} "
               f"in {dl_elapsed:.1f}s\n")

        # ── Phase 2: Compute Indicators + Charts ──────────────────────
        tprint(f"  PHASE 2/2 - Computing indicators + signals + charts")
        tprint(f"  Workers   : {MAX_WORKERS}  |  Stocks: {len(items_to_fetch):,}")
        tprint("-" * 65)

        t_cp_start  = time.time()
        cp_lock     = threading.Lock()
        blast_list  = []

        def worker(args):
            name, ticker = args
            return name, ticker, process_stock(name, ticker, prefetched.get(ticker))

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as exe:
            futures = {exe.submit(worker, item): item for item in items_to_fetch}

            for fut in as_completed(futures):
                done += 1
                try:
                    name, ticker, result = fut.result()
                    if result:
                        with cp_lock:
                            results.append(result)
                            cache_results[ticker] = result
                            ok_count += 1
                            if result['blast']:
                                blast_list.append(ticker)
                    else:
                        with cp_lock:
                            fail_count += 1
                except Exception:
                    with cp_lock:
                        fail_count += 1

                with cp_lock:
                    elapsed = time.time() - t_cp_start
                    rate    = done / max(elapsed, 1)
                    eta_s   = (total_scanned - done) / max(rate, 0.01)
                    suffix  = (f"{ok_count:,} OK  {fail_count} skip  "
                               f"{rate:.1f}/s  ETA ~{int(eta_s)}s")
                    progress_bar(done, total_scanned, width=32,
                                 prefix='Compute ', suffix=suffix)

                if done % 250 == 0:
                    bl = f"  | BLAST: {', '.join(blast_list[-5:])}" if blast_list else ""
                    tprint(f"\n  [{done}/{total_scanned}] "
                           f"OK={ok_count}  skip={fail_count}  "
                           f"elapsed={time.time()-t_cp_start:.0f}s{bl}")

                if done % 500 == 0:
                    save_cache({'date': today_str, 'done': done,
                                'total': total_scanned, 'results': cache_results})

        cp_elapsed    = time.time() - t_cp_start
        total_elapsed = time.time() - t_start
        tprint(f"\n\n{'='*65}")
        tprint(f"  SCAN COMPLETE")
        tprint(f"  Processed : {ok_count:,}  |  Skipped: {fail_count:,}")
        if blast_list:
            tprint(f"  BLAST     : {len(blast_list)} -> {', '.join(blast_list[:10])}")
        tprint(f"  Time      : DL={dl_elapsed:.0f}s  Compute={cp_elapsed:.0f}s  "
               f"Total={total_elapsed:.0f}s")
        tprint(f"{'='*65}\n")

    # ── Build HTML report ─────────────────────────────────────────────
    tprint("  Building HTML report...")
    html = build_html(results, scan_time, total_scanned, ok_count)
    with open(REPORT_HTML, 'w', encoding='utf-8') as f:
        f.write(html)
    size_mb = Path(REPORT_HTML).stat().st_size / 1024 / 1024
    tprint(f"  Saved: {REPORT_HTML}  ({size_mb:.1f} MB)")
    tprint(f"  Stocks: {ok_count}  |  BLAST signals: {sum(1 for r in results if r.get('blast'))}")

    # ── GitHub Push ───────────────────────────────────────────────────
   # tprint("\n  Pushing to GitHub...")
   # push_to_github()

    save_cache({'date': today_str, 'done': total_scanned,
                'total': total_scanned, 'results': cache_results})
    tprint(f"\n  Done!  Total runtime: {time.time()-t_start:.0f}s\n")


if __name__ == '__main__':
    main()
