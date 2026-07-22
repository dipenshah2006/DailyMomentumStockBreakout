"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  INTRADAY MOMENTUM BREAKOUT SCANNER — NSE                                   ║
║  Multi-Timeframe: Daily · Weekly · Monthly · 4H · 1H · 15M · 5M            ║
║                                                                              ║
║  Strategies:                                                                 ║
║   1. PDH Breakout (price > previous day high + volume surge)                ║
║   2. MTF Momentum (Daily/Weekly/Monthly aligned → drill into 15M/1H/4H)    ║
║   3. VWAP Breakout (intraday price crossing above VWAP)                     ║
║   4. Bollinger Squeeze → Expansion breakout                                 ║
║   5. Mean Reversion (RSI oversold + key support + volume)                  ║
║   6. Fibonacci Extension targets (127.2%, 161.8%, 200%, 261.8%)            ║
║                                                                              ║
║  Indicators (TA-Lib):                                                        ║
║   RSI · MACD · ADX+DI · Bollinger · EMA(9/21/50/200) · ATR                ║
║   Stochastic · CCI · OBV · Volume Ratio · Supertrend · VWAP               ║
║                                                                              ║
║  Target: 5–20% intraday returns on high price-action + volume stocks       ║
║  Runs on: port 5001  |  Auto-refresh: every 15 minutes                     ║
╚══════════════════════════════════════════════════════════════════════════════╝

Run:  python intraday_scanner.py
URL:  http://localhost:5001
"""

import os, sys, json, time, logging, warnings, io, csv, threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache

import numpy as np
import pandas as pd
import talib
import yfinance as yf
from flask import Flask, jsonify, request

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

LOCAL_NSE_CSV    = "india/NSE/NSECash/EQUITY_L.csv"
LOCAL_FO_CSV     = "india/NSE/nse_fo_list.csv"
PORT             = 3000
SCAN_INTERVAL    = 15          # minutes between auto-scans
MAX_DAILY_SCAN   = 500         # stocks scanned on daily timeframe first
MAX_INTRADAY     = 150         # top candidates for full MTF intraday analysis
WORKER_THREADS   = 10          # parallel download workers
MIN_PRICE        = 20          # skip penny stocks below ₹20
MIN_VOLUME       = 10_000      # skip stocks with avg volume < 10K

# Scoring weights
W_DAILY    = 0.30
W_WEEKLY   = 0.15
W_MONTHLY  = 0.10
W_4H       = 0.20
W_1H       = 0.15
W_15M      = 0.10

# Signal thresholds
RSI_BULL   = 55    # RSI above = bullish momentum
RSI_BEAR   = 45    # RSI below = bearish
RSI_OB     = 70    # overbought
RSI_OS     = 30    # oversold (mean reversion)
ADX_TREND  = 25    # ADX above = confirmed trend
ADX_STRONG = 40    # ADX above = strong trend
VOL_SURGE  = 1.8   # volume > 1.8x average = surge
PDH_BUFFER = 0.002 # 0.2% above PDH = confirmed breakout

# Fibonacci levels
FIB_RET    = [0.236, 0.382, 0.5, 0.618, 0.786]
FIB_EXT    = [1.0, 1.272, 1.618, 2.0, 2.618]

# ═══════════════════════════════════════════════════════════════════════════════
#  GLOBAL STATE
# ═══════════════════════════════════════════════════════════════════════════════

_state = {
    "last_run": None,
    "status": "idle",
    "results": [],
    "html": "",
    "scan_count": 0,
    "universe_size": 0,
    "candidates": 0,
    "market_time": "",
}
_lock = threading.Lock()
_FO_SET: set = set()

# ═══════════════════════════════════════════════════════════════════════════════
#  UNIVERSE LOADER
# ═══════════════════════════════════════════════════════════════════════════════

def load_universe() -> list[dict]:
    stocks = []
    if os.path.exists(LOCAL_NSE_CSV):
        try:
            df = pd.read_csv(LOCAL_NSE_CSV)
            sym_col  = next((c for c in df.columns if "SYMBOL"  in c.upper()), None)
            name_col = next((c for c in df.columns if "NAME"    in c.upper()), None)
            ser_col  = next((c for c in df.columns if "SERIES"  in c.upper()), None)
            for _, row in df.iterrows():
                if ser_col and str(row.get(ser_col, "")).strip() not in ("EQ", ""):
                    continue
                sym = str(row[sym_col]).strip() if sym_col else ""
                nm  = str(row[name_col]).strip() if name_col else sym
                if sym:
                    stocks.append({"ticker": sym, "company": nm[:35]})
        except Exception as e:
            log.warning(f"CSV load error: {e}")

    if not stocks:
        stocks = [{"ticker": t, "company": t} for t in [
            "RELIANCE","TCS","INFY","HDFCBANK","ICICIBANK","KOTAKBANK","SBIN","AXISBANK",
            "LT","WIPRO","HCLTECH","BAJFINANCE","TITAN","ASIANPAINT","MARUTI","ULTRACEMCO",
            "NTPC","POWERGRID","ONGC","COALINDIA","TATAMOTORS","M&M","SUNPHARMA","DRREDDY"
        ]]
        log.warning("Using fallback Nifty24 list")
    return stocks[:MAX_DAILY_SCAN]

def load_fo_list():
    global _FO_SET
    if os.path.exists(LOCAL_FO_CSV):
        try:
            df = pd.read_csv(LOCAL_FO_CSV)
            col = next((c for c in df.columns if "SYMBOL" in c.upper()), df.columns[0])
            _FO_SET = set(df[col].str.strip().str.upper().tolist())
            log.info(f"F&O list: {len(_FO_SET)} symbols")
        except Exception:
            pass

# ═══════════════════════════════════════════════════════════════════════════════
#  DATA DOWNLOADER
# ═══════════════════════════════════════════════════════════════════════════════

def _dl(tickers: list[str], period: str, interval: str) -> dict[str, pd.DataFrame]:
    """Download yfinance data for a list of tickers, return dict ticker→DataFrame."""
    if not tickers:
        return {}
    suffixed = [f"{t}.NS" for t in tickers]
    out = {}
    try:
        raw = yf.download(
            suffixed, period=period, interval=interval,
            group_by="ticker", auto_adjust=True, progress=False, threads=True,
            ignore_tz=True
        )
        if isinstance(raw.columns, pd.MultiIndex):
            for t, ts in zip(tickers, suffixed):
                try:
                    df = raw[ts].dropna(how="all")
                    if len(df) >= 5:
                        out[t] = df
                except Exception:
                    pass
        else:
            if len(tickers) == 1 and len(raw) >= 5:
                out[tickers[0]] = raw.dropna(how="all")
    except Exception as e:
        log.debug(f"Download error ({interval}): {e}")
    return out

def batch_download(tickers: list[str], period: str, interval: str,
                   batch_size: int = 20, pause: float = 0.5) -> dict[str, pd.DataFrame]:
    result = {}
    batches = [tickers[i:i+batch_size] for i in range(0, len(tickers), batch_size)]
    for b in batches:
        result.update(_dl(b, period, interval))
        if pause:
            time.sleep(pause)
    return result

# ═══════════════════════════════════════════════════════════════════════════════
#  INDICATOR ENGINE (TA-Lib)
# ═══════════════════════════════════════════════════════════════════════════════

def safe_talib(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except Exception:
        return np.array([np.nan])

def compute_indicators(df: pd.DataFrame) -> dict:
    """Compute all TA-Lib indicators on a OHLCV DataFrame. Returns dict of values (latest bar)."""
    if len(df) < 20:
        return {}

    o = df["Open"].values.astype(float)
    h = df["High"].values.astype(float)
    l = df["Low"].values.astype(float)
    c = df["Close"].values.astype(float)
    v = df["Volume"].values.astype(float)

    def last(arr):
        if arr is None or len(arr) == 0:
            return np.nan
        arr = np.asarray(arr, dtype=float)
        vals = arr[~np.isnan(arr)]
        return float(vals[-1]) if len(vals) else np.nan

    def prev(arr, n=1):
        arr = np.asarray(arr, dtype=float)
        idx = np.where(~np.isnan(arr))[0]
        return float(arr[idx[-1-n]]) if len(idx) > n else np.nan

    # RSI
    rsi14 = talib.RSI(c, timeperiod=14)
    rsi7  = talib.RSI(c, timeperiod=7)

    # MACD
    macd_line, macd_sig, macd_hist = talib.MACD(c, fastperiod=12, slowperiod=26, signalperiod=9)
    macd3, msig3, mhist3           = talib.MACD(c, fastperiod=5,  slowperiod=13, signalperiod=5)

    # ADX + Directional
    adx      = talib.ADX(h, l, c, timeperiod=14)
    plus_di  = talib.PLUS_DI(h, l, c, timeperiod=14)
    minus_di = talib.MINUS_DI(h, l, c, timeperiod=14)

    # Bollinger Bands (20, 2)
    bb_upper, bb_mid, bb_lower = talib.BBANDS(c, timeperiod=20, nbdevup=2, nbdevdn=2)
    bb_width = (last(bb_upper) - last(bb_lower)) / (last(bb_mid) + 1e-9)
    bb_pct_b = (last(c) - last(bb_lower)) / (last(bb_upper) - last(bb_lower) + 1e-9)

    # EMAs
    ema9   = talib.EMA(c, timeperiod=9)
    ema21  = talib.EMA(c, timeperiod=21)
    ema50  = talib.EMA(c, timeperiod=50)
    ema200 = talib.EMA(c, timeperiod=200)

    # ATR
    atr14  = talib.ATR(h, l, c, timeperiod=14)
    atr_pct = (last(atr14) / (last(c) + 1e-9)) * 100

    # Stochastic
    stoch_k, stoch_d = talib.STOCH(h, l, c, fastk_period=14, slowk_period=3, slowd_period=3)

    # Stochastic RSI
    try:
        srsi_k, srsi_d = talib.STOCHRSI(c, timeperiod=14, fastk_period=5, fastd_period=3)
    except Exception:
        srsi_k, srsi_d = np.full(len(c), np.nan), np.full(len(c), np.nan)

    # CCI
    cci20 = talib.CCI(h, l, c, timeperiod=20)

    # OBV
    obv = talib.OBV(c, v)
    obv_ema = talib.EMA(obv, timeperiod=20)

    # Volume analysis
    vol_sma20 = talib.SMA(v, timeperiod=20)
    vol_ratio = (last(v) / (last(vol_sma20) + 1e-9))

    # Supertrend (ATR-based, 3×ATR14)
    def supertrend(h, l, c, atr, mult=3.0):
        hl2 = (h + l) / 2
        upper = hl2 + mult * atr
        lower = hl2 - mult * atr
        trend = np.full(len(c), 1)
        for i in range(1, len(c)):
            if np.isnan(atr[i]):
                trend[i] = trend[i-1]
                continue
            if c[i] > upper[i-1]:
                trend[i] = 1
            elif c[i] < lower[i-1]:
                trend[i] = -1
            else:
                trend[i] = trend[i-1]
        return trend

    atr14_full = talib.ATR(h, l, c, timeperiod=14)
    st_trend   = supertrend(h, l, c, atr14_full)

    # VWAP (intraday: cumulative from first bar of today or first bar available)
    typical_price = (h + l + c) / 3
    cum_tpv = np.cumsum(typical_price * v)
    cum_vol = np.cumsum(v)
    vwap    = cum_tpv / (cum_vol + 1e-9)

    # Pivot Points (from yesterday's OHLC)
    if len(df) >= 2:
        ph, pl, pc = float(h[-2]), float(l[-2]), float(c[-2])
        pp   = (ph + pl + pc) / 3
        r1   = 2 * pp - pl
        r2   = pp + (ph - pl)
        r3   = ph + 2 * (pp - pl)
        s1   = 2 * pp - ph
        s2   = pp - (ph - pl)
        s3   = pl - 2 * (ph - pp)
    else:
        pp = r1 = r2 = r3 = s1 = s2 = s3 = np.nan

    # Fibonacci: swing low→high over last 20 bars
    window = min(20, len(c))
    sw_low  = float(np.min(l[-window:]))
    sw_high = float(np.max(h[-window:]))
    sw_rng  = sw_high - sw_low
    fib_retracements = {f"F{int(f*100)}": round(sw_high - f * sw_rng, 2) for f in FIB_RET}
    fib_extensions   = {f"FE{int(f*100)}": round(sw_low  + f * sw_rng, 2) for f in FIB_EXT}

    # Previous day high/low (useful for PDH breakout)
    pdh = prev(h, 1) if len(h) >= 2 else np.nan
    pdl = prev(l, 1) if len(l) >= 2 else np.nan

    close_now = last(c)
    open_now  = last(o)
    high_now  = last(h)
    low_now   = last(l)
    vol_now   = last(v)

    # EMA stack check
    e9, e21, e50, e200 = last(ema9), last(ema21), last(ema50), last(ema200)
    ema_bull = (close_now > e9 > e21 > e50) if all(not np.isnan(x) for x in [e9, e21, e50]) else False
    ema_bear = (close_now < e9 < e21 < e50) if all(not np.isnan(x) for x in [e9, e21, e50]) else False

    # MACD direction
    macd_bull = (last(macd_line) > last(macd_sig)) and (last(macd_hist) > 0)
    macd_bear = (last(macd_line) < last(macd_sig)) and (last(macd_hist) < 0)
    macd_cross_bull = (last(macd_hist) > 0) and (prev(macd_hist) < 0)
    macd_cross_bear = (last(macd_hist) < 0) and (prev(macd_hist) > 0)

    # ADX trend
    adx_val    = last(adx)
    pdi_val    = last(plus_di)
    mdi_val    = last(minus_di)
    trend_bull = (adx_val > ADX_TREND) and (pdi_val > mdi_val)
    trend_bear = (adx_val > ADX_TREND) and (mdi_val > pdi_val)

    # RSI signals
    rsi_val  = last(rsi14)
    rsi_bull = rsi_val > RSI_BULL
    rsi_bear = rsi_val < RSI_BEAR
    rsi_os   = rsi_val < RSI_OS
    rsi_ob   = rsi_val > RSI_OB

    # Bollinger breakout
    bb_upper_val = last(bb_upper)
    bb_lower_val = last(bb_lower)
    bb_breakout  = close_now > bb_upper_val
    bb_support   = close_now < bb_lower_val * 1.01  # near lower band

    # Volume surge
    vol_surge = vol_ratio > VOL_SURGE

    # VWAP position
    vwap_val  = last(vwap)
    above_vwap = close_now > vwap_val

    # OBV trend
    obv_bull = last(obv) > last(obv_ema)

    # Supertrend
    st_bull = st_trend[-1] == 1

    return {
        "close":        round(close_now, 2),
        "open":         round(open_now, 2),
        "high":         round(high_now, 2),
        "low":          round(low_now, 2),
        "volume":       int(vol_now),
        "rsi14":        round(rsi_val, 1),
        "rsi7":         round(last(rsi7), 1),
        "macd":         round(last(macd_line), 3),
        "macd_sig":     round(last(macd_sig), 3),
        "macd_hist":    round(last(macd_hist), 3),
        "adx":          round(adx_val, 1),
        "plus_di":      round(pdi_val, 1),
        "minus_di":     round(mdi_val, 1),
        "bb_upper":     round(bb_upper_val, 2),
        "bb_mid":       round(last(bb_mid), 2),
        "bb_lower":     round(bb_lower_val, 2),
        "bb_width":     round(bb_width * 100, 2),
        "bb_pct_b":     round(bb_pct_b * 100, 1),
        "ema9":         round(e9, 2),
        "ema21":        round(e21, 2),
        "ema50":        round(e50, 2),
        "ema200":       round(e200, 2) if not np.isnan(e200) else None,
        "atr14":        round(last(atr14), 2),
        "atr_pct":      round(atr_pct, 2),
        "stoch_k":      round(last(stoch_k), 1),
        "stoch_d":      round(last(stoch_d), 1),
        "srsi_k":       round(last(srsi_k) * 100, 1),
        "cci20":        round(last(cci20), 1),
        "vol_ratio":    round(vol_ratio, 2),
        "vwap":         round(vwap_val, 2),
        "pp":           round(pp, 2),
        "r1": round(r1, 2), "r2": round(r2, 2), "r3": round(r3, 2),
        "s1": round(s1, 2), "s2": round(s2, 2), "s3": round(s3, 2),
        "pdh":          round(pdh, 2) if not np.isnan(pdh) else None,
        "pdl":          round(pdl, 2) if not np.isnan(pdl) else None,
        "sw_high":      round(sw_high, 2),
        "sw_low":       round(sw_low, 2),
        "fib_ret":      fib_retracements,
        "fib_ext":      fib_extensions,
        "obv_bull":     obv_bull,
        "st_bull":      st_bull,
        "ema_bull":     ema_bull,
        "ema_bear":     ema_bear,
        "macd_bull":    macd_bull,
        "macd_bear":    macd_bear,
        "macd_cross_bull": macd_cross_bull,
        "macd_cross_bear": macd_cross_bear,
        "trend_bull":   trend_bull,
        "trend_bear":   trend_bear,
        "rsi_bull":     rsi_bull,
        "rsi_bear":     rsi_bear,
        "rsi_os":       rsi_os,
        "rsi_ob":       rsi_ob,
        "bb_breakout":  bb_breakout,
        "bb_support":   bb_support,
        "vol_surge":    vol_surge,
        "above_vwap":   above_vwap,
    }

def resample_4h(df_1h: pd.DataFrame) -> pd.DataFrame:
    """Resample 1H data to 4H bars."""
    try:
        return df_1h.resample("4h", closed="right", label="right").agg({
            "Open": "first", "High": "max", "Low": "min",
            "Close": "last", "Volume": "sum"
        }).dropna()
    except Exception:
        return df_1h

def score_indicators(ind: dict, weight: float) -> float:
    """Score a single timeframe's indicators: 0–1 → scaled by weight."""
    if not ind:
        return 0.0
    score = 0.0
    # RSI momentum (0–3 pts)
    rsi = ind.get("rsi14", 50)
    if rsi > 70:   score += 3.0
    elif rsi > 60: score += 2.0
    elif rsi > 55: score += 1.0
    elif rsi < 30: score -= 2.0
    elif rsi < 40: score -= 1.0

    # MACD (0–3 pts)
    if ind.get("macd_cross_bull"): score += 3.0
    elif ind.get("macd_bull"):     score += 1.5

    # ADX trend (0–2 pts)
    if ind.get("trend_bull"):
        adx = ind.get("adx", 0)
        if adx > ADX_STRONG: score += 2.0
        else:                 score += 1.0

    # EMA stack (0–2 pts)
    if ind.get("ema_bull"): score += 2.0

    # Supertrend (0–1 pt)
    if ind.get("st_bull"): score += 1.0

    # OBV (0–1 pt)
    if ind.get("obv_bull"): score += 1.0

    # Volume surge (0–1 pt)
    if ind.get("vol_surge"): score += 1.0

    # BB breakout (0–1 pt)
    if ind.get("bb_breakout"): score += 1.0

    # VWAP (0–1 pt)
    if ind.get("above_vwap"): score += 0.5

    max_possible = 15.0
    normalised = max(0.0, min(score / max_possible, 1.0))
    return round(normalised * weight * 100, 2)

# ═══════════════════════════════════════════════════════════════════════════════
#  SIGNAL CLASSIFIER
# ═══════════════════════════════════════════════════════════════════════════════

def classify_signal(ticker: str, inds: dict, close: float) -> dict:
    """
    Determine trade signal type, entry, stop loss, and Fibonacci targets.
    Returns a signal dict.
    """
    tf_15m = inds.get("15m", {})
    tf_1h  = inds.get("1h",  {})
    tf_4h  = inds.get("4h",  {})
    tf_d   = inds.get("d",   {})

    signals = []
    entry_price = close
    signal_type = "WATCH"
    sl = None
    t1 = t2 = t3 = None
    reasoning = []

    pdh  = tf_d.get("pdh")  or tf_1h.get("pdh")
    pdl  = tf_d.get("pdl")  or tf_1h.get("pdl")
    atr  = tf_1h.get("atr14") or tf_d.get("atr14") or 0
    vwap = tf_15m.get("vwap") or tf_1h.get("vwap") or 0

    # ── STRATEGY 1: PDH Breakout ─────────────────────────────────────────────
    pdh_breakout = (pdh and close > pdh * (1 + PDH_BUFFER)
                    and tf_1h.get("vol_surge")
                    and (tf_1h.get("rsi14", 50) > RSI_BULL or tf_15m.get("rsi14", 50) > RSI_BULL))
    if pdh_breakout:
        signals.append("PDH_BREAKOUT")
        reasoning.append(f"Price ₹{close} > PDH ₹{pdh:.2f} + Vol surge")
        sl   = round(pdh * 0.997, 2)
        t1   = round(close + atr * 1.5, 2)
        t2   = round(close + atr * 3.0, 2)
        t3   = round(close + atr * 5.0, 2)

    # ── STRATEGY 2: VWAP Breakout ─────────────────────────────────────────────
    if (tf_15m.get("above_vwap") and tf_15m.get("vol_surge")
            and tf_15m.get("macd_bull") and tf_1h.get("trend_bull")):
        signals.append("VWAP_BREAKOUT")
        reasoning.append(f"Price above VWAP ₹{vwap:.2f} with volume")
        if sl is None:
            sl = round(vwap * 0.998, 2)
            t1 = round(close + atr * 1.2, 2)
            t2 = round(close + atr * 2.5, 2)
            t3 = round(close + atr * 4.0, 2)

    # ── STRATEGY 3: MTF Momentum (D+4H+1H aligned) ───────────────────────────
    mtf_bull = (tf_d.get("ema_bull") and tf_d.get("macd_bull")
                and tf_4h.get("trend_bull") and tf_1h.get("rsi14", 50) > RSI_BULL)
    if mtf_bull:
        signals.append("MTF_MOMENTUM")
        reasoning.append("Daily EMA stack + 4H ADX trend + 1H RSI bullish")
        if sl is None:
            sl = round(close - atr * 1.5, 2)
            t1 = round(close + atr * 2.0, 2)
            t2 = round(close + atr * 4.0, 2)
            t3 = round(close + atr * 6.0, 2)

    # ── STRATEGY 4: Bollinger Squeeze → Expansion ────────────────────────────
    bb_exp = (tf_1h.get("bb_breakout") and tf_1h.get("vol_surge")
              and tf_1h.get("bb_width", 99) < 3.0  # tight band first
              and tf_d.get("macd_bull"))
    if bb_exp:
        signals.append("BB_EXPANSION")
        reasoning.append("BB squeeze breakout with volume confirmation")
        if sl is None:
            sl = round(tf_1h.get("bb_mid", close) * 0.997, 2)
            t1 = round(close + atr * 1.5, 2)
            t2 = round(close + atr * 3.0, 2)
            t3 = round(close + atr * 5.0, 2)

    # ── STRATEGY 5: Mean Reversion (RSI oversold + support) ──────────────────
    mean_rev = (tf_1h.get("rsi_os") and tf_1h.get("bb_support")
                and tf_d.get("ema_bull")  # only trade reversal in uptrend
                and tf_1h.get("vol_ratio", 0) > 1.2)
    if mean_rev:
        signals.append("MEAN_REVERSION")
        reasoning.append("RSI oversold + near BB lower band in uptrend")
        if sl is None:
            s2  = tf_1h.get("s2", close * 0.97)
            sl  = round(s2 * 0.998, 2)
            t1  = round(tf_1h.get("pp",  close * 1.02), 2)
            t2  = round(tf_1h.get("r1",  close * 1.04), 2)
            t3  = round(tf_1h.get("r2",  close * 1.06), 2)

    # ── STRATEGY 6: PDL Breakdown (short signal for context) ─────────────────
    pdl_break = (pdl and close < pdl * (1 - PDH_BUFFER)
                 and tf_1h.get("vol_surge")
                 and tf_1h.get("rsi14", 50) < RSI_BEAR)
    if pdl_break:
        signals.append("PDL_BREAKDOWN")
        reasoning.append(f"Price ₹{close} < PDL ₹{pdl:.2f} — breakdown")

    # Fibonacci extension targets (override if available)
    fib_ext = tf_1h.get("fib_ext") or tf_d.get("fib_ext") or {}
    if fib_ext and "FE127" in fib_ext and close < fib_ext["FE127"]:
        t1 = fib_ext.get("FE127", t1)
        t2 = fib_ext.get("FE161", t2)
        t3 = fib_ext.get("FE200", t3)

    # Final signal type
    priority = ["PDH_BREAKOUT", "VWAP_BREAKOUT", "BB_EXPANSION", "MTF_MOMENTUM",
                "MEAN_REVERSION", "PDL_BREAKDOWN", "WATCH"]
    for p in priority:
        if p in signals:
            signal_type = p
            break

    # Potential % return
    pct_t1 = round((t1 / close - 1) * 100, 2) if t1 else None
    pct_t2 = round((t2 / close - 1) * 100, 2) if t2 else None
    pct_sl = round((close / sl - 1) * 100, 2)  if sl else None

    return {
        "signal":    signal_type,
        "signals":   signals,
        "reasoning": " | ".join(reasoning) if reasoning else "Monitoring",
        "sl":        sl,
        "t1":        t1,
        "t2":        t2,
        "t3":        t3,
        "pct_t1":    pct_t1,
        "pct_t2":    pct_t2,
        "pct_sl":    pct_sl,
        "pdh":       pdh,
        "pdl":       pdl,
        "vwap":      round(vwap, 2) if vwap else None,
    }

# ═══════════════════════════════════════════════════════════════════════════════
#  STOCK ANALYSER
# ═══════════════════════════════════════════════════════════════════════════════

def analyse_stock(ticker: str, company: str,
                  df_d: pd.DataFrame, df_w: pd.DataFrame,
                  df_m: pd.DataFrame, df_1h: pd.DataFrame,
                  df_15m: pd.DataFrame) -> dict | None:
    try:
        close = float(df_d["Close"].iloc[-1])
        if close < MIN_PRICE or close != close:
            return None

        avg_vol = float(df_d["Volume"].rolling(20).mean().iloc[-1])
        if avg_vol < MIN_VOLUME:
            return None

        # Compute indicators per timeframe
        ind_d   = compute_indicators(df_d.iloc[-250:])     # 1 year daily
        ind_w   = compute_indicators(df_w.iloc[-104:])     # 2 years weekly
        ind_m   = compute_indicators(df_m.iloc[-60:])      # 5 years monthly
        ind_1h  = compute_indicators(df_1h.iloc[-48:])     # last 48 hours

        df_4h   = resample_4h(df_1h) if df_1h is not None and len(df_1h) >= 4 else pd.DataFrame()
        ind_4h  = compute_indicators(df_4h.iloc[-20:]) if len(df_4h) >= 5 else {}

        ind_15m = compute_indicators(df_15m.iloc[-64:]) if df_15m is not None and len(df_15m) >= 10 else {}

        # Composite momentum score (0–100)
        score  = (score_indicators(ind_d,   W_DAILY)
                + score_indicators(ind_w,   W_WEEKLY)
                + score_indicators(ind_m,   W_MONTHLY)
                + score_indicators(ind_4h,  W_4H)
                + score_indicators(ind_1h,  W_1H)
                + score_indicators(ind_15m, W_15M))

        # Classify trading signal
        inds = {"d": ind_d, "w": ind_w, "m": ind_m,
                "4h": ind_4h, "1h": ind_1h, "15m": ind_15m}
        sig = classify_signal(ticker, inds, close)

        # 52-week high/low
        high_52w = float(df_d["High"].rolling(min(252, len(df_d))).max().iloc[-1])
        low_52w  = float(df_d["Low"].rolling(min(252, len(df_d))).min().iloc[-1])
        ath_pct  = round((close / high_52w - 1) * 100, 1) if high_52w else None
        atl_pct  = round((close / low_52w  - 1) * 100, 1) if low_52w  else None

        return {
            "ticker":    ticker,
            "company":   company,
            "is_fo":     ticker in _FO_SET,
            "close":     round(close, 2),
            "score":     round(score, 1),
            "high_52w":  round(high_52w, 2),
            "low_52w":   round(low_52w, 2),
            "ath_pct":   ath_pct,
            "atl_pct":   atl_pct,
            "signal":    sig["signal"],
            "signals":   sig["signals"],
            "reasoning": sig["reasoning"],
            "sl":        sig["sl"],
            "t1":        sig["t1"],
            "t2":        sig["t2"],
            "t3":        sig["t3"],
            "pct_t1":    sig["pct_t1"],
            "pct_t2":    sig["pct_t2"],
            "pct_sl":    sig["pct_sl"],
            "pdh":       sig["pdh"],
            "pdl":       sig["pdl"],
            "vwap":      sig["vwap"],
            "ind_d":     _compact(ind_d),
            "ind_w":     _compact(ind_w),
            "ind_m":     _compact(ind_m),
            "ind_4h":    _compact(ind_4h),
            "ind_1h":    _compact(ind_1h),
            "ind_15m":   _compact(ind_15m),
        }
    except Exception as e:
        log.debug(f"{ticker} error: {e}")
        return None

def _compact(ind: dict) -> dict:
    """Return only the numeric/scalar values for JSON serialisation."""
    KEYS = ["rsi14","rsi7","macd","macd_hist","adx","plus_di","minus_di",
            "bb_upper","bb_mid","bb_lower","bb_width","bb_pct_b",
            "ema9","ema21","ema50","ema200","atr14","atr_pct",
            "stoch_k","stoch_d","srsi_k","cci20","vol_ratio","vwap",
            "pp","r1","r2","s1","s2","pdh","pdl",
            "ema_bull","ema_bear","macd_bull","macd_cross_bull",
            "trend_bull","trend_bear","rsi_bull","rsi_bear","rsi_os","rsi_ob",
            "bb_breakout","bb_support","vol_surge","above_vwap","st_bull","obv_bull",
            "close","volume"]
    return {k: ind[k] for k in KEYS if k in ind}

# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN SCANNER ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════════════════

def run_scan():
    """Full scan: daily pre-filter → intraday deep-dive → generate HTML."""
    with _lock:
        if _state["status"] == "running":
            log.info("Scan already running, skipping.")
            return
        _state["status"] = "running"

    try:
        start_ts = time.time()
        log.info("=" * 60)
        log.info("🔍 INTRADAY SCANNER — Starting scan")
        log.info("=" * 60)

        universe = load_universe()
        tickers  = [s["ticker"] for s in universe]
        co_map   = {s["ticker"]: s["company"] for s in universe}

        with _lock:
            _state["universe_size"] = len(tickers)

        # ── Phase 1: Daily data for all stocks ────────────────────────────────
        log.info(f"Phase 1: Downloading daily data for {len(tickers)} stocks…")
        daily_data = batch_download(tickers, "1y", "1d", batch_size=25, pause=0.4)
        log.info(f"  Daily data: {len(daily_data)} stocks")

        # Quick daily filter — score by RSI + MACD + EMA only
        daily_scores = {}
        for t, df in daily_data.items():
            try:
                if len(df) < 30:
                    continue
                c = df["Close"].values.astype(float)
                if float(c[-1]) < MIN_PRICE:
                    continue
                rsi = talib.RSI(c, 14)
                e9  = talib.EMA(c, 9)
                e21 = talib.EMA(c, 21)
                _, _, mhist = talib.MACD(c, 12, 26, 9)
                sc = 0
                r  = float(rsi[-1]) if not np.isnan(rsi[-1]) else 50
                if r > 55: sc += 2
                if r > 65: sc += 1
                if not np.isnan(e9[-1]) and not np.isnan(e21[-1]) and c[-1] > e9[-1] > e21[-1]:
                    sc += 2
                if not np.isnan(mhist[-1]) and mhist[-1] > 0:
                    sc += 1
                daily_scores[t] = sc
            except Exception:
                pass

        # Top candidates by daily score
        top = sorted(daily_scores, key=lambda x: -daily_scores[x])[:MAX_INTRADAY]
        log.info(f"Phase 1 complete: {len(top)} candidates selected")

        # ── Phase 2: Intraday + weekly + monthly for top candidates ──────────
        log.info(f"Phase 2: Downloading intraday data for {len(top)} candidates…")

        weekly_data  = batch_download(top, "2y", "1wk",  batch_size=25, pause=0.3)
        monthly_data = batch_download(top, "5y", "1mo",  batch_size=25, pause=0.3)
        h1_data      = batch_download(top, "60d","1h",   batch_size=20, pause=0.4)
        m15_data     = batch_download(top, "5d", "15m",  batch_size=15, pause=0.5)

        log.info(f"  Weekly: {len(weekly_data)} | 1H: {len(h1_data)} | 15M: {len(m15_data)}")

        # ── Phase 3: Full analysis ────────────────────────────────────────────
        results = []
        for t in top:
            df_d   = daily_data.get(t,   pd.DataFrame())
            df_w   = weekly_data.get(t,  pd.DataFrame())
            df_m   = monthly_data.get(t, pd.DataFrame())
            df_1h  = h1_data.get(t,      pd.DataFrame())
            df_15m = m15_data.get(t,     pd.DataFrame())

            if len(df_d) < 30:
                continue

            rec = analyse_stock(t, co_map.get(t, t), df_d, df_w, df_m, df_1h, df_15m)
            if rec:
                results.append(rec)

        results.sort(key=lambda x: (-len(x.get("signals", [])), -x["score"]))

        elapsed = round(time.time() - start_ts, 1)
        run_ts  = datetime.now().strftime("%d %b %Y %H:%M IST")
        html    = build_html(results, run_ts, elapsed)

        # Save HTML
        with open("intraday_latest.html", "w", encoding="utf-8") as f:
            f.write(html)

        with _lock:
            _state["results"]    = results
            _state["html"]       = html
            _state["last_run"]   = run_ts
            _state["scan_count"] += 1
            _state["candidates"] = len(results)
            _state["status"]     = "ready"
            _state["market_time"]= run_ts

        log.info(f"✅ Scan complete in {elapsed}s — {len(results)} candidates")
        log.info(f"   PDH breakouts: {sum(1 for r in results if 'PDH_BREAKOUT' in r.get('signals', []))}")
        log.info(f"   VWAP breakouts: {sum(1 for r in results if 'VWAP_BREAKOUT' in r.get('signals', []))}")
        log.info(f"   MTF momentum: {sum(1 for r in results if 'MTF_MOMENTUM' in r.get('signals', []))}")

    except Exception as e:
        log.exception(f"Scan error: {e}")
        with _lock:
            _state["status"] = "error"

# ═══════════════════════════════════════════════════════════════════════════════
#  HTML REPORT BUILDER
# ═══════════════════════════════════════════════════════════════════════════════

SIGNAL_COLORS = {
    "PDH_BREAKOUT":    ("#d1fae5", "#065f46", "🚀"),
    "VWAP_BREAKOUT":   ("#dbeafe", "#1e3a8a", "⚡"),
    "MTF_MOMENTUM":    ("#fef9c3", "#78350f", "📈"),
    "BB_EXPANSION":    ("#ede9fe", "#4c1d95", "🎯"),
    "MEAN_REVERSION":  ("#fce7f3", "#831843", "🔄"),
    "PDL_BREAKDOWN":   ("#fee2e2", "#7f1d1d", "📉"),
    "WATCH":           ("#f3f4f6", "#374151", "👀"),
}

def ind_badge(val, good_thresh=None, bad_thresh=None, fmt=".1f", suffix="") -> str:
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return '<span style="color:#9ca3af">—</span>'
    txt = f"{val:{fmt}}{suffix}"
    if good_thresh is not None and val >= good_thresh:
        color = "#059669"
    elif bad_thresh is not None and val <= bad_thresh:
        color = "#dc2626"
    else:
        color = "#374151"
    return f'<span style="color:{color};font-weight:600">{txt}</span>'

def bool_dot(flag: bool) -> str:
    return ('●' if flag else '○')

def tf_row(label: str, ind: dict) -> str:
    if not ind:
        return f'<tr><td style="padding:2px 6px;color:#9ca3af;font-size:10px">{label}</td><td colspan="8" style="color:#9ca3af;font-size:10px;text-align:center">no data</td></tr>'
    rsi   = ind.get("rsi14", 50)
    adx   = ind.get("adx", 0)
    macd_h= ind.get("macd_hist", 0)
    vol   = ind.get("vol_ratio", 1)
    ema_b = ind.get("ema_bull", False)
    mac_b = ind.get("macd_bull", False)
    tr_b  = ind.get("trend_bull", False)
    st_b  = ind.get("st_bull", False)
    bb_b  = ind.get("bb_breakout", False)
    av    = ind.get("above_vwap", False)
    bb_w  = ind.get("bb_width", 0)
    cci   = ind.get("cci20", 0)
    stk   = ind.get("stoch_k", 50)

    rsi_color = "#059669" if rsi > 60 else "#dc2626" if rsi < 40 else "#374151"
    vol_color = "#059669" if vol > VOL_SURGE else "#374151"
    adx_color = "#059669" if adx > ADX_TREND else "#6b7280"

    return f"""
<tr style="border-bottom:1px solid #f0f0f0">
  <td style="padding:3px 6px;font-weight:700;font-size:10px;white-space:nowrap;color:#374151">{label}</td>
  <td style="padding:3px 6px;font-size:10px;text-align:center;color:{rsi_color};font-weight:600">{rsi:.0f}</td>
  <td style="padding:3px 6px;font-size:10px;text-align:center;color:{'#059669' if macd_h>0 else '#dc2626'};font-weight:600">{'▲' if macd_h>0 else '▼'}{abs(macd_h):.2f}</td>
  <td style="padding:3px 6px;font-size:10px;text-align:center;color:{adx_color};font-weight:600">{adx:.0f}</td>
  <td style="padding:3px 6px;font-size:10px;text-align:center;color:{vol_color};font-weight:600">{vol:.1f}x</td>
  <td style="padding:3px 6px;font-size:10px;text-align:center;color:{'#059669' if ema_b else '#9ca3af'}">{bool_dot(ema_b)}</td>
  <td style="padding:3px 6px;font-size:10px;text-align:center;color:{'#059669' if mac_b else '#9ca3af'}">{bool_dot(mac_b)}</td>
  <td style="padding:3px 6px;font-size:10px;text-align:center;color:{'#059669' if tr_b else '#9ca3af'}">{bool_dot(tr_b)}</td>
  <td style="padding:3px 6px;font-size:10px;text-align:center;color:{'#059669' if st_b else '#9ca3af'}">{bool_dot(st_b)}</td>
  <td style="padding:3px 6px;font-size:10px;text-align:center;color:{'#6d28d9' if bb_b else '#9ca3af'}">{bool_dot(bb_b)}</td>
  <td style="padding:3px 6px;font-size:10px;text-align:center;color:{'#0369a1' if av else '#9ca3af'}">{bool_dot(av)}</td>
  <td style="padding:3px 6px;font-size:10px;text-align:center;color:#374151">{cci:.0f}</td>
  <td style="padding:3px 6px;font-size:10px;text-align:center;color:#374151">{stk:.0f}</td>
  <td style="padding:3px 6px;font-size:10px;text-align:center;color:#374151">{bb_w:.1f}%</td>
</tr>"""

def stock_card(r: dict) -> str:
    sig_color, sig_fg, sig_icon = SIGNAL_COLORS.get(r["signal"], SIGNAL_COLORS["WATCH"])
    fo_badge = ('<span style="background:#7c3aed;color:#fff;font-size:9px;font-weight:700;'
                'padding:1px 5px;border-radius:3px;margin-left:4px">F&amp;O</span>'
                if r.get("is_fo") else "")

    # All signal badges
    sig_tags = ""
    for s in r.get("signals", []):
        sc, sf, si = SIGNAL_COLORS.get(s, SIGNAL_COLORS["WATCH"])
        sig_tags += (f'<span style="background:{sc};color:{sf};font-size:9px;font-weight:700;'
                     f'padding:2px 6px;border-radius:3px;margin-right:3px">{si} {s.replace("_"," ")}</span>')

    # Fibonacci levels
    fib_ext   = r.get("ind_1h", {}).get("fib_ext", {}) or r.get("ind_d", {}).get("fib_ext", {}) or {}
    fib_str   = " · ".join(f"FE{k[2:]}%=₹{v:,.0f}" for k, v in list(fib_ext.items())[:4]) if fib_ext else "—"

    # Levels from 1H
    ind_1h  = r.get("ind_1h", {})
    ind_d   = r.get("ind_d", {})
    pp  = ind_1h.get("pp")  or ind_d.get("pp")
    r1  = ind_1h.get("r1")  or ind_d.get("r1")
    s1  = ind_1h.get("s1")  or ind_d.get("s1")
    r2  = ind_1h.get("r2")  or ind_d.get("r2")
    s2  = ind_1h.get("s2")  or ind_d.get("s2")

    def lvl(v): return f"₹{v:,.2f}" if v else "—"

    # Entry setup
    t1_pct  = f'+{r["pct_t1"]}%' if r.get("pct_t1") else "—"
    t2_pct  = f'+{r["pct_t2"]}%' if r.get("pct_t2") else "—"
    sl_pct  = f'-{r["pct_sl"]}%' if r.get("pct_sl") else "—"
    rr_str  = ""
    if r.get("pct_t1") and r.get("pct_sl") and r["pct_sl"] != 0:
        rr = round(abs(r["pct_t1"] / r["pct_sl"]), 1)
        rr_str = f' · R/R {rr}:1'

    # Score bar width
    bar_w = min(100, int(r["score"]))
    bar_c = "#059669" if bar_w >= 60 else "#d97706" if bar_w >= 40 else "#6b7280"

    return f"""
<div style="background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:14px 16px;
            margin-bottom:14px;box-shadow:0 1px 4px rgba(0,0,0,.06)">
  <!-- Header row -->
  <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px;flex-wrap:wrap;gap:6px">
    <div>
      <span style="font-size:17px;font-weight:800;color:#111">{r['ticker']}</span>{fo_badge}
      <span style="font-size:12px;color:#6b7280;margin-left:8px">{r['company']}</span>
    </div>
    <div style="text-align:right">
      <span style="font-size:18px;font-weight:800;color:#1f2937">₹{r['close']:,.2f}</span>
      <span style="font-size:11px;color:#6b7280;margin-left:6px">52W H: ₹{r.get('high_52w',0):,.0f} ({r.get('ath_pct','—')}%)</span>
    </div>
  </div>

  <!-- Signal tags -->
  <div style="margin-bottom:8px">{sig_tags or '<span style="color:#9ca3af;font-size:11px">No active signal</span>'}</div>

  <!-- Reasoning -->
  <div style="font-size:11px;color:#374151;background:#f9fafb;border-radius:6px;padding:6px 10px;margin-bottom:10px">
    💡 {r.get('reasoning','—')}
  </div>

  <!-- Score bar -->
  <div style="margin-bottom:10px">
    <div style="display:flex;justify-content:space-between;font-size:10px;color:#6b7280;margin-bottom:2px">
      <span>Momentum Score</span><span style="font-weight:700;color:{bar_c}">{r['score']:.1f}/100</span>
    </div>
    <div style="background:#f3f4f6;border-radius:4px;height:6px">
      <div style="background:{bar_c};width:{bar_w}%;height:6px;border-radius:4px"></div>
    </div>
  </div>

  <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px">
    <!-- Trade Setup -->
    <div style="background:#f0fdf4;border:1px solid #86efac;border-radius:8px;padding:10px">
      <div style="font-size:11px;font-weight:700;color:#065f46;margin-bottom:6px">📊 Trade Setup</div>
      <table style="width:100%;font-size:11px;border-collapse:collapse">
        <tr><td style="color:#6b7280;padding:1px 4px">Entry</td><td style="font-weight:700;color:#111">₹{r['close']:,.2f}</td></tr>
        <tr><td style="color:#6b7280;padding:1px 4px">Stop Loss</td><td style="font-weight:700;color:#dc2626">{lvl(r.get('sl'))} <span style="color:#9ca3af">({sl_pct})</span></td></tr>
        <tr><td style="color:#6b7280;padding:1px 4px">Target 1</td><td style="font-weight:700;color:#059669">{lvl(r.get('t1'))} <span style="color:#9ca3af">({t1_pct})</span></td></tr>
        <tr><td style="color:#6b7280;padding:1px 4px">Target 2</td><td style="font-weight:700;color:#059669">{lvl(r.get('t2'))} <span style="color:#9ca3af">({t2_pct})</span></td></tr>
        <tr><td style="color:#6b7280;padding:1px 4px">Risk/Reward</td><td style="font-weight:700;color:#1d4ed8">{rr_str or '—'}</td></tr>
      </table>
    </div>

    <!-- Levels -->
    <div style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:8px;padding:10px">
      <div style="font-size:11px;font-weight:700;color:#1e3a8a;margin-bottom:6px">📐 Support &amp; Resistance</div>
      <table style="width:100%;font-size:11px;border-collapse:collapse">
        <tr><td style="color:#6b7280;padding:1px 4px">Prev D High</td><td style="font-weight:600;color:#059669">{lvl(r.get('pdh'))}</td></tr>
        <tr><td style="color:#6b7280;padding:1px 4px">Prev D Low</td><td style="font-weight:600;color:#dc2626">{lvl(r.get('pdl'))}</td></tr>
        <tr><td style="color:#6b7280;padding:1px 4px">VWAP</td><td style="font-weight:600;color:#7c3aed">{lvl(r.get('vwap'))}</td></tr>
        <tr><td style="color:#6b7280;padding:1px 4px">R1 / R2</td><td style="font-weight:600;color:#059669">{lvl(r1)} / {lvl(r2)}</td></tr>
        <tr><td style="color:#6b7280;padding:1px 4px">PP / S1</td><td style="font-weight:600;color:#374151">{lvl(pp)} / {lvl(s1)}</td></tr>
      </table>
    </div>
  </div>

  <!-- Fibonacci Extensions -->
  <div style="background:#fdf4ff;border:1px solid #e9d5ff;border-radius:6px;padding:6px 10px;margin-bottom:10px">
    <span style="font-size:10px;font-weight:700;color:#6d28d9">Fibonacci Extensions: </span>
    <span style="font-size:10px;color:#374151">{fib_str}</span>
  </div>

  <!-- MTF Indicator Table -->
  <details>
    <summary style="cursor:pointer;font-size:11px;font-weight:700;color:#374151;margin-bottom:4px;list-style:none">
      ▸ Multi-Timeframe Indicator Detail
    </summary>
    <div style="overflow-x:auto;margin-top:6px">
      <table style="width:100%;border-collapse:collapse;font-size:10px">
        <thead>
          <tr style="background:#f9fafb">
            <th style="padding:4px 6px;text-align:left;color:#6b7280">TF</th>
            <th style="padding:4px 6px;color:#6b7280">RSI</th>
            <th style="padding:4px 6px;color:#6b7280">MACD</th>
            <th style="padding:4px 6px;color:#6b7280">ADX</th>
            <th style="padding:4px 6px;color:#6b7280">Vol×</th>
            <th style="padding:4px 6px;color:#6b7280">EMA</th>
            <th style="padding:4px 6px;color:#6b7280">MACD</th>
            <th style="padding:4px 6px;color:#6b7280">ADX</th>
            <th style="padding:4px 6px;color:#6b7280">ST</th>
            <th style="padding:4px 6px;color:#6b7280">BB</th>
            <th style="padding:4px 6px;color:#6b7280">VWP</th>
            <th style="padding:4px 6px;color:#6b7280">CCI</th>
            <th style="padding:4px 6px;color:#6b7280">Stk</th>
            <th style="padding:4px 6px;color:#6b7280">BBW</th>
          </tr>
        </thead>
        <tbody>
          {tf_row("Monthly", r.get("ind_m", {}))}
          {tf_row("Weekly",  r.get("ind_w", {}))}
          {tf_row("Daily",   r.get("ind_d", {}))}
          {tf_row("4 Hour",  r.get("ind_4h",{}))}
          {tf_row("1 Hour",  r.get("ind_1h",{}))}
          {tf_row("15 Min",  r.get("ind_15m",{}))}
        </tbody>
      </table>
    </div>
    <div style="font-size:9px;color:#9ca3af;margin-top:4px">
      EMA=EMA stack bull · MACD=MACD bull · ADX=Trend confirmed · ST=Supertrend bull
      · BB=BB upper breakout · VWP=Above VWAP
    </div>
  </details>
</div>"""

def build_html(results: list[dict], run_ts: str, elapsed: float) -> str:
    signal_order = ["PDH_BREAKOUT", "VWAP_BREAKOUT", "BB_EXPANSION",
                    "MTF_MOMENTUM", "MEAN_REVERSION", "PDL_BREAKDOWN", "WATCH"]
    tabs = {}
    for s in signal_order:
        group = [r for r in results if r["signal"] == s]
        if group:
            tabs[s] = group

    def tab_btn(s, active=False):
        cnt = len(tabs.get(s, []))
        sc, sf, si = SIGNAL_COLORS.get(s, SIGNAL_COLORS["WATCH"])
        border = "2px solid #3b82f6" if active else "2px solid transparent"
        return (f'<button onclick="showTab(\'{s}\')" id="btn-{s}" '
                f'style="background:{sc if active else "#f9fafb"};color:{sf if active else "#6b7280"};'
                f'border:{border};border-radius:6px;padding:6px 12px;font-size:12px;font-weight:700;'
                f'cursor:pointer;margin:2px">{si} {s.replace("_"," ")} ({cnt})</button>')

    all_btn  = (f'<button onclick="showTab(\'ALL\')" id="btn-ALL" '
                f'style="background:#3b82f6;color:#fff;border:2px solid #3b82f6;'
                f'border-radius:6px;padding:6px 12px;font-size:12px;font-weight:700;'
                f'cursor:pointer;margin:2px">📋 ALL ({len(results)})</button>')

    tab_btns = all_btn + "".join(tab_btn(s) for s in signal_order if s in tabs)

    # Summary stats
    pdh_cnt  = sum(1 for r in results if "PDH_BREAKOUT"  in r.get("signals",[]))
    vwap_cnt = sum(1 for r in results if "VWAP_BREAKOUT" in r.get("signals",[]))
    mtf_cnt  = sum(1 for r in results if "MTF_MOMENTUM"  in r.get("signals",[]))
    mr_cnt   = sum(1 for r in results if "MEAN_REVERSION"in r.get("signals",[]))

    # Cards HTML per tab
    all_cards = "\n".join(stock_card(r) for r in results)
    tab_sections = f'<div id="tab-ALL">{all_cards}</div>\n'
    for s in signal_order:
        group = tabs.get(s, [])
        disp = "none"
        cards = "\n".join(stock_card(r) for r in group) if group else '<p style="color:#9ca3af">No stocks in this category.</p>'
        tab_sections += f'<div id="tab-{s}" style="display:{disp}">{cards}</div>\n'

    universe_size = _state.get("universe_size", 0)
    scan_count    = _state.get("scan_count",    0)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="{SCAN_INTERVAL * 60}">
<title>🔍 Intraday Momentum Scanner — NSE</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f1f5f9;color:#1f2937 }}
  .header {{ background:linear-gradient(135deg,#0f172a,#1e3a8a);color:#fff;padding:20px 28px }}
  .header h1 {{ font-size:22px;font-weight:800 }}
  .header p  {{ font-size:12px;opacity:.75;margin-top:4px }}
  .stat-bar  {{ display:flex;gap:12px;flex-wrap:wrap;padding:12px 28px;background:#fff;border-bottom:1px solid #e5e7eb }}
  .stat      {{ display:flex;flex-direction:column;align-items:center;min-width:80px }}
  .stat-val  {{ font-size:22px;font-weight:800 }}
  .stat-lbl  {{ font-size:10px;color:#6b7280;text-transform:uppercase;letter-spacing:.5px }}
  .tabs      {{ padding:10px 28px;background:#fff;border-bottom:1px solid #e5e7eb;display:flex;flex-wrap:wrap;gap:4px }}
  .content   {{ max-width:1100px;margin:20px auto;padding:0 16px }}
  .refresh   {{ font-size:11px;color:#6b7280;text-align:right;margin-bottom:8px }}
  details summary::-webkit-details-marker {{ display:none }}
</style>
</head>
<body>

<div class="header">
  <h1>🔍 NSE Intraday Momentum Breakout Scanner</h1>
  <p>Multi-Timeframe: Monthly · Weekly · Daily · 4H · 1H · 15M &nbsp;|&nbsp;
     TA-Lib: RSI · MACD · ADX · Bollinger · EMA · ATR · Stochastic · CCI · OBV · VWAP · Supertrend</p>
</div>

<!-- Stats Bar -->
<div class="stat-bar">
  <div class="stat"><span class="stat-val" style="color:#059669">{pdh_cnt}</span><span class="stat-lbl">PDH Breakout</span></div>
  <div class="stat"><span class="stat-val" style="color:#1d4ed8">{vwap_cnt}</span><span class="stat-lbl">VWAP Breakout</span></div>
  <div class="stat"><span class="stat-val" style="color:#d97706">{mtf_cnt}</span><span class="stat-lbl">MTF Momentum</span></div>
  <div class="stat"><span class="stat-val" style="color:#db2777">{mr_cnt}</span><span class="stat-lbl">Mean Reversion</span></div>
  <div class="stat"><span class="stat-val" style="color:#374151">{len(results)}</span><span class="stat-lbl">Total Candidates</span></div>
  <div class="stat"><span class="stat-val" style="color:#6b7280">{universe_size}</span><span class="stat-lbl">Universe</span></div>
  <div style="margin-left:auto;display:flex;align-items:center;gap:10px">
    <span style="font-size:11px;color:#6b7280">Scanned in {elapsed}s &nbsp;|&nbsp; Run #{scan_count}</span>
    <a href="/run" style="background:#3b82f6;color:#fff;border:none;border-radius:6px;padding:8px 14px;
       font-size:12px;font-weight:700;text-decoration:none;cursor:pointer">🔄 Scan Now</a>
  </div>
</div>

<!-- Tab Buttons -->
<div class="tabs">{tab_btns}</div>

<div class="content">
  <div class="refresh">⏱ Last scan: {run_ts} &nbsp;|&nbsp; Auto-refreshes every {SCAN_INTERVAL} min</div>
  {tab_sections}
</div>

<script>
function showTab(name) {{
  document.querySelectorAll('[id^="tab-"]').forEach(d => d.style.display = 'none');
  const el = document.getElementById('tab-' + name);
  if (el) el.style.display = 'block';
  document.querySelectorAll('[id^="btn-"]').forEach(b => {{
    b.style.background = '#f9fafb';
    b.style.color = '#6b7280';
    b.style.border = '2px solid transparent';
  }});
  const btn = document.getElementById('btn-' + name);
  if (btn) {{
    btn.style.background = '#3b82f6';
    btn.style.color = '#fff';
    btn.style.border = '2px solid #3b82f6';
  }}
}}
// Show ALL tab on load
showTab('ALL');
</script>
</body>
</html>"""

# ═══════════════════════════════════════════════════════════════════════════════
#  FLASK APP
# ═══════════════════════════════════════════════════════════════════════════════

app = Flask(__name__)

@app.route("/")
def dashboard():
    with _lock:
        html  = _state.get("html", "")
        status= _state.get("status", "idle")

    if html:
        from flask import Response
        return Response(html, mimetype="text/html")

    if status == "running":
        return Response("""<!DOCTYPE html><html><head>
<meta charset="UTF-8"><meta http-equiv="refresh" content="10">
<title>Scanning…</title></head>
<body style="display:flex;align-items:center;justify-content:center;height:100vh;
font-family:system-ui;background:#f1f5f9">
<div style="text-align:center">
  <div style="font-size:40px;margin-bottom:16px">🔍</div>
  <h2 style="color:#1e3a8a">Intraday Scan in Progress…</h2>
  <p style="color:#6b7280;margin-top:8px">Analysing NSE stocks across 6 timeframes. Please wait.</p>
  <p style="color:#9ca3af;font-size:12px;margin-top:4px">Page auto-refreshes every 10 seconds</p>
</div></body></html>""", mimetype="text/html")

    return Response("""<!DOCTYPE html><html><head>
<meta charset="UTF-8"><title>Intraday Scanner</title></head>
<body style="display:flex;align-items:center;justify-content:center;height:100vh;font-family:system-ui;background:#f1f5f9">
<div style="text-align:center">
  <div style="font-size:40px;margin-bottom:16px">🚀</div>
  <h2 style="color:#1e3a8a">NSE Intraday Momentum Scanner</h2>
  <p style="color:#6b7280;margin-top:8px">Click below to run the first scan.</p>
  <a href="/run" style="display:inline-block;margin-top:16px;background:#3b82f6;color:#fff;
     padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:700;font-size:14px">
     ▶ Start Scan</a>
</div></body></html>""", mimetype="text/html")

@app.route("/run")
def trigger_scan():
    with _lock:
        if _state["status"] == "running":
            return Response('<script>history.back()</script>', mimetype="text/html")
        _state["status"] = "running"

    t = threading.Thread(target=run_scan, daemon=True)
    t.start()
    return Response("""<!DOCTYPE html><html><head>
<meta charset="UTF-8"><meta http-equiv="refresh" content="5;url=/">
<title>Scanning…</title></head>
<body style="display:flex;align-items:center;justify-content:center;height:100vh;font-family:system-ui;background:#f1f5f9">
<div style="text-align:center">
  <div style="font-size:40px;margin-bottom:16px">🔍</div>
  <h2 style="color:#1e3a8a">Scan Triggered!</h2>
  <p style="color:#6b7280;margin-top:8px">Returning to dashboard in 5 seconds…</p>
</div></body></html>""", mimetype="text/html")

@app.route("/status")
def status():
    with _lock:
        return jsonify({
            "status":        _state["status"],
            "last_run":      _state["last_run"],
            "candidates":    _state["candidates"],
            "scan_count":    _state["scan_count"],
            "universe_size": _state["universe_size"],
        })

@app.route("/api/stocks")
def api_stocks():
    sig_filter = request.args.get("signal", "")
    with _lock:
        results = _state["results"]
    if sig_filter:
        results = [r for r in results if sig_filter.upper() in r.get("signals", [])]
    slim = [{
        "ticker":   r["ticker"],
        "company":  r["company"],
        "close":    r["close"],
        "score":    r["score"],
        "signal":   r["signal"],
        "signals":  r["signals"],
        "is_fo":    r["is_fo"],
        "sl":       r["sl"],
        "t1":       r["t1"],
        "t2":       r["t2"],
        "pct_t1":   r["pct_t1"],
        "pct_t2":   r["pct_t2"],
        "reasoning":r["reasoning"],
    } for r in results]
    return jsonify(slim)

# ═══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    load_fo_list()

    # Start background scheduler for auto-refresh every SCAN_INTERVAL minutes
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        sched = BackgroundScheduler(daemon=True)
        sched.add_job(run_scan, "interval", minutes=SCAN_INTERVAL, id="intraday_scan",
                      next_run_time=datetime.now())  # run immediately on start
        sched.start()
        log.info(f"⏱ Scheduler started — scanning every {SCAN_INTERVAL} minutes")
    except ImportError:
        log.warning("APScheduler not found — manual /run endpoint only")
        t = threading.Thread(target=run_scan, daemon=True)
        t.start()

    log.info(f"🌐 Dashboard → http://0.0.0.0:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
