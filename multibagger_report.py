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
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
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
TRADES_REPORT_HTML = "multibagger_trades_report.html"
CHARTS_DIR     = Path("charts")
CACHE_FILE     = Path("charts/scan_cache.json")
LOOKBACK_YEARS = 'max'       # fetch full history from IPO date
MAX_WORKERS    = 20           # parallel THREADS for I/O-bound downloads (network-bound: GIL released during I/O, so threads work well)
BATCH_SIZE     = 50           # tickers per yfinance batch download
MIN_DAILY_BARS = 10            # accept stocks with as few as 10 trading days (new IPOs)

# Compute phase (indicators + Darvas/S-R/backtest + matplotlib chart rendering) is 100%
# CPU-bound. Threads can't parallelize this — Python's GIL means only one thread runs
# Python/NumPy/pandas/matplotlib bytecode at a time, so a ThreadPoolExecutor here just
# time-slices a single core. Real speedup requires separate OS processes (each with its
# own interpreter + GIL), so this phase uses ProcessPoolExecutor with one worker per CPU core.
COMPUTE_WORKERS = max(1, os.cpu_count() or 4)

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

# ── Trend-beginning-stage detection thresholds ────────────────────────────────
NIFTY_INDEX_TICKER   = "^NSEI"   # NIFTY 50 index, used for Relative Strength

STAGE_SMA_PERIOD      = 150   # daily-SMA proxy for Weinstein's 30-week MA
STAGE_SLOPE_LOOKBACK  = 20    # ~1 month, used to judge whether SMA150 is rising/falling
STAGE_FLAT_SLOPE_PCT  = 0.5   # SMA150 slope within +-0.5% over lookback = "flat" (basing)

SQUEEZE_BB_WINDOW     = 20    # Bollinger Band window used for width calc
SQUEEZE_PCTILE_WINDOW = 120   # trailing days used to rank how tight the current BB width is
SQUEEZE_PCTILE_THRESH = 20    # BB width in bottom 20th percentile = "squeeze" (coiling base)
SQUEEZE_LOOKBACK_DAYS = 10    # squeeze must have occurred within the last N days to count as "fresh"

RSI_TARGET_LO         = 55    # RSI zone considered "just turning up" (trend-start momentum)
RSI_TARGET_HI         = 65
RSI_TARGET_HORIZON    = 20    # trading days forward used to measure historical analog moves
RSI_TARGET_MIN_SAMPLES = 5    # need at least this many historical analogs to trust the target

RS_LOOKBACKS_DAYS     = [63, 126, 189, 252]   # ~3/6/9/12 months, IBD-style weighting
RS_WEIGHTS            = [0.4, 0.2, 0.2, 0.2]  # heavier weight on the most recent quarter


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
    """Build the (name, ticker) universe.

    IMPORTANT: uniqueness is enforced on the TICKER (the actual identifier used
    to fetch price data), never on the company name. Company names are NOT
    guaranteed unique in NSE's own data (DVR share classes, partly-paid share
    classes, and renamed companies can carry identical or near-identical name
    strings) — keying a dict by name risks two different real stocks silently
    colliding, where one symbol's ticker gets discarded or a name ends up
    pointing at the wrong company's price. Returns a list of (name, ticker)
    tuples with every ticker guaranteed unique.
    """
    seen_tickers = {}   # ticker -> name, the single source of truth for uniqueness
    seen_names   = {}    # name -> ticker, used only to detect + disambiguate name clashes
    dup_name_ct  = 0

    def _add(ticker, name):
        nonlocal dup_name_ct
        if ticker in seen_tickers:
            return False   # exact duplicate ticker (e.g. duplicate CSV row) — skip silently
        if name in seen_names:
            dup_name_ct += 1
            # Disambiguate rather than silently overwrite — two different
            # tickers must never end up displayed under the same identical name.
            name = f"{name} ({ticker.replace('.NS','').replace('.BO','')})"
        seen_tickers[ticker] = name
        seen_names[name] = ticker
        return True

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
            added = 0
            for s, n in zip(symbols, names):
                if not s or s.lower() == 'nan':
                    continue
                if _add(f"{s}.NS", n):
                    added += 1
            if dup_name_ct:
                tprint(f"  ⚠️  {dup_name_ct} duplicate company name(s) in NSE Cash CSV "
                       f"— disambiguated by symbol so no ticker was dropped")
            tprint(f"  📋 NSE Cash (EQ): {added} stocks")
        except Exception as e:
            tprint(f"  ⚠️  NSE Cash CSV error: {e}")

    # NSE SME (MW-SME-05-May-2026.csv)  [vectorised: no iterrows]
    if NSE_SME_CSV.exists():
        try:
            df = pd.read_csv(NSE_SME_CSV)
            df.columns = [c.strip() for c in df.columns]
            sym_col = next((c for c in df.columns if 'SYMBOL' in c.upper()), None)
            added, skipped_collision = 0, 0
            if sym_col:
                syms = df[sym_col].astype(str).str.strip().str.strip('"')
                valid = syms[~syms.isin(['SYMBOL', 'nan', '']) & (syms.str.len() > 0)]
                for sym in valid:
                    ticker = f"{sym}.NS"
                    if ticker in seen_tickers:
                        # Same exact ticker string already claimed by the mainboard
                        # list. Yahoo Finance can only serve one real security per
                        # ticker string, so we can't safely show both under this
                        # symbol — keep the mainboard entry and skip the SME one
                        # rather than silently mislabeling either.
                        skipped_collision += 1
                        continue
                    if _add(ticker, f"{sym} (SME)"):
                        added += 1
            if skipped_collision:
                tprint(f"  ⚠️  {skipped_collision} NSE SME symbol(s) collide with a mainboard "
                       f"ticker — kept the mainboard listing, skipped the SME duplicate")
            tprint(f"  📋 NSE SME: {added} stocks")
        except Exception as e:
            tprint(f"  ⚠️  NSE SME CSV error: {e}")

    return [(name, ticker) for ticker, name in seen_tickers.items()]  # ticker uniqueness enforced above

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

def donchian(high, low, period=20):
    """Donchian Channel: rolling high/low over `period` bars, EXCLUDING the
    current bar (shifted by 1) so a breakout check (Close > upper) reflects a
    genuine break of the prior range rather than today's own high padding it."""
    upper = high.rolling(period).max().shift(1)
    lower = low.rolling(period).min().shift(1)
    mid   = (upper + lower) / 2
    return upper, lower, mid

def cci(high, low, close, period=20):
    """Commodity Channel Index. MAD is computed via a vectorized sliding-window
    view rather than pandas' per-window .apply() — with period=200 across a
    multi-thousand-stock universe, .apply()'s per-row Python call overhead adds
    up fast; this does the same math with no Python-level loop."""
    tp  = (high + low + close) / 3
    sma = tp.rolling(period).mean()
    tp_vals = tp.to_numpy(dtype=float)
    n = len(tp_vals)
    mad = np.full(n, np.nan)
    if n >= period:
        windows = np.lib.stride_tricks.sliding_window_view(tp_vals, period)
        wmean = windows.mean(axis=1)
        mad[period - 1:] = np.abs(windows - wmean[:, None]).mean(axis=1)
    mad_s = pd.Series(mad, index=tp.index)
    return (tp - sma) / (0.015 * mad_s.replace(0, np.nan))

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
    x = np.arange(len(data))
    y = data['Close'].values
    if np.std(y) == 0:
        # Flat/illiquid series (common on thinly-traded SME stocks) — polyfit on a
        # zero-variance series is ill-conditioned and raises numpy.RankWarning.
        # A flat trend line is also the mathematically correct fit here anyway.
        trend = np.full_like(y, y[0], dtype=float)
        return trend, trend, trend, data.index
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

# ── Stage Analysis (Weinstein-style) ─────────────────────────────────────────
def detect_stage(df_d):
    """Classify the stock into a Weinstein-style market stage using SMA150 (daily
    proxy for the classic 30-week MA) as the trend baseline and its slope.

    Returns (stage_num, stage_label):
      1 = Basing        (price chopping around a flat SMA150 — pre-breakout base)
      2 = Advancing      (price above a rising SMA150 — the "trend beginning" zone)
      3 = Topping        (price above SMA150, but the MA has stopped rising)
      4 = Declining       (price below a falling SMA150)
      0 = Unknown        (not enough history to judge, e.g. new IPOs)
    """
    n = len(df_d)
    if n < STAGE_SMA_PERIOD + STAGE_SLOPE_LOOKBACK:
        return 0, 'Unknown'

    sma = df_d['Close'].rolling(STAGE_SMA_PERIOD, min_periods=STAGE_SMA_PERIOD).mean()
    cur_close = float(df_d['Close'].iloc[-1])
    cur_sma   = float(sma.iloc[-1])
    prev_sma  = float(sma.iloc[-1 - STAGE_SLOPE_LOOKBACK])

    if pd.isna(cur_sma) or pd.isna(prev_sma) or prev_sma == 0:
        return 0, 'Unknown'

    slope_pct = (cur_sma - prev_sma) / abs(prev_sma) * 100
    above_ma  = cur_close >= cur_sma

    if above_ma and slope_pct > STAGE_FLAT_SLOPE_PCT:
        return 2, 'Advancing'
    if above_ma and slope_pct <= STAGE_FLAT_SLOPE_PCT:
        return 3, 'Topping'
    if (not above_ma) and slope_pct < -STAGE_FLAT_SLOPE_PCT:
        return 4, 'Declining'
    return 1, 'Basing'


# ── Volatility Squeeze (VCP / Bollinger-squeeze proxy) ───────────────────────
def detect_squeeze(df_d):
    """Detect volatility contraction ("coiling") followed by expansion — the
    volume-dry-up-then-breakout pattern typical of real trend starts.

    Returns dict: {in_squeeze_now, squeeze_recent, bb_width_pctile}
      in_squeeze_now : today's BB width is in the bottom SQUEEZE_PCTILE_THRESH% of
                       the trailing SQUEEZE_PCTILE_WINDOW days (tight base right now)
      squeeze_recent : the tightest point in the last SQUEEZE_LOOKBACK_DAYS days was a
                       squeeze, i.e. the stock was recently coiled and may now be
                       expanding out of it (this is the actual breakout signal)
      bb_width_pctile: current BB-width percentile rank (0-100, lower = tighter)
    """
    n = len(df_d)
    if n < SQUEEZE_PCTILE_WINDOW + SQUEEZE_BB_WINDOW:
        return {'in_squeeze_now': False, 'squeeze_recent': False, 'bb_width_pctile': np.nan}

    mid = df_d['Close'].rolling(SQUEEZE_BB_WINDOW).mean()
    std = df_d['Close'].rolling(SQUEEZE_BB_WINDOW).std()
    bb_width = (4 * std / mid.replace(0, np.nan))  # (upper-lower)/mid, 2*std each side

    pctile = bb_width.rolling(SQUEEZE_PCTILE_WINDOW).rank(pct=True) * 100
    cur_pctile = float(pctile.iloc[-1]) if not pd.isna(pctile.iloc[-1]) else np.nan

    recent_pctile = pctile.tail(SQUEEZE_LOOKBACK_DAYS)
    squeeze_recent = bool((recent_pctile <= SQUEEZE_PCTILE_THRESH).any())
    in_squeeze_now = bool(cur_pctile <= SQUEEZE_PCTILE_THRESH) if not pd.isna(cur_pctile) else False

    return {'in_squeeze_now': in_squeeze_now, 'squeeze_recent': squeeze_recent,
            'bb_width_pctile': cur_pctile}


# ── RSI Momentum Analog Target ───────────────────────────────────────────────
def rsi_momentum_target(df_d):
    """Data-driven target: find every past occurrence where this stock's RSI was
    just turning up through the RSI_TARGET_LO-RSI_TARGET_HI zone (the same setup
    as "now"), and measure what the stock actually did over the following
    RSI_TARGET_HORIZON trading days. The median of those historical moves,
    applied to the current price, gives a target grounded in the stock's own
    behavior rather than a fixed multiplier.

    Returns dict: {target, median_gain_pct, sample_count} — target is None if
    there isn't enough history of similar setups to trust the estimate.
    """
    rsi_s = df_d['RSI']
    close = df_d['Close']
    n = len(df_d)
    if n < RSI_TARGET_HORIZON + 30:
        return {'target': None, 'median_gain_pct': None, 'sample_count': 0}

    # Forward-looking max close over the next RSI_TARGET_HORIZON days (excludes today)
    fwd_max = close.shift(-1)[::-1].rolling(RSI_TARGET_HORIZON, min_periods=1).max()[::-1]

    setup_mask = (rsi_s >= RSI_TARGET_LO) & (rsi_s < RSI_TARGET_HI) & (rsi_s > rsi_s.shift(1))
    # Exclude the most recent RSI_TARGET_HORIZON bars — they don't have forward data yet
    setup_mask.iloc[-RSI_TARGET_HORIZON:] = False
    valid = setup_mask & fwd_max.notna() & (close > 0)

    if valid.sum() < RSI_TARGET_MIN_SAMPLES:
        return {'target': None, 'median_gain_pct': None, 'sample_count': int(valid.sum())}

    gains_pct = (fwd_max[valid] - close[valid]) / close[valid] * 100
    median_gain = float(gains_pct.median())
    cur_price = float(close.iloc[-1])
    target = cur_price * (1 + median_gain / 100)

    return {'target': round(target, 2), 'median_gain_pct': round(median_gain, 1),
            'sample_count': int(valid.sum())}


# ── Relative Strength vs Index (IBD-style RS raw score) ──────────────────────
def relative_strength_raw(df_d):
    """Weighted blended return over ~3/6/9/12 months (IBD-style, heavier weight on
    the most recent quarter). This is a raw score for one stock; call this on the
    index too, and rank all stocks' scores as a percentile to get an RS Rating.
    Returns None if there isn't enough history.
    """
    n = len(df_d)
    if n <= max(RS_LOOKBACKS_DAYS):
        return None
    close = df_d['Close']
    cur = float(close.iloc[-1])
    score = 0.0
    for days, w in zip(RS_LOOKBACKS_DAYS, RS_WEIGHTS):
        past = float(close.iloc[-1 - days])
        if past <= 0:
            return None
        ret = (cur / past) - 1
        score += w * ret
    return score


# ── Generic Crossover State (RSI200/SMA34, CCI200/SMA34, etc.) ───────────────
def crossover_state(fast, slow, fresh_lookback=5):
    """Given two aligned series (e.g. RSI200 vs its SMA34), returns:
      state       : 'Bullish' (fast > slow), 'Bearish' (fast < slow), 'Unknown'
      fresh_cross : 'Bull Cross' / 'Bear Cross' / None — True only if the cross
                    happened within the last `fresh_lookback` bars (i.e. it's a
                    genuinely recent, actionable signal rather than a crossover
                    from months ago that just happens to still be in that state)
    """
    if len(fast) < 2 or pd.isna(fast.iloc[-1]) or pd.isna(slow.iloc[-1]):
        return 'Unknown', None

    state = 'Bullish' if fast.iloc[-1] > slow.iloc[-1] else 'Bearish'

    diff = (fast - slow)
    recent = diff.tail(fresh_lookback + 1).dropna()
    fresh_cross = None
    if len(recent) >= 2:
        sign = np.sign(recent.values)
        # any adjacent-sign flip within the recent window counts as a fresh cross
        flips = np.where(np.diff(sign) != 0)[0]
        if len(flips) > 0:
            fresh_cross = 'Bull Cross' if sign[-1] > 0 else 'Bear Cross'

    return state, fresh_cross


def macd_trend_state(df, hist_lookback=3):
    """Friendly MACD(12,26,9) trend read for a timeframe (weekly/monthly), for
    checking — the way a discretionary trader would — whether the higher
    timeframes agree with a daily BUY signal. Returns one of:
      'Uptrend'      : MACD above signal, and not losing momentum
      'Weakening'    : MACD above signal, but the histogram is fading
                       (a bullish state that's starting to roll over)
      'Reversing Up' : MACD below signal, but the histogram is improving
                       (a downtrend that's starting to turn)
      'Downtrend'    : MACD below signal, and not improving
      'Unknown'      : not enough history
    """
    macd, sig, hist = df['MACD'], df['MACD_sig'], df['MACD_hist']
    if len(macd) < hist_lookback + 1 or pd.isna(macd.iloc[-1]) or pd.isna(sig.iloc[-1]):
        return 'Unknown'

    bullish = macd.iloc[-1] > sig.iloc[-1]
    recent_hist = hist.tail(hist_lookback + 1).dropna()
    improving = len(recent_hist) >= 2 and recent_hist.iloc[-1] > recent_hist.iloc[0]

    if bullish:
        return 'Uptrend' if improving else 'Weakening'
    else:
        return 'Reversing Up' if improving else 'Downtrend'


# ── Multi-timeframe RSI direction signal ─────────────────────────────────────
def mtf_rsi_signal(df_w, df_m):
    """Weekly + monthly RSI(14) direction combined into one signal:
      both rising  -> 'BUY'   (momentum building across timeframes)
      both falling -> 'SELL'  (momentum fading across timeframes)
      mixed        -> 'NEUTRAL'
    Returns (signal, w_rising, m_rising) — the individual directions are
    returned too so they can be shown separately (e.g. on the RSI chart panel).
    """
    def _rising(rsi_series):
        s = rsi_series.dropna()
        if len(s) < 2:
            return None
        return bool(s.iloc[-1] > s.iloc[-2])

    w_rising = _rising(df_w['RSI'])
    m_rising = _rising(df_m['RSI'])

    if w_rising is None or m_rising is None:
        return 'NEUTRAL', w_rising, m_rising
    if w_rising and m_rising:
        return 'BUY', w_rising, m_rising
    if (not w_rising) and (not m_rising):
        return 'SELL', w_rising, m_rising
    return 'NEUTRAL', w_rising, m_rising


# ── Chart Pattern Recognition ─────────────────────────────────────────────────
PATTERN_LOOKBACK      = 180   # bars considered for pattern detection (daily)
PATTERN_MIN_PIVOT_PCT = 6.0   # minimum % swing to count as a structural turn — kept fairly
                               # high because a low threshold produces many small pivots,
                               # and more pivots means far more combinatorial opportunities
                               # for a pure random walk to coincidentally satisfy a shape check
PATTERN_TOL_PCT       = 3.0   # tolerance for "roughly equal" peaks/troughs/shoulders —
                               # kept at a realistic level (real double tops/H&S shoulders
                               # commonly differ by 2-3% and are still textbook-valid);
                               # noise is filtered mainly via MIN_SEPARATION and DEPTH_MULT
                               # below rather than by squeezing this so tight it rejects
                               # genuinely valid patterns
PATTERN_MIN_DEPTH_MULT = 1.5  # pattern depth (neckline distance) must be this many times
                               # the min swing threshold — barely-there depth is a much
                               # weaker signal than min_pct alone would allow through
PATTERN_MIN_SEPARATION = 12   # minimum bars between a pattern's key points — real chart
                               # patterns take time to form; without this, a persistently
                               # trending random walk can produce several tightly-clustered
                               # pivots that coincidentally satisfy a similarity tolerance
PATTERN_TRIANGLE_SLOPE_PCT = 2.0  # min rise/fall needed between the first and last
                                   # confirmed pivot to call a triangle boundary "rising"
                                   # or "falling" — deliberately smaller than
                                   # PATTERN_MIN_PIVOT_PCT: each pivot compared here has
                                   # already cleared that bar individually, so requiring
                                   # the same large threshold again between them would be
                                   # redundant and reject genuine, gently-sloped triangles

PATTERN_PRIORITY = ['Head & Shoulders', 'Inverse H&S', 'Double Top', 'Double Bottom',
                    'Cup & Handle', 'Ascending Triangle', 'Descending Triangle',
                    'Symmetrical Triangle']

def _zigzag_pivots(df, min_pct=PATTERN_MIN_PIVOT_PCT):
    """Classic percentage ZigZag: alternating swing highs (from High) and swing
    lows (from Low), where each swing must move at least min_pct% from the
    prior extreme to register — this merges out day-to-day noise and keeps
    only structurally significant turns. Returns a chronological list of
    (bar_position, price, 'H'|'L'); the final entry is the still-forming swing.
    """
    h, l = df['High'].to_numpy(dtype=float), df['Low'].to_numpy(dtype=float)
    n = len(h)
    if n < 10:
        return []

    # Seed: find the first bar where price has moved min_pct% away from bar 0
    # in either direction — this establishes the initial swing direction.
    trend, seed_i = None, None
    for i in range(1, n):
        up   = (h[i] - l[0]) / l[0] * 100 if l[0] else 0
        down = (h[0] - l[i]) / h[0] * 100 if h[0] else 0
        if up >= min_pct:
            trend, seed_i = 1, i
            break
        if down >= min_pct:
            trend, seed_i = -1, i
            break
    if trend is None:
        return []

    pivots = []
    if trend == 1:
        pivots.append((0, l[0], 'L'))
        ext_idx, ext_price = seed_i, h[seed_i]
    else:
        pivots.append((0, h[0], 'H'))
        ext_idx, ext_price = seed_i, l[seed_i]

    for i in range(seed_i + 1, n):
        if trend == 1:
            if h[i] > ext_price:
                ext_price, ext_idx = h[i], i
                continue
            pullback = (ext_price - l[i]) / ext_price * 100
            if pullback >= min_pct:
                pivots.append((ext_idx, ext_price, 'H'))
                trend, ext_idx, ext_price = -1, i, l[i]
        else:
            if l[i] < ext_price:
                ext_price, ext_idx = l[i], i
                continue
            rally = (h[i] - ext_price) / ext_price * 100
            if rally >= min_pct:
                pivots.append((ext_idx, ext_price, 'L'))
                trend, ext_idx, ext_price = 1, i, h[i]

    pivots.append((ext_idx, ext_price, 'H' if trend == 1 else 'L'))
    return pivots


def _pct_similar(a, b, tol=PATTERN_TOL_PCT):
    return abs(a - b) / max(abs(a), abs(b), 1e-9) * 100 <= tol


def _detect_cup_and_handle(window, cur_close, min_depth_pct=12.0, max_handle_retrace=0.5):
    """Cup & Handle: a rounded U-shaped recovery (left rim ~ right rim height,
    with meaningful depth between them) followed by a shallow 'handle' pullback
    near the highs. This looks at the actual price path (not just pivots),
    since the rounded shape is a geometric property the sparse ZigZag pivots
    alone wouldn't reliably capture.
    """
    c = window['Close']
    n = len(c)
    if n < 40:
        return None
    c_vals = c.to_numpy(dtype=float)

    # Reserve the tail of the series for the handle so the cup's "right rim"
    # search can't wander into the handle/breakout and mistake a later breakout
    # high for the actual rim.
    handle_reserve = max(5, min(20, n // 5))
    cup_end = n - handle_reserve
    if cup_end < 20:
        return None

    cup_region = c_vals[:cup_end]
    bottom_pos = int(cup_region.argmin())
    if bottom_pos < 8 or bottom_pos > len(cup_region) - 8:
        return None  # need room on both sides for the rims

    left_rim      = float(cup_region[:bottom_pos + 1].max())
    left_rim_pos  = int(cup_region[:bottom_pos + 1].argmax())
    right_slice   = cup_region[bottom_pos:]
    right_rim     = float(right_slice.max())
    right_rim_pos = bottom_pos + int(right_slice.argmax())
    bottom_price  = float(cup_region[bottom_pos])

    rim_similar = abs(left_rim - right_rim) / max(left_rim, right_rim) * 100 <= 6.0
    depth_pct   = (min(left_rim, right_rim) - bottom_price) / min(left_rim, right_rim) * 100
    duration_ok = (right_rim_pos - left_rim_pos) >= 15

    if not (rim_similar and depth_pct >= min_depth_pct and duration_ok):
        return None

    handle = c_vals[right_rim_pos:]
    if len(handle) < 3:
        return None
    handle_low       = float(handle.min())
    handle_depth_pct = (right_rim - handle_low) / right_rim * 100
    cup_depth_pct    = (right_rim - bottom_price) / right_rim * 100
    if handle_depth_pct > cup_depth_pct * max_handle_retrace:
        return None  # handle pulled back too far to still count as a handle

    status = 'confirmed' if cur_close > right_rim else 'forming'
    return {'name': 'Cup & Handle', 'direction': 'bullish', 'status': status, 'key_level': round(right_rim, 2)}


def detect_chart_patterns(df, lookback=PATTERN_LOOKBACK, min_pct=PATTERN_MIN_PIVOT_PCT, tol_pct=PATTERN_TOL_PCT):
    """Detects classic price-action chart patterns from a ZigZag pivot sequence
    (Double Top/Bottom, Head & Shoulders, Triangles) plus a dedicated Cup &
    Handle shape check. Returns a list of pattern dicts, each:
      {'name', 'direction' ('bullish'/'bearish'/'neutral'),
       'status' ('confirmed' if price already broke the key level, else
                 'forming'), 'key_level'}
    An empty list means no pattern currently qualifies — patterns are
    intentionally strict (tolerance-gated) to avoid false positives.
    """
    n = len(df)
    if n < 30:
        return []

    window = df.tail(min(lookback, n))
    piv = _zigzag_pivots(window, min_pct=min_pct)
    cur_close = float(df['Close'].iloc[-1])
    patterns = []

    def similar(a, b):
        return _pct_similar(a, b, tol_pct)

    # Scan a small recent window of pivots (not just the literal tail) for the
    # MOST RECENT match of a given type-sequence — this matters because once a
    # pattern's breakout keeps moving, price registers a further new swing, and
    # the pattern would otherwise "fall off the end" of a naive tail check even
    # though the breakout clearly already happened.
    def most_recent_match(want_types, checker):
        recent = piv[-9:]
        found = None
        span = len(want_types)
        for i in range(len(recent) - span + 1):
            group = recent[i:i + span]
            if [p[2] for p in group] == want_types:
                res = checker(*group)
                if res:
                    found = res   # keep overwriting -> last (most recent) match wins
        return found

    # -- Double Top / Double Bottom (H,L,H or L,H,L) -------------------------
    def _double_top(p1, p2, p3):
        if p3[0] - p1[0] < PATTERN_MIN_SEPARATION:
            return None   # pattern formed too fast to be a real structural double top
        if similar(p1[1], p3[1]):
            neckline = p2[1]
            if (min(p1[1], p3[1]) - neckline) / neckline * 100 >= min_pct * PATTERN_MIN_DEPTH_MULT:
                status = 'confirmed' if cur_close < neckline else 'forming'
                return {'name': 'Double Top', 'direction': 'bearish',
                        'status': status, 'key_level': round(neckline, 2)}
        return None

    def _double_bottom(p1, p2, p3):
        if p3[0] - p1[0] < PATTERN_MIN_SEPARATION:
            return None
        if similar(p1[1], p3[1]):
            neckline = p2[1]
            if (neckline - max(p1[1], p3[1])) / neckline * 100 >= min_pct * PATTERN_MIN_DEPTH_MULT:
                status = 'confirmed' if cur_close > neckline else 'forming'
                return {'name': 'Double Bottom', 'direction': 'bullish',
                        'status': status, 'key_level': round(neckline, 2)}
        return None

    if len(piv) >= 3:
        m = most_recent_match(['H', 'L', 'H'], _double_top)
        if m: patterns.append(m)
        m = most_recent_match(['L', 'H', 'L'], _double_bottom)
        if m: patterns.append(m)

    # -- Head & Shoulders / Inverse H&S (H,L,H,L,H or L,H,L,H,L) -------------
    def _hns(p1, p2, p3, p4, p5):
        if p5[0] - p1[0] < PATTERN_MIN_SEPARATION * 2:
            return None   # three peaks crammed into too few bars isn't a real H&S
        head_prominence = (p3[1] - max(p1[1], p5[1])) / max(p1[1], p5[1]) * 100
        if head_prominence >= PATTERN_TRIANGLE_SLOPE_PCT and similar(p1[1], p5[1]) and similar(p2[1], p4[1]):
            neckline = (p2[1] + p4[1]) / 2
            status = 'confirmed' if cur_close < neckline else 'forming'
            return {'name': 'Head & Shoulders', 'direction': 'bearish',
                    'status': status, 'key_level': round(neckline, 2)}
        return None

    def _inv_hns(p1, p2, p3, p4, p5):
        if p5[0] - p1[0] < PATTERN_MIN_SEPARATION * 2:
            return None
        head_prominence = (min(p1[1], p5[1]) - p3[1]) / min(p1[1], p5[1]) * 100
        if head_prominence >= PATTERN_TRIANGLE_SLOPE_PCT and similar(p1[1], p5[1]) and similar(p2[1], p4[1]):
            neckline = (p2[1] + p4[1]) / 2
            status = 'confirmed' if cur_close > neckline else 'forming'
            return {'name': 'Inverse H&S', 'direction': 'bullish',
                    'status': status, 'key_level': round(neckline, 2)}
        return None

    if len(piv) >= 5:
        m = most_recent_match(['H', 'L', 'H', 'L', 'H'], _hns)
        if m: patterns.append(m)
        m = most_recent_match(['L', 'H', 'L', 'H', 'L'], _inv_hns)
        if m: patterns.append(m)

    # -- Triangles (flat/rising/falling highs vs lows over confirmed pivots) --
    # Exclude the final pivot: it's the still-forming/live swing (could already
    # be a breakout move), so using it to judge the *shape* would corrupt the
    # flat/rising/falling read. The shape comes from confirmed structure only;
    # cur_close (live) is used separately to judge confirmed-vs-forming status.
    confirmed_piv = piv[:-1] if len(piv) > 1 else piv
    recent = confirmed_piv[-6:]
    highs = [p[1] for p in recent if p[2] == 'H']
    lows  = [p[1] for p in recent if p[2] == 'L']
    if len(highs) >= 2 and len(lows) >= 2:
        h_flat    = similar(highs[0], highs[-1])
        l_flat    = similar(lows[0], lows[-1])
        h_falling = highs[-1] < highs[0] * (1 - PATTERN_TRIANGLE_SLOPE_PCT / 100)
        l_rising  = lows[-1]  > lows[0]  * (1 + PATTERN_TRIANGLE_SLOPE_PCT / 100)
        resistance, support = max(highs), min(lows)
        if h_flat and l_rising:
            status = 'confirmed' if cur_close > resistance else 'forming'
            patterns.append({'name': 'Ascending Triangle', 'direction': 'bullish',
                              'status': status, 'key_level': round(resistance, 2)})
        elif l_flat and h_falling:
            status = 'confirmed' if cur_close < support else 'forming'
            patterns.append({'name': 'Descending Triangle', 'direction': 'bearish',
                              'status': status, 'key_level': round(support, 2)})
        elif h_falling and l_rising:
            patterns.append({'name': 'Symmetrical Triangle', 'direction': 'neutral',
                              'status': 'forming', 'key_level': None})

    # -- Cup & Handle ---------------------------------------------------------
    cup = _detect_cup_and_handle(window, cur_close)
    if cup:
        patterns.append(cup)

    return patterns


def pick_primary_pattern(patterns):
    """Choose the single most noteworthy pattern to headline (for the icon +
    filter): confirmed patterns outrank forming ones, then fall back to a
    fixed priority order (more specific/reliable patterns first)."""
    if not patterns:
        return None
    confirmed = [p for p in patterns if p['status'] == 'confirmed']
    pool = confirmed if confirmed else patterns
    pool = sorted(pool, key=lambda p: PATTERN_PRIORITY.index(p['name'])
                  if p['name'] in PATTERN_PRIORITY else len(PATTERN_PRIORITY))
    return pool[0]

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

        # Guard against duplicate column names — yfinance occasionally returns
        # these for certain tickers. A duplicate name makes df['Close'] return
        # a DataFrame instead of a Series, which then silently corrupts every
        # downstream indicator calculation until a later assignment finally
        # raises "Cannot set a DataFrame with multiple columns to the single
        # column EMA9" (or whichever indicator happens to hit it first) — a
        # confusing error far from the actual cause. Dedupe right here instead.
        if df.columns.duplicated().any():
            df = df.loc[:, ~df.columns.duplicated(keep='first')]

        required = ['Open', 'High', 'Low', 'Close', 'Volume']
        if not all(c in df.columns for c in required):
            return None
        df = df[required].copy()

        # Belt-and-suspenders: confirm every required column really is 1-D
        # (a Series) before handing this off to the indicator pipeline.
        if any(isinstance(df[c], pd.DataFrame) for c in required):
            return None

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
    df['BB_UP_BREAK'] = c > df['BB_hi']

    don_p = min(20, max(2, n // 2))
    df['DON_HI'], df['DON_LO'], df['DON_MID'] = donchian(h, l, don_p)
    df['DON_BREAK'] = c > df['DON_HI']

    # RSI(14) trend filter: SMA(14) of RSI itself, bullish regime when above 50
    df['RSI_SMA14'] = df['RSI'].rolling(min(14, max(2, n // 3))).mean()
    df['RSI_SMA14_BULL'] = df['RSI_SMA14'] > 50

    # RSI(200)/SMA(34) crossover — a long-period RSI is very smooth, so a cross
    # of its own moving average is a slow, high-conviction trend-shift signal.
    if n >= 200:
        df['RSI200'] = rsi(c, 200)
        df['RSI200_SMA34'] = df['RSI200'].rolling(34).mean()
    else:
        df['RSI200'] = np.nan
        df['RSI200_SMA34'] = np.nan

    # CCI(200)/SMA(34) crossover — same idea applied to CCI.
    if n >= 200:
        df['CCI200'] = cci(h, l, c, 200)
        df['CCI200_SMA34'] = df['CCI200'].rolling(34).mean()
    else:
        df['CCI200'] = np.nan
        df['CCI200_SMA34'] = np.nan

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
    df['RSI_SMA14'] = df['RSI'].rolling(min(14, max(2, n // 3)), min_periods=1).mean()

    cci_p = min(20, max(2, n // 2))
    df['CCI20'] = cci(h, l, c, cci_p)
    df['CCI20_SMA20'] = df['CCI20'].rolling(cci_p, min_periods=1).mean()

    bb_p = min(20, max(2, n // 2))
    df['BB_lo'], df['BB_mid'], df['BB_hi'] = bollinger(c, bb_p, 2)
    df['BB_UP_BREAK'] = c > df['BB_hi']

    don_p = min(20, max(2, n // 2))
    df['DON_HI'], df['DON_LO'], df['DON_MID'] = donchian(h, l, don_p)
    df['DON_BREAK'] = c > df['DON_HI']

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
def fibonacci_targets(df_d, lookback=252, base_box=None):
    """Fibonacci extension targets.

    If base_box (the most recent Darvas box) is supplied, the swing low/high is
    anchored to that actual consolidation range — i.e. the base the stock is
    breaking out of — which gives a much more meaningful measured-move target
    than a generic rolling high/low. Falls back to the rolling-window method
    (e.g. for stocks with no detected box yet) if base_box is None.
    """
    cur = float(df_d['Close'].iloc[-1])
    if base_box is not None:
        sl = float(base_box['bottom'])
        sh = float(base_box['top'])
    else:
        recent = df_d.tail(lookback)
        sl = float(recent['Low'].min())
        sh = float(recent['High'].max())
    rng = sh - sl
    return {
        'swing_low':  round(sl, 2),  'swing_high': round(sh, 2),
        'current':    round(cur, 2),
        'fib_0618':   round(cur + 0.618*rng, 2),
        'fib_1618':   round(cur + 1.618*rng, 2),
        'fib_2618':   round(cur + 2.618*rng, 2),
        'fib_4236':   round(cur + 4.236*rng, 2),
        'base_anchored': base_box is not None,
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


# ── Early Trend Score ─────────────────────────────────────────────────────────
def compute_early_trend_score(stage_num, squeeze_recent, vol_ratio, d_rsi_val, adx_val,
                               rs_rating=None):
    """Combines everything that distinguishes a genuine trend-beginning setup from
    a stock that's simply moving (which BLAST alone can't tell apart):
      - Stage 2 (Advancing) is the target zone; Stage 1 (Basing) gets partial
        credit since it's the setup that precedes Stage 2.
      - A recent volatility squeeze = the coiling/base-tightening pattern that
        typically precedes real breakouts (as opposed to a random volume spike).
      - Volume expansion confirms the breakout is being bought, not just noise.
      - RSI in a healthy momentum zone (55-75) beats RSI already at 85+, which
        usually means the move is late, not early.
      - ADX 18-40 = a trend that's just getting going; ADX 40+ is often already
        mature/extended.
      - RS Rating (percentile vs the rest of today's scanned universe, IBD-style)
        rewards stocks already outperforming — a weak stock breaking out on its
        own base is a much lower-quality signal than a market leader doing it.
    Returns an int score 0-100.
    """
    score = 0
    if stage_num == 2:   score += 30
    elif stage_num == 1: score += 10

    if squeeze_recent: score += 20

    if vol_ratio >= BLAST_VOL_RATIO: score += 15
    elif vol_ratio >= VOL_SURGE:     score += 8

    if 55 <= d_rsi_val <= 75: score += 15
    elif d_rsi_val > 75:      score += 5

    if 18 <= adx_val <= 40: score += 10
    elif adx_val > 40:      score += 3

    if rs_rating is not None:
        if rs_rating >= 80:   score += 25
        elif rs_rating >= 70: score += 15
        elif rs_rating >= 50: score += 5

    return min(100, score)


# ── Trend Exit Score ──────────────────────────────────────────────────────────
def compute_trend_exit_score(stage_num, mtf_signal, dv_w_status, dv_m_status,
                              rsi200_state, rsi200_cross, d_rsi_val,
                              macd_us_state=None, macd_us_cross=None,
                              w_macd_state=None, m_macd_state=None,
                              m_rsi_sma_state=None, m_rsi_sma_cross=None,
                              m_cci_state=None, m_cci_cross=None):
    """The exit-side counterpart to the Early Trend Score, for POSITION trades
    (weeks-to-months holds riding a Stage 2 trend) rather than short swing
    trades. generate_signals()'s daily 'SELL' (RSI<50 crossing under SMA20, or
    RSI<45) is a *tactical* trigger — it fires on ordinary pullbacks that
    happen routinely inside a real uptrend, and would repeatedly whipsaw a
    position trader out of a multi-month/multi-year trend. This score instead
    only weighs bigger-picture, structural deterioration:
      - Stage flipping from 2 (Advancing) to 3 (Topping) or 4 (Declining) —
        the single biggest signal that the trend itself, not just the daily
        candle, has turned.
      - MTF RSI signal at 'SELL' (both weekly AND monthly RSI falling) — the
        same multi-timeframe logic used to confirm entries, applied in reverse.
      - Weekly or Monthly Darvas box status at 'BREAKDOWN' — a structural
        break on the timeframes you're supposed to be riding the trend on.
      - RSI(200)/SMA(34) turning Bearish (or a fresh Bear Cross) — a slow,
        high-conviction trend-shift confirmation.
      - MACD(34,1000,20) on the daily chart turning bearish — this ultra-slow
        MACD is a well-known discretionary sell signal; a fresh bear cross
        here carries real weight.
      - Weekly or Monthly MACD(12,26,9) in a 'Downtrend' state — mirrors the
        manual check of "did I confirm weekly/monthly MACD before trusting
        this buy" applied on the way out too.
      - Monthly RSI(14)/SMA(14) or CCI(20)/SMA(20) turning bearish — slow,
        monthly-timeframe confirmation that momentum has broken down.
      - Daily RSI < 45 adds a small amount of weight only as a minor, current
        confirming factor — not as the primary trigger.
    Returns an int score 0-100; treat >=40 as "worth reviewing the position,"
    not as an automatic sell instruction.
    """
    score = 0
    if stage_num == 4:   score += 45
    elif stage_num == 3: score += 30

    if mtf_signal == 'SELL': score += 25

    if dv_w_status == 'BREAKDOWN': score += 20
    if dv_m_status == 'BREAKDOWN': score += 15

    if rsi200_cross == 'Bear Cross': score += 20
    elif rsi200_state == 'Bearish':  score += 10

    if macd_us_cross == 'Bear Cross': score += 20
    elif macd_us_state == 'Bearish':  score += 10

    if w_macd_state == 'Downtrend': score += 12
    if m_macd_state == 'Downtrend': score += 15

    if m_rsi_sma_cross == 'Bear Cross': score += 12
    elif m_rsi_sma_state == 'Bearish':  score += 6

    if m_cci_cross == 'Bear Cross': score += 10
    elif m_cci_state == 'Bearish':  score += 5

    if d_rsi_val < 45: score += 10

    return min(100, score)

# ── Chart: Daily (5-panel) with Darvas + Blast + S/R ─────────────────────────
def chart_daily(ticker, name, df_d, signals, fibs, boxes_d, is_blast, blast_score,
                resistance, support, out_path, stage_label=None,
                mtf_signal=None, w_rsi_rising=None, m_rsi_rising=None,
                primary_pattern=None,
                w_macd_state=None, m_macd_state=None, mtf_macd_confirmed=None,
                trend_exit=None, trend_exit_score=None):
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
        stage_txt = f'  |  Stage: {stage_label}' if stage_label else ''
        pattern_txt = ''
        if primary_pattern:
            tag = 'CONFIRMED' if primary_pattern['status'] == 'confirmed' else 'forming'
            pattern_txt = f"  |  Pattern: {primary_pattern['name']} ({tag})"
        exit_title_txt = f'  |  🔔 TREND EXIT ({trend_exit_score})' if trend_exit else ''
        title_color = '#ff6b6b' if trend_exit else ('#00ff88' if is_blast else '#e6edf3')
        ax_p.set_title(f'{name} [{ticker}] — Daily Chart{blast_txt}{stage_txt}{pattern_txt}{exit_title_txt}',
                       fontsize=12, color=title_color,
                       fontweight='bold', pad=6)
        ax_p.legend(fontsize=6.5, loc='upper left', ncol=5,
                    facecolor='#161b22', edgecolor='#30363d', labelcolor='#e6edf3')
        ax_p.grid(True, alpha=0.25)
        ax_p.yaxis.tick_right()

        # Draw the pattern's key level (neckline / triangle boundary / cup rim)
        # so the trigger price is visible directly on the chart.
        if primary_pattern and primary_pattern.get('key_level') is not None:
            lvl = primary_pattern['key_level']
            pcolor = '#00ff88' if primary_pattern['direction'] == 'bullish' else \
                     ('#ff6b6b' if primary_pattern['direction'] == 'bearish' else '#f0b429')
            ax_p.axhline(lvl, color=pcolor, lw=0.8, ls=':', alpha=0.8)
            ax_p.annotate(f"{primary_pattern['name']} {lvl:,.1f}",
                          xy=(x[-1], lvl), xytext=(-100, -10 if primary_pattern['direction']=='bearish' else 6),
                          textcoords='offset points', fontsize=6, color=pcolor)

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
        if 'RSI_SMA14' in df.columns:
            ax_r.plot(x, df['RSI_SMA14'], color='#f0b429', lw=0.8, ls='--', label='SMA(14) of RSI')
        ax_r.axhline(70, color='#ff6b6b', lw=0.6, ls='--', alpha=0.6)
        ax_r.axhline(50, color='#8b949e', lw=0.4, ls='-',  alpha=0.4)
        ax_r.axhline(30, color='#26d07c', lw=0.6, ls='--', alpha=0.6)
        ax_r.fill_between(x, df['RSI'], 70, where=df['RSI']>=70, color='#ff6b6b', alpha=0.12)
        ax_r.fill_between(x, df['RSI'], 30, where=df['RSI']<=30, color='#26d07c', alpha=0.12)
        ax_r.set_ylim(0, 100); ax_r.set_ylabel('RSI', color='#8b949e', fontsize=6.5)
        ax_r.yaxis.tick_right()

        # Weekly/Monthly RSI direction -> combined MTF signal, shown right on
        # the daily RSI panel so it's visible alongside the daily RSI reading.
        if mtf_signal is not None:
            mtf_colors = {'BUY': '#00ff88', 'SELL': '#ff4444', 'NEUTRAL': '#8b949e'}
            w_arrow = '↑' if w_rsi_rising else ('↓' if w_rsi_rising is False else '·')
            m_arrow = '↑' if m_rsi_rising else ('↓' if m_rsi_rising is False else '·')
            mtf_txt = f'W-RSI{w_arrow} M-RSI{m_arrow}  MTF: {mtf_signal}'
            ax_r.annotate(mtf_txt, xy=(0.99, 0.90), xycoords='axes fraction',
                          ha='right', va='top', fontsize=6.5, fontweight='bold',
                          color=mtf_colors.get(mtf_signal, '#8b949e'))

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

        # Weekly/Monthly MACD(12,26,9) trend state — the automated version of
        # manually checking "is either higher timeframe in a downtrend" before
        # trusting a daily buy signal.
        if w_macd_state is not None:
            macd_state_colors = {'Uptrend': '#00ff88', 'Weakening': '#fbbf24',
                                  'Reversing Up': '#58a6ff', 'Downtrend': '#ff6b6b', 'Unknown': '#8b949e'}
            wc = macd_state_colors.get(w_macd_state, '#8b949e')
            mc = macd_state_colors.get(m_macd_state, '#8b949e')
            check = '✓ Confirmed' if mtf_macd_confirmed else '✗ Not Confirmed'
            check_color = '#00ff88' if mtf_macd_confirmed else '#ff6b6b'
            ax_m.annotate(f'W-MACD: {w_macd_state}', xy=(0.99, 0.92), xycoords='axes fraction',
                          ha='right', va='top', fontsize=6.5, fontweight='bold', color=wc)
            ax_m.annotate(f'M-MACD: {m_macd_state}', xy=(0.99, 0.78), xycoords='axes fraction',
                          ha='right', va='top', fontsize=6.5, fontweight='bold', color=mc)
            ax_m.annotate(check, xy=(0.99, 0.64), xycoords='axes fraction',
                          ha='right', va='top', fontsize=6.5, fontweight='bold', color=check_color)

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

        # Trend Exit Score — shown right on the MACD(34,1000,20) panel since
        # that's the chart you personally use for the sell decision.
        if trend_exit_score is not None:
            if trend_exit:
                ax_u.axhspan(ax_u.get_ylim()[0], ax_u.get_ylim()[1], color='#ff4444', alpha=0.08)
                exit_txt = f'🔔 TREND EXIT ({trend_exit_score})'
                exit_color = '#ff6b6b'
            else:
                exit_txt = f'Trend Exit Score: {trend_exit_score}'
                exit_color = '#8b949e'
            ax_u.annotate(exit_txt, xy=(0.99, 0.90), xycoords='axes fraction',
                          ha='right', va='top', fontsize=7, fontweight='bold', color=exit_color)

        plt.setp(ax_p.get_xticklabels(), visible=False)
        plt.setp(ax_v.get_xticklabels(), visible=False)
        plt.setp(ax_r.get_xticklabels(), visible=False)
        plt.setp(ax_m.get_xticklabels(), visible=False)
        ax_u.tick_params(axis='x', labelsize=7, rotation=30)

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

        # ── Trend-beginning-stage signals ──────────────────────────────────
        # Fibonacci targets anchored to the actual base (most recent Darvas box)
        # being broken out of, when one exists — a real measured-move target
        # rather than a generic rolling-window guess.
        fib_base = boxes_d[-1] if boxes_d else None
        fibs = fibonacci_targets(df_d, base_box=fib_base)

        stage_num, stage_label = detect_stage(df_d)
        squeeze_info = detect_squeeze(df_d)
        rsi_tgt      = rsi_momentum_target(df_d)
        # Cross-sectional relative-strength raw score. Turned into an IBD-style
        # 1-99 percentile "RS Rating" in main() once every stock's score is known
        # (RS is inherently relative to the whole universe, not computable alone).
        rs_raw = relative_strength_raw(df_d)

        # ── New requested signals: BB/Donchian breakout (D/W/M), RSI-SMA
        # trend filter, MTF RSI direction, RSI200/CCI200 crossovers ─────────
        bb_break_d = bool(df_d['BB_UP_BREAK'].iloc[-1]) if not pd.isna(df_d['BB_UP_BREAK'].iloc[-1]) else False
        bb_break_w = bool(df_w['BB_UP_BREAK'].iloc[-1]) if not pd.isna(df_w['BB_UP_BREAK'].iloc[-1]) else False
        bb_break_m = bool(df_m['BB_UP_BREAK'].iloc[-1]) if not pd.isna(df_m['BB_UP_BREAK'].iloc[-1]) else False

        don_break_d = bool(df_d['DON_BREAK'].iloc[-1]) if not pd.isna(df_d['DON_BREAK'].iloc[-1]) else False
        don_break_w = bool(df_w['DON_BREAK'].iloc[-1]) if not pd.isna(df_w['DON_BREAK'].iloc[-1]) else False
        don_break_m = bool(df_m['DON_BREAK'].iloc[-1]) if not pd.isna(df_m['DON_BREAK'].iloc[-1]) else False

        rsi_sma14_bull = bool(df_d['RSI_SMA14_BULL'].iloc[-1]) if not pd.isna(df_d['RSI_SMA14'].iloc[-1]) else False

        mtf_signal, w_rsi_rising, m_rsi_rising = mtf_rsi_signal(df_w, df_m)

        rsi200_state, rsi200_cross = crossover_state(df_d['RSI200'], df_d['RSI200_SMA34'])
        cci200_state, cci200_cross = crossover_state(df_d['CCI200'], df_d['CCI200_SMA34'])

        # MACD(34,1000,20) on daily — a well-known discretionary sell signal.
        macd_us_state, macd_us_cross = crossover_state(df_d['MACD_US'], df_d['MACD_US_sig'])

        # Weekly/Monthly MACD(12,26,9) trend state — mirrors manually checking
        # "is either higher timeframe in a downtrend" before trusting a daily buy.
        w_macd_state = macd_trend_state(df_w)
        m_macd_state = macd_trend_state(df_m)
        mtf_macd_confirmed = (w_macd_state not in ('Downtrend',)) and (m_macd_state not in ('Downtrend',))

        # Monthly RSI(14)/SMA(14) and CCI(20)/SMA(20) bearish crossovers.
        m_rsi_sma_state, m_rsi_sma_cross = crossover_state(df_m['RSI'], df_m['RSI_SMA14'])
        m_cci_state, m_cci_cross = crossover_state(df_m['CCI20'], df_m['CCI20_SMA20'])

        # Chart pattern recognition (daily) — Double Top/Bottom, Head & Shoulders,
        # Triangles, Cup & Handle, via ZigZag swing-pivot geometry.
        chart_patterns   = detect_chart_patterns(df_d)
        primary_pattern  = pick_primary_pattern(chart_patterns)

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

        # Trend Exit Score — position-trading exit signal (see function docstring
        # for why this differs from generate_signals()'s tactical daily SELL).
        trend_exit_score = compute_trend_exit_score(
            stage_num=stage_num, mtf_signal=mtf_signal,
            dv_w_status=dv_w_status, dv_m_status=dv_m_status,
            rsi200_state=rsi200_state, rsi200_cross=rsi200_cross,
            d_rsi_val=d_rsi_val,
            macd_us_state=macd_us_state, macd_us_cross=macd_us_cross,
            w_macd_state=w_macd_state, m_macd_state=m_macd_state,
            m_rsi_sma_state=m_rsi_sma_state, m_rsi_sma_cross=m_rsi_sma_cross,
            m_cci_state=m_cci_state, m_cci_cross=m_cci_cross,
        )
        trend_exit = trend_exit_score >= 40

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
                        is_blast, blast_score, res_d, sup_d, path_d,
                        stage_label=stage_label,
                        mtf_signal=mtf_signal, w_rsi_rising=w_rsi_rising, m_rsi_rising=m_rsi_rising,
                        primary_pattern=primary_pattern,
                        w_macd_state=w_macd_state, m_macd_state=m_macd_state,
                        mtf_macd_confirmed=mtf_macd_confirmed,
                        trend_exit=trend_exit, trend_exit_score=trend_exit_score)
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
            'trade_list': trades,   # full per-trade history, used by the separate trades report
            'fibs': fibs,
            # IPO / listing metadata
            'data_days': data_days,
            'listing_date': listing_date,
            'is_recent_ipo': is_recent_ipo,
            'is_new_ipo': is_new_ipo,
            # Blast
            'blast': is_blast, 'blast_score': blast_score, 'blast_reason': blast_reason,
            # Trend-beginning-stage signals
            'stage_num': stage_num, 'stage_label': stage_label,
            'squeeze_now': squeeze_info['in_squeeze_now'],
            'squeeze_recent': squeeze_info['squeeze_recent'],
            'bb_width_pctile': (round(squeeze_info['bb_width_pctile'], 1)
                                 if not pd.isna(squeeze_info['bb_width_pctile']) else None),
            'rsi_target': rsi_tgt['target'],
            'rsi_target_gain_pct': rsi_tgt['median_gain_pct'],
            'rsi_target_samples': rsi_tgt['sample_count'],
            'rs_raw': rs_raw,   # converted to rs_rating (1-99 percentile) in main()
            # Bollinger / Donchian breakouts across all 3 timeframes
            'bb_break_d': bb_break_d, 'bb_break_w': bb_break_w, 'bb_break_m': bb_break_m,
            'don_break_d': don_break_d, 'don_break_w': don_break_w, 'don_break_m': don_break_m,
            # RSI trend filters
            'rsi_sma14_bull': rsi_sma14_bull,
            'mtf_rsi_signal': mtf_signal, 'w_rsi_rising': w_rsi_rising, 'm_rsi_rising': m_rsi_rising,
            'rsi200_state': rsi200_state, 'rsi200_cross': rsi200_cross,
            'cci200_state': cci200_state, 'cci200_cross': cci200_cross,
            # MACD(34,1000,20) daily sell signal + weekly/monthly MACD(12,26,9) trend state
            'macd_us_state': macd_us_state, 'macd_us_cross': macd_us_cross,
            'w_macd_state': w_macd_state, 'm_macd_state': m_macd_state,
            'mtf_macd_confirmed': mtf_macd_confirmed,
            # Monthly RSI(14)/SMA(14) and CCI(20)/SMA(20) crossovers
            'm_rsi_sma_state': m_rsi_sma_state, 'm_rsi_sma_cross': m_rsi_sma_cross,
            'm_cci_state': m_cci_state, 'm_cci_cross': m_cci_cross,
            # Chart patterns
            'chart_patterns': chart_patterns,
            'primary_pattern': primary_pattern,
            # Trend Exit Score (position-trading exit signal)
            'trend_exit_score': trend_exit_score,
            'trend_exit': trend_exit,
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
            'chart_d': f"charts/{safe_sym}_daily.png",
            'chart_w': f"charts/{safe_sym}_weekly.png",
            'chart_m': f"charts/{safe_sym}_monthly.png",
        }
    except Exception as e:
        tprint(f"  ⚠️  {ticker}: {e}")
        return None


def _process_stock_task(args):
    """Top-level picklable wrapper around process_stock() for ProcessPoolExecutor.

    Must live at module scope (not as a nested/local function) — ProcessPoolExecutor
    pickles the callable to send it to worker processes, and on Windows (spawn start
    method in particular) closures/local functions cannot be pickled.
    """
    name, ticker, df_raw = args
    try:
        return name, ticker, process_stock(name, ticker, df_raw)
    except Exception as e:
        tprint(f"  ⚠️  {ticker}: {e}")
        return name, ticker, None

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

def stage_badge(stage_num, stage_label):
    colors = {2: ('#00ff88', '#003319'), 1: ('#fbbf24', '#1a1200'),
              3: ('#ff9800', '#1a0a00'), 4: ('#ff6b6b', '#2c0000'), 0: ('#555', '#1a1f2c')}
    c, bg = colors.get(stage_num, ('#555', '#1a1f2c'))
    tip = {2: 'Above a rising SMA150 — classic trend-beginning zone',
           1: 'Basing near a flat SMA150 — pre-breakout consolidation',
           3: 'Above SMA150 but the MA has stopped rising — trend may be maturing',
           4: 'Below a falling SMA150 — downtrend',
           0: 'Not enough history to classify'}.get(stage_num, '')
    return (f'<span style="background:{bg};color:{c};border:1px solid {c};border-radius:4px;'
            f'padding:2px 7px;font-size:10px;font-weight:700;" title="{tip}">{stage_label}</span>')

def squeeze_badge(squeeze_now, squeeze_recent):
    if squeeze_now:
        return ('<span style="color:#c084fc;font-size:11px;font-weight:700;" '
                'title="Bollinger Band width is in the tightest 20% of the last 120 days right now">'
                '🌀 Tight</span>')
    if squeeze_recent:
        return ('<span style="color:#58a6ff;font-size:11px;font-weight:700;" '
                'title="Was in a volatility squeeze within the last 10 days — may be expanding out of a base">'
                '↗ Firing</span>')
    return '<span style="color:#444;font-size:11px;">—</span>'

def rs_rating_display(rs_rating):
    if rs_rating is None:
        return '<span style="color:#444;font-size:11px;">—</span>'
    if rs_rating >= 80: color = '#00ff88'
    elif rs_rating >= 60: color = '#26d07c'
    elif rs_rating >= 40: color = '#8b949e'
    else: color = '#ff6b6b'
    return (f'<span style="color:{color};font-weight:700;font-size:12px;" '
            f'title="Percentile rank vs all stocks scanned today (IBD-style RS Rating)">'
            f'{rs_rating:.0f}</span>')

def early_trend_badge(is_early, score):
    if not is_early:
        return f'<span style="color:#444;font-size:11px;">{score}</span>'
    return (f'<span style="background:#04140a;color:#00ff88;border:1px solid #00ff88;'
            f'border-radius:4px;padding:2px 7px;font-size:11px;font-weight:700;" '
            f'title="Stage + squeeze + volume + RSI + ADX + RS Rating combined">'
            f'🌱 {score}</span>')

def timeframe_break_badge(brk_d, brk_w, brk_m, label):
    """Compact D/W/M pill set for a breakout flag (BB upper band, Donchian, etc.)."""
    def pill(active, tf):
        if active:
            return (f'<span style="background:#04140a;color:#00ff88;border:1px solid #00ff88;'
                     f'border-radius:3px;padding:1px 4px;font-size:9px;font-weight:700;margin-right:2px;">{tf}</span>')
        return f'<span style="color:#333;font-size:9px;margin-right:2px;">{tf}</span>'
    return (f'<span title="{label} breakout — Daily / Weekly / Monthly">'
            f'{pill(brk_d,"D")}{pill(brk_w,"W")}{pill(brk_m,"M")}</span>')

def rsi_sma_badge(bull):
    if bull:
        return ('<span style="color:#00ff88;font-size:11px;font-weight:700;" '
                'title="SMA(14) of RSI(14) is above 50 — bullish momentum regime">▲ Bull</span>')
    return '<span style="color:#8b949e;font-size:11px;" title="SMA(14) of RSI(14) is below 50">▽ Neutral</span>'

def mtf_signal_badge(signal):
    colors = {'BUY': ('#00ff88', '#04140a'), 'SELL': ('#ff4444', '#1a0000'),
              'NEUTRAL': ('#8b949e', '#161b22')}
    c, bg = colors.get(signal, ('#8b949e', '#161b22'))
    return (f'<span style="background:{bg};color:{c};border:1px solid {c};border-radius:4px;'
            f'padding:2px 7px;font-size:10px;font-weight:700;" '
            f'title="Weekly RSI(14) and Monthly RSI(14) both rising = BUY, both falling = SELL">'
            f'{signal}</span>')

def crossover_badge(state, cross, label):
    colors = {'Bullish': '#00ff88', 'Bearish': '#ff6b6b', 'Unknown': '#444'}
    c = colors.get(state, '#444')
    fresh = ''
    if cross == 'Bull Cross':
        fresh = ' 🔥'
    elif cross == 'Bear Cross':
        fresh = ' ⚠️'
    tip = f'{label}: {state}' + (f' — fresh {cross} within last 5 bars' if cross else '')
    return f'<span style="color:{c};font-size:10px;font-weight:700;" title="{tip}">{state}{fresh}</span>'

PATTERN_ICONS = {
    'Double Top':          '📉 Ⓜ',
    'Double Bottom':       '📈 Ⓦ',
    'Head & Shoulders':    '📉 H&S',
    'Inverse H&S':         '📈 Inv H&S',
    'Ascending Triangle':  '📈 ◺',
    'Descending Triangle': '📉 ◹',
    'Symmetrical Triangle':'🔺 Sym',
    'Cup & Handle':        '☕',
}

def pattern_badge(primary_pattern, all_patterns=None):
    if not primary_pattern:
        return '<span style="color:#333;font-size:11px;">—</span>'
    name   = primary_pattern['name']
    status = primary_pattern['status']
    direction = primary_pattern['direction']
    icon = PATTERN_ICONS.get(name, '◆')
    dir_color = {'bullish': '#00ff88', 'bearish': '#ff6b6b', 'neutral': '#f0b429'}.get(direction, '#8b949e')

    extra_n = len(all_patterns) - 1 if all_patterns and len(all_patterns) > 1 else 0
    extra_txt = f' +{extra_n}' if extra_n else ''
    lvl = primary_pattern.get('key_level')
    lvl_txt = f" | Level: {fmt_price(lvl)}" if lvl else ''
    tip = f"{name} ({status}){lvl_txt}"
    if extra_n:
        others = ', '.join(f"{p['name']} ({p['status']})" for p in all_patterns if p is not primary_pattern)
        tip += f" | Also detected: {others}"

    if status == 'confirmed':
        return (f'<span style="background:#04140a;color:{dir_color};border:1px solid {dir_color};'
                f'border-radius:4px;padding:2px 7px;font-size:10px;font-weight:700;" title="{tip}">'
                f'{icon} {name}{extra_txt}</span>')
    return (f'<span style="color:{dir_color};font-size:10px;font-weight:600;opacity:0.75;" title="{tip}">'
            f'{icon} {name} (forming){extra_txt}</span>')

def trend_exit_badge(trend_exit, score):
    tip = ("Structural exit signal for position trades: Stage flipped to Topping/Declining, "
           "and/or MTF RSI at SELL, and/or Weekly/Monthly Darvas breakdown, and/or RSI(200)/SMA(34) "
           "turned Bearish, and/or MACD(34,1000,20) bearish, and/or Weekly/Monthly MACD(12,26,9) "
           "in a Downtrend, and/or Monthly RSI-SMA(14)/CCI-SMA(20) bearish. "
           "Review the position — not an automatic sell instruction.")
    if trend_exit:
        return (f'<span style="background:#1a0000;color:#ff6b6b;border:1px solid #ff6b6b;'
                f'border-radius:4px;padding:2px 7px;font-size:11px;font-weight:700;" title="{tip}">'
                f'🔔 {score}</span>')
    return f'<span style="color:#444;font-size:11px;" title="{tip}">{score}</span>'

MACD_STATE_COLORS = {'Uptrend': '#00ff88', 'Weakening': '#fbbf24',
                      'Reversing Up': '#58a6ff', 'Downtrend': '#ff6b6b', 'Unknown': '#444'}

def mtf_macd_badge(w_state, m_state, confirmed):
    wc = MACD_STATE_COLORS.get(w_state, '#444')
    mc = MACD_STATE_COLORS.get(m_state, '#444')
    tip = (f"Weekly MACD(12,26,9): {w_state}  |  Monthly MACD(12,26,9): {m_state}  |  "
           f"{'Confirms the daily buy — neither timeframe is in a Downtrend' if confirmed else 'Does NOT confirm — check before trusting this buy'}")
    check = '✓' if confirmed else '✗'
    check_color = '#00ff88' if confirmed else '#ff6b6b'
    return (f'<span style="font-size:10px;" title="{tip}">'
            f'<span style="color:{wc};font-weight:700;">W:{w_state}</span> '
            f'<span style="color:{mc};font-weight:700;">M:{m_state}</span> '
            f'<span style="color:{check_color};font-weight:900;">{check}</span></span>')

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

        # New trend-beginning-stage fields — .get() with fallbacks so same-day
        # cache entries written before this feature existed don't crash the report.
        stage_num   = r.get('stage_num', 0)
        stage_label = r.get('stage_label', 'Unknown')
        early_score = r.get('early_trend_score', 0)
        is_early    = r.get('early_trend', False)
        rs_rating   = r.get('rs_rating')
        sq_now      = r.get('squeeze_now', False)
        sq_recent   = r.get('squeeze_recent', False)
        fibs        = r.get('fibs') or {}
        fib_target  = fibs.get('fib_1618')
        fib_anchor  = 'base' if fibs.get('base_anchored') else '252d range'
        fib_tip     = (f"0.618: {fmt_price(fibs.get('fib_0618'))}  |  2.618: {fmt_price(fibs.get('fib_2618'))}  "
                       f"|  anchor: {fib_anchor}") if fibs else ''
        rsi_target      = r.get('rsi_target')
        rsi_target_gain = r.get('rsi_target_gain_pct')
        rsi_target_n    = r.get('rsi_target_samples', 0)
        rsi_tip = (f"Median of {rsi_target_n} historical analogs: {rsi_target_gain:+.1f}%"
                   if rsi_target is not None else f"Only {rsi_target_n} historical analogs — not enough to trust")

        bb_d, bb_w, bb_m   = r.get('bb_break_d', False), r.get('bb_break_w', False), r.get('bb_break_m', False)
        don_d, don_w, don_m = r.get('don_break_d', False), r.get('don_break_w', False), r.get('don_break_m', False)
        rsi_sma_bull    = r.get('rsi_sma14_bull', False)
        mtf_sig         = r.get('mtf_rsi_signal', 'NEUTRAL')
        rsi200_state    = r.get('rsi200_state', 'Unknown')
        rsi200_cross    = r.get('rsi200_cross')
        cci200_state    = r.get('cci200_state', 'Unknown')
        cci200_cross    = r.get('cci200_cross')
        w_macd_state    = r.get('w_macd_state', 'Unknown')
        m_macd_state    = r.get('m_macd_state', 'Unknown')
        mtf_macd_ok     = r.get('mtf_macd_confirmed', False)
        any_bb_break    = bb_d or bb_w or bb_m
        any_don_break   = don_d or don_w or don_m
        chart_patterns  = r.get('chart_patterns') or []
        primary_pattern = r.get('primary_pattern')
        pat_name = primary_pattern['name'] if primary_pattern else 'None'
        pat_dir  = primary_pattern['direction'] if primary_pattern else 'none'
        pat_status = primary_pattern['status'] if primary_pattern else 'none'
        trend_exit_score = r.get('trend_exit_score', 0)
        trend_exit_flag  = r.get('trend_exit', False)

        rows_html.append(f"""
<tr class="stock-row {'blast-row' if r['blast'] else ''}"
    data-signal="{r['signal']}"
    data-blast="{1 if r['blast'] else 0}"
    data-score="{r['score']}"
    data-mrsi="{r['m_rsi']:.1f}"
    data-wrsi="{r['w_rsi']:.1f}"
    data-drsi="{r['d_rsi']:.1f}"
    data-darvas="{r['darvas_d']}"
    data-early="{1 if is_early else 0}"
    data-stage="{stage_num}"
    data-bbbreak="{1 if any_bb_break else 0}"
    data-donbreak="{1 if any_don_break else 0}"
    data-rsisma="{1 if rsi_sma_bull else 0}"
    data-mtfsig="{mtf_sig}"
    data-rsi200="{rsi200_state}"
    data-cci200="{cci200_state}"
    data-mtfmacd="{1 if mtf_macd_ok else 0}"
    data-pattern="{pat_name}"
    data-patterndir="{pat_dir}"
    data-patternstatus="{pat_status}"
    data-exit="{1 if trend_exit_flag else 0}">
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
  <td style="padding:6px 6px;">{stage_badge(stage_num, stage_label)}</td>
  <td style="padding:6px 6px;">{early_trend_badge(is_early, early_score)}</td>
  <td style="padding:6px 4px;">{rs_rating_display(rs_rating)}</td>
  <td style="padding:6px 6px;">{squeeze_badge(sq_now, sq_recent)}</td>
  <td style="padding:6px 4px;color:#e6edf3;font-size:11px;" title="{fib_tip}">{fmt_price(fib_target) if fib_target else '—'}</td>
  <td style="padding:6px 4px;color:#e6edf3;font-size:11px;" title="{rsi_tip}">{fmt_price(rsi_target) if rsi_target else '—'}</td>
  <td style="padding:6px 6px;">{timeframe_break_badge(bb_d, bb_w, bb_m, 'Bollinger Band upper')}</td>
  <td style="padding:6px 6px;">{timeframe_break_badge(don_d, don_w, don_m, 'Donchian Channel')}</td>
  <td style="padding:6px 6px;">{rsi_sma_badge(rsi_sma_bull)}</td>
  <td style="padding:6px 6px;">{mtf_signal_badge(mtf_sig)}</td>
  <td style="padding:6px 6px;">{mtf_macd_badge(w_macd_state, m_macd_state, mtf_macd_ok)}</td>
  <td style="padding:6px 6px;">{crossover_badge(rsi200_state, rsi200_cross, 'RSI(200)/SMA(34)')}</td>
  <td style="padding:6px 6px;">{crossover_badge(cci200_state, cci200_cross, 'CCI(200)/SMA(34)')}</td>
  <td style="padding:6px 6px;">{pattern_badge(primary_pattern, chart_patterns)}</td>
  <td style="padding:6px 6px;">{trend_exit_badge(trend_exit_flag, trend_exit_score)}</td>
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
  <button class="filter-btn" id="earlyBtn" onclick="toggleFilter('early')" title="Stage 1/2 + squeeze + volume + RSI + ADX + RS Rating combined">🌱 Early Trend</button>
  <button class="filter-btn" id="buyBtn"   onclick="toggleFilter('buy')">📈 BUY Signals</button>
  <button class="filter-btn" id="mrsi70Btn" onclick="toggleFilter('mrsi70')">💜 M-RSI &gt; 70</button>
  <button class="filter-btn" id="dboxBtn"  onclick="toggleFilter('darvas')">📦 Darvas Break</button>
  <button class="filter-btn" id="bbBtn"    onclick="toggleFilter('bbbreak')" title="Close above the upper Bollinger Band on any timeframe">📊 BB Breakout</button>
  <button class="filter-btn" id="donBtn"   onclick="toggleFilter('donbreak')" title="Close above the Donchian Channel high on any timeframe">🌊 Donchian Break</button>
  <button class="filter-btn" id="rsismaBtn" onclick="toggleFilter('rsisma')" title="SMA(14) of RSI(14) above 50">💪 RSI-SMA Bull</button>
  <button class="filter-btn" id="mtfBuyBtn" onclick="toggleFilter('mtfbuy')" title="Weekly + Monthly RSI both rising">🔀 MTF RSI Buy</button>
  <button class="filter-btn" id="rsi200Btn" onclick="toggleFilter('rsi200')" title="RSI(200) above its SMA(34)">📐 RSI200 Bull</button>
  <button class="filter-btn" id="cci200Btn" onclick="toggleFilter('cci200')" title="CCI(200) above its SMA(34)">📐 CCI200 Bull</button>
  <select id="patternFilter" onchange="setPatternFilter(this.value)"
          style="background:#161b22;color:#e6edf3;border:1px solid #30363d;border-radius:6px;
                 padding:5px 10px;font-size:12px;font-family:inherit;cursor:pointer;">
    <option value="all">📐 Chart Pattern: All</option>
    <option value="any">Any Pattern Detected</option>
    <option value="bullish">Any Bullish Pattern</option>
    <option value="bearish">Any Bearish Pattern</option>
    <option value="confirmed">Confirmed Only</option>
    <option value="Double Top">Double Top</option>
    <option value="Double Bottom">Double Bottom</option>
    <option value="Head & Shoulders">Head &amp; Shoulders</option>
    <option value="Inverse H&S">Inverse H&amp;S</option>
    <option value="Ascending Triangle">Ascending Triangle</option>
    <option value="Descending Triangle">Descending Triangle</option>
    <option value="Symmetrical Triangle">Symmetrical Triangle</option>
    <option value="Cup & Handle">Cup &amp; Handle</option>
  </select>
  <button class="filter-btn" id="exitBtn" onclick="toggleFilter('exit')" title="Position-trade exit signal: Stage flip to Topping/Declining, MTF RSI SELL, Weekly/Monthly Darvas breakdown, or RSI200/SMA34 turning Bearish">🔔 Trend Exit</button>
  <button class="filter-btn" id="mtfmacdBtn" onclick="toggleFilter('mtfmacd')" title="Weekly AND Monthly MACD(12,26,9) both confirm — neither is in a Downtrend">🎯 MTF MACD OK</button>
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
  <th onclick="sortTable(11,'str')" title="Weinstein-style stage: Basing -> Advancing (trend beginning) -> Topping -> Declining">Stage</th>
  <th onclick="sortTable(12,'num')" title="Combined trend-beginning score: Stage + Squeeze + Volume + RSI + ADX + RS Rating">🌱 Early</th>
  <th onclick="sortTable(13,'num')" title="IBD-style percentile rank vs all stocks scanned today (1-99)">RS Rtg</th>
  <th onclick="sortTable(14,'str')" title="Bollinger Band volatility contraction — a coiling base often precedes breakouts">Squeeze</th>
  <th onclick="sortTable(15,'num')" title="Fibonacci 1.618 extension target, anchored to the base breakout when detected">Fib Tgt</th>
  <th onclick="sortTable(16,'num')" title="Median historical price move after RSI entered this same 55-65 zone">RSI Tgt</th>
  <th title="Close above the upper Bollinger Band — Daily/Weekly/Monthly">BB Break</th>
  <th title="Close above the prior 20-period Donchian Channel high — Daily/Weekly/Monthly">Donchian</th>
  <th onclick="sortTable(19,'str')" title="SMA(14) of RSI(14) above 50 — sustained bullish momentum regime">RSI-SMA</th>
  <th onclick="sortTable(20,'str')" title="Weekly RSI(14) + Monthly RSI(14) direction combined: both rising=BUY, both falling=SELL">MTF RSI</th>
  <th title="Weekly + Monthly MACD(12,26,9) trend state — checks whether the higher timeframes confirm a daily buy, the same way you'd check manually">MTF MACD</th>
  <th onclick="sortTable(22,'str')" title="RSI(200) vs its own SMA(34) — a slow, high-conviction trend-shift signal">RSI200/34</th>
  <th onclick="sortTable(23,'str')" title="CCI(200) vs its own SMA(34)">CCI200/34</th>
  <th onclick="sortTable(24,'str')" title="Double Top/Bottom, Head & Shoulders, Triangles, Cup & Handle — detected via swing-pivot geometry">Pattern</th>
  <th onclick="sortTable(25,'num')" title="Position-trade exit signal: Stage flip to Topping/Declining, MTF RSI SELL, Weekly/Monthly Darvas breakdown, RSI200/SMA34 or MACD(34,1000,20) turning bearish, Weekly/Monthly MACD downtrend, or Monthly RSI-SMA/CCI-SMA bearish. Not the same as the tactical daily SELL signal.">🔔 Exit</th>
  <th onclick="sortTable(26,'str')">Darvas D</th>
  <th onclick="sortTable(27,'str')">Darvas W</th>
  <th onclick="sortTable(28,'str')">Darvas M</th>
  <th>Resistance</th>
  <th>Support</th>
  <th onclick="sortTable(31,'num')">Vol</th>
  <th onclick="sortTable(32,'num')">Trades</th>
  <th onclick="sortTable(33,'num')">Win%</th>
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
let patternFilterVal = 'all';
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
    if (activeFilter === 'early')  matchFilter = r.dataset.early  === '1';
    if (activeFilter === 'buy')    matchFilter = ['STRONG BUY','MACD MEGA BUY','BUY','VOL BUY','M-RSI BUY'].includes(r.dataset.signal);
    if (activeFilter === 'mrsi70') matchFilter = parseFloat(r.dataset.mrsi) >= 70;
    if (activeFilter === 'darvas') matchFilter = r.dataset.darvas === 'BREAKOUT';
    if (activeFilter === 'bbbreak')  matchFilter = r.dataset.bbbreak  === '1';
    if (activeFilter === 'donbreak') matchFilter = r.dataset.donbreak === '1';
    if (activeFilter === 'rsisma')   matchFilter = r.dataset.rsisma   === '1';
    if (activeFilter === 'mtfbuy')   matchFilter = r.dataset.mtfsig   === 'BUY';
    if (activeFilter === 'rsi200')   matchFilter = r.dataset.rsi200   === 'Bullish';
    if (activeFilter === 'cci200')   matchFilter = r.dataset.cci200   === 'Bullish';
    if (activeFilter === 'exit')     matchFilter = r.dataset.exit     === '1';
    if (activeFilter === 'mtfmacd')  matchFilter = r.dataset.mtfmacd  === '1';

    let matchPattern = true;
    const pv = patternFilterVal;
    if (pv === 'any')       matchPattern = r.dataset.pattern !== 'None';
    else if (pv === 'bullish')  matchPattern = r.dataset.patterndir === 'bullish';
    else if (pv === 'bearish')  matchPattern = r.dataset.patterndir === 'bearish';
    else if (pv === 'confirmed') matchPattern = r.dataset.patternstatus === 'confirmed';
    else if (pv !== 'all')   matchPattern = r.dataset.pattern === pv;

    const show = matchSearch && matchFilter && matchPattern;
    r.classList.toggle('hidden', !show);
    if (show) vis++;
  }});
  document.getElementById('countBadge').textContent = vis + ' stocks';
}}

function setPatternFilter(val) {{
  patternFilterVal = val;
  filterTable();
}}

function toggleFilter(name) {{
  activeFilter = (activeFilter === name) ? null : name;
  ['blastBtn','earlyBtn','buyBtn','mrsi70Btn','dboxBtn','bbBtn','donBtn','rsismaBtn','mtfBuyBtn','rsi200Btn','cci200Btn','exitBtn','mtfmacdBtn'].forEach(id => {{
    document.getElementById(id).classList.remove('active');
  }});
  const map = {{blast:'blastBtn',early:'earlyBtn',buy:'buyBtn',mrsi70:'mrsi70Btn',darvas:'dboxBtn',
                bbbreak:'bbBtn',donbreak:'donBtn',rsisma:'rsismaBtn',mtfbuy:'mtfBuyBtn',
                rsi200:'rsi200Btn',cci200:'cci200Btn',exit:'exitBtn',mtfmacd:'mtfmacdBtn'}};
  if (activeFilter && map[activeFilter]) document.getElementById(map[activeFilter]).classList.add('active');
  filterTable();
}}

function clearFilters() {{
  activeFilter = null;
  patternFilterVal = 'all';
  document.getElementById('searchBox').value = '';
  document.getElementById('patternFilter').value = 'all';
  ['blastBtn','earlyBtn','buyBtn','mrsi70Btn','dboxBtn','bbBtn','donBtn','rsismaBtn','mtfBuyBtn','rsi200Btn','cci200Btn','exitBtn','mtfmacdBtn'].forEach(id => document.getElementById(id).classList.remove('active'));
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
    d: `charts/${{sym}}_daily.png`,
    w: `charts/${{sym}}_weekly.png`,
    m: `charts/${{sym}}_monthly.png`,
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


# ── Historical Trades Report ──────────────────────────────────────────────────
def build_trades_html(results, scan_time):
    """Separate report: every historical BUY->SELL trade the signals would have
    taken on each stock (from backtest()'s trade log), with entry/exit dates,
    % return, and days held — so the daily/weekly/monthly signal combination
    can be checked against its own trade-by-trade track record, not just a
    single win-rate summary number.
    """
    all_trades = []
    for r in results:
        for t in r.get('trade_list', []):
            all_trades.append({
                'symbol': r['symbol'], 'name': r['name'],
                'signal': t.get('signal', ''),
                'entry_date': t['entry_date'], 'entry_price': t['entry_price'],
                'exit_date': t['exit_date'], 'exit_price': t['exit_price'],
                'return_pct': t['return_pct'], 'days_held': t['days_held'],
                'open': bool(t.get('open', False)),
            })

    # Sort by exit date desc (most recent trades first)
    all_trades.sort(key=lambda t: t['exit_date'], reverse=True)

    total = len(all_trades)
    closed = [t for t in all_trades if not t['open']]
    open_trades = [t for t in all_trades if t['open']]
    wins = [t for t in closed if t['return_pct'] > 0]
    win_rate = (len(wins) / len(closed) * 100) if closed else 0
    avg_ret = np.mean([t['return_pct'] for t in closed]) if closed else 0
    avg_days = np.mean([t['days_held'] for t in closed]) if closed else 0
    best = max(closed, key=lambda t: t['return_pct']) if closed else None
    worst = min(closed, key=lambda t: t['return_pct']) if closed else None

    rows_html = []
    for i, t in enumerate(all_trades, 1):
        ret = t['return_pct']
        ret_color = '#00ff88' if ret > 0 else ('#ff6b6b' if ret < 0 else '#8b949e')
        status_badge = ('<span style="color:#f0b429;font-weight:700;">● OPEN</span>' if t['open']
                         else ('<span style="color:#00ff88;">✓ WIN</span>' if ret > 0
                               else '<span style="color:#ff6b6b;">✗ LOSS</span>'))
        rows_html.append(f"""
<tr class="trade-row" data-status="{'open' if t['open'] else ('win' if ret>0 else 'loss')}" data-symbol="{t['symbol']}">
  <td style="color:#555;font-size:11px;padding:6px 4px;">{i}</td>
  <td style="padding:6px 8px;">
    <div style="font-weight:700;color:#e6edf3;font-size:12px;">{t['symbol']}</div>
    <div style="color:#8b949e;font-size:10px;max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{t['name']}</div>
  </td>
  <td style="padding:6px 8px;">{sig_badge(t['signal'])}</td>
  <td style="padding:6px 8px;color:#8b949e;font-size:11px;white-space:nowrap;">{t['entry_date'].strftime('%Y-%m-%d')}</td>
  <td style="padding:6px 8px;color:#e6edf3;font-size:11px;">{fmt_price(t['entry_price'])}</td>
  <td style="padding:6px 8px;color:#8b949e;font-size:11px;white-space:nowrap;">{t['exit_date'].strftime('%Y-%m-%d')}</td>
  <td style="padding:6px 8px;color:#e6edf3;font-size:11px;">{fmt_price(t['exit_price'])}</td>
  <td style="padding:6px 8px;color:#8b949e;font-size:11px;">{t['days_held']}</td>
  <td style="padding:6px 8px;color:{ret_color};font-weight:700;font-size:12px;">{ret:+.1f}%</td>
  <td style="padding:6px 8px;">{status_badge}</td>
</tr>""")

    rows = '\n'.join(rows_html)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>📒 Multibagger Trades Report — Historical Buy/Sell Log</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Segoe UI',system-ui,sans-serif;background:#0d1117;color:#e6edf3;min-height:100vh}}
.header{{padding:20px 24px;border-bottom:1px solid #30363d;background:#161b22;}}
.header h1{{font-size:20px;margin-bottom:4px;}}
.header .meta{{color:#8b949e;font-size:12px;}}
.stats{{display:flex;gap:16px;padding:16px 24px;flex-wrap:wrap;}}
.stat-card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px 18px;min-width:120px;}}
.stat-card .label{{color:#8b949e;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;}}
.stat-card .value{{font-size:22px;font-weight:700;margin-top:4px;}}
.toolbar{{display:flex;gap:8px;padding:0 24px 16px;flex-wrap:wrap;align-items:center;}}
#searchBox{{background:#161b22;color:#e6edf3;border:1px solid #30363d;border-radius:6px;
            padding:7px 12px;font-size:13px;min-width:220px;font-family:inherit;}}
.filter-btn{{background:#161b22;color:#8b949e;border:1px solid #30363d;border-radius:6px;
             padding:6px 12px;font-size:12px;cursor:pointer;font-family:inherit;}}
.filter-btn.active{{background:#1f6feb22;color:#58a6ff;border-color:#58a6ff;}}
#countBadge{{color:#8b949e;font-size:12px;margin-left:auto;}}
table{{width:100%;border-collapse:collapse;font-size:12px;}}
thead th{{position:sticky;top:0;background:#161b22;color:#8b949e;text-align:left;
          padding:8px;border-bottom:1px solid #30363d;cursor:pointer;user-select:none;
          font-size:11px;text-transform:uppercase;letter-spacing:0.4px;white-space:nowrap;}}
thead th:hover{{color:#e6edf3;}}
tbody tr{{border-bottom:1px solid #21262d;}}
tbody tr:hover{{background:#161b22;}}
tbody tr.hidden{{display:none;}}
.table-wrap{{padding:0 24px 32px;overflow-x:auto;}}
</style>
</head>
<body>
<div class="header">
  <h1>📒 Multibagger Trades Report — Historical Buy → Sell Log</h1>
  <div class="meta">Generated {scan_time} &nbsp;|&nbsp; Every BUY→SELL trade the daily signal
    combination would have taken, from the main scan's backtest. Companion to the main
    multibagger_report.html — use this to check a signal's actual trade-by-trade track
    record rather than a single win-rate summary.</div>
</div>

<div class="stats">
  <div class="stat-card"><div class="label">Total Trades</div><div class="value">{total:,}</div></div>
  <div class="stat-card"><div class="label">Closed</div><div class="value">{len(closed):,}</div></div>
  <div class="stat-card"><div class="label">Open Now</div><div class="value" style="color:#f0b429;">{len(open_trades):,}</div></div>
  <div class="stat-card"><div class="label">Win Rate</div><div class="value" style="color:{'#00ff88' if win_rate>=50 else '#ff6b6b'};">{win_rate:.0f}%</div></div>
  <div class="stat-card"><div class="label">Avg Return</div><div class="value" style="color:{'#00ff88' if avg_ret>=0 else '#ff6b6b'};">{avg_ret:+.1f}%</div></div>
  <div class="stat-card"><div class="label">Avg Days Held</div><div class="value">{avg_days:.0f}</div></div>
  <div class="stat-card"><div class="label">Best Trade</div><div class="value" style="color:#00ff88;font-size:16px;">{(best['symbol']+' '+f"{best['return_pct']:+.0f}%") if best else '—'}</div></div>
  <div class="stat-card"><div class="label">Worst Trade</div><div class="value" style="color:#ff6b6b;font-size:16px;">{(worst['symbol']+' '+f"{worst['return_pct']:+.0f}%") if worst else '—'}</div></div>
</div>

<div class="toolbar">
  <input type="text" id="searchBox" placeholder="🔍 Search symbol or name..." oninput="filterTrades()">
  <button class="filter-btn" id="winBtn" onclick="toggleTradeFilter('win')">✓ Wins Only</button>
  <button class="filter-btn" id="lossBtn" onclick="toggleTradeFilter('loss')">✗ Losses Only</button>
  <button class="filter-btn" id="openBtn" onclick="toggleTradeFilter('open')">● Open Positions</button>
  <button class="filter-btn" onclick="clearTradeFilters()" style="color:#26d07c;border-color:#26d07c;">Show All</button>
  <span id="countBadge">{total:,} trades</span>
</div>

<div class="table-wrap">
<table id="tradesTable">
<thead>
<tr>
  <th>#</th>
  <th onclick="sortTrades(1,'str')">Symbol</th>
  <th onclick="sortTrades(2,'str')">Signal</th>
  <th onclick="sortTrades(3,'str')">Entry Date</th>
  <th onclick="sortTrades(4,'num')">Entry Price</th>
  <th onclick="sortTrades(5,'str')">Exit Date</th>
  <th onclick="sortTrades(6,'num')">Exit Price</th>
  <th onclick="sortTrades(7,'num')">Days Held</th>
  <th onclick="sortTrades(8,'num')">Return %</th>
  <th onclick="sortTrades(9,'str')">Status</th>
</tr>
</thead>
<tbody id="tradesBody">
{rows}
</tbody>
</table>
</div>

<script>
let tradeFilter = null;

function filterTrades() {{
  const q = document.getElementById('searchBox').value.toLowerCase();
  const rows = document.querySelectorAll('#tradesBody .trade-row');
  let vis = 0;
  rows.forEach(r => {{
    const matchSearch = !q || r.textContent.toLowerCase().includes(q);
    let matchFilter = true;
    if (tradeFilter === 'win')  matchFilter = r.dataset.status === 'win';
    if (tradeFilter === 'loss') matchFilter = r.dataset.status === 'loss';
    if (tradeFilter === 'open') matchFilter = r.dataset.status === 'open';
    const show = matchSearch && matchFilter;
    r.classList.toggle('hidden', !show);
    if (show) vis++;
  }});
  document.getElementById('countBadge').textContent = vis.toLocaleString() + ' trades';
}}

function toggleTradeFilter(name) {{
  tradeFilter = (tradeFilter === name) ? null : name;
  ['winBtn','lossBtn','openBtn'].forEach(id => document.getElementById(id).classList.remove('active'));
  const map = {{win:'winBtn', loss:'lossBtn', open:'openBtn'}};
  if (tradeFilter) document.getElementById(map[tradeFilter]).classList.add('active');
  filterTrades();
}}

function clearTradeFilters() {{
  tradeFilter = null;
  document.getElementById('searchBox').value = '';
  ['winBtn','lossBtn','openBtn'].forEach(id => document.getElementById(id).classList.remove('active'));
  filterTrades();
}}

let tSortCol = 8, tSortDir = -1;
function sortTrades(col, type) {{
  const tbody = document.getElementById('tradesBody');
  const rows = Array.from(tbody.querySelectorAll('.trade-row'));
  tSortDir = (tSortCol === col) ? -tSortDir : -1;
  tSortCol = col;
  rows.sort((a, b) => {{
    let av = a.children[col].textContent.trim();
    let bv = b.children[col].textContent.trim();
    if (type === 'num') {{
      av = parseFloat(av.replace(/[^0-9.\\-]/g, '')) || 0;
      bv = parseFloat(bv.replace(/[^0-9.\\-]/g, '')) || 0;
      return (av - bv) * tSortDir;
    }}
    return av.localeCompare(bv) * tSortDir;
  }});
  rows.forEach(r => tbody.appendChild(r));
}}
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
        subprocess.run(['git', 'add', REPORT_HTML, TRADES_REPORT_HTML, str(CHARTS_DIR)],
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
def _json_default(obj):
    """Fallback encoder for save_cache(). json.dump() only natively handles
    str/int/float/bool/None/list/dict — this covers everything else that
    actually shows up in a result dict: pandas Timestamps (from trade_list's
    entry_date/exit_date), and numpy scalar types (int64/float64/bool_) that
    can slip through if a value wasn't explicitly cast with int()/float().
    """
    if isinstance(obj, (pd.Timestamp, datetime)):
        return obj.isoformat()
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return None if np.isnan(obj) else float(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


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
                # trade_list's entry_date/exit_date were serialized to ISO strings
                # by _json_default() — restore them to Timestamps so downstream
                # code (build_trades_html's .strftime(), date sorting) still works
                # on cache-hit results exactly like freshly-computed ones.
                for r in results.values():
                    for t in (r.get('trade_list') or []):
                        if isinstance(t.get('entry_date'), str):
                            t['entry_date'] = pd.Timestamp(t['entry_date'])
                        if isinstance(t.get('exit_date'), str):
                            t['exit_date'] = pd.Timestamp(t['exit_date'])
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
        json.dump(cache, f, default=_json_default)

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
    items = stocks   # already a list of (name, ticker) tuples, ticker-uniqueness enforced in load_nse_stocks()

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
        # CPU-bound (pandas/numpy indicator math + matplotlib chart rendering), so this
        # uses a ProcessPoolExecutor for real multi-core parallelism instead of threads
        # (threads would be serialized by the GIL for this kind of work).
        tprint(f"  PHASE 2/2 - Computing indicators + signals + charts")
        tprint(f"  Workers   : {COMPUTE_WORKERS} processes ({os.cpu_count()} CPU cores detected)  "
               f"|  Stocks: {len(items_to_fetch):,}")
        tprint("-" * 65)

        t_cp_start  = time.time()
        cp_lock     = threading.Lock()
        blast_list  = []

        with ProcessPoolExecutor(max_workers=COMPUTE_WORKERS) as exe:
            futures = {
                exe.submit(_process_stock_task, (name, ticker, prefetched.get(ticker))): (name, ticker)
                for name, ticker in items_to_fetch
            }

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

    # ── Cross-sectional RS Rating + Early Trend scoring ────────────────
    # RS Rating needs every stock's raw score before it can rank any one of them
    # (it's a percentile within today's universe), so this has to happen once,
    # after Phase 2 is fully done — not inside process_stock(). Uses .get() with
    # fallbacks throughout since same-day cached results from a run before this
    # feature existed won't have these fields.
    tprint("  Computing RS Ratings + Early Trend scores...")
    rs_series = pd.Series({i: r['rs_raw'] for i, r in enumerate(results)
                            if r.get('rs_raw') is not None})
    if len(rs_series) >= 10:
        rs_pctile = rs_series.rank(pct=True) * 98 + 1   # IBD-style 1-99 scale
    else:
        rs_pctile = pd.Series(dtype=float)

    for i, r in enumerate(results):
        rs_rating = round(float(rs_pctile[i]), 1) if i in rs_pctile.index else None
        r['rs_rating'] = rs_rating
        r['early_trend_score'] = compute_early_trend_score(
            stage_num=r.get('stage_num', 0),
            squeeze_recent=r.get('squeeze_recent', False),
            vol_ratio=r.get('vol_ratio', 1.0),
            d_rsi_val=r.get('d_rsi', 50),
            adx_val=r.get('adx', 0),
            rs_rating=rs_rating,
        )
        r['early_trend'] = (r['early_trend_score'] >= 55 and
                             r.get('stage_num', 0) in (1, 2))

    # ── Build HTML report ─────────────────────────────────────────────
    tprint("  Building HTML report...")
    html = build_html(results, scan_time, total_scanned, ok_count)
    with open(REPORT_HTML, 'w', encoding='utf-8') as f:
        f.write(html)
    size_mb = Path(REPORT_HTML).stat().st_size / 1024 / 1024
    tprint(f"  Saved: {REPORT_HTML}  ({size_mb:.1f} MB)")
    tprint(f"  Stocks: {ok_count}  |  BLAST signals: {sum(1 for r in results if r.get('blast'))}")

    # ── Build historical trades report ─────────────────────────────────
    tprint("  Building historical trades report...")
    trades_html = build_trades_html(results, scan_time)
    with open(TRADES_REPORT_HTML, 'w', encoding='utf-8') as f:
        f.write(trades_html)
    trades_size_mb = Path(TRADES_REPORT_HTML).stat().st_size / 1024 / 1024
    total_trade_count = sum(len(r.get('trade_list', [])) for r in results)
    tprint(f"  Saved: {TRADES_REPORT_HTML}  ({trades_size_mb:.1f} MB, {total_trade_count:,} trades)")

    # ── GitHub Push ───────────────────────────────────────────────────
    tprint("\n  Pushing to GitHub...")
    push_to_github()

    save_cache({'date': today_str, 'done': total_scanned,
                'total': total_scanned, 'results': cache_results})
    tprint(f"\n  Done!  Total runtime: {time.time()-t_start:.0f}s\n")


if __name__ == '__main__':
    main()
